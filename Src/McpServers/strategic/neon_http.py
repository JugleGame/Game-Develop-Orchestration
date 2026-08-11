"""Neon 을 **HTTP(443)로** 조회하는 폴백 어댑터.

Postgres 와이어 프로토콜은 5432 를 쓴다. 사내·학교 방화벽이 그 포트를 막으면
`asyncpg` 는 TCP 단계에서 매달리고, StrategicMcpServer 는 MCP 핸드셰이크
30초 제한을 넘겨 아예 뜨지 못한다 — 실제로 이 환경이 그랬다 (같은 Neon IP 의
443 은 열리고 5432 만 막힘). Neon 은 같은 호스트에서 SQL-over-HTTP 엔드포인트
(`POST https://<host>/sql`)를 제공하므로, 그 경로로 **똑같은 SQL 을** 보낸다.

`NeonHttpPool` 은 `asyncpg.Pool` 중 이 서버가 실제로 쓰는 부분만 흉내낸다 —
`fetch` / `fetchrow` / `fetchval` / `execute` / `acquire` / `close`. 호출부
(`SpecStore`·`ConceptStore`·`ResearchRepository`)는 한 줄도 바뀌지 않는다.

## 실제 엔드포인트를 찔러 확인한 계약 (2026-07-28)

* 응답은 `{"fields":[{name,dataTypeID}],"rows":[{col:val}],"command",...}`.
* `Neon-Raw-Text-Output: true` 면 **모든 값이 문자열**로 온다. 그래서 OID 를
  보고 파이썬 타입으로 되돌린다 — `score` 는 float 여야 하고(`round()` 를 탄다),
  `tags` 는 list 여야 한다.
* **JSONB 는 문자열로 남긴다.** `asyncpg` 도 코덱 없이는 그렇게 주고, 호출부가
  `json.loads(row["document"])` 를 하기 때문이다. 여기서 미리 파싱하면 깨진다.
* 배열은 `{a,b}` 리터럴로 오고 파라미터도 같은 형식으로 넣어야 한다.
* bool 은 `"t"` / `"f"`.
* **한 요청에 여러 문장을 넣으면 거부된다** (`42601 cannot insert multiple
  commands into a prepared statement`). `init_schema()` 의 DDL 이 정확히 그
  모양이라, `execute()` 가 문장을 쪼개 `queries` 배치로 보낸다.

## 지원하지 않는 것

`asyncpg` 와 달리 HTTP 는 세션이 없다. 인터랙티브 트랜잭션
(`async with conn.transaction():`), 커서, `LISTEN/NOTIFY`, 프리페어드
스테이트먼트 재사용은 쓸 수 없다. 현재 호출부는 이 중 아무것도 쓰지 않으므로
폴백이 성립한다 — **트랜잭션을 쓰는 코드를 새로 추가하면 이 폴백이 깨진다.**

`Neon-Connection-String` 헤더에는 자격증명이 들어간다. **헤더를 로그에 남기지
않는다.**
"""

from __future__ import annotations

import asyncio
import logging
import re
import socket
import time
from collections.abc import Callable, Iterator, Mapping
from datetime import date, datetime
from decimal import Decimal
from types import TracebackType
from typing import Any, NoReturn
from urllib.parse import urlsplit

import asyncpg
import httpx

from .research_repo import build_ssl_context

logger = logging.getLogger(__name__)

_INT_OIDS = frozenset({20, 21, 23, 26})
_FLOAT_OIDS = frozenset({700, 701})
_BOOL_OID = 16
_NUMERIC_OID = 1700
_DATE_OID = 1082
_TIMESTAMP_OIDS = frozenset({1114, 1184})

#: 배열 OID → 원소 OID. 1차원 배열만 쓴다 (`tags`·`elements`·`genres`).
_ARRAY_ELEMENT: dict[int, int] = {
    199: 114,
    1000: 16,
    1005: 21,
    1007: 23,
    1009: 25,
    1015: 1043,
    1016: 20,
    1021: 700,
    1022: 701,
    1231: 1700,
    3807: 3802,
}

