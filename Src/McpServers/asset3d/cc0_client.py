"""Deterministic CC0 discovery for Poly Haven and operator-owned local packs."""

from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx


POLY_HAVEN_URL = "https://api.polyhaven.com"
USER_AGENT = "Game-Develop-Orchestration/1.0 (CC0 asset acquisition)"
TIMEOUT_SECONDS = 30.0
SUPPORTED_FORMATS = frozenset({"glb", "fbx"})
CC0_LICENSES = frozenset({"cc0", "cc0-1.0", "cc0 1.0", "public-domain"})
_WORDS = re.compile(r"[a-z0-9]+")
_STOP_WORDS = frozenset(
    {
        "and", "the", "with", "that", "one", "two", "for", "from", "into",
        "asset", "model", "three", "low", "poly", "stylized", "materials",
        "warm", "muted", "gray", "blue", "stage", "visual", "small", "large",
    }
)


class CC0ProviderError(Exception):
    """A configured CC0 source could not be searched or downloaded."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _terms(asset_spec: dict[str, Any]) -> set[str]:
    design = asset_spec.get("design") or {}
    text = " ".join(
        str(value)
        for value in (
            asset_spec.get("assetName", ""),
            asset_spec.get("assetType", ""),
            asset_spec.get("gameplayRole", ""),
            design.get("description", ""),
            design.get("style", ""),
        )
    )
    return {
        word
        for word in _WORDS.findall(text.lower())
        if len(word) > 2 and word not in _STOP_WORDS
    }


def _identity_terms(asset_spec: dict[str, Any]) -> set[str]:
    """Return explicit identity words that a semantic CC0 match must contain."""
    asset_type = str(asset_spec.get("assetType", "")).lower()
    text = f"{asset_spec.get('assetId', '')} {asset_spec.get('assetName', '')}"
    return {
        word
        for word in _WORDS.findall(text.lower())
        if len(word) > 2 and word not in _STOP_WORDS and word != asset_type
    }


def _primary_identity_term(asset_spec: dict[str, Any]) -> str:
    asset_type = str(asset_spec.get("assetType", "")).lower()
    words = [
        word
        for word in _WORDS.findall(str(asset_spec.get("assetName", "")).lower())
        if len(word) > 2 and word not in _STOP_WORDS and word != asset_type
    ]
    return words[-1] if words else ""


def _candidate_score(asset_spec: dict[str, Any], candidate: dict[str, Any]) -> tuple[int, int, str]:
    haystack = " ".join(
        str(value)
        for value in (
            candidate.get("name", ""),
            candidate.get("description", ""),
            candidate.get("category", ""),
            " ".join(candidate.get("tags") or []),
            candidate.get("id", ""),
        )
    ).lower()
    candidate_terms = set(_WORDS.findall(haystack))
    identity_matches = len(_identity_terms(asset_spec) & candidate_terms)
    primary = _primary_identity_term(asset_spec)
    if not primary or primary not in candidate_terms:
        identity_matches = 0
    total_matches = len(_terms(asset_spec) & candidate_terms)
    return identity_matches, total_matches, str(candidate.get("id", ""))


def _license_is_cc0(value: Any) -> bool:
    return isinstance(value, str) and value.strip().lower() in CC0_LICENSES


def _manifest_paths() -> list[Path]:
    raw = os.getenv("CC0_MANIFEST_PATHS", "")
    return [Path(item).expanduser().resolve() for item in raw.split(os.pathsep) if item.strip()]


def _manifest_entries(path: Path) -> list[dict[str, Any]]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CC0ProviderError(f"local CC0 manifest could not be read: {path}: {exc}") from exc
    value = value.get("assets", value) if isinstance(value, dict) else value
    if isinstance(value, dict):
        value = [{"id": key, **item} for key, item in value.items() if isinstance(item, dict)]
    if not isinstance(value, list):
        raise CC0ProviderError(f"local CC0 manifest assets must be a list or object: {path}")
    return [item for item in value if isinstance(item, dict)]


def search_local(asset_spec: dict[str, Any]) -> dict[str, Any]:
    rejected_license = 0
    rejected_quality = 0
    candidates: list[tuple[tuple[int, int, str], dict[str, Any], Path]] = []
    for manifest_path in _manifest_paths():
        for entry in _manifest_entries(manifest_path):
            if not _license_is_cc0(entry.get("license")):
                rejected_license += 1
                continue
            declared = str(entry.get("format") or Path(str(entry.get("path", ""))).suffix[1:]).lower()
            if declared not in SUPPORTED_FORMATS:
                rejected_quality += 1
                continue
            if entry.get("hasMesh") is False or entry.get("qualityPassed") is False:
                rejected_quality += 1
                continue
            triangles = entry.get("triangles")
            maximum = ((asset_spec.get("geometry") or {}).get("maxTriangles"))
            if isinstance(triangles, int) and isinstance(maximum, int) and triangles > maximum:
                rejected_quality += 1
                continue
            score = _candidate_score(asset_spec, entry)
            if score[0] > 0:
                candidates.append((score, entry, manifest_path))
    if candidates:
        _, entry, manifest_path = max(candidates, key=lambda item: item[0])
        source = Path(str(entry.get("path", "")))
        source = source.resolve() if source.is_absolute() else (manifest_path.parent / source).resolve()
        try:
            content = source.read_bytes()
        except OSError as exc:
            raise CC0ProviderError(f"local CC0 asset could not be read: {source}: {exc}") from exc
        actual_hash = _sha256(content)
        declared_hash = entry.get("sha256")
        if declared_hash and str(declared_hash).lower() != actual_hash:
            return {"status": "quality_rejected", "provider": "local_manifest", "reason": "hash_mismatch"}
        return {
            "status": "found",
            "provider": str(entry.get("provider") or "local_manifest"),
            "assetId": str(entry.get("id") or source.stem),
            "sourcePath": str(source),
            "content": content,
            "format": source.suffix[1:].lower(),
            "provenance": {
                "license": "CC0-1.0",
                "provider": str(entry.get("provider") or "local_manifest"),
                "sourceUrl": entry.get("sourceUrl"),
                "packId": entry.get("packId") or manifest_path.stem,
                "downloadedAt": _now(),
                "sha256": actual_hash,
            },
        }
    if rejected_license:
        return {"status": "license_rejected", "provider": "local_manifest", "rejected": rejected_license}
    if rejected_quality:
        return {"status": "quality_rejected", "provider": "local_manifest", "rejected": rejected_quality}
    return {"status": "not_found", "provider": "local_manifest"}


def _flatten_files(value: Any, trail: tuple[str, ...] = ()) -> list[tuple[tuple[str, ...], dict[str, Any]]]:
    found: list[tuple[tuple[str, ...], dict[str, Any]]] = []
    if isinstance(value, dict):
        if isinstance(value.get("url"), str):
            found.append((trail, value))
        for key, child in value.items():
            found.extend(_flatten_files(child, (*trail, str(key).lower())))
    elif isinstance(value, list):
        for child in value:
            found.extend(_flatten_files(child, trail))
    return found


async def _get_json(client: httpx.AsyncClient, path: str) -> dict[str, Any]:
    try:
        response = await client.get(path)
        response.raise_for_status()
        value = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise CC0ProviderError(f"Poly Haven request failed: {exc}") from exc
    if not isinstance(value, dict):
        raise CC0ProviderError("Poly Haven returned a non-object response")
    return value


async def search_poly_haven(asset_spec: dict[str, Any]) -> dict[str, Any]:
    async with httpx.AsyncClient(
        base_url=POLY_HAVEN_URL,
        headers={"User-Agent": USER_AGENT},
        timeout=TIMEOUT_SECONDS,
        follow_redirects=True,
    ) as client:
        assets = await _get_json(client, "/assets?t=models")
        candidates = []
        for asset_id, metadata in assets.items():
            if not isinstance(metadata, dict):
                continue
            candidate = {"id": asset_id, **metadata}
            score = _candidate_score(asset_spec, candidate)
            if score[0] > 0:
                candidates.append((score, candidate))
        if not candidates:
            return {"status": "not_found", "provider": "poly_haven"}
        _, candidate = max(candidates, key=lambda item: item[0])
        files = await _get_json(client, f"/files/{candidate['id']}")
        options = []
        for trail, descriptor in _flatten_files(files):
            joined = "/".join(trail)
            url = descriptor.get("url", "")
            suffix = Path(httpx.URL(url).path).suffix[1:].lower()
            model_format = suffix if suffix in SUPPORTED_FORMATS else ""
            if model_format in SUPPORTED_FORMATS:
                resolution_rank = next((index for index, token in enumerate(("1k", "2k", "4k", "8k")) if token in joined), 99)
                options.append((resolution_rank, model_format != "glb", url, model_format))
        if not options:
            return {"status": "quality_rejected", "provider": "poly_haven", "reason": "no_supported_model_format"}
        _, _, url, model_format = min(options)
        try:
            download = await client.get(url)
            download.raise_for_status()
        except httpx.HTTPError as exc:
            raise CC0ProviderError(f"Poly Haven download failed: {exc}") from exc
        content = download.content
        if not content:
            raise CC0ProviderError("Poly Haven download was empty")
        return {
            "status": "found",
            "provider": "poly_haven",
            "assetId": candidate["id"],
            "content": content,
            "format": model_format,
            "provenance": {
                "license": "CC0-1.0",
                "provider": "poly_haven",
                "sourceUrl": f"https://polyhaven.com/a/{candidate['id']}",
                "downloadUrl": url,
                "downloadedAt": _now(),
                "sha256": _sha256(content),
            },
        }


async def acquire(asset_spec: dict[str, Any]) -> dict[str, Any]:
    """Search local packs first, then Poly Haven, preserving distinct failure states."""
    local = search_local(asset_spec)
    if local["status"] == "found":
        return local
    try:
        remote = await search_poly_haven(asset_spec)
    except CC0ProviderError as exc:
        return {"status": "provider_failed", "provider": "poly_haven", "reason": str(exc)}
    if remote["status"] == "found":
        return remote
    if local["status"] in {"license_rejected", "quality_rejected"}:
        return local
    return remote
