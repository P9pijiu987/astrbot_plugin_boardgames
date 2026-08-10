from __future__ import annotations

# ruff: noqa: E402

import sys
import unittest
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN_ROOT))

from boardgames.base import FIRST, SECOND
from boardgames.chess_engine import ChessEngine
from boardgames.go_engine import GoEngine
from boardgames.grid_games import (
    GomokuEngine,
    ReversiEngine,
    TicTacToeEngine,
)
from boardgames.registry import create_engine, restore_engine
from boardgames.render import BoardRenderer
from boardgames.session import GameSession, Player
from boardgames.xiangqi_engine import XiangqiEngine


class ChessInputTests(unittest.TestCase):
    def test_move_candidate_does_not_capture_other_commands(self):
        engine = ChessEngine()
        self.assertTrue(engine.move_candidate("/Nc3"))
        self.assertTrue(engine.move_candidate("下棋 Nc3"))
        self.assertFalse(engine.move_candidate("/认输"))
        self.assertFalse(engine.move_candidate("今天天气不错"))

    def test_san_with_slash(self):
        engine = ChessEngine()
        outcome = engine.play("/Nc3")
        self.assertTrue(outcome.ok)
        self.assertEqual(engine.moves, ["b1c3"])

    def test_uci_compact(self):
        engine = ChessEngine()
        self.assertTrue(engine.play("/b1c3").ok)

    def test_uci_with_space(self):
        engine = ChessEngine()
        self.assertTrue(engine.play("/b1 c3").ok)
        self.assertEqual(engine.last_move, ((1, 0), (2, 2)))


