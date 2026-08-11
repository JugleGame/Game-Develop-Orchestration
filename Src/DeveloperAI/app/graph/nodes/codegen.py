"""CodeGen node: designs the game's architecture once, then writes one file at a time.

**The unit of work is a file, not a feature.** A spec states one mechanism, and a
mechanism is rarely one class — data definition, runtime behaviour and
presentation are different jobs. While the loop ran once per feature and the
server returned one file per call, "one spec = one file" was not a tendency but
an arithmetic identity, and the file could only be named after the spec that
asked for it (``spec-001`` → ``Spec001``). See ``Doc/설계/06 §1``.

So the pass now starts by asking UnityMcpServer to plan the whole game: which
files exist, what each is responsible for, which are MonoBehaviours, and where
each MonoBehaviour is attached. ``state["architecture"]`` holds that plan.

**The plan is made once per game.** A retry reuses it. Re-deciding the
decomposition every round would give a different structure every round, which is
the known cost of letting the developer side own the split (06 §3.1), and this
is the first mitigation for it.

On a retry loop (ErrorCorrection or a failed FunctionalTest routing back here),
``state["last_error"]`` carries the previous failure; its message/suggested fix
is appended to the affected file's prompt, and the file's previous source is
handed back as ``previous_source`` so the server **repairs** that file instead
of rewriting it from a blank page. A rewrite re-rolls every line, so a fix can
break a neighbouring line that was already correct, and five rounds of
re-rolling is not a convergent process.

A retry regenerates only the file(s) ``last_error`` is attributed to. Every
other file already has a working script from the previous pass, held in
``state["created_scripts"]`` and looked up **by path** rather than by list
position — a positional parallel only holds while every feature yields exactly
one file, which is the assumption this node just stopped making.

Two pieces of shared context travel with every call, because a ``create_script``
call is otherwise an isolated conversation that cannot see the rest of the game:

* ``project_context`` — the blueprint summary plus the full type map from the
  design. Fixed for the whole game, which is what lets the server cache it as a
  prompt prefix, and what lets the *first* file know its siblings exist.
* ``existing_types`` — the types actually written so far. Grows during the pass,
  so it deliberately rides *after* that cached prefix.
"""

from collections.abc import Callable, Coroutine
from typing import Any

from app.graph.state import GraphState, Stage
from app.mcp.unity_client import CreatedScript, UnityClient
from app.models.schemas import JobStatus
from app.utils.project_context import build_project_context

NodeFn = Callable[[GraphState], Coroutine[Any, Any, dict]]


def _error_context(last_error: dict[str, Any]) -> str:
    """Render ``last_error`` as prompt instructions for the code-gen LLM."""

    lines = [f"\n\n[Previous attempt failed] {last_error.get('message', 'Unknown error')}"]
    if last_error.get("file"):
        location = last_error["file"]
        if last_error.get("line") is not None:
            location += f":{last_error['line']}"
        lines.append(f"[Location] {location}")
    if last_error.get("suggested_fix"):
        lines.append(f"[Suggested fix] {last_error['suggested_fix']}")
    lines.append("Regenerate the script so this error does not occur again.")
    return "\n".join(lines)


def _error_applies_to(last_error: dict[str, Any], planned: dict[str, Any]) -> bool:
    """Does this failure belong to this file?

    Three ways a failure names its target, in descending order of precision:

    * the compiler's file path — exact, so only that one file is regenerated;
    * a ``related_feature_id`` — every file implementing that feature;
    * neither — widen to every file. The conservative fallback, not the rule.

    Path beats feature id deliberately. A compile error names a file, and now
    that a feature owns several files, attributing it to the feature would
    re-roll siblings that compiled cleanly.
    """

    failed_file = (last_error.get("file") or "").strip()
    if failed_file:
        # The compiler reports an absolute or project-relative path; comparing
        # the tail keeps both forms working.
        return failed_file.replace("\\", "/").endswith(planned["path"])

    feature_id = last_error.get("related_feature_id")
    if not feature_id:
        return True
    return feature_id in planned.get("featureIds", [])


def _previous_by_path(state: GraphState) -> dict[str, CreatedScript]:
    """Last pass's scripts, keyed by path.

    Keyed rather than positional on purpose: list position only identifies a
    file while every feature produces exactly one, and the point of the design
    pass is that it no longer does.
    """

    previous: dict[str, CreatedScript] = {}
    for item in state.get("created_scripts") or []:
        path = item.get("file", "")
        if not path:
            continue
        previous[path] = CreatedScript(
            file=path,
            contents=item.get("contents", ""),
            class_name=item.get("class_name", ""),
            types=tuple(item.get("types") or ()),
        )
    return previous


def _types_of(script: CreatedScript) -> tuple[str, ...]:
    """Every type the file declares, falling back to its class name.

    The fallback matters for a state checkpointed before ``types`` existed, and
    for a server that omits the field: the caller then knows one type instead of
    none, which is the old behaviour rather than a regression.
    """

    if script.types:
        return script.types
    return (script.class_name,) if script.class_name else ()


