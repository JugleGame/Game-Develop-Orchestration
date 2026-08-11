"""HTTP 폴백(`strategic/neon_http.py`)의 순수 로직 검증.

네트워크를 타지 않는다. 값 복원·배열·파라미터 인코딩·문장 분리는 전부 텍스트
변환이므로 여기서 잡을 수 있고, 실제 Neon 왕복은 별도로 확인했다.

여기서 지키려는 계약은 하나다 — **`asyncpg` 가 줬을 값과 같은 파이썬 타입을
준다.** 특히 두 가지가 어긋나기 쉽다.

* `score` 는 float 여야 한다. 문자열이면 `Card.to_dict()` 의 `round()` 가 터진다.
* JSONB 는 **문자열로 남아야** 한다. 호출부가 `json.loads()` 를 하기 때문에
  여기서 미리 파싱하면 두 번 파싱하게 된다.
"""

import asyncio
import socket
import time
from datetime import date, datetime
from decimal import Decimal

import pytest

from strategic import neon_http
from strategic.neon_http import (
    NeonHttpPool,
    Record,
    _array_literal,
    _decode,
    _encode_param,
    connect_pool,
    split_statements,
)

# Postgres OID — 실제 엔드포인트 응답에서 확인한 값.
BOOL, INT8, INT4, TEXT, JSONB, FLOAT8, NUMERIC = 16, 20, 23, 25, 3802, 701, 1700
TEXT_ARRAY, INT4_ARRAY, TIMESTAMPTZ, DATE = 1009, 1007, 1184, 1082


class TestDecode:
    def test_null_is_none_whatever_the_type(self) -> None:
        assert _decode(INT4, None) is None
        assert _decode(TEXT_ARRAY, None) is None

    @pytest.mark.parametrize("oid,raw,expected", [(INT4, "42", 42), (INT8, "-7", -7)])
    def test_integers(self, oid: int, raw: str, expected: int) -> None:
        value = _decode(oid, raw)
        assert value == expected and isinstance(value, int)

    def test_float_is_float_so_round_works(self) -> None:
        value = _decode(FLOAT8, "0.25")
        assert value == 0.25 and isinstance(value, float)
        assert round(value, 4) == 0.25

    def test_numeric_is_decimal(self) -> None:
        assert _decode(NUMERIC, "1.10") == Decimal("1.10")

    def test_bool_uses_t_and_f(self) -> None:
        assert _decode(BOOL, "t") is True
        assert _decode(BOOL, "f") is False

    def test_jsonb_stays_a_string(self) -> None:
        """호출부가 ``json.loads`` 를 하므로 여기서 파싱하면 안 된다."""

        raw = '{"k": 1}'
        assert _decode(JSONB, raw) == raw

    def test_timestamptz_short_offset(self) -> None:
        value = _decode(TIMESTAMPTZ, "2026-07-28 04:25:05.361782+00")
        assert isinstance(value, datetime) and value.year == 2026

    def test_date(self) -> None:
        assert _decode(DATE, "2026-07-28") == date(2026, 7, 28)

    def test_unparsable_timestamp_falls_back_to_text(self) -> None:
        assert _decode(TIMESTAMPTZ, "infinity") == "infinity"


class TestArrayDecode:
    def test_plain(self) -> None:
        assert _decode(TEXT_ARRAY, "{a,b}") == ["a", "b"]

    def test_empty(self) -> None:
        assert _decode(TEXT_ARRAY, "{}") == []

    def test_quoted_element_containing_comma(self) -> None:
        assert _decode(TEXT_ARRAY, '{"a,b",c}') == ["a,b", "c"]

    def test_unquoted_null_is_none_but_quoted_is_text(self) -> None:
        assert _decode(TEXT_ARRAY, '{NULL,"NULL"}') == [None, "NULL"]

    def test_escapes(self) -> None:
        assert _decode(TEXT_ARRAY, '{"a\\"b","c\\\\d"}') == ['a"b', "c\\d"]

    def test_elements_are_typed(self) -> None:
        assert _decode(INT4_ARRAY, "{1,2}") == [1, 2]


