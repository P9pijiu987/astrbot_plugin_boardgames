from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any

from .base import FIRST, SECOND, GameEngine
from .registry import restore_engine


@dataclass(slots=True)
class Player:
    user_id: str
    name: str

    def to_dict(self) -> dict[str, str]:
        return {"user_id": self.user_id, "name": self.name}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Player:
        return cls(str(data["user_id"]), str(data.get("name", data["user_id"])))


@dataclass(slots=True)
class GameSession:
    key: str
    game_id: str
    engine: GameEngine
    players: dict[str, Player | None]
    status: str = "waiting"
    created_at: float = field(default_factory=time.time)
    last_action_at: float = field(default_factory=time.time)
    pending: dict[str, Any] | None = None
    side_choices: dict[str, str] = field(default_factory=dict)
    clock: dict[str, Any] | None = None
    clock_disabled: bool = False

    def side_for(self, user_id: str) -> str | None:
        uid = str(user_id)
        for side in (FIRST, SECOND):
            player = self.players.get(side)
            if player and player.user_id == uid:
                return side
        return None

    def other_player(self, side: str) -> Player | None:
        return self.players.get(SECOND if side == FIRST else FIRST)

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "game_id": self.game_id,
            "engine": self.engine.to_dict(),
            "players": {
                side: player.to_dict() if player else None
                for side, player in self.players.items()
            },
            "status": self.status,
            "created_at": self.created_at,
            "last_action_at": self.last_action_at,
            "pending": self.pending,
            "side_choices": dict(self.side_choices),
            "clock": self.clock,
            "clock_disabled": self.clock_disabled,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> GameSession:
        players = {
            side: Player.from_dict(value) if value else None
            for side, value in dict(data.get("players", {})).items()
        }
        players.setdefault(FIRST, None)
        players.setdefault(SECOND, None)
        return cls(
            key=str(data["key"]),
            game_id=str(data["game_id"]),
            engine=restore_engine(dict(data["engine"])),
            players=players,
            status=str(data.get("status", "waiting")),
            created_at=float(data.get("created_at", time.time())),
            last_action_at=float(data.get("last_action_at", time.time())),
            pending=data.get("pending"),
            side_choices={
                str(user_id): str(side)
                for user_id, side in dict(data.get("side_choices", {})).items()
            },
            clock=dict(data["clock"]) if data.get("clock") else None,
            clock_disabled=bool(data.get("clock_disabled", False)),
        )


class SessionStore:
    """In-memory sessions with one lock per conversation."""

    def __init__(self):
        self.sessions: dict[str, GameSession] = {}
        self.locks: dict[str, asyncio.Lock] = {}

    def get(self, key: str) -> GameSession | None:
        return self.sessions.get(key)

    def lock(self, key: str) -> asyncio.Lock:
        return self.locks.setdefault(key, asyncio.Lock())

    def put(self, session: GameSession) -> None:
        self.sessions[session.key] = session

    def remove(self, key: str) -> GameSession | None:
        self.locks.pop(key, None)
        return self.sessions.pop(key, None)

    def to_dict(self) -> dict[str, Any]:
        return {key: session.to_dict() for key, session in self.sessions.items()}

    def restore(self, data: dict[str, Any]) -> list[str]:
        errors = []
        for key, value in data.items():
            try:
                session = GameSession.from_dict(dict(value))
                self.sessions[str(key)] = session
            except Exception as exc:  # noqa: BLE001 - one broken room must not block all restores
                errors.append(f"{key}: {exc}")
        return errors
