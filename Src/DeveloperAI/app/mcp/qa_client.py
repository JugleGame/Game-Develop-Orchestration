"""Client for QaMcpServer (§2.3)."""

from typing import Any

from app.mcp.base_client import BaseToolClient
from app.models.schemas import ExecutionErrorReport, GameDesignDocument, QAPolicy


class QaClient(BaseToolClient):
    """Wraps QA policy authoring and prototype verification tools."""

    async def establish_qa_policy(self, *, game_design: GameDesignDocument) -> QAPolicy:
        body = await self.call_tool(
            "establish_qa_policy", {"gameDesign": game_design.model_dump(mode="json")}
        )
        return QAPolicy.model_validate(body["qaPolicy"])

    async def verify_prototype_structure(
        self, *, game_design: GameDesignDocument, build: str
    ) -> dict[str, Any]:
        return await self.call_tool(
            "verify_prototype_structure",
            {"gameDesign": game_design.model_dump(mode="json"), "build": build},
        )

    async def compare_structure(
        self, *, game_design: GameDesignDocument, build: str
    ) -> dict[str, Any]:
        return await self.call_tool(
            "compare_structure",
            {"gameDesign": game_design.model_dump(mode="json"), "build": build},
        )

    async def run_functional_verification(
        self, *, game_design: GameDesignDocument, build: str, qa_policy: QAPolicy
    ) -> tuple[bool, ExecutionErrorReport | None]:
        body = await self.call_tool(
            "run_functional_verification",
            {
                "gameDesign": game_design.model_dump(mode="json"),
                "build": build,
                "qaPolicy": qa_policy.model_dump(mode="json"),
            },
        )
        passed = body["result"] == "PASS"
        error_report = None
        if not passed and body.get("errorReport"):
            error_report = ExecutionErrorReport.model_validate(body["errorReport"])
        return passed, error_report

    async def generate_error_report(
        self, *, game_design: GameDesignDocument, build: str, logs: str
    ) -> ExecutionErrorReport:
        body = await self.call_tool(
            "generate_error_report",
            {"gameDesign": game_design.model_dump(mode="json"), "build": build, "logs": logs},
        )
        return ExecutionErrorReport.model_validate(body["errorReport"])
