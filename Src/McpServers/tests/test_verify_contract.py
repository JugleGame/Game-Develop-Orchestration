"""Regression coverage for the standalone MCP contract verifier."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


MCP_ROOT = Path(__file__).resolve().parents[1]


def test_contract_verifier_uses_utf8_when_windows_codepage_is_legacy() -> None:
    environment = os.environ.copy()
    environment["PYTHONIOENCODING"] = "cp1252"

    completed = subprocess.run(
        [sys.executable, "verify_contract.py"],
        cwd=MCP_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="strict",
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert "OK Agent-first 4서버 계약" in completed.stdout
