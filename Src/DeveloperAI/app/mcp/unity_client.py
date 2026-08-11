"""Client for UnityMcpServer (§2.4)."""

from dataclasses import dataclass
from typing import Any

from app.mcp.base_client import BaseToolClient
from app.models.schemas import CompileError


@dataclass(frozen=True)
class CreatedScript:
    """What UnityMcpServer wrote, and what it wrote there.

    §03 only promises ``body["file"]``; ``contents``, ``class_name`` and
    ``types`` are extras this server returns. They are what let a retry hand the
    previous source back for repair, and let the next file in the same pass know
    which types already exist — all empty against a server that omits them.

    ``types`` lists every type the file declares, not just the one it is named
    after. Carrying only the first made sibling types inside the same file
    invisible to later scripts, so one file could redefine what another already
    had (Doc/설계/06 §2.1).
    """

    file: str
    contents: str = ""
    class_name: str = ""
    types: tuple[str, ...] = ()


class UnityClient(BaseToolClient):
    """Wraps the Unity build/scripting tools proxied to UnityEditorPlugin."""

    async def create_script(
        self,
        *,
        feature_id: str,
        prompt: str,
        contents: str = "",
        project_context: str = "",
        existing_types: list[str] | None = None,
        previous_source: str = "",
        planned_path: str = "",
        planned_class: str = "",
    ) -> CreatedScript:
        """Create (or repair) a C# script for ``feature_id``.

        ``contents`` carries a finished C# source when the caller has already
        produced one; UnityMcpServer then skips its own generation step (and the
        ``ANTHROPIC_API_KEY`` it would need for it) and only writes the file.
        Left empty, the server generates from ``prompt`` as before.

        ``project_context`` / ``existing_types`` give the generator the rest of
        the game — without them each call is an isolated conversation that
        cannot know which other types this game defines. ``previous_source``
        turns a retry into a repair of that file instead of a rewrite from
        scratch.
        """

        payload: dict[str, Any] = {"featureId": feature_id, "prompt": prompt}
        # Each extra is sent only when there is something to send: §03 defines
        # the tool as ``create_script(featureId, prompt)``, so a server built to
        # that contract rejects unknown arguments outright.
        if contents:
            payload["contents"] = contents
        if project_context:
            payload["projectContext"] = project_context
        if existing_types:
            payload["existingTypes"] = existing_types
        if previous_source:
            payload["previousSource"] = previous_source
        # The architecture pass already named this file and its type. Sending
        # them keeps the server from re-deriving a name from ``feature_id``,
        # which is where ``Spec001`` came from.
        if planned_path:
            payload["plannedPath"] = planned_path
        if planned_class:
            payload["plannedClass"] = planned_class

        body = await self.call_tool("create_script", payload)
        return CreatedScript(
            file=body["file"],
            contents=body.get("contents") or "",
            class_name=body.get("className") or "",
            types=tuple(body.get("types") or ()),
        )

    async def design_architecture(
        self,
        *,
        game_id: str,
        game_design: dict[str, Any],
        feature_prompts: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Plan the whole game's files, prefabs and scene before writing code.

        Called once per game. The result is fixed for the run, so retries reuse
        it rather than re-deciding the decomposition — a design that is re-rolled
        each round gives a different structure each round, which is the known
        cost of letting the developer side own the split (Doc/설계/06 §3.1).
        """

        return await self.call_tool(
            "design_architecture",
            {
                "gameId": game_id,
                "gameDesign": game_design,
                "featurePrompts": feature_prompts,
            },
        )

    async def create_scene(self, *, feature_id: str, scene_name: str) -> dict[str, Any]:
        return await self.call_tool(
            "create_scene", {"featureId": feature_id, "sceneName": scene_name}
        )

    async def import_asset(self, *, feature_id: str, asset_path: str) -> dict[str, Any]:
        return await self.call_tool(
            "import_asset", {"featureId": feature_id, "assetPath": asset_path}
        )

    async def build_project(self, *, game_id: str) -> dict[str, Any]:
        return await self.call_tool("build_project", {"gameId": game_id})

    async def inspect_project_layout(self, *, game_id: str) -> dict[str, Any]:
        """Which scripts are actually wired into a scene or prefab.

        Reads files only, so it answers even when the Editor is not running —
        unlike every other tool on this server. QA folds the result into its
        ``build`` reference; without it the structure judge is asked whether the
        prototype is assembled while being shown only build metadata.
        """

        return await self.call_tool("inspect_project_layout", {"gameId": game_id})

    async def run_playmode_test(self, *, game_id: str) -> dict[str, Any]:
        return await self.call_tool("run_playmode_test", {"gameId": game_id})

    async def get_compile_errors(self, *, game_id: str) -> list[CompileError]:
        body = await self.call_tool("get_compile_errors", {"gameId": game_id})
        return [CompileError.model_validate(item) for item in body.get("errors", [])]