#: `$tag$...$tag$` 달러 인용. `$1` 같은 자리표시자와 헷갈리지 않도록 숫자는 뺀다.
_DOLLAR_TAG = re.compile(r"\$[A-Za-z_]\w*\$|\$\$")
_TZ_SUFFIX = re.compile(r"[+-]\d{2}$")


# ---------------------------------------------------------------------------
# 값 복원 — 텍스트를 asyncpg 가 줬을 타입으로 되돌린다
# ---------------------------------------------------------------------------
def _parse_datetime(raw: str, oid: int) -> Any:
    """`2026-07-28 04:25:05.361782+00` 처럼 오는 값을 datetime 으로.

    되돌리지 못하면 원문 문자열을 그대로 준다 — 조회가 실패하는 것보다 낫다.
    """

    try:
        if oid == _DATE_OID:
            return date.fromisoformat(raw)
        text = raw + ":00" if _TZ_SUFFIX.search(raw) else raw
        return datetime.fromisoformat(text)
    except ValueError:
        return raw


def _decode_scalar(oid: int, raw: str) -> Any:
    """스칼라 한 칸. JSON/JSONB 와 텍스트는 문자열 그대로 둔다."""

    if oid in _INT_OIDS:
        return int(raw)
    if oid in _FLOAT_OIDS:
        return float(raw)
    if oid == _BOOL_OID:
        return raw == "t"
    if oid == _NUMERIC_OID:
        return Decimal(raw)
    if oid == _DATE_OID or oid in _TIMESTAMP_OIDS:
        return _parse_datetime(raw, oid)
    return raw


def _parse_array(raw: str, decode: Callable[[str], Any]) -> list[Any]:
    """`{a,"b,c",NULL}` 형태의 1차원 배열 리터럴을 리스트로."""

    if not (raw.startswith("{") and raw.endswith("}")):
        return []
    body = raw[1:-1]
    if not body:
        return []

    out: list[Any] = []
    buf: list[str] = []
    quoted = False
    escaped = False
    was_quoted = False

    for ch in body:
        if escaped:
            buf.append(ch)
            escaped = False
        elif quoted and ch == "\\":
            escaped = True
        elif ch == '"':
            quoted = not quoted
            was_quoted = True
        elif ch == "," and not quoted:
            out.append(_array_item("".join(buf), was_quoted, decode))
            buf, was_quoted = [], False
        else:
            buf.append(ch)
    out.append(_array_item("".join(buf), was_quoted, decode))
    return out


def _array_item(text: str, was_quoted: bool, decode: Callable[[str], Any]) -> Any:
    """따옴표 없는 `NULL` 만 진짜 NULL 이다. `"NULL"` 은 문자열이다."""

    if not was_quoted and text == "NULL":
        return None
    return decode(text)


def _decode(oid: int, raw: str | None) -> Any:
    if raw is None:
        return None
    element = _ARRAY_ELEMENT.get(oid)
    if element is not None:
        return _parse_array(raw, lambda text: _decode_scalar(element, text))
    return _decode_scalar(oid, raw)


# ---------------------------------------------------------------------------
# 파라미터 인코딩
# ---------------------------------------------------------------------------
def _array_literal(values: list[Any] | tuple[Any, ...]) -> str:
    """파이썬 시퀀스를 Postgres 배열 리터럴로. `ANY($1::text[])` 에 쓰인다."""

    parts: list[str] = []
    for value in values:
        if value is None:
            parts.append("NULL")
            continue
        text = str(value)
        if text == "" or text.upper() == "NULL" or re.search(r'[{},"\\\s]', text):
            escaped = text.replace("\\", "\\\\").replace('"', '\\"')
            parts.append(f'"{escaped}"')
        else:
            parts.append(text)
    return "{" + ",".join(parts) + "}"


