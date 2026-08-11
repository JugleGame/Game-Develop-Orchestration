"""BuildPrototype node: triggers a Unity project build for the job.

Alongside the build it records the project's **layout** — which scripts are
attached to a scene or prefab and which are not. That fact exists only in the
project files; the build result does not carry it, and a build succeeds whether
or not a single script is wired into the game. QA reads it through
``qa_common.build_reference``.
"""

import logging
from collections.abc import Callable, Coroutine
from typing import Any

from app.graph.state import GraphState, Stage
from app.mcp.unity_client import UnityClient
from app.models.schemas import JobStatus

NodeFn = Callable[[GraphState], Coroutine[Any, Any, dict]]

logger = logging.getLogger(__name__)


def build_build_prototype_node(client: UnityClient) -> NodeFn:
    async def build_prototype_node(state: GraphState) -> dict:
        build_result = await client.build_project(game_id=state["game_id"])

        # A layout inspection that fails must not fail the build — the build is
        # the deliverable and this is a report about it. QA then sees no
        # ``projectLayout`` key, which is honest: it means "not measured", not
        # "measured and clean". Silently substituting an empty report would tell
        # the judge the opposite.
        project_layout: dict[str, Any] | None = None
        try:
            project_layout = await client.inspect_project_layout(game_id=state["game_id"])
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Project layout inspection failed; QA will judge without it: %s: %s",
                type(exc).__name__,
                exc,
            )

        return {
            "current_stage": Stage.BUILD_PROTOTYPE,
            "status": JobStatus.DEVELOPING.value,
            "build_result": build_result,
            "project_layout": project_layout,
        }

    return build_prototype_node
