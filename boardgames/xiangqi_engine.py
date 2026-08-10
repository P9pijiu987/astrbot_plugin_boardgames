from __future__ import annotations

import math
import re
from collections.abc import Iterable
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

_CN_NUM = "零一二三四五六七八九"
_PIECE_NAMES = {
    "K": "帅",
    "A": "仕",
    "B": "相",
    "N": "马",
    "R": "车",
    "C": "炮",
    "P": "兵",
    "k": "将",
    "a": "士",
    "b": "象",
    "n": "马",
    "r": "车",
    "c": "炮",
    "p": "卒",
}


class XiangqiEngine(GameEngine):
    game_id = "xiangqi"
    display_name = "中国象棋"
    side_names = ("红方", "黑方")

    def __init__(self, moves: list[str] | None = None):
        self.board = [["" for _ in range(9)] for _ in range(10)]
        self.board[0] = list("RNBAKABNR")
        self.board[2][1] = self.board[2][7] = "C"
        for x in range(0, 9, 2):
            self.board[3][x] = "P"
        self.board[9] = list("rnbakabnr")
        self.board[7][1] = self.board[7][7] = "c"
        for x in range(0, 9, 2):
            self.board[6][x] = "p"
        self.moves: list[str] = []
        self.cn_moves: list[str] = []
        self.turn = FIRST
        self.last_move = None
        for move in moves or []:
            result = self.play(move)
            if not result.ok:
                raise ValueError(f"无法恢复中国象棋走法: {move}")

    @staticmethod
    def _side_of(piece: str) -> str | None:
        if not piece:
            return None
        return FIRST if piece.isupper() else SECOND

    @staticmethod
    def _in_palace(side: str, x: int, y: int) -> bool:
        return 3 <= x <= 5 and ((0 <= y <= 2) if side == FIRST else (7 <= y <= 9))

    def _clear_between(self, x1: int, y1: int, x2: int, y2: int) -> int:
        if x1 != x2 and y1 != y2:
            return -1
        count = 0
        if x1 == x2:
            for y in range(min(y1, y2) + 1, max(y1, y2)):
                count += bool(self.board[y][x1])
        else:
            for x in range(min(x1, x2) + 1, max(x1, x2)):
                count += bool(self.board[y1][x])
        return count

    def _piece_can_move(
        self, x1: int, y1: int, x2: int, y2: int, attacks: bool = False
    ) -> bool:
        piece = self.board[y1][x1]
        target = self.board[y2][x2]
        side = self._side_of(piece)
        if not piece or (target and self._side_of(target) == side):
            return False
        dx, dy = x2 - x1, y2 - y1
        kind = piece.upper()
        if kind == "K":
            if abs(dx) + abs(dy) == 1 and self._in_palace(side, x2, y2):
                return True
            return (
                x1 == x2
                and target.upper() == "K"
                and self._clear_between(x1, y1, x2, y2) == 0
            )
        if kind == "A":
            return abs(dx) == abs(dy) == 1 and self._in_palace(side, x2, y2)
        if kind == "B":
            if abs(dx) != 2 or abs(dy) != 2:
                return False
            if side == FIRST and y2 > 4:
                return False
            if side == SECOND and y2 < 5:
                return False
            return not self.board[y1 + dy // 2][x1 + dx // 2]
        if kind == "N":
            if sorted((abs(dx), abs(dy))) != [1, 2]:
                return False
            leg_x = x1 + (dx // 2 if abs(dx) == 2 else 0)
            leg_y = y1 + (dy // 2 if abs(dy) == 2 else 0)
            return not self.board[leg_y][leg_x]
        if kind == "R":
            return self._clear_between(x1, y1, x2, y2) == 0
        if kind == "C":
            between = self._clear_between(x1, y1, x2, y2)
            return between == (1 if target else 0)
        if kind == "P":
            forward = 1 if side == FIRST else -1
            crossed = y1 >= 5 if side == FIRST else y1 <= 4
            return (dx == 0 and dy == forward) or (crossed and dy == 0 and abs(dx) == 1)
        return False

    def _pseudo_moves(self, side: str) -> Iterable[tuple[int, int, int, int]]:
        for y1 in range(10):
            for x1 in range(9):
                if self._side_of(self.board[y1][x1]) != side:
                    continue
                for y2 in range(10):
                    for x2 in range(9):
                        if self._piece_can_move(x1, y1, x2, y2):
                            yield x1, y1, x2, y2

    def _king_pos(self, side: str) -> tuple[int, int] | None:
        king = "K" if side == FIRST else "k"
        for y in range(10):
            for x in range(9):
                if self.board[y][x] == king:
                    return x, y
        return None

    def _in_check(self, side: str) -> bool:
        king = self._king_pos(side)
        if king is None:
            return True
        kx, ky = king
        return any(
            (x2, y2) == (kx, ky) for _, _, x2, y2 in self._pseudo_moves(opponent(side))
        )

    def _legal_moves(self, side: str) -> list[tuple[int, int, int, int]]:
        legal = []
        for move in list(self._pseudo_moves(side)):
            x1, y1, x2, y2 = move
            piece, target = self.board[y1][x1], self.board[y2][x2]
            self.board[y2][x2], self.board[y1][x1] = piece, ""
            safe = not self._in_check(side)
            self.board[y1][x1], self.board[y2][x2] = piece, target
            if safe:
                legal.append(move)
        return legal

    @staticmethod
    def _coord_text(move: tuple[int, int, int, int]) -> str:
        x1, y1, x2, y2 = move
        return f"{chr(97 + x1)}{y1}{chr(97 + x2)}{y2}"

    @staticmethod
    def _file_number(side: str, x: int) -> int:
        return 9 - x if side == FIRST else x + 1

    def _cn_notation(self, move: tuple[int, int, int, int]) -> str:
        x1, y1, x2, y2 = move
        piece = self.board[y1][x1]
        side = self._side_of(piece) or FIRST
        name = _PIECE_NAMES[piece]
        source = self._file_number(side, x1)
        if y1 == y2:
            action = "平"
            destination = self._file_number(side, x2)
        else:
            forward = y2 > y1 if side == FIRST else y2 < y1
            action = "进" if forward else "退"
            if piece.upper() in {"N", "B", "A"}:
                destination = self._file_number(side, x2)
            else:
                destination = abs(y2 - y1)
        return f"{name}{_CN_NUM[source]}{action}{_CN_NUM[destination]}"

    @staticmethod
    def _normalize_cn(value: str) -> str:
        table = str.maketrans(
            "１２３４５６７８９123456789車馬砲帥將",
            "一二三四五六七八九一二三四五六七八九车马炮帅将",
        )
        return value.translate(table).replace(" ", "")

    def _parse_move(self, value: str) -> tuple[tuple[int, int, int, int] | None, str]:
        legal = self._legal_moves(self.turn)
        compact = value.lower().replace(" ", "").replace("-", "")
        if re.fullmatch(r"[a-i][0-9][a-i][0-9]", compact):
            move = (
                ord(compact[0]) - 97,
                int(compact[1]),
                ord(compact[2]) - 97,
                int(compact[3]),
            )
            return (move, "") if move in legal else (None, "该坐标走法不合法或会送将。")
        normalized = self._normalize_cn(value)
        matches = [
            move
            for move in legal
            if self._normalize_cn(self._cn_notation(move)) == normalized
        ]
        if len(matches) == 1:
            return matches[0], ""
        if len(matches) > 1:
            return None, "该中文记谱有歧义，请改用坐标记谱。"
        return None, "无法识别或不合法。支持炮二平五、h2e2、h2 e2。"

    def move_candidate(self, text: str) -> bool:
        value, explicit = clean_move_text(text)
        compact = value.replace(" ", "")
        return (
            explicit
            or bool(re.fullmatch(r"[a-iA-I][0-9][a-iA-I][0-9]", compact))
            or bool(
                re.fullmatch(
                    r"[帅将仕士相象马車车炮砲兵卒][一二三四五六七八九1-9][进退平][一二三四五六七八九1-9]",
                    compact,
                )
            )
        )

    def play(self, text: str) -> MoveOutcome:
        value, _ = clean_move_text(text)
        move, error = self._parse_move(value)
        if move is None:
            return MoveOutcome(False, error)
        x1, y1, x2, y2 = move
        cn = self._cn_notation(move)
        captured = self.board[y2][x2]
        self.board[y2][x2], self.board[y1][x1] = self.board[y1][x1], ""
        coordinate = self._coord_text(move)
        self.moves.append(coordinate)
        self.cn_moves.append(cn)
        self.last_move = ((x1, y1), (x2, y2))
        mover = self.turn
        next_side = opponent(mover)
        if captured.upper() == "K" or not self._legal_moves(next_side):
            message = (
                "将死，棋局结束。" if self._in_check(next_side) else "困毙，棋局结束。"
            )
            return MoveOutcome(True, message, cn, (x1, y1), (x2, y2), True, mover)
        self.turn = next_side
        message = "将军。" if self._in_check(next_side) else ""
        return MoveOutcome(True, message, cn, (x1, y1), (x2, y2))

    def undo(self, steps: int = 1) -> bool:
        if steps < 1 or len(self.moves) < steps:
            return False
        self.__init__(self.moves[:-steps])
        return True

    def notation_history(self) -> list[str]:
        return list(self.cn_moves)

    def to_dict(self) -> dict[str, Any]:
        return {"game_id": self.game_id, "moves": list(self.moves)}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> XiangqiEngine:
        return cls(list(data.get("moves", [])))

    @staticmethod
    def _material(board: list[list[str]]) -> float:
        values = {
            "K": 10000,
            "R": 900,
            "C": 450,
            "N": 400,
            "B": 200,
            "A": 200,
            "P": 100,
        }
        score = 0.0
        for row in board:
            for piece in row:
                if piece:
                    score += values[piece.upper()] * (1 if piece.isupper() else -1)
        return score

    def analyze(self) -> Analysis:
        score = self._material(self.board)
        legal = self._legal_moves(self.turn)
        best_move = None
        best_value = -math.inf if self.turn == FIRST else math.inf
        for move in legal:
            x1, y1, x2, y2 = move
            piece, target = self.board[y1][x1], self.board[y2][x2]
            self.board[y2][x2], self.board[y1][x1] = piece, ""
            value = self._material(self.board)
            if self._in_check(opponent(self.turn)):
                value += 40 if self.turn == FIRST else -40
            self.board[y1][x1], self.board[y2][x2] = piece, target
            if (self.turn == FIRST and value > best_value) or (
                self.turn == SECOND and value < best_value
            ):
                best_value, best_move = value, move
        first_rate = 100.0 / (1.0 + math.exp(-score / 850.0))
        lead = "红方" if score > 50 else "黑方" if score < -50 else "局面接近均衡"
        return Analysis(
            f"轻量子力评估：{lead}；红方估计胜率 {first_rate:.0f}%（未进行深层搜索）。",
            self._cn_notation(best_move) if best_move else None,
            first_rate,
        )
