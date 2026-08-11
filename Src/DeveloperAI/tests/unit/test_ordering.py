"""Unit tests for the dependency-aware feature ordering used by CodeGen."""

from app.utils.ordering import order_by_dependencies


def _feature(feature_id: str, *deps: str) -> dict:
    return {"feature_id": feature_id, "description": feature_id, "dependencies": list(deps)}


def _ids(features: list[dict]) -> list[str]:
    return [f["feature_id"] for f in features]


def test_dependency_comes_before_dependent():
    features = [_feature("f-2", "f-1"), _feature("f-1")]

    assert _ids(order_by_dependencies(features)) == ["f-1", "f-2"]


def test_original_order_is_preserved_among_independent_features():
    features = [_feature("f-1"), _feature("f-2"), _feature("f-3")]

    assert _ids(order_by_dependencies(features)) == ["f-1", "f-2", "f-3"]


def test_chain_orders_transitively():
    features = [_feature("f-3", "f-2"), _feature("f-2", "f-1"), _feature("f-1")]

    assert _ids(order_by_dependencies(features)) == ["f-1", "f-2", "f-3"]


def test_unknown_dependency_id_is_ignored():
    # The plan may reference an already-shipped feature not in this batch.
    features = [_feature("f-1", "already-shipped"), _feature("f-2")]

    assert _ids(order_by_dependencies(features)) == ["f-1", "f-2"]


def test_cycle_falls_back_to_original_order_instead_of_failing():
    features = [_feature("f-1", "f-2"), _feature("f-2", "f-1"), _feature("f-3")]

    ordered = _ids(order_by_dependencies(features))

    # f-3 has no dependencies so it is placed first; the cyclic pair keeps
    # its original relative order rather than aborting the pipeline.
    assert ordered == ["f-3", "f-1", "f-2"]


def test_missing_dependencies_key_is_treated_as_no_dependencies():
    features = [{"feature_id": "f-1", "description": "no deps key"}]

    assert _ids(order_by_dependencies(features)) == ["f-1"]
