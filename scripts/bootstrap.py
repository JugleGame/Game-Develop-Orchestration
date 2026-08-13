"""Agent-first MCP 개발 환경을 설치하고 로컬 클라이언트 설정을 만든다."""

from __future__ import annotations

import argparse
import json
import platform
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VENV = ROOT / ".venv"
MCP_ROOT = ROOT / "Src" / "McpServers"
ENV_FILE = ROOT / ".env"
MCP_CONFIG = ROOT / ".mcp.json"
LOCK_FILE = ROOT / "requirements.lock"
MIN_PYTHON = (3, 12)
REQUIRED_IMPORTS = (
    "mcp",
    "PIL",
    "asyncpg",
    "httpx",
    "pytest",
    "strategic.server",
    "unity.server",
    "asset.server",
    "asset3d.server",
)


def venv_python() -> Path:
    return VENV / ("Scripts/python.exe" if platform.system() == "Windows" else "bin/python")


def run(*args: str) -> int:
    print("  $", " ".join(args))
    return subprocess.call(args, cwd=ROOT)


def create_environment() -> int:
    python = venv_python()
    if python.exists():
        print(f"  가상환경 유지: {python}")
        return 0
    if VENV.exists():
        print(f"  오류: 불완전한 가상환경이 있습니다: {VENV}", file=sys.stderr)
        print("  내용을 확인한 뒤 직접 제거하고 다시 실행하세요.", file=sys.stderr)
        return 1
    return run(sys.executable, "-m", "venv", str(VENV))


def install() -> int:
    python = str(venv_python())
    if not LOCK_FILE.is_file():
        print(f"Dependency lock file is missing: {LOCK_FILE}", file=sys.stderr)
        return 1
    if run(python, "-m", "pip", "install", "-r", str(LOCK_FILE)):
        return 1
    return run(
        python,
        "-m",
        "pip",
        "install",
        "--no-deps",
        "--no-build-isolation",
        "-e",
        f"{MCP_ROOT}[dev]",
    )


def seed_local_files() -> None:
    if not ENV_FILE.exists():
        shutil.copyfile(ROOT / ".env.example", ENV_FILE)
        print(f"  생성: {ENV_FILE}")
    else:
        print(f"  유지: {ENV_FILE}")

    if MCP_CONFIG.exists():
        print(f"  유지: {MCP_CONFIG}")
        return

    python = str(venv_python().resolve())
    cwd = str(MCP_ROOT.resolve())
    servers = {
        name: {
            "command": python,
            "args": ["-m", module],
            "cwd": cwd,
            "env": {"PYTHONPATH": cwd},
        }
        for name, module in (
            ("research", "strategic.server"),
            ("unity", "unity.server"),
            ("asset", "asset.server"),
        )
    }
    MCP_CONFIG.write_text(
        json.dumps({"mcpServers": servers}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"  생성: {MCP_CONFIG}")


def verify() -> int:
    python = venv_python()
    if not python.exists():
        print("  가상환경이 없습니다. --check 없이 실행하세요.", file=sys.stderr)
        return 1
    probe = (
        "import importlib, json; "
        f"names={list(REQUIRED_IMPORTS)!r}; "
        "failed={}; "
        "exec('for n in names:\\n try: importlib.import_module(n)\\n except Exception as e: failed[n]=str(e)'); "
        "print(json.dumps(failed, ensure_ascii=False))"
    )
    result = subprocess.run(
        [str(python), "-c", probe],
        cwd=MCP_ROOT,
        capture_output=True,
        text=True,
    )
    if result.returncode:
        print(result.stderr.strip(), file=sys.stderr)
        return 1
    failed = json.loads(result.stdout.strip().splitlines()[-1])
    if failed:
        for name, reason in failed.items():
            print(f"  X {name}: {reason}", file=sys.stderr)
        return 1
    print(f"  OK 필수 모듈 {len(REQUIRED_IMPORTS)}개")
    return 0


def expected_mcp_config() -> dict[str, object]:
    """Return the MCP configuration for this specific checkout."""

    python = str(venv_python().resolve())
    cwd = str(MCP_ROOT.resolve())
    servers = {
        name: {
            "command": python,
            "args": ["-m", module],
            "cwd": cwd,
            "env": {"PYTHONPATH": cwd},
        }
        for name, module in (
            ("research", "strategic.server"),
            ("unity", "unity.server"),
            ("asset", "asset.server"),
            ("asset3d", "asset3d.server"),
            ("asset3d", "asset3d.server"),
        )
    }
    return {"mcpServers": servers}


def mcp_config_is_current() -> bool:
    """Check a preserved local config without reading or printing any secrets."""

    try:
        current = json.loads(MCP_CONFIG.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return current == expected_mcp_config()


def repair_mcp_config() -> int:
    """Back up an existing local config, then recreate it for this checkout."""

    backup = MCP_CONFIG.with_suffix(".json.bak")
    if MCP_CONFIG.exists():
        if backup.exists():
            print(f"MCP config backup already exists: {backup}", file=sys.stderr)
            return 1
        shutil.copyfile(MCP_CONFIG, backup)
        print(f"MCP config backup: {backup}")
    MCP_CONFIG.write_text(
        json.dumps(expected_mcp_config(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"MCP config repaired: {MCP_CONFIG}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="설치 없이 현재 상태만 검사")
    parser.add_argument(
        "--repair-mcp-config",
        action="store_true",
        help="back up and recreate .mcp.json for this checkout",
    )
    args = parser.parse_args()
    if sys.version_info < MIN_PYTHON and not args.check:
        print("Python 3.12 이상이 필요합니다.", file=sys.stderr)
        return 1
    if args.check:
        return verify()
    if create_environment() or install():
        return 1
    seed_local_files()
    if args.repair_mcp_config:
        if repair_mcp_config():
            return 1
    elif not mcp_config_is_current():
        print(
            "WARNING: .mcp.json does not match this checkout. Existing local settings were "
            "preserved. Run with --repair-mcp-config to back up and recreate it."
        )
    return verify()


if __name__ == "__main__":
    raise SystemExit(main())
