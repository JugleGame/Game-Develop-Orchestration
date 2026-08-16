"""Bootstrap registers only the MCP boundaries required by the selected role."""

import importlib.util
from pathlib import Path


def _load_bootstrap():
    root = Path(__file__).resolve().parents[3]
    spec = importlib.util.spec_from_file_location("bootstrap", root / "scripts" / "bootstrap.py")
    assert spec and spec.loader
    bootstrap = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bootstrap)
    return bootstrap


def test_new_config_defaults_to_research_only(tmp_path, monkeypatch):
    bootstrap = _load_bootstrap()

    monkeypatch.setattr(bootstrap, "MCP_CONFIG", tmp_path / ".mcp.json")
    monkeypatch.setattr(bootstrap, "CODEX_CONFIG", tmp_path / ".codex" / "config.toml")
    monkeypatch.setattr(bootstrap, "ENV_FILE", tmp_path / ".env")
    monkeypatch.setattr(bootstrap, "VENV", tmp_path / ".venv")
    (tmp_path / ".env.example").write_text("", encoding="utf-8")
    monkeypatch.setattr(bootstrap, "ROOT", tmp_path)
    monkeypatch.setattr(bootstrap, "MCP_ROOT", tmp_path / "Src" / "McpServers")
    bootstrap.seed_local_files()

    servers = __import__("json").loads(bootstrap.MCP_CONFIG.read_text(encoding="utf-8"))["mcpServers"]
    assert set(servers) == {"research"}
    assert {"mcpServers": servers} == bootstrap.expected_mcp_config()
    codex = __import__("tomllib").loads(bootstrap.CODEX_CONFIG.read_text(encoding="utf-8"))
    assert set(codex["mcp_servers"]) == {"research"}
    assert bootstrap.codex_config_is_current()


def test_each_role_profile_registers_only_its_server():
    bootstrap = _load_bootstrap()

    expected = {
        "research": {"research"},
        "unity": {"unity"},
        "asset2d": {"asset"},
        "asset3d": {"asset3d"},
        "all": {"research", "unity", "asset", "asset3d"},
    }
    for profile, names in expected.items():
        config = bootstrap.expected_mcp_config(profile)
        assert set(config["mcpServers"]) == names
        codex = __import__("tomllib").loads(bootstrap.expected_codex_config(profile))
        assert set(codex["mcp_servers"]) == names
        assert all(server["required"] is True for server in codex["mcp_servers"].values())

    assert bootstrap.expected_mcp_config("asset3d")["mcpServers"]["asset3d"]["args"] == [
        "-m",
        "asset3d.server",
    ]


def test_profile_can_switch_repeatedly_without_overwriting_first_backup(tmp_path, monkeypatch):
    bootstrap = _load_bootstrap()
    monkeypatch.setattr(bootstrap, "MCP_CONFIG", tmp_path / ".mcp.json")
    monkeypatch.setattr(bootstrap, "CODEX_CONFIG", tmp_path / ".codex" / "config.toml")
    monkeypatch.setattr(bootstrap, "VENV", tmp_path / ".venv")
    monkeypatch.setattr(bootstrap, "MCP_ROOT", tmp_path / "Src" / "McpServers")
    bootstrap.MCP_CONFIG.write_text('{"custom": true}\n', encoding="utf-8")
    bootstrap.CODEX_CONFIG.parent.mkdir()
    bootstrap.CODEX_CONFIG.write_text("custom = true\n", encoding="utf-8")

    assert bootstrap.repair_mcp_config("unity") == 0
    assert bootstrap.repair_mcp_config("asset2d") == 0

    backup = bootstrap.MCP_CONFIG.with_suffix(".json.bak")
    assert backup.read_text(encoding="utf-8") == '{"custom": true}\n'
    assert set(__import__("json").loads(bootstrap.MCP_CONFIG.read_text())["mcpServers"]) == {
        "asset"
    }
    codex_backup = bootstrap.CODEX_CONFIG.with_suffix(".toml.bak")
    assert codex_backup.read_text(encoding="utf-8") == "custom = true\n"
    codex = __import__("tomllib").loads(bootstrap.CODEX_CONFIG.read_text(encoding="utf-8"))
    assert set(codex["mcp_servers"]) == {"asset"}