def _encode_param(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, bool):  # bool 이 int 의 하위형이라 숫자보다 먼저 걸러야 한다
        return "t" if value else "f"
    if isinstance(value, (int, float, str)):
        return value
    if isinstance(value, (list, tuple)):
        return _array_literal(value)
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return str(value)


# ---------------------------------------------------------------------------
# SQL 문장 분리 — 멀티 스테이트먼트 DDL 을 배치로 보내기 위해
# ---------------------------------------------------------------------------
def split_statements(sql: str) -> list[str]:
    """`;` 로 문장을 나눈다. 문자열·식별자·주석·달러 인용 안의 `;` 는 건드리지 않는다."""

    statements: list[str] = []
    buf: list[str] = []
    i, n = 0, len(sql)

    while i < n:
        ch = sql[i]
        if ch == "'":
            buf.append(ch)
            i += 1
            while i < n:
                if sql[i] == "'":
                    if i + 1 < n and sql[i + 1] == "'":
                        buf.append("''")
                        i += 2
                        continue
                    buf.append("'")
                    i += 1
                    break
                buf.append(sql[i])
                i += 1
            continue
        if ch == '"':
            buf.append(ch)
            i += 1
            while i < n:
                buf.append(sql[i])
                closing = sql[i] == '"'
                i += 1
                if closing:
                    break
            continue
        if sql.startswith("--", i):
            while i < n and sql[i] != "\n":
                buf.append(sql[i])
                i += 1
            continue
        if sql.startswith("/*", i):
            depth = 1
            buf.append("/*")
            i += 2
            while i < n and depth:
                if sql.startswith("/*", i):
                    depth += 1
                    buf.append("/*")
                    i += 2
                elif sql.startswith("*/", i):
                    depth -= 1
                    buf.append("*/")
                    i += 2
                else:
                    buf.append(sql[i])
                    i += 1
            continue
        if ch == "$":
            match = _DOLLAR_TAG.match(sql, i)
            if match:
                tag = match.group(0)
                end = sql.find(tag, i + len(tag))
                end = n if end == -1 else end + len(tag)
                buf.append(sql[i:end])
                i = end
                continue
        if ch == ";":
            statements.append("".join(buf))
            buf = []
            i += 1
            continue
        buf.append(ch)
        i += 1

    statements.append("".join(buf))
    return [s.strip() for s in statements if s.strip()]


# ---------------------------------------------------------------------------
# Record
# ---------------------------------------------------------------------------
class Record(Mapping[str, Any]):
    """`asyncpg.Record` 대역. 이름·정수 인덱스 접근과 `dict(row)` 를 지원한다."""

    __slots__ = ("_data",)

    def __init__(self, data: dict[str, Any]) -> None:
        self._data = data

    def __getitem__(self, key: str | int) -> Any:
        if isinstance(key, int):
            return list(self._data.values())[key]
        return self._data[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._data)

    def __len__(self) -> int:
        return len(self._data)

    def __repr__(self) -> str:
        return f"<Record {self._data!r}>"