class TestEncodeParam:
    def test_scalars_pass_through(self) -> None:
        assert _encode_param(7) == 7
        assert _encode_param("hi") == "hi"
        assert _encode_param(None) is None

    def test_bool_is_checked_before_int(self) -> None:
        """``bool`` 은 ``int`` 의 하위형이라 순서를 틀리면 True 가 1 로 나간다."""

        assert _encode_param(True) == "t"
        assert _encode_param(False) == "f"

    def test_list_becomes_array_literal(self) -> None:
        assert _encode_param(["a", "b"]) == "{a,b}"

    def test_array_literal_quotes_what_needs_quoting(self) -> None:
        assert _array_literal(["a,b"]) == '{"a,b"}'
        assert _array_literal([None]) == "{NULL}"
        assert _array_literal(["NULL"]) == '{"NULL"}'
        assert _array_literal([""]) == '{""}'
        assert _array_literal(['a"b']) == '{"a\\"b"}'

    def test_roundtrip_through_decode(self) -> None:
        values = ["a,b", "plain", None, "NULL", 'q"q']
        assert _decode(TEXT_ARRAY, _array_literal(values)) == values


class TestSplitStatements:
    def test_single_statement_stays_one(self) -> None:
        assert split_statements("SELECT 1") == ["SELECT 1"]

    def test_trailing_semicolon_does_not_add_empty(self) -> None:
        assert split_statements("SELECT 1;") == ["SELECT 1"]

    def test_splits_multiple(self) -> None:
        assert split_statements("SELECT 1; SELECT 2") == ["SELECT 1", "SELECT 2"]

    def test_semicolon_inside_string_literal(self) -> None:
        sql = "CREATE TABLE a(x text DEFAULT 'a;b'); CREATE TABLE b(y int)"
        assert len(split_statements(sql)) == 2

    def test_semicolon_inside_line_comment(self) -> None:
        assert len(split_statements("SELECT 1; -- a;b\nSELECT 2")) == 2

    def test_semicolon_inside_block_comment(self) -> None:
        assert len(split_statements("SELECT 1 /* a;b */; SELECT 2")) == 2

    def test_dollar_quoted_body(self) -> None:
        sql = "CREATE FUNCTION f() RETURNS int AS $$ BEGIN; RETURN 1; END $$ LANGUAGE plpgsql"
        assert split_statements(sql) == [sql]

    def test_placeholders_are_not_dollar_quotes(self) -> None:
        """``$1`` 을 달러 인용으로 오인하면 뒤가 통째로 삼켜진다."""

        assert split_statements("SELECT $1; SELECT $2") == ["SELECT $1", "SELECT $2"]

    def test_real_ddl_from_concept_store(self) -> None:
        from strategic.concepts import _DDL

        assert len(split_statements(_DDL)) == 1

    def test_real_ddl_from_spec_store_is_multi(self) -> None:
        """이 DDL 이 멀티 스테이트먼트라서 배치 전송이 필요했다."""

        from strategic.specs import _DDL

        assert len(split_statements(_DDL)) > 1


class TestRecord:
    def test_name_and_index_access(self) -> None:
        row = Record({"a": 1, "b": "x"})
        assert row["a"] == 1
        assert row[0] == 1
        assert row[1] == "x"

    def test_dict_conversion(self) -> None:
        assert dict(Record({"a": 1})) == {"a": 1}

    def test_mapping_protocol(self) -> None:
        row = Record({"a": 1})
        assert "a" in row and len(row) == 1 and list(row) == ["a"]


