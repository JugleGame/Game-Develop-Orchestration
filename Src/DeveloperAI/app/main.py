"""DeveloperAI FastAPI entrypoint: wires config, MCP clients, the LangGraph
workflow, and the Job Store together (§2 Architecture)."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import redis.asyncio as redis
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

from app.api.routers.games import router as games_router
from app.config.settings import get_settings
from app.graph.graph import build_graph
from app.mcp.clients import build_tool_clients
from app.repository.database import async_session_factory, engine, init_models
from app.services.exceptions import GameJobNotFoundError, InvalidJobStateError
from app.services.game_service import GameService
from app.services.workflow_runner import WorkflowRunner
from app.utils.logging import configure_logging, get_logger
from app.utils.rate_limit import SlidingWindowRateLimiter

logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    configure_logging()
    settings = get_settings()

    # API gating (§7.3): consumed by app.api.deps on every request.
    app.state.api_key = settings.api_key
    if settings.api_key is None:
        logger.warning("API authentication is DISABLED (API_KEY not set) — local dev only")
    app.state.create_rate_limiter = (
        SlidingWindowRateLimiter(settings.rate_limit_create_per_minute)
        if settings.rate_limit_create_per_minute > 0
        else None
    )

    await init_models()

    tool_clients = build_tool_clients(settings)
    redis_client = redis.from_url(settings.redis_url, decode_responses=True)

    # Durable checkpointer (§7.1): LangGraph run state survives process
    # restarts, so in-flight games resume instead of being lost on redeploy.
    async with AsyncPostgresSaver.from_conn_string(
        settings.checkpointer_database_url
    ) as checkpointer:
        await checkpointer.setup()
        graph = build_graph(
            tool_clients,
            max_iterations=settings.development_qa_max_iterations,
            planning_max_iterations=settings.planning_max_iterations,
            concept_max_iterations=settings.concept_max_iterations,
            checkpointer=checkpointer,
        )
        runner = WorkflowRunner(
            graph,
            async_session_factory,
            redis_client,
            max_concurrent_jobs=settings.max_concurrent_jobs,
        )

        app.state.game_service = GameService(
            session_factory=async_session_factory,
            redis_client=redis_client,
            runner=runner,
        )

        logger.info("DeveloperAI started")
        try:
            yield
        finally:
            await tool_clients.aclose()
            await redis_client.aclose()
            await engine.dispose()
            logger.info("DeveloperAI stopped")


app = FastAPI(title="DeveloperAI", version="0.1.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=get_settings().cors_allow_origins,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(games_router)


@app.exception_handler(GameJobNotFoundError)
async def handle_job_not_found(request: Request, exc: GameJobNotFoundError) -> JSONResponse:
    return JSONResponse(status_code=404, content={"detail": str(exc)})


@app.exception_handler(InvalidJobStateError)
async def handle_invalid_state(request: Request, exc: InvalidJobStateError) -> JSONResponse:
    return JSONResponse(status_code=409, content={"detail": str(exc)})
