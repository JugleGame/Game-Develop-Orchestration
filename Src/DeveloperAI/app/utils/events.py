"""Redis pub/sub helpers used to stream job progress to the Web front-end."""

from collections.abc import AsyncIterator
from typing import Any

import redis.asyncio as redis
from pydantic import BaseModel


class JobEvent(BaseModel):
    """A single progress update emitted while a job's graph is running."""

    game_id: str
    stage: str
    status: str
    detail: dict[str, Any] = {}


def _channel_name(game_id: str) -> str:
    return f"job-events:{game_id}"


async def publish_job_event(client: redis.Redis, event: JobEvent) -> None:
    """Publish a job event to its game-scoped pub/sub channel."""

    await client.publish(_channel_name(event.game_id), event.model_dump_json())


async def subscribe_job_events(client: redis.Redis, game_id: str) -> AsyncIterator[str]:
    """Yield raw JSON payloads published for ``game_id`` until cancelled."""

    pubsub = client.pubsub()
    await pubsub.subscribe(_channel_name(game_id))
    try:
        async for message in pubsub.listen():
            if message["type"] != "message":
                continue
            yield message["data"]
    finally:
        await pubsub.unsubscribe(_channel_name(game_id))
        await pubsub.aclose()