class RulesTests(unittest.TestCase):
    def test_go_defaults_to_standard_19_lines(self):
        engine = create_engine("go")
        self.assertEqual(engine.size, 19)

    def test_xiangqi_chinese_and_coordinate_notation(self):
        chinese = XiangqiEngine()
        result = chinese.play("炮二平五")
        self.assertTrue(result.ok, result.message)
        self.assertEqual(result.notation, "炮二平五")

        coordinate = XiangqiEngine()
        self.assertTrue(coordinate.play("h2 e2").ok)

    def test_go_capture(self):
        engine = GoEngine(9)
        for move in ["A2", "B2", "B1", "pass", "C2", "pass", "B3"]:
            result = engine.play(move)
            self.assertTrue(result.ok, f"{move}: {result.message}")
        self.assertEqual(engine.board[1][1], 0)

    def test_go_two_passes_end_game(self):
        engine = GoEngine(9)
        self.assertTrue(engine.play("pass").ok)
        result = engine.play("pass")
        self.assertTrue(result.ended)

    def test_gomoku_win(self):
        engine = GomokuEngine(15)
        result = None
        for move in ["A1", "A2", "B1", "B2", "C1", "C2", "D1", "D2", "E1"]:
            result = engine.play(move)
            self.assertTrue(result.ok, result.message)
        self.assertTrue(result.ended)
        self.assertEqual(result.winner, FIRST)

    def test_gomoku_standard_and_renju_overline(self):
        standard = GomokuEngine(15, rule="standard")
        for x in range(5):
            standard.board[0][x] = 1
        result = standard.play("F1")
        self.assertTrue(result.ok)
        self.assertFalse(result.ended)

        renju = GomokuEngine(15, rule="renju")
        for x in range(5):
            renju.board[0][x] = 1
        result = renju.play("F1")
        self.assertFalse(result.ok)
        self.assertIn("长连禁手", result.message)

    def test_gomoku_renju_double_three(self):
        engine = GomokuEngine(15, rule="renju")
        # H8 落子会同时形成横向与纵向的活三。
        for coord in ("G8", "I8", "H7", "H9"):
            x = ord(coord[0]) - ord("A")
            y = int(coord[1:]) - 1
            engine.board[y][x] = 1
        result = engine.play("H8")
        self.assertFalse(result.ok)
        self.assertIn("三三禁手", result.message)

    def test_gomoku_renju_double_four(self):
        engine = GomokuEngine(15, rule="renju")
        for coord in ("G8", "I8", "J8", "H7", "H9", "H10"):
            x = ord(coord[0]) - ord("A")
            y = int(coord[1:]) - 1
            engine.board[y][x] = 1
        result = engine.play("H8")
        self.assertFalse(result.ok)
        self.assertIn("四四禁手", result.message)

    def test_gomoku_renju_exact_five_takes_priority(self):
        engine = GomokuEngine(15, rule="renju")
        for coord in ("A1", "B1", "C1", "D1", "E1", "F2", "F3", "F4", "F5"):
            x = ord(coord[0]) - ord("A")
            y = int(coord[1:]) - 1
            engine.board[y][x] = 1
        result = engine.play("F1")
        self.assertTrue(result.ok)
        self.assertTrue(result.ended)
        self.assertEqual(result.winner, FIRST)

    def test_gomoku_swap2_flow_and_round_trip(self):
        engine = GomokuEngine(15, rule="freestyle", opening="swap2")
        for move in ("H8", "I8", "H9"):
            self.assertTrue(engine.play(move).ok)
        self.assertEqual(engine.opening_phase, "choice")
        self.assertEqual(engine.turn, SECOND)
        self.assertTrue(engine.choose_opening("add_two").ok)
        self.assertTrue(engine.play("G8").ok)
        self.assertTrue(engine.play("G9").ok)
        self.assertEqual(engine.opening_phase, "final_choice")
        choice = engine.choose_opening("white")
        self.assertTrue(choice.ok)
        self.assertTrue(choice.extra["swap_players"])
        self.assertEqual(engine.opening_phase, "normal")
        self.assertEqual(engine.turn, SECOND)
        restored = restore_engine(engine.to_dict())
        self.assertEqual(restored.to_dict(), engine.to_dict())

    def test_go_influence_and_analysis(self):
        engine = GoEngine(9)
        for move in ("C3", "G7", "D3", "F7"):
            self.assertTrue(engine.play(move).ok)
        influence = engine.influence_map()
        self.assertEqual(len(influence), 9)
        self.assertEqual(len(influence[0]), 9)
        self.assertGreater(influence[2][2], 0)
        self.assertLess(influence[6][6], 0)
        analysis = engine.analyze()
        self.assertIsNotNone(analysis.first_win_rate)
        self.assertIsNotNone(analysis.recommended)

    def test_tictactoe_win_and_minimax(self):
        engine = TicTacToeEngine()
        for move in ["1", "4", "2", "5"]:
            self.assertTrue(engine.play(move).ok)
        analysis = engine.analyze()
        self.assertEqual(analysis.recommended, "C1")
        result = engine.play("3")
        self.assertTrue(result.ended)
        self.assertEqual(result.winner, FIRST)

    def test_reversi_flip(self):
        engine = ReversiEngine()
        result = engine.play("D3")
        self.assertTrue(result.ok, result.message)
        self.assertEqual(engine.board[3][3], 1)
        self.assertEqual(engine.turn, SECOND)

    def test_engine_round_trip(self):
        engine = GomokuEngine(13)
        engine.play("G7")
        engine.play("H7")
        restored = restore_engine(engine.to_dict())
        self.assertEqual(restored.to_dict(), engine.to_dict())


class RenderingTests(unittest.TestCase):
    def test_all_boards_render_as_png(self):
        renderer = BoardRenderer(PLUGIN_ROOT / "assets")
        engines = [
            ChessEngine(),
            GoEngine(9),
            XiangqiEngine(),
            GomokuEngine(13),
            TicTacToeEngine(),
            ReversiEngine(),
        ]
        first_moves = {
            "chess": "e4",
            "go": "D4",
            "xiangqi": "h2e2",
            "gomoku": "G7",
            "tictactoe": "5",
            "reversi": "D3",
        }
        for engine in engines:
            with self.subTest(engine=engine.game_id):
                outcome = engine.play(first_moves[engine.game_id])
                self.assertTrue(outcome.ok, outcome.message)
                self.assertEqual(engine.turn, SECOND)
                session = GameSession(
                    "test",
                    engine.game_id,
                    engine,
                    {
                        FIRST: Player("1", "甲"),
                        SECOND: Player("2", "乙"),
                    },
                    status="playing",
                )
                data = renderer.render(session)
                self.assertTrue(data.startswith(b"\x89PNG\r\n\x1a\n"))
                self.assertGreater(len(data), 1000)
                if isinstance(engine, GoEngine):
                    influence = renderer.render_go_influence(session)
                    self.assertTrue(influence.startswith(b"\x89PNG\r\n\x1a\n"))


if __name__ == "__main__":
    unittest.main()