class TestConnectPool:
    """asyncpg 가 죽지 않고 그냥 응답이 없는 경우(사내망이 5432를 조용히 버림)에도
    폴백이 실제로 발동하는지. 네트워크를 타지 않도록 ``asyncpg.create_pool``
    자체를 대체한다."""

    @pytest.fixture(autouse=True)
    def _reachable(self, monkeypatch):
        """5432 가 열려 있는 상태를 기본값으로 둔다.

        이걸 막지 않으면 ``host`` 라는 이름이 풀리지 않아 사전 확인에서 곧장
        폴백으로 빠지고, 아래 세 테스트가 **asyncpg 경로를 한 줄도 지나지 않은
        채** 통과한다. 실제로 첫 테스트만 그 사실을 드러내며 실패해 있었다.
        """

        monkeypatch.setattr(neon_http, "_tcp_reachable", lambda host, port, timeout: True)

    async def test_returns_the_asyncpg_pool_when_it_connects_in_time(self, monkeypatch) -> None:
        sentinel = object()

        async def fake_create_pool(**kwargs):
            return sentinel

        monkeypatch.setattr(neon_http.asyncpg, "create_pool", fake_create_pool)

        pool = await connect_pool("postgresql://u:p@host/db", tcp_timeout=1.0)

        assert pool is sentinel

    async def test_falls_back_to_http_when_the_port_is_unreachable(self, monkeypatch) -> None:
        monkeypatch.setattr(neon_http, "_tcp_reachable", lambda host, port, timeout: False)

        async def must_not_be_called(**kwargs):
            raise AssertionError("사전 확인이 실패했는데 asyncpg 로 붙었다")

        monkeypatch.setattr(neon_http.asyncpg, "create_pool", must_not_be_called)

        pool = await connect_pool("postgresql://u:p@host/db", tcp_timeout=1.0)

        assert isinstance(pool, NeonHttpPool)
        await pool.close()

    async def test_falls_back_to_http_when_asyncpg_hangs_past_the_timeout(
        self, monkeypatch
    ) -> None:
        async def hangs_forever(**kwargs):
            await asyncio.sleep(10)

        monkeypatch.setattr(neon_http.asyncpg, "create_pool", hangs_forever)

        pool = await connect_pool("postgresql://u:p@host/db", tcp_timeout=0.05)

        assert isinstance(pool, NeonHttpPool)
        await pool.close()

    async def test_falls_back_to_http_on_a_connection_error(self, monkeypatch) -> None:
        async def raises_os_error(**kwargs):
            raise OSError("connection refused")

        monkeypatch.setattr(neon_http.asyncpg, "create_pool", raises_os_error)

        pool = await connect_pool("postgresql://u:p@host/db", tcp_timeout=1.0)

        assert isinstance(pool, NeonHttpPool)
        await pool.close()


class TestTcpProbe:
    def test_the_tcp_probe_budget_covers_every_address_not_each_one(self, monkeypatch) -> None:
        """호스트 하나가 A 레코드 여러 개로 풀릴 때의 실제 대기 시간.

        ``socket.create_connection`` 은 타임아웃을 주소마다 적용한다. Neon
        pooler 는 A 레코드가 3개라 8초 예산이 24초가 됐고(실측 2026-08-03),
        그 뒤 폴백까지 하면 MCP 핸드셰이크 30초를 넘겨 strategic 서버가 아예
        안 떴다. 예산은 전체에 대한 것이어야 한다.
        """

        addresses = [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", (f"10.0.0.{n}", 5432)) for n in (1, 2, 3)
        ]
        monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: addresses)

        class _Blackhole:
            """SYN 을 조용히 버리는 방화벽 — 거부가 아니라 무응답이다."""

            def __init__(self, *args):
                self._timeout = 0.0

            def settimeout(self, value):
                self._timeout = value

            def connect(self, address):
                time.sleep(self._timeout)
                raise TimeoutError("timed out")

            def close(self):
                pass

        monkeypatch.setattr(socket, "socket", _Blackhole)

        started = time.monotonic()
        assert neon_http._tcp_reachable("neon.example", 5432, 0.3) is False
        elapsed = time.monotonic() - started

        assert elapsed < 0.3 * len(addresses), f"주소마다 예산을 다시 썼다 ({elapsed:.2f}초)"
