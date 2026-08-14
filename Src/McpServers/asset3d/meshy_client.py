"""Minimal Meshy REST boundary for 3D generation tasks."""

from __future__ import annotations

import base64
import os
from typing import Any

import httpx


BASE_URL = "https://api.meshy.ai"
API_KEY_ENV = "MESHY_API_KEY"
TIMEOUT_SECONDS = 60.0


class MeshyUnavailable(Exception):
    """Meshy is not configured, rejected a request, or returned invalid data."""


def is_configured() -> bool:
    return bool(os.getenv("MESHY_API_KEY"))


def _request(method: str, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    api_key = os.getenv("MESHY_API_KEY")
    if not api_key:
        raise MeshyUnavailable(f"{API_KEY_ENV} not set")
    try:
        response = httpx.request(
            method,
            f"{BASE_URL}{path}",
            json=payload,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=TIMEOUT_SECONDS,
        )
    except httpx.HTTPError as exc:
        raise MeshyUnavailable(f"Meshy request failed: {exc}") from exc
    try:
        data = response.json()
    except ValueError:
        data = {}
    if response.status_code >= 400:
        message = data.get("message") or data.get("error") or response.text[:400]
        raise MeshyUnavailable(f"Meshy returned {response.status_code}: {message}")
    if not isinstance(data, dict):
        raise MeshyUnavailable("Meshy returned a non-object response")
    return data


def _create(path: str, payload: dict[str, Any]) -> str:
    task_id = _request("POST", path, payload).get("result")
    if not isinstance(task_id, str) or not task_id:
        raise MeshyUnavailable("Meshy returned no task id")
    return task_id


def create_text_preview(prompt: str, model_format: str, max_triangles: int) -> str:
    return _create(
        "/openapi/v2/text-to-3d",
        {
            "mode": "preview",
            "prompt": prompt,
            "model_type": "standard",
            "ai_model": "meshy-6",
            "should_remesh": True,
            "topology": "triangle",
            "target_polycount": max_triangles,
            "target_formats": [model_format],
        },
    )


def create_text_refine(
    preview_task_id: str, model_format: str, texture_prompt: str
) -> str:
    return _create(
        "/openapi/v2/text-to-3d",
        {
            "mode": "refine",
            "preview_task_id": preview_task_id,
            "ai_model": "meshy-6",
            "enable_pbr": True,
            "texture_prompt": texture_prompt,
            "texture_resolution": "2k",
            "remove_lighting": True,
            "target_formats": [model_format],
        },
    )


def create_image_task(image_url: str, model_format: str, max_triangles: int) -> str:
    return _create(
        "/openapi/v1/image-to-3d",
        {
            "image_url": image_url,
            "model_type": "smart-topology",
            "ai_model": "meshy-t2",
            "should_texture": False,
            "target_polycount": max_triangles,
            "target_formats": [model_format],
        },
    )


def create_retexture_task(model: bytes, model_format: str, texture_prompt: str) -> str:
    model_url = "data:application/octet-stream;base64," + base64.b64encode(model).decode()
    return _create(
        "/openapi/v1/retexture",
        {
            "model_url": model_url,
            "text_style_prompt": texture_prompt,
            "ai_model": "meshy-6",
            "enable_original_uv": False,
            "enable_pbr": True,
            "target_formats": [model_format],
        },
    )


def get_task(method: str, task_id: str) -> dict[str, Any]:
    path = {
        "image_to_3d": "/openapi/v1/image-to-3d",
        "retexture": "/openapi/v1/retexture",
    }.get(method, "/openapi/v2/text-to-3d")
    return _request("GET", f"{path}/{task_id}")


def download_model(url: str) -> bytes:
    try:
        response = httpx.get(url, timeout=TIMEOUT_SECONDS)
        response.raise_for_status()
    except httpx.HTTPError as exc:
        raise MeshyUnavailable(f"Meshy model download failed: {exc}") from exc
    if not response.content:
        raise MeshyUnavailable("Meshy model download was empty")
    return response.content
