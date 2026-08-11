"""Async SQLAlchemy engine/session wiring for the Postgres job store."""

from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config.settings import get_settings
from app.models.orm import Base

_settings = get_settings()

engine = create_async_engine(_settings.database_url, pool_pre_ping=True)
async_session_factory = async_sessionmaker(engine, expire_on_commit=False)


async def init_models() -> None:
    """Create tables that do not yet exist. Intended for dev/test bootstrap."""

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency yielding a request-scoped session."""

    async with async_session_factory() as session:
        yield session
