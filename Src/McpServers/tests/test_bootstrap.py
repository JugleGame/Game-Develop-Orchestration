"""Bootstrap must register every MCP boundary for this checkout."""

import importlib.util
from pathlib import Path


def test_expected_mcp_config_registers_asset3d_server():
    root = Path(__file__).resolve().parents[3]
    spec = importlib.util.spec_from_file_location("bootstrap", root / "scripts" / "bootstrap.py")
    assert spec and spec.loader
    bootstrap = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bootstrap)

    servers = bootstrap.expected_mcp_config()["mcpServers"]
    assert set(servers) == {"research", "unity", "asset", "asset3d"}
    assert servers["asset3d"]["args"] == ["-m", "asset3d.server"]
