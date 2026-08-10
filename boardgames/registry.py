from __future__ import annotations

from typing import Any

from .base import GameEngine
from .chess_engine import ChessEngine
from .go_engine import GoEngine
from .grid_games import GomokuEngine, ReversiEngine, TicTacToeEngine
from .xiangqi_engine import XiangqiEngine

GAME_INFO = {
    "chess": {"name": "国际象棋", "sides": ChessEngine.side_names},
    "go": {"name": "围棋", "sides": GoEngine.side_names},
    "xiangqi": {"name": "中国象棋", "sides": XiangqiEngine.side_names},
    "gomoku": {"name": "五子棋", "sides": GomokuEngine.side_names},
    "tictactoe": {"name": "井字棋", "sides": TicTacToeEngine.side_names},
    "reversi": {"name": "黑白棋", "sides": ReversiEngine.side_names},
}

GAME_ALIASES = {
    "国际象棋": "chess",
    "西洋棋": "chess",
    "chess": "chess",
    "围棋": "go",
    "go": "go",
    "中国象棋": "xiangqi",
    "象棋": "xiangqi",
    "xiangqi": "xiangqi",
    "五子棋": "gomoku",
    "五子": "gomoku",
    "gomoku": "gomoku",
    "井字棋": "tictactoe",
    "井字": "tictactoe",
    "ttt": "tictactoe",
    "黑白棋": "reversi",
    "奥赛罗": "reversi",
    "reversi": "reversi",
    "othello": "reversi",
}

_CLASSES: dict[str, type[GameEngine]] = {
    "chess": ChessEngine,
    "go": GoEngine,
    "xiangqi": XiangqiEngine,
    "gomoku": GomokuEngine,
    "tictactoe": TicTacToeEngine,
    "reversi": ReversiEngine,
}


def normalize_game(value: str) -> str | None:
    return GAME_ALIASES.get(value.strip().lower()) or GAME_ALIASES.get(value.strip())


def create_engine(
    game_id: str,
    size: int | None = None,
    *,
    go_komi: float = 6.5,
    gomoku_rule: str = "freestyle",
    gomoku_opening: str = "normal",
) -> GameEngine:
    if game_id == "go":
        return GoEngine(size or 19, komi=go_komi)
    if game_id == "gomoku":
        return GomokuEngine(
            size or 15,
            rule=gomoku_rule,
            opening=gomoku_opening,
        )
    if size is not None:
        raise ValueError(
            f"{GAME_INFO.get(game_id, {'name': game_id})['name']}不支持自定义路数"
        )
    if game_id not in _CLASSES:
        raise ValueError(f"未知棋种: {game_id}")
    return _CLASSES[game_id]()


def restore_engine(data: dict[str, Any]) -> GameEngine:
    game_id = str(data.get("game_id", ""))
    if game_id not in _CLASSES:
        raise ValueError(f"无法恢复未知棋种: {game_id}")
    return _CLASSES[game_id].from_dict(data)