def _prewritten_contents(
    planned: dict[str, Any],
    features_by_id: dict[str, dict[str, Any]],
    files_per_feature: dict[str, int],
) -> str:
    """C# that was written outside the graph, if any, for this file.

    Two places can supply it, and both are escape hatches for callers who
    already have the source (which is what lets the pipeline run without an
    API key):

    * the planned file itself — usable always, because a file is exactly what
      ``contents`` describes;
    * the feature — usable **only when that feature owns this one file.** A
      feature split across three files has one blob of source and three places
      to put it, and picking one silently would write the same file three times.
      So it is ignored in that case rather than guessed at.
    """

    planned_contents = str(planned.get("contents") or "").strip()
    if planned_contents:
        return planned_contents

    feature_ids = planned.get("featureIds") or []
    if len(feature_ids) != 1:
        return ""
    feature_id = feature_ids[0]
    if files_per_feature.get(feature_id, 0) != 1:
        return ""
    return str((features_by_id.get(feature_id) or {}).get("contents") or "").strip()


def _file_prompt(planned: dict[str, Any], features_by_id: dict[str, dict[str, Any]]) -> str:
    """What this one file has to do, plus the specs it comes from.

    The spec text still travels — acceptance criteria and architecture-card
    guidance live there, and the design pass deliberately did not copy them.
    """

    lines = [
        f"Type: {planned['className']} ({planned['kind']})",
        f"Responsibility: {planned['responsibility']}",
    ]
    if planned.get("dependsOn"):
        lines.append("Depends on: " + ", ".join(planned["dependsOn"]))

    for feature_id in planned.get("featureIds", []):
        feature = features_by_id.get(feature_id)
        if feature is None:
            continue
        lines.append(f"\n--- Spec {feature_id} — {feature.get('title', '')} ---")
        lines.append(str(feature.get("description", "")))

    lines.append(
        "\nImplement only the responsibility above. The other types in this game "
        "are listed in the project context; reference them, do not redefine them."
    )
    return "\n".join(lines)


def build_codegen_node(client: UnityClient) -> NodeFn:
    async def codegen_node(state: GraphState) -> dict:
        last_error = state.get("last_error")
        features = state.get("feature_prompts", [])
        features_by_id = {item["feature_id"]: item for item in features}

        # Once per game. A retry reuses the stored plan rather than re-deciding
        # how the game splits into files.
        architecture = state.get("architecture")
        if not architecture:
            architecture = await client.design_architecture(
                game_id=state["game_id"],
                game_design=state.get("game_design") or {},
                feature_prompts=features,
            )

        planned_files: list[dict[str, Any]] = architecture.get("files", [])
        project_context = build_project_context(
            state.get("game_design"), type_map=architecture.get("typeMap", "")
        )
        files_per_feature: dict[str, int] = {}
        for item in planned_files:
            for feature_id in item.get("featureIds") or []:
                files_per_feature[feature_id] = files_per_feature.get(feature_id, 0) + 1

        # Which round of the retry loop this is. Both counters advance on their
        # own loop, so their sum is the number of times this node has already
        # run for this job; +1 makes the first pass attempt 1.
        attempt = state.get("dev_iteration_count", 0) + state.get("qa_iteration_count", 0) + 1

        previous = _previous_by_path(state)
        created: list[CreatedScript] = []
        # Parallel to ``created``: True where this pass actually generated the
        # script, False where a previous pass's file was carried over. Tracing
        # counts generations, and a reused file is not one.
        generated: list[bool] = []
        prompts: list[str] = []
        # Types the model may reference, in the order they were written.
        known_types: list[str] = []

        # The design pass already returned files in dependency order.
        for planned in planned_files:
            path = planned["path"]
            implicated = last_error is not None and _error_applies_to(last_error, planned)
            prior = previous.get(path)

            if last_error is not None and not implicated and prior is not None:
                # A retry, this file is not the one that failed, and a
                # previous-pass file exists for it — reuse it instead of
                # regenerating an identical script.
                created.append(prior)
                generated.append(False)
                prompts.append("")
                known_types.extend(_types_of(prior))
                continue

            prompt = _file_prompt(planned, features_by_id)
            if implicated:
                prompt += _error_context(last_error)

            script = await client.create_script(
                # The first feature this file serves is what a failure without a
                # file path is attributed back to; ``featureIds`` keeps them all.
                feature_id=(planned.get("featureIds") or [""])[0],
                prompt=prompt,
                contents=_prewritten_contents(planned, features_by_id, files_per_feature),
                project_context=project_context,
                existing_types=list(known_types),
                # Only on a retry that has something to repair.
                previous_source=prior.contents if (implicated and prior) else "",
                planned_path=path,
                planned_class=planned["className"],
            )
            created.append(script)
            generated.append(True)
            prompts.append(prompt)
            known_types.extend(_types_of(script))

        return {
            "current_stage": Stage.CODE_GEN,
            "status": JobStatus.DEVELOPING.value,
            # Stored so the retry loop reuses this plan instead of re-rolling it,
            # and so Deployment/QA can say what structure was intended.
            "architecture": architecture,
            "codegen_attempt": attempt,
            "codegen_project_context": project_context,
            # Kept as the plain path list ErrorCorrection matches against.
            "created_files": [script.file for script in created],
            "created_scripts": [
                {
                    "feature_id": (planned.get("featureIds") or [""])[0],
                    "feature_ids": planned.get("featureIds", []),
                    "file": script.file,
                    "contents": script.contents,
                    "class_name": script.class_name,
                    "types": list(script.types),
                    # Carried for the trace store, which records what the model
                    # was actually asked — including the appended error context
                    # on a retry, which is the part worth learning from.
                    "prompt": prompt,
                    "generated": was_generated,
                }
                for planned, script, prompt, was_generated in zip(
                    planned_files, created, prompts, generated
                )
            ],
            # Consume the note so a stale error never leaks into the next loop.
            "last_error": None,
        }

    return codegen_node
