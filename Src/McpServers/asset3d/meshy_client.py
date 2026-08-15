"""Async, bounded Meshy REST boundary for 3D generation tasks."""

from __future__ import annotations

import asyncio
import base64
import os
from typing import Any, Coroutine

import httpx


BASE_URL = "https://api.meshy.ai"
API_KEY_ENV = "MESHY_API_KEY"
TIMEOUT_SECONDS = 60.0
MAX_ATTEMPTS = 3
BACKOFF_SECONDS = 0.25
CREDIT_ESTIMATES = {
    "text_preview_meshy_6": 20,
    "text_refine_2k": 10,
    "image_smart_topology_untextured": 5,
    "multi_image_meshy_6_untextured": 20,
    "retexture_2k": 10,
}
CREDIT_ESTIMATE_SOURCE = "https://docs.meshy.ai/en/api/pricing"


class MeshyUnavailable(Exception):
    """Meshy rejected a request or returned invalid data with a stable state."""

    def __init__(self, message: str, *, status: str = "provider_failed", http_status: int | None = None):
        super().__init__(message)
        self.status = status
        self.http_status = http_status


def is_configured() -> bool:
    return bool(os.getenv("MESHY_API_KEY"))


def estimate_credits(operation: str) -> dict[str, Any]:
    return {
        "credits": CREDIT_ESTIMATES[operation],
        "operation": operation,
        "source": CREDIT_ESTIMATE_SOURCE,
    }


def _failure_state(status_code: int, message: str) -> str:
    lowered = message.lower()
    if status_code == 401:
        return "auth_failed"
    if status_code == 402:
        return "insufficient_credits"
    if status_code == 429:
        return "queue_limit" if "queue" in lowered or "concurrent" in lowered else "rate_limited"
    if "queue" in lowered and ("limit" in lowered or "full" in lowered):
        return "queue_limit"
    return "provider_failed"


async def _request_async(
    method: str, path: str, payload: dict[str, Any] | None = None
) -> dict[str, Any]:
    api_key = os.getenv("MESHY_API_KEY")
    if not api_key:
        raise MeshyUnavailable(f"{API_KEY_ENV} not set", status="provider_unconfigured")
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            async with httpx.AsyncClient(
                base_url=BASE_URL,
                headers={"Authorization": f"Bearer {api_key}"},
                timeout=TIMEOUT_SECONDS,
            ) as client:
                response = await client.request(method, path, json=payload)
        except httpx.HTTPError as exc:
            if attempt < MAX_ATTEMPTS:
                await asyncio.sleep(BACKOFF_SECONDS * (2 ** (attempt - 1)))
                continue
            raise MeshyUnavailable(f"Meshy request failed after {attempt} attempts: {exc}") from exc
        try:
            data = response.json()
        except ValueError:
            data = {}
        if response.status_code < 400:
            if not isinstance(data, dict):
                raise MeshyUnavailable("Meshy returned a non-object response")
            return data
        message = str(data.get("message") or data.get("error") or response.text[:400])
        state = _failure_state(response.status_code, message)
        retryable = response.status_code == 429 or response.status_code >= 500
        if retryable and attempt < MAX_ATTEMPTS:
            retry_after = response.headers.get("Retry-After")
            try:
                delay = min(float(retry_after), 5.0) if retry_after else BACKOFF_SECONDS * (2 ** (attempt - 1))
            except ValueError:
                delay = BACKOFF_SECONDS * (2 ** (attempt - 1))
            await asyncio.sleep(delay)
            continue
        raise MeshyUnavailable(
            f"Meshy returned {response.status_code}: {message}",
            status=state,
            http_status=response.status_code,
        )
    raise MeshyUnavailable("Meshy retry budget exhausted")


def _request(
    method: str, path: str, payload: dict[str, Any] | None = None
) -> Coroutine[Any, Any, dict[str, Any]]:
    """Return an awaitable so compatibility tests can replace this narrow seam."""
    return _request_async(method, path, payload)


async def _create_async(path: str, payload: dict[str, Any]) -> str:
    task_id = (await _request(path=path, method="POST", payload=payload)).get("result")
    if not isinstance(task_id, str) or not task_id:
        raise MeshyUnavailable("Meshy returned no task id")
    return task_id


def _create(path: str, payload: dict[str, Any]) -> Coroutine[Any, Any, str]:
    return _create_async(path, payload)


def create_text_preview(prompt: str, model_format: str, max_triangles: int) -> Any:
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


def create_text_refine(preview_task_id: str, model_format: str, texture_prompt: str) -> Any:
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


def create_image_task(image_url: str, model_format: str, max_triangles: int) -> Any:
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


def create_multi_image_task(
    image_urls: list[str], model_format: str, max_triangles: int
) -> Any:
    return _create(
        "/openapi/v1/multi-image-to-3d",
        {
            "image_urls": image_urls,
            "ai_model": "meshy-6",
            "should_texture": False,
            "should_remesh": True,
            "topology": "triangle",
            "target_polycount": max_triangles,
            "target_formats": [model_format],
        },
    )


def create_retexture_task(model: bytes, model_format: str, texture_prompt: str) -> Any:
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


def get_task(method: str, task_id: str) -> Any:
    path = {
        "image_to_3d": "/openapi/v1/image-to-3d",
        "multi_image_to_3d": "/openapi/v1/multi-image-to-3d",
        "retexture": "/openapi/v1/retexture",
    }.get(method, "/openapi/v2/text-to-3d")
    return _request("GET", f"{path}/{task_id}")


def get_balance() -> Any:
    return _request("GET", "/openapi/v1/balance")


def cancel_task(method: str, task_id: str) -> Any:
    path = {
        "image_to_3d": "/openapi/v1/image-to-3d",
        "multi_image_to_3d": "/openapi/v1/multi-image-to-3d",
        "retexture": "/openapi/v1/retexture",
    }.get(method, "/openapi/v2/text-to-3d")
    return _request("DELETE", f"{path}/{task_id}")


async def _download_model_async(url: str) -> bytes:
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS, follow_redirects=True) as client:
                response = await client.get(url)
                response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code in {401, 403, 404, 410}:
                raise MeshyUnavailable(
                    f"Meshy model download URL expired or was rejected: {exc}",
                    status="download_url_expired",
                    http_status=exc.response.status_code,
                ) from exc
            if attempt < MAX_ATTEMPTS:
                await asyncio.sleep(BACKOFF_SECONDS * (2 ** (attempt - 1)))
                continue
            raise MeshyUnavailable(f"Meshy model download failed: {exc}") from exc
        except httpx.HTTPError as exc:
            if attempt < MAX_ATTEMPTS:
                await asyncio.sleep(BACKOFF_SECONDS * (2 ** (attempt - 1)))
                continue
            raise MeshyUnavailable(f"Meshy model download failed: {exc}") from exc
        if not response.content:
            raise MeshyUnavailable("Meshy model download was empty")
        return response.content
    raise MeshyUnavailable("Meshy model download retry budget exhausted")


def download_model(url: str) -> Any:
    return _download_model_async(url)
