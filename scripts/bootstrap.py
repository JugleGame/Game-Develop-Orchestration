"""Set up this repository on a new machine, in one command.

    python scripts/bootstrap.py            # create the venv, install, verify
    python scripts/bootstrap.py --check    # verify only, install nothing

What it does, and why each step was a trap before:

1. **One virtual environment, at ``Src/DeveloperAI/.venv``.** ``.mcp.json`` and
   ``.vscode/*.json`` name that path, so it is the one that has to exist —
   ``Src/McpServers/README.md`` used to send readers to a second venv that
   nothing ever launched.
2. **Both distributions installed into it.** ``Src/DeveloperAI`` is a package
   (``pip install -e``); ``Src/McpServers`` is a flat set of top-level
   packages that setuptools cannot auto-discover, so only its dependencies are
   installed and ``PYTHONPATH`` carries the imports (the way ``serve_all.py``
   and ``.mcp.json`` already do it). Both dependency lists are read from the
   ``pyproject.toml`` files, never restated here.
3. **``.env`` from ``.env.example``**, if it is missing.
4. **A verification pass** that imports what each path needs and reports which
   credentials are still blank — the failures this repository actually hits on
   a fresh checkout are a missing import and an unset ``RESEARCH_DSN``, and
   both otherwise appear minutes later inside a pipeline run.

Only the standard library is used: this has to run before any environment
exists. ``print`` is the interface here, as in ``Src/McpServers/serve_all.py``.
"""

from __future__ import annotations

import argparse
import os
import platform
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
VENV_DIR = REPO_ROOT / "Src" / "DeveloperAI" / ".venv"
ORCHESTRATOR = REPO_ROOT / "Src" / "DeveloperAI"
MCP_SERVERS = REPO_ROOT / "Src" / "McpServers"
ENV_FILE = REPO_ROOT / ".env"
ENV_EXAMPLE = REPO_ROOT / ".env.example"
WEB_ENV_FILE = REPO_ROOT / "Src" / "Web" / ".env"
WEB_ENV_EXAMPLE = REPO_ROOT / "Src" / "Web" / ".env.example"

MIN_PYTHON = (3, 12)

# Imported by the orchestrator (path A) and by the tool servers (both paths).
# A name here that fails to import means the install did not take.
REQUIRED_IMPORTS: tuple[tuple[str, str], ...] = (
    ("fastapi", "orchestrator"),
    ("langgraph", "orchestrator"),
    ("sqlalchemy", "orchestrator"),
    ("redis", "orchestrator"),
    ("pydantic_settings", "orchestrator"),
    ("mcp", "both"),
    ("anthropic", "mcp servers"),
    ("git", "mcp servers"),
    ("PIL", "mcp servers"),
    ("asyncpg", "mcp servers"),
    ("pytest", "tests"),
)


def _tolerant_console() -> None:
    """Never let the console encoding be the thing that stops setup.

    A Korean Windows console is cp949, which encodes Hangul but not every
    punctuation mark. Re-encoding to UTF-8 would turn the Hangul into mojibake
    there, so the encoding is left alone and only the error policy is relaxed.
    """

    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(errors="replace")


def venv_python(venv: Path = VENV_DIR) -> Path:
    """The interpreter inside ``venv``, on either OS layout."""

    if platform.system() == "Windows":
        return venv / "Scripts" / "python.exe"
    return venv / "bin" / "python"


def _run(args: list[str], cwd: Path | None = None) -> int:
    printable = " ".join(str(a) for a in args)
    print(f"  $ {printable}")
    return subprocess.call(args, cwd=str(cwd) if cwd else None)


def _dependencies(pyproject: Path) -> list[str]:
    """The ``project.dependencies`` list, so nothing is restated in this file."""

    with pyproject.open("rb") as handle:
        data = tomllib.load(handle)
    return list(data.get("project", {}).get("dependencies", []))


def create_venv() -> int:
    """Create the virtual environment if it is not already there."""

    python = venv_python()
    if python.exists():
        print(f"  venv 있음: {python}")
        return 0
    if VENV_DIR.exists():
        print(f"  ! {VENV_DIR} 가 있지만 파이썬이 없다. 지우고 다시 만든다.")
        shutil.rmtree(VENV_DIR)
    print(f"  venv 생성: {VENV_DIR}")
    return _run([sys.executable, "-m", "venv", str(VENV_DIR)])


def install(python: Path) -> int:
    """Install both distributions into ``python``'s environment."""

    code = _run([str(python), "-m", "pip", "install", "--upgrade", "pip"])
    if code:
        return code
    code = _run([str(python), "-m", "pip", "install", "-e", f"{ORCHESTRATOR}[dev]"])
    if code:
        return code
    deps = _dependencies(MCP_SERVERS / "pyproject.toml")
    if not deps:
        print("  ! Src/McpServers/pyproject.toml 에 dependencies 가 없다.", file=sys.stderr)
        return 1
    return _run([str(python), "-m", "pip", "install", *deps])


def ensure_env_file() -> None:
    """Seed ``.env`` from the committed examples, never overwriting.

    The dashboard keeps its own file because Vite only exposes ``VITE_``
    variables to the browser bundle, and the root file holds secrets that must
    never be shipped there.
    """

    for example, target, label in (
        (ENV_EXAMPLE, ENV_FILE, ".env"),
        (WEB_ENV_EXAMPLE, WEB_ENV_FILE, "Src/Web/.env"),
    ):
        if target.exists():
            print(f"  {label} 있음, 손대지 않는다: {target}")
            continue
        shutil.copyfile(example, target)
        print(f"  {label} 생성: {target}")
    print("  -> 자격증명은 직접 채운다.")