# ---------------------------------------------------------------------------
# 풀
# ---------------------------------------------------------------------------
class NeonHttpPool:
    """`asyncpg.Pool` 중 이 서버가 쓰는 부분만 HTTP 로 구현한 대역."""

    def __init__(
        self,
        dsn: str,
        *,
        timeout: float = 30.0,
        retries: int = 1,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        host = urlsplit(dsn).hostname
        if not host:
            raise ValueError("DSN 에서 호스트를 읽지 못했다.")
        self._url = f"https://{host}/sql"
        # 이 헤더에는 자격증명이 들어간다. 로그에 남기지 않는다.
        self._headers = {
            "Neon-Connection-String": dsn,
            "Neon-Raw-Text-Output": "true",
            "Neon-Array-Mode": "false",
            "Content-Type": "application/json",
        }
        self._client = client or httpx.AsyncClient(timeout=timeout)
        self._owns_client = client is None
        self._retries = retries

    # -- asyncpg.Pool 대역 ------------------------------------------------
    async def fetch(self, sql: str, *args: Any) -> list[Record]:
        return _records(await self._run(sql, args))

    async def fetchrow(self, sql: str, *args: Any) -> Record | None:
        rows = _records(await self._run(sql, args))
        return rows[0] if rows else None

    async def fetchval(self, sql: str, *args: Any, column: int = 0) -> Any:
        rows = _records(await self._run(sql, args))
        return rows[0][column] if rows else None

    async def execute(self, sql: str, *args: Any) -> str:
        statements = split_statements(sql)
        if len(statements) <= 1:
            return str((await self._run(sql, args)).get("command") or "")

        if args:
            raise ValueError("문장이 여러 개면 파라미터를 함께 쓸 수 없다.")
        payload = {"queries": [{"query": s, "params": []} for s in statements]}
        results = (await self._request(payload)).get("results") or []
        return str(results[-1].get("command") or "") if results else ""

    def acquire(self) -> _Acquired:
        """HTTP 에는 세션이 없다. 문맥 관리자 모양만 맞춰 자기 자신을 준다."""

        return _Acquired(self)

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    # -- 내부 -------------------------------------------------------------
    async def _run(self, sql: str, args: tuple[Any, ...]) -> dict[str, Any]:
        payload = {"query": sql, "params": [_encode_param(a) for a in args]}
        return await self._request(payload)

    async def _request(self, payload: dict[str, Any]) -> dict[str, Any]:
        last: Exception | None = None
        for attempt in range(self._retries + 1):
            try:
                response = await self._client.post(self._url, headers=self._headers, json=payload)
            except httpx.TransportError as exc:  # 네트워크 흔들림만 재시도한다
                last = exc
                if attempt >= self._retries:
                    break
                await asyncio.sleep(0.5 * (attempt + 1))
                continue

            if response.status_code == 200:
                return dict(response.json())
            _raise_postgres_error(response)

        raise ConnectionError(f"Neon HTTP 요청 실패: {last}") from last


class _Acquired:
    """`async with pool.acquire() as conn:` 을 성립시키는 최소 문맥 관리자."""

    __slots__ = ("_pool",)

    def __init__(self, pool: NeonHttpPool) -> None:
        self._pool = pool

    async def __aenter__(self) -> NeonHttpPool:
        return self._pool

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> bool:
        return False


def _tcp_reachable(host: str, port: int, timeout: float) -> bool:
    """블로킹 소켓으로 TCP 연결을 시도한다. ``timeout`` 은 **전체** 예산이다.

    ``asyncio.wait_for`` 로 ``asyncpg.create_pool`` 을 감싸는 방식은 Windows
    ``ProactorEventLoop`` 에서 못 먹힌다 — 방화벽이 SYN 을 조용히 버리는
    (거부가 아니라 무응답) 상황에서 보류 중인 overlapped ``ConnectEx`` 를
    ``cancel()`` 이 실제로 끊지 못해, wait_for 에 넘긴 타임아웃과 무관하게
    OS 자체 타임아웃(수십 초)까지 이벤트루프 스레드가 그대로 막힌다 (실측,
    2026-08-02: ``tcp_timeout=3`` 을 줘도 20초 넘게 안 풀림). 블로킹 소켓의
    ``settimeout()`` 은 같은 상황에서 신뢰성 있게 먹히므로, 그 소켓을 별도
    스레드(``asyncio.to_thread``)에서 돌려 이벤트루프를 막지 않으면서
    타임아웃도 확실히 지킨다.

    주소를 직접 순회하는 이유는 ``socket.create_connection`` 이 타임아웃을
    **주소마다** 적용하기 때문이다. Neon pooler 는 A 레코드가 3개라 8초 예산이
    실제로는 24초가 됐고(실측 2026-08-03), 그 뒤 폴백까지 하면 Claude Code 의
    MCP 핸드셰이크 30초를 넘겨 **strategic 서버가 아예 안 떴다.**
    """

    deadline = time.monotonic() + timeout
    try:
        addresses = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError:
        return False

    for family, socktype, proto, _canonname, sockaddr in addresses:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        sock = socket.socket(family, socktype, proto)
        try:
            sock.settimeout(remaining)
            sock.connect(sockaddr)
            return True
        except OSError:
            continue
        finally:
            sock.close()
    return False


async def connect_pool(
    dsn: str,
    *,
    min_size: int = 1,
    max_size: int = 5,
    tcp_timeout: float = 8.0,
) -> Any:
    """TCP 5432 가 열려 있는지 먼저 확인하고, 열려 있을 때만 asyncpg 로
    연결한다. 막혀 있으면 이 모듈의 HTTP(443) 폴백으로 넘어간다. 반환값은
    ``asyncpg.Pool`` 또는 ``NeonHttpPool`` — 둘 다 이 서버가 쓰는 부분
    (``fetch``/``fetchrow``/``fetchval``/``execute``/``acquire``/``close``)의
    계약이 같으므로 호출부는 어느 쪽인지 몰라도 된다.

    사전 확인을 ``_tcp_reachable`` 로 분리한 이유는 위 함수 docstring 참고.
    """

    clean_dsn = dsn.split("?")[0]  # asyncpg 는 URL 파라미터를 해석하지 않는다
    parsed = urlsplit(clean_dsn)
    host, port = parsed.hostname, parsed.port or 5432

    reachable = host is None or await asyncio.to_thread(_tcp_reachable, host, port, tcp_timeout)
    if not reachable:
        logger.warning(
            "Postgres %s:%s 연결이 %.0f초 안에 되지 않아 Neon HTTP(443) 폴백으로 "
            "전환합니다 (사내망이 5432를 막는 환경일 수 있음).",
            host,
            port,
            tcp_timeout,
        )
        return NeonHttpPool(dsn)

    try:
        pool = await asyncio.wait_for(
            asyncpg.create_pool(
                dsn=clean_dsn,
                ssl=build_ssl_context(dsn),
                min_size=min_size,
                max_size=max_size,
                command_timeout=60,
            ),
            timeout=tcp_timeout,
        )
    except (TimeoutError, OSError):
        logger.warning(
            "Postgres 5432 연결이 %.0f초 안에 되지 않아 Neon HTTP(443) 폴백으로 "
            "전환합니다 (사내망이 5432를 막는 환경일 수 있음).",
            tcp_timeout,
        )
        return NeonHttpPool(dsn)

    if pool is None:
        raise RuntimeError("asyncpg.create_pool returned None")
    return pool


def _records(result: dict[str, Any]) -> list[Record]:
    """행을 컬럼명 기준으로 복원한다. 같은 이름이 두 번 나오면 뒤가 이긴다 —
    현재 쿼리들은 모두 이름이 겹치지 않는다."""

    fields = result.get("fields") or []
    oids = [(f["name"], int(f["dataTypeID"])) for f in fields]
    return [
        Record({name: _decode(oid, row.get(name)) for name, oid in oids})
        for row in result.get("rows") or []
    ]


def _raise_postgres_error(response: httpx.Response) -> NoReturn:
    """Neon 의 오류 payload 를 `asyncpg.PostgresError` 로 올린다.

    호출부가 asyncpg 예외를 기대하므로 타입을 맞춘다. 메시지에 SQLSTATE 를
    붙여 §03 에러코드로 감쌀 때 원인이 남게 한다.
    """

    try:
        payload = response.json()
    except ValueError:
        raise asyncpg.PostgresError(f"Neon HTTP {response.status_code}") from None

    message = payload.get("message") or f"Neon HTTP {response.status_code}"
    code = payload.get("code")
    error = asyncpg.PostgresError(f"{message} (SQLSTATE {code})" if code else message)
    raise error


#: 두 전송 방식 중 무엇이 오든 호출부는 같은 메서드만 쓴다.
PoolLike = asyncpg.Pool | NeonHttpPool
RecordLike = asyncpg.Record | Record
