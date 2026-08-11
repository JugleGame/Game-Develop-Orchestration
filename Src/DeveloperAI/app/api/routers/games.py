"""§6 Web <-> Backend API. Routes only validate input and call GameService."""

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse

from app.api.deps import enforce_create_rate_limit, get_game_service, require_api_key
from app.models.api import (
    ApprovalRequest,
    ApprovalResponse,
    ArtifactResponse,
    CancelResponse,
    GameCreateRequest,
    GameCreateResponse,
    GameStatusResponse,
    GameSummaryResponse,
)
from app.services.game_service import GameService

router = APIRouter(
    prefix="/games", tags=["games"], dependencies=[Depends(require_api_key)]
)


@router.post(
    "",
    response_model=GameCreateResponse,
    status_code=201,
    dependencies=[Depends(enforce_create_rate_limit)],
)
async def create_game(
    request: GameCreateRequest, service: GameService = Depends(get_game_service)
) -> GameCreateResponse:
    return await service.create_game(request.prompt)


@router.get("", response_model=list[GameSummaryResponse])
async def list_games(
    limit: int = 50, service: GameService = Depends(get_game_service)
) -> list[GameSummaryResponse]:
    return await service.list_games(limit=limit)


@router.post(
    "/{game_id}/revise",
    response_model=GameCreateResponse,
    dependencies=[Depends(enforce_create_rate_limit)],
)
async def revise_game(
    game_id: str, request: GameCreateRequest, service: GameService = Depends(get_game_service)
) -> GameCreateResponse:
    return await service.revise_game(game_id, request.prompt)


@router.get("/{game_id}/status", response_model=GameStatusResponse)
async def get_game_status(
    game_id: str, service: GameService = Depends(get_game_service)
) -> GameStatusResponse:
    return await service.get_status(game_id)


@router.get("/{game_id}/stream")
async def stream_game_progress(
    game_id: str, service: GameService = Depends(get_game_service)
) -> StreamingResponse:
    async def event_source():
        async for payload in service.stream_events(game_id):
            yield f"data: {payload}\n\n"

    return StreamingResponse(event_source(), media_type="text/event-stream")


@router.post("/{game_id}/approve", response_model=ApprovalResponse)
async def approve_game(
    game_id: str, request: ApprovalRequest, service: GameService = Depends(get_game_service)
) -> ApprovalResponse:
    return await service.approve(
        game_id,
        approved=request.approved,
        feedback=request.feedback,
        edited_idea=request.edited_idea,
    )


@router.get("/{game_id}/artifact", response_model=ArtifactResponse)
async def get_game_artifact(
    game_id: str, service: GameService = Depends(get_game_service)
) -> ArtifactResponse:
    return await service.get_artifact(game_id)


@router.post("/{game_id}/cancel", response_model=CancelResponse)
async def cancel_game(
    game_id: str, service: GameService = Depends(get_game_service)
) -> CancelResponse:
    return await service.cancel(game_id)
