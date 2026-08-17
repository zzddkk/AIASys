"""Team collaboration runtime backend — mission state layer."""

from .store import (
    VALID_TRANSITIONS,
    TeamError,
    TeamMission,
    TeamState,
    TeamStore,
    _scope_covers_path,
    _scopes_overlap,
)

__all__ = [
    "TeamStore",
    "TeamState",
    "TeamMission",
    "TeamError",
    "VALID_TRANSITIONS",
    "_scopes_overlap",
    "_scope_covers_path",
]
