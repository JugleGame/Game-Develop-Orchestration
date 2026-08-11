"""Domain exceptions raised by the service layer.

API routers translate these into HTTP responses; they are not HTTP-aware
themselves so the service layer stays transport-agnostic.
"""


class GameJobError(Exception):
    """Base class for all game-job domain errors."""


class GameJobNotFoundError(GameJobError):
    def __init__(self, game_id: str) -> None:
        self.game_id = game_id
        super().__init__(f"Game job not found: {game_id}")


class InvalidJobStateError(GameJobError):
    """Raised when an operation is not valid for the job's current status."""

    def __init__(self, game_id: str, expected: str, actual: str) -> None:
        self.game_id = game_id
        self.expected = expected
        self.actual = actual
        super().__init__(f"Game job {game_id} expected status '{expected}' but was '{actual}'")
