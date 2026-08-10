from __future__ import annotations

import math
import re
from typing import Any

import chess

from .base import FIRST, SECOND, Analysis, GameEngine, MoveOutcome, clean_move_text

_MOVE_RE = re.compile(
    r"^(?:O-O(?:-O)?|0-0(?:-0)?|[KQRBN]?[a-h]?[1-8]?x?[a-h][1-8](?:=[QRBN])?[+#]?|[a-h][1-8][a-h][1-8][qrbn]?)$",
    re.IGNORECASE,
)


class ChessEngine(GameEngine):
    game_id = "chess"
    display_name = "国际象棋"
    side_names = ("白方", "黑方")

    def __init__(self, moves: list[str] | None = None):
        self.board = chess.Board()
        self.moves: list[str] = []
        self.san_moves: list[str] = []
        self.last_move = None
        for uci in moves or []:
            move = chess.Move.from_uci(uci)
            if move not in self.board.legal_moves:
                raise ValueError(f"无法恢复非法国际象棋走法: {uci}")
            self.san_moves.append(self.board.san(move))
            self.board.push(move)
            self.moves.append(uci)
        self._refresh_last_move()

    @property
    def turn(self) -> str:
        return FIRST if self.board.turn == chess.WHITE else SECOND

    def _refresh_last_move(self) -> None:
        if not self.board.move_stack:
            self.last_move = None
            return
        move = self.board.peek()
        self.last_move = (
            (chess.square_file(move.from_square), chess.square_rank(move.from_square)),
            (chess.square_file(move.to_square), chess.square_rank(move.to_square)),
        )

    def move_candidate(self, text: str) -> bool:
        value, explicit = clean_move_text(text)
        value = re.sub(r"\s+", "", value)
        return bool(value) and (explicit or bool(_MOVE_RE.fullmatch(value)))

    def play(self, text: str) -> MoveOutcome:
        value, _ = clean_move_text(text)
        value = re.sub(r"\s+", "", value)
        if not value:
            return MoveOutcome(False, "请输入走法，例如 Nc3、b1c3 或 b1 c3。")

        move = None
        try:
            move = self.board.parse_uci(value.lower())
        except (ValueError, chess.InvalidMoveError, chess.IllegalMoveError):
            try:
                move = self.board.parse_san(value.replace("0", "O"))
            except chess.AmbiguousMoveError:
                return MoveOutcome(False, "走法有歧义，请补全来源，例如 Nbd2。")
            except (ValueError, chess.InvalidMoveError, chess.IllegalMoveError):
                return MoveOutcome(False, "走法不合法。支持 SAN（Nc3）或 UCI（b1c3）。")

        if move not in self.board.legal_moves:
            return MoveOutcome(False, "该走法会违反规则或无法解除将军。")

        san = self.board.san(move)
        origin = (
            chess.square_file(move.from_square),
            chess.square_rank(move.from_square),
        )
        target = (chess.square_file(move.to_square), chess.square_rank(move.to_square))
        self.board.push(move)
        self.moves.append(move.uci())
        self.san_moves.append(san)
        self.last_move = (origin, target)

        outcome = self.board.outcome(claim_draw=True)
        if not outcome:
            msg = "将军。" if self.board.is_check() else ""
            return MoveOutcome(True, msg, san, origin, target)
        if outcome.winner is None:
            return MoveOutcome(True, "和棋。", san, origin, target, True, None, True)
        winner = FIRST if outcome.winner == chess.WHITE else SECOND
        return MoveOutcome(True, "将死，棋局结束。", san, origin, target, True, winner)

    def undo(self, steps: int = 1) -> bool:
        if steps < 1 or len(self.moves) < steps:
            return False
        for _ in range(steps):
            self.board.pop()
            self.moves.pop()
            self.san_moves.pop()
        self._refresh_last_move()
        return True

    def notation_history(self) -> list[str]:
        return list(self.san_moves)

    def to_dict(self) -> dict[str, Any]:
        return {"game_id": self.game_id, "moves": list(self.moves)}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ChessEngine:
        return cls(list(data.get("moves", [])))

    @staticmethod
    def _score(board: chess.Board) -> float:
        if board.is_checkmate():
            return -100000.0 if board.turn == chess.WHITE else 100000.0
        values = {
            chess.PAWN: 100,
            chess.KNIGHT: 320,
            chess.BISHOP: 330,
            chess.ROOK: 500,
            chess.QUEEN: 900,
        }
        score = 0.0
        for piece_type, value in values.items():
            score += len(board.pieces(piece_type, chess.WHITE)) * value
            score -= len(board.pieces(piece_type, chess.BLACK)) * value
        mobility = board.legal_moves.count()
        score += mobility * (2 if board.turn == chess.WHITE else -2)
        if board.is_check():
            score += -35 if board.turn == chess.WHITE else 35
        return score

    def analyze(self) -> Analysis:
        current_score = self._score(self.board)
        best_move = None
        best_value = -math.inf if self.turn == FIRST else math.inf
        for move in self.board.legal_moves:
            san = self.board.san(move)
            self.board.push(move)
            value = self._score(self.board)
            self.board.pop()
            if (self.turn == FIRST and value > best_value) or (
                self.turn == SECOND and value < best_value
            ):
                best_value = value
                best_move = san

        first_rate = 100.0 / (1.0 + math.exp(-current_score / 420.0))
        material = round(current_score / 100.0, 1)
        lead = (
            "白方" if material > 0.2 else "黑方" if material < -0.2 else "局面接近均衡"
        )
        summary = f"轻量评估：{lead}；白方估计胜率 {first_rate:.0f}%（启发式，不等同于引擎分）。"
        return Analysis(summary, best_move, first_rate)
