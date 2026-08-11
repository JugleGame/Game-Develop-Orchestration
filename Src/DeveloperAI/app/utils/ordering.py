"""Dependency-aware ordering for FeatureImplementationPrompt dicts (§5.2).

Strategic AI declares ``dependencies`` between features; CodeGen must generate
a feature's script only after the scripts it depends on exist. This is a
stable topological sort: among features whose dependencies are satisfied, the
original (priority) order is preserved.
"""

from typing import Any

FeatureDict = dict[str, Any]


def order_by_dependencies(features: list[FeatureDict]) -> list[FeatureDict]:
    """Return ``features`` reordered so dependencies come before dependents.

    Robust by design rather than strict: a dependency id that names no feature
    in the list is ignored (the plan may reference already-shipped features),
    and on a dependency cycle the still-unplaced features are appended in
    their original order instead of failing the whole pipeline.
    """

    known_ids = {feature["feature_id"] for feature in features}
    placed: set[str] = set()
    remaining = list(features)
    ordered: list[FeatureDict] = []

    while remaining:
        ready = [
            feature
            for feature in remaining
            if all(
                dep in placed or dep not in known_ids
                for dep in feature.get("dependencies", [])
            )
        ]
        if not ready:
            # Dependency cycle: fall back to the original order for the rest.
            ordered.extend(remaining)
            break

        for feature in ready:
            ordered.append(feature)
            placed.add(feature["feature_id"])
        remaining = [feature for feature in remaining if feature["feature_id"] not in placed]

    return ordered
