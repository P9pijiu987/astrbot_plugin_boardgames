from __future__ import annotations

import math
import re
from collections import deque
from typing import Any

from .base import (
    FIRST,
    SECOND,
    Analysis,
    GameEngine,
    MoveOutcome,
    clean_move_text,
    opponent,
)

_GO_LETTERS = "ABCDEFGHJKLMNOPQRST"


class GoEngine(GameEngine):
    game_id = "go"
    display_name = "围棋"
    side_names = ("黑方", "白方")

    def __init__(
        self, size: int = 19, moves: list[str] | None = None, komi: float = 6.5
    ):
        if size not in {9, 13, 19}:
            raise ValueError("围棋仅支持 9、13 或 19 路")
        self.size = size
        self.komi = float(komi)
        self.board = [[0 for _ in range(size)] for _ in range(size)]
        self.moves: list[str] = []
        self.turn = FIRST
        self.consecutive_passes = 0
        self.last_move = None
        self._positions = [self._hash_board(self.board)]
        for move in moves or []:
            result = self.play(move)
            if not result.ok:
                raise ValueError(f"无法恢复围棋走法: {move}")

    @staticmethod
    def _hash_board(board: list[list[int]]) -> str:
        return "".join(str(value) for row in board for value in row)

    def _neighbors(self, x: int, y: int):
        for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            nx, ny = x + dx, y + dy
            if 0 <= nx < self.size and 0 <= ny < self.size:
                yield nx, ny

    def _group_and_liberties(
        self, board: list[list[int]], x: int, y: int
    ) -> tuple[set[tuple[int, int]], set[tuple[int, int]]]:
        stone = board[y][x]
        group = {(x, y)}
        liberties: set[tuple[int, int]] = set()
        queue = deque([(x, y)])
        while queue:
            px, py = queue.popleft()
            for nx, ny in self._neighbors(px, py):
                value = board[ny][nx]
                if value == 0:
                    liberties.add((nx, ny))
                elif value == stone and (nx, ny) not in group:
                    group.add((nx, ny))
                    queue.append((nx, ny))
        return group, liberties

    def _try_play(
        self, x: int, y: int, stone: int
    ) -> tuple[list[list[int]] | None, list[tuple[int, int]], str]:
        if self.board[y][x]:
            return None, [], "该交叉点已有棋子。"
        board = [row[:] for row in self.board]
        board[y][x] = stone
        captured: list[tuple[int, int]] = []
        for nx, ny in self._neighbors(x, y):
            if board[ny][nx] != 3 - stone:
                continue
            group, liberties = self._group_and_liberties(board, nx, ny)
            if not liberties:
                for gx, gy in group:
                    board[gy][gx] = 0
                    captured.append((gx, gy))
        _, liberties = self._group_and_liberties(board, x, y)
        if not liberties:
            return None, [], "禁入点：该手会导致己方棋块无气。"
        board_hash = self._hash_board(board)
        if board_hash in self._positions:
            return None, [], "打劫/全局同形：不能立即还原到之前出现过的局面。"
        return board, captured, ""

    def _parse_coord(self, value: str) -> tuple[int, int] | None:
        match = re.fullmatch(r"([A-HJ-Ta-hj-t])(\d{1,2})", value.strip())
        if not match:
            return None
        letter = match.group(1).upper()
        if letter not in _GO_LETTERS[: self.size]:
            return None
        x = _GO_LETTERS.index(letter)
        y = int(match.group(2)) - 1
        if 0 <= y < self.size:
            return x, y
        return None

    def _coord_text(self, x: int, y: int) -> str:
        return f"{_GO_LETTERS[x]}{y + 1}"

    def move_candidate(self, text: str) -> bool:
        value, explicit = clean_move_text(text)
        return (
            explicit
            or value.lower() in {"pass", "p"}
            or value in {"停一手", "虚手"}
            or bool(re.fullmatch(r"[A-HJ-Ta-hj-t]\d{1,2}", value))
        )

    def _score(self) -> tuple[float, float]:
        black = sum(row.count(1) for row in self.board)
        white = sum(row.count(2) for row in self.board) + self.komi
        seen: set[tuple[int, int]] = set()
        for y in range(self.size):
            for x in range(self.size):
                if self.board[y][x] or (x, y) in seen:
                    continue
                region = {(x, y)}
                borders: set[int] = set()
                queue = deque([(x, y)])
                seen.add((x, y))
                while queue:
                    px, py = queue.popleft()
                    for nx, ny in self._neighbors(px, py):
                        value = self.board[ny][nx]
                        if value:
                            borders.add(value)
                        elif (nx, ny) not in seen:
                            seen.add((nx, ny))
                            region.add((nx, ny))
                            queue.append((nx, ny))
                if borders == {1}:
                    black += len(region)
                elif borders == {2}:
                    white += len(region)
        return float(black), float(white)

    def influence_map(self) -> list[list[float]]:
        """Estimate local influence from -1 (white) to +1 (black).

        This is deliberately lightweight: stones radiate distance-decaying influence.
        It is useful as a visual aid, but it does not attempt professional life-and-death
        reading like KataGo.
        """
        stones = [
            (x, y, 1.0 if self.board[y][x] == 1 else -1.0)
            for y in range(self.size)
            for x in range(self.size)
            if self.board[y][x]
        ]
        result: list[list[float]] = []
        scale = 3.0 if self.size == 19 else 2.4 if self.size == 13 else 1.9
        for y in range(self.size):
            row = []
            for x in range(self.size):
                if self.board[y][x] == 1:
                    row.append(1.0)
                    continue
                if self.board[y][x] == 2:
                    row.append(-1.0)
                    continue
                total = 0.0
                for sx, sy, sign in stones:
                    distance = abs(x - sx) + abs(y - sy)
                    total += sign * math.exp(-distance / scale)
                row.append(math.tanh(total / 1.8))
            result.append(row)
        return result

    def estimated_score(self) -> tuple[float, float]:
        """Estimate area from the influence field for mid-game analysis."""
        influence = self.influence_map()
        black = 0.0
        white = self.komi
        for y in range(self.size):
            for x in range(self.size):
                stone = self.board[y][x]
                if stone == 1:
                    black += 1.0
                elif stone == 2:
                    white += 1.0
                else:
                    value = influence[y][x]
                    if value > 0.16:
                        black += min(1.0, value)
                    elif value < -0.16:
                        white += min(1.0, -value)
        return black, white

    def play(self, text: str) -> MoveOutcome:
        value, _ = clean_move_text(text)
        if value.lower() in {"pass", "p"} or value in {"停一手", "虚手"}:
            self.moves.append("pass")
            self.consecutive_passes += 1
            self.last_move = None
            self._positions.append(self._hash_board(self.board))
            notation = "停一手"
            if self.consecutive_passes >= 2:
                black, white = self._score()
                if black == white:
                    return MoveOutcome(
                        True,
                        f"双方连续停一手。数子结果 黑 {black:g} : 白 {white:g}，和棋。",
                        notation,
                        ended=True,
                        draw=True,
                    )
                winner = FIRST if black > white else SECOND
                return MoveOutcome(
                    True,
                    f"双方连续停一手。数子结果 黑 {black:g} : 白 {white:g}。",
                    notation,
                    ended=True,
                    winner=winner,
                )
            self.turn = opponent(self.turn)
            return MoveOutcome(True, "停一手。", notation)

        coord = self._parse_coord(value.replace(" ", ""))
        if coord is None:
            last_letter = _GO_LETTERS[self.size - 1]
            return MoveOutcome(
                False,
                f"请输入 A1～{last_letter}{self.size}（坐标跳过 I），或输入 pass。",
            )
        x, y = coord
        stone = 1 if self.turn == FIRST else 2
        board, captured, error = self._try_play(x, y, stone)
        if board is None:
            return MoveOutcome(False, error)
        self.board = board
        notation = self._coord_text(x, y)
        self.moves.append(notation)
        self.consecutive_passes = 0
        self.last_move = (None, (x, y))
        self._positions.append(self._hash_board(self.board))
        self.turn = opponent(self.turn)
        message = f"提掉 {len(captured)} 子。" if captured else ""
        return MoveOutcome(
            True, message, notation, None, (x, y), extra={"captured": captured}
        )

    def undo(self, steps: int = 1) -> bool:
        if steps < 1 or len(self.moves) < steps:
            return False
        self.__init__(self.size, self.moves[:-steps], self.komi)
        return True

    def notation_history(self) -> list[str]:
        return list(self.moves)

    def to_dict(self) -> dict[str, Any]:
        return {
            "game_id": self.game_id,
            "size": self.size,
            "komi": self.komi,
            "moves": list(self.moves),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> GoEngine:
        return cls(
            int(data.get("size", 19)),
            list(data.get("moves", [])),
            float(data.get("komi", 6.5)),
        )

    def analyze(self) -> Analysis:
        stone = 1 if self.turn == FIRST else 2
        occupied = [
            (x, y)
            for y in range(self.size)
            for x in range(self.size)
            if self.board[y][x]
        ]
        candidates: set[tuple[int, int]] = set()
        if not occupied:
            hoshi = 3 if self.size >= 13 else 2
            candidates = {
                (hoshi, hoshi),
                (self.size - 1 - hoshi, self.size - 1 - hoshi),
            }
        else:
            for x, y in occupied:
                for dx in (-2, -1, 0, 1, 2):
                    for dy in (-2, -1, 0, 1, 2):
                        nx, ny = x + dx, y + dy
                        if (
                            0 <= nx < self.size
                            and 0 <= ny < self.size
                            and not self.board[ny][nx]
                        ):
                            candidates.add((nx, ny))
        best = None
        best_score = -(10**9)
        center = (self.size - 1) / 2
        for x, y in candidates:
            board, captured, _ = self._try_play(x, y, stone)
            if board is None:
                continue
            _, liberties = self._group_and_liberties(board, x, y)
            score = (
                len(captured) * 20
                + len(liberties) * 2
                - (abs(x - center) + abs(y - center)) * 0.1
            )
            if score > best_score:
                best_score, best = score, (x, y)
        black, white = self.estimated_score()
        first_rate = 100.0 / (1.0 + math.exp(-(black - white) / 7.5))
        return Analysis(
            f"轻量势力估算：黑约 {black:.1f}、白约 {white:.1f}（含贴目；死活未判定）。",
            self._coord_text(*best) if best else "pass",
            first_rate,
        )
