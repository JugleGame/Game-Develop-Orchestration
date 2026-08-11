"""UTF-8 출력 — Windows 콘솔에서 결과를 한 줄도 못 보는 것을 막는다.

Windows 콘솔은 시스템 코드페이지(한국어면 cp949)를 쓴다. 이 저장소의 CLI 는
전부 한국어와 ``✅``/``—`` 같은 기호를 찍으므로, 그대로 두면 첫 출력에서
``UnicodeEncodeError`` 로 죽는다. 특히 **통과 경로만 ASCII 인 검사기**에서
위험하다 — 위반이 있을 때만 죽어서, 정작 봐야 할 때 아무것도 못 본다
(``lint_spec.py`` 가 실제로 그랬다).
"""

import sys
from typing import TextIO


def use_utf8_output() -> None:
    """``sys.stdout``/``sys.stderr`` 를 UTF-8 로 돌린다. CLI 진입점에서 부른다."""

    _reconfigure(sys.stdout)
    _reconfigure(sys.stderr)


def _reconfigure(stream: TextIO | None) -> None:
    # 파이프로 리디렉션되면 stream 이 없거나 reconfigure 가 없을 수 있다.
    reconfigure = getattr(stream, "reconfigure", None)
    if reconfigure is None:
        return
    encoding = getattr(stream, "encoding", None) or ""
    if encoding.lower().replace("-", "") != "utf8":
        reconfigure(encoding="utf-8", errors="replace")