def _import_report(python: Path) -> tuple[bool, str]:
    """Import every required module inside ``python`` and report failures."""

    names = [name for name, _ in REQUIRED_IMPORTS]
    owner = dict(REQUIRED_IMPORTS)
    probe = (
        "import importlib, json, sys\n"
        f"names = {names!r}\n"
        "missing = []\n"
        "for n in names:\n"
        "    try:\n"
        "        importlib.import_module(n)\n"
        "    except Exception:\n"
        "        missing.append(n)\n"
        "print(json.dumps({'python': sys.version.split()[0], 'missing': missing}))\n"
    )
    result = subprocess.run(
        [str(python), "-c", probe], capture_output=True, text=True, cwd=str(REPO_ROOT)
    )
    if result.returncode != 0:
        return False, f"검사 실행 실패: {result.stderr.strip()[:400]}"

    import json as _json

    payload = _json.loads(result.stdout.strip().splitlines()[-1])
    missing = payload["missing"]
    lines = [f"  파이썬 {payload['python']}  ({python})"]
    if missing:
        for name in missing:
            lines.append(f"  X {name} 없음 ({owner[name]})")
        lines.append("  -> python scripts/bootstrap.py 를 --check 없이 다시 실행한다.")
        return False, "\n".join(lines)
    lines.append(f"  OK 필수 패키지 {len(names)}개 모두 import 된다.")
    return True, "\n".join(lines)


def _credentials_report() -> str:
    """Which optional-but-load-bearing values are still blank."""

    sys.path.insert(0, str(MCP_SERVERS))
    from common.env import parse_env_file  # noqa: PLC0415 — needs the path above

    values: dict[str, str] = {}
    if ENV_FILE.exists():
        values = parse_env_file(ENV_FILE.read_text(encoding="utf-8"))

    def configured(name: str) -> bool:
        return bool(os.environ.get(name) or values.get(name))

    lines: list[str] = []
    checks = (
        ("ANTHROPIC_API_KEY", "경로 A(오케스트레이터)의 LLM 호출. 경로 B 는 없어도 된다."),
        ("RESEARCH_DSN", "strategic 서버. 없으면 그 서버만 기동 직후 종료한다."),
        ("UNITY_PROJECT_PATH", "unity 서버의 스크립트 투입·빌드 대상."),
    )
    for name, note in checks:
        mark = "OK" if configured(name) else "--"
        lines.append(f"  {mark} {name}: {note}")
    return "\n".join(lines)


def verify(python: Path, header: str) -> int:
    """Report the state of the environment; non-zero when it cannot run."""

    print(f"\n{header}")
    ok, report = _import_report(python)
    print(report)
    print("\n  설정값:")
    print(_credentials_report())

    if platform.system() != "Windows":
        rel = os.path.relpath(python, MCP_SERVERS)
        print(
            "\n  Windows 가 아니다. Claude Code 를 켜기 전에 이 한 줄이 필요하다:\n"
            f"    export GDAI_PYTHON='{rel.replace(os.sep, '/')}'\n"
            "  (.mcp.json 의 command 가 이 변수를 읽는다. Claude Code 는 .env 를 읽지 않는다.)"
        )
    return 0 if ok else 1


def main() -> int:
    _tolerant_console()
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--check", action="store_true", help="설치 없이 현재 환경만 검사한다")
    args = parser.parse_args()

    if sys.version_info < MIN_PYTHON and not args.check:
        print(
            f"파이썬 {MIN_PYTHON[0]}.{MIN_PYTHON[1]} 이상이 필요하다 "
            f"(지금 {sys.version.split()[0]}).",
            file=sys.stderr,
        )
        return 1

    python = venv_python()

    if args.check:
        if not python.exists():
            print(f"venv 가 없다: {VENV_DIR}", file=sys.stderr)
            print("python scripts/bootstrap.py 를 먼저 실행한다.", file=sys.stderr)
            return 1
        return verify(python, f"환경 검사: {REPO_ROOT}")

    print(f"Game-Developer-AI 환경 구성: {REPO_ROOT}\n")

    print("[1/4] 가상환경")
    if create_venv():
        return 1
    python = venv_python()

    print("\n[2/4] 의존성 (오케스트레이터 + MCP 서버, 한 venv 에)")
    if install(python):
        return 1

    print("\n[3/4] .env")
    ensure_env_file()

    code = verify(python, "[4/4] 검증")

    print(
        "\n다음 단계:\n"
        "  1. .env 를 열어 ANTHROPIC_API_KEY / RESEARCH_DSN / UNITY_PROJECT_PATH 를 채운다\n"
        "  2. cd Src/DeveloperAI && docker compose up -d      # Postgres + Redis\n"
        "  3. cd Src/McpServers && python serve_all.py        # MCP 서버 5대\n"
        "  4. cd Src/McpServers && python verify_contract.py  # §03 계약 대조\n"
        "  자세한 것은 SETUP.md."
    )
    return code


if __name__ == "__main__":
    raise SystemExit(main())
