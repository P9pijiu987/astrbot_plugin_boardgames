from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

FIRST = "first"
SECOND = "second"


def opponent(side: str) -> str:
    return SECOND if side == FIRST else FIRST


@dataclass(slots=True)
class MoveOutcome:
    ok: bool
    message: str = ""
    notation: str = ""
    origin: tuple[int, int] | None = None
    target: tuple[int, int] | None = None
    ended: bool = False
    winner: str | None = None
    draw: bool = False
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class Analysis:
    summary: str
    recommended: str | None = None
    first_win_rate: float | None = None


class GameEngine(ABC):
    game_id: str
    display_name: str
    side_names: tuple[str, str]
    turn: str
    last_move: tuple[tuple[int, int] | None, tuple[int, int] | None] | None

    @abstractmethod
    def play(self, text: str) -> MoveOutcome:
        raise NotImplementedError

    @abstractmethod
    def undo(self, steps: int = 1) -> bool:
        raise NotImplementedError

    @abstractmethod
    def to_dict(self) -> dict[str, Any]:
        raise NotImplementedError

    @classmethod
    @abstractmethod
    def from_dict(cls, data: dict[str, Any]) -> GameEngine:
        raise NotImplementedError

    @abstractmethod
    def move_candidate(self, text: str) -> bool:
        """Return whether bare text looks like a move for this game."""
        raise NotImplementedError

    @abstractmethod
    def notation_history(self) -> list[str]:
        raise NotImplementedError

    def analyze(self) -> Analysis:
        return Analysis("这个棋种暂未提供自动分析。")


def clean_move_text(text: str) -> tuple[str, bool]:
    """Strip the optional slash/下棋 prefix and report whether it was explicit."""
    raw = text.strip()
    explicit = raw.startswith("下棋")
    if raw.startswith("/"):
        raw = raw[1:].lstrip()
    if raw.startswith("下棋"):
        raw = raw[2:].lstrip()
        explicit = True
    return raw.strip(), explicit


def coord_to_text(x: int, y: int) -> str:
    return f"{chr(ord('A') + x)}{y + 1}"


def parse_letter_coord(text: str, size: int) -> tuple[int, int] | None:
    value = text.strip().upper().replace("，", ",")
    if len(value) < 2 or not value[0].isalpha() or not value[1:].isdigit():
        return None
    x = ord(value[0]) - ord("A")
    y = int(value[1:]) - 1
    if 0 <= x < size and 0 <= y < size:
        return x, y
    return None


def count_direction(
    board: list[list[int]], x: int, y: int, dx: int, dy: int, value: int
) -> int:
    total = 0
    x += dx
    y += dy
    while 0 <= y < len(board) and 0 <= x < len(board[y]) and board[y][x] == value:
        total += 1
        x += dx
        y += dy
    return total


def stable_sorted(items: Iterable[str]) -> list[str]:
    return sorted(items, key=lambda item: (len(item), item))
