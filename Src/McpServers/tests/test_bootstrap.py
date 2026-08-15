"""Bootstrap must register every MCP boundary for this checkout."""

import importlib.util
from pathlib import Path


def test_new_and_expected_mcp_config_register_every_server(tmp_path, monkeypatch):
    root = Path(__file__).resolve().parents[3]
    spec = importlib.util.spec_from_file_location("bootstrap", root / "scripts" / "bootstrap.py")
    assert spec and spec.loader
    bootstrap = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bootstrap)

    monkeypatch.setattr(bootstrap, "MCP_CONFIG", tmp_path / ".mcp.json")
    monkeypatch.setattr(bootstrap, "ENV_FILE", tmp_path / ".env")
    monkeypatch.setattr(bootstrap, "VENV", tmp_path / ".venv")
    (tmp_path / ".env.example").write_text("", encoding="utf-8")
    monkeypatch.setattr(bootstrap, "ROOT", tmp_path)
    monkeypatch.setattr(bootstrap, "MCP_ROOT", tmp_path / "Src" / "McpServers")
    bootstrap.seed_local_files()

    servers = __import__("json").loads(bootstrap.MCP_CONFIG.read_text(encoding="utf-8"))["mcpServers"]
    assert set(servers) == {"research", "unity", "asset", "asset3d"}
    assert servers["asset3d"]["args"] == ["-m", "asset3d.server"]
    assert {"mcpServers": servers} == bootstrap.expected_mcp_config()
