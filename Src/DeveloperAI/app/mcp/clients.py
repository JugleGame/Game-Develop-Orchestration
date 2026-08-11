"""Factory wiring all MCP clients from application settings."""

from dataclasses import dataclass

from app.config.settings import Settings
from app.mcp.asset_client import AssetClient
from app.mcp.git_client import GitClient
from app.mcp.qa_client import QaClient
from app.mcp.strategic_client import StrategicClient
from app.mcp.unity_client import UnityClient


@dataclass(frozen=True)
class ToolClients:
    """Bundle of all MCP clients the orchestrator depends on."""

    strategic: StrategicClient
    unity: UnityClient
    qa: QaClient
    asset: AssetClient
    git: GitClient

    async def aclose(self) -> None:
        for client in (self.strategic, self.unity, self.qa, self.asset, self.git):
            await client.aclose()


def build_tool_clients(settings: Settings) -> ToolClients:
    """Construct one client per configured MCP server."""

    return ToolClients(
        strategic=StrategicClient(
            server_name="StrategicMcpServer",
            url=settings.strategic_mcp_url,
            timeout_seconds=settings.strategic_mcp_timeout_seconds,
            max_retries=settings.mcp_max_retries,
        ),
        unity=UnityClient(
            server_name="UnityMcpServer",
            url=settings.unity_mcp_url,
            timeout_seconds=settings.unity_mcp_timeout_seconds,
            max_retries=settings.mcp_max_retries,
        ),
        qa=QaClient(
            server_name="QaMcpServer",
            url=settings.qa_mcp_url,
            timeout_seconds=settings.qa_mcp_timeout_seconds,
            max_retries=settings.mcp_max_retries,
        ),
        asset=AssetClient(
            server_name="AssetGenMcpServer",
            url=settings.asset_mcp_url,
            timeout_seconds=settings.asset_mcp_timeout_seconds,
            max_retries=settings.mcp_max_retries,
        ),
        git=GitClient(
            server_name="GitMcpServer",
            url=settings.git_mcp_url,
            timeout_seconds=settings.git_mcp_timeout_seconds,
            max_retries=settings.mcp_max_retries,
        ),
    )
