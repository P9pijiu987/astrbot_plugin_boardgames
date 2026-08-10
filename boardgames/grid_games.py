from __future__ import annotations

import re
from typing import Any

from .base import (
    FIRST,
    SECOND,
    Analysis,
    GameEngine,
    MoveOutcome,
    clean_move_text,
    coord_to_text,
    count_direction,
    opponent,
    parse_letter_coord,
)


class GomokuEngine(GameEngine):
    game_id = "gomoku"
    display_name = "五子棋"
    side_names = ("黑方", "白方")

    RULE_LABELS = {
        "freestyle": "自由规则（五子或以上）",
        "standard": "标准规则（恰好五子）",
        "renju": "连珠规则（黑方禁手）",
    }
    OPENING_LABELS = {"normal": "普通开局", "swap2": "Swap2 平衡开局"}

    def __init__(
        self,
        size: int = 15,
        moves: list[str] | None = None,
        *,
        rule: str = "freestyle",
        opening: str = "normal",
        events: list[dict[str, Any]] | None = None,
    ):
        if size not in {13, 15, 19}:
            raise ValueError("五子棋棋盘仅支持 13、15 或 19 路")
        if rule not in self.RULE_LABELS:
            raise ValueError("五子棋规则仅支持 freestyle、standard 或 renju")
        if opening not in self.OPENING_LABELS:
            raise ValueError("五子棋开局仅支持 normal 或 swap2")
        if rule == "renju" and size != 15:
            raise ValueError("连珠规则使用标准 15 路棋盘")
        self.size = size
        self.rule = rule
        self.opening = opening
        self.board = [[0 for _ in range(size)] for _ in range(size)]
        self.moves: list[str] = []
        self.events: list[dict[str, Any]] = []
        self.turn = FIRST
        self.last_move = None
        self.opening_phase = "place_three" if opening == "swap2" else "normal"
        self.opening_count = 0
        self.normal_move_start = 0
        if events is not None:
            for event in events:
                kind = str(event.get("kind", ""))
                if kind == "place":
                    result = self.play(str(event.get("coord", "")))
                elif kind == "choice":
                    result = self.choose_opening(str(event.get("choice", "")))
                else:
                    raise ValueError(f"未知五子棋历史事件: {event}")
                if not result.ok:
                    raise ValueError(f"无法恢复五子棋历史: {result.message}")
        else:
            # 兼容 2.0.0 及更早版本只保存 moves 的自由五子棋棋局。
            for move in moves or []:
                result = self.play(move)
                if not result.ok:
                    raise ValueError(f"无法恢复五子棋走法: {move}")

    def move_candidate(self, text: str) -> bool:
        value, explicit = clean_move_text(text)
        return explicit or bool(re.fullmatch(r"[A-Za-z]\d{1,2}", value))

    def _line_lengths(self, x: int, y: int, stone: int) -> list[int]:
        return [
            1
            + count_direction(self.board, x, y, dx, dy, stone)
            + count_direction(self.board, x, y, -dx, -dy, stone)
            for dx, dy in ((1, 0), (0, 1), (1, 1), (1, -1))
        ]

    def _is_win(self, x: int, y: int, stone: int) -> bool:
        lengths = self._line_lengths(x, y, stone)
        if self.rule == "freestyle":
            return any(length >= 5 for length in lengths)
        if self.rule == "standard":
            return any(length == 5 for length in lengths)
        if stone == 1:
            return any(length == 5 for length in lengths)
        return any(length >= 5 for length in lengths)

    def _four_structures(
        self, origin: tuple[int, int], stone: int = 1
    ) -> dict[frozenset[tuple[int, int]], set[tuple[int, int]]]:
        """Return fours containing origin and their exact-five completion points."""
        ox, oy = origin
        structures: dict[
            frozenset[tuple[int, int]], set[tuple[int, int]]
        ] = {}
        for dx, dy in ((1, 0), (0, 1), (1, 1), (1, -1)):
            for offset in range(-4, 5):
                x, y = ox + offset * dx, oy + offset * dy
                if not (0 <= x < self.size and 0 <= y < self.size):
                    continue
                if self.board[y][x]:
                    continue
                self.board[y][x] = stone
                neg = count_direction(self.board, x, y, -dx, -dy, stone)
                pos = count_direction(self.board, x, y, dx, dy, stone)
                length = 1 + neg + pos
                if length == 5:
                    start_x, start_y = x - neg * dx, y - neg * dy
                    row = frozenset(
                        (start_x + i * dx, start_y + i * dy) for i in range(5)
                    )
                    existing = frozenset(point for point in row if point != (x, y))
                    if origin in existing:
                        structures.setdefault(existing, set()).add((x, y))
                self.board[y][x] = 0
        return structures

    def _basic_forbidden_reason(self, x: int, y: int) -> str | None:
        lengths = self._line_lengths(x, y, 1)
        # RIF 规则中，黑方若同时形成正好五连，胜负优先于禁手判定。
        if any(length == 5 for length in lengths):
            return None
        if any(length > 5 for length in lengths):
            return "长连禁手"
        fours = self._four_structures((x, y))
        if len(fours) >= 2:
            return "四四禁手"
        return None

    def _forbidden_reason(self, x: int, y: int, depth: int = 0) -> str | None:
        basic = self._basic_forbidden_reason(x, y)
        if basic or any(length == 5 for length in self._line_lengths(x, y, 1)):
            return basic

        threes: set[frozenset[tuple[int, int]]] = set()
        for dx, dy in ((1, 0), (0, 1), (1, 1), (1, -1)):
            for offset in range(-4, 5):
                px, py = x + offset * dx, y + offset * dy
                if not (0 <= px < self.size and 0 <= py < self.size):
                    continue
                if self.board[py][px]:
                    continue
                self.board[py][px] = 1
                makes_five = any(
                    length == 5 for length in self._line_lengths(px, py, 1)
                )
                extension_reason = self._basic_forbidden_reason(px, py)
                # Two recursive layers cover practical false-three chains while keeping
                # chat-time validation predictable on a full 15×15 board.
                if extension_reason is None and not makes_five and depth < 2:
                    extension_reason = self._forbidden_reason(px, py, depth + 1)
                if extension_reason is None and not makes_five:
                    for structure, completions in self._four_structures((x, y)).items():
                        if (
                            len(completions) >= 2
                            and (x, y) in structure
                            and (px, py) in structure
                        ):
                            threes.add(
                                frozenset(point for point in structure if point != (px, py))
                            )
                self.board[py][px] = 0
        if len(threes) >= 2:
            return "三三禁手"
        return None

    @property
    def rule_label(self) -> str:
        label = self.RULE_LABELS[self.rule]
        if self.opening == "swap2":
            label += " · Swap2"
        return label

    @property
    def opening_prompt(self) -> str | None:
        return {
            "place_three": "开局方依次摆黑、白、黑三子",
            "choice": "后加入者请选择 /选白、/交换 或 /加两子",
            "place_two": "后加入者继续依次摆白、黑两子",
            "final_choice": "开局方请选择 /选黑 或 /选白",
        }.get(self.opening_phase)

    def play(self, text: str) -> MoveOutcome:
        value, _ = clean_move_text(text)
        if self.opening_phase in {"choice", "final_choice"}:
            return MoveOutcome(False, self.opening_prompt or "请先完成 Swap2 选色。")
        coord = parse_letter_coord(value.replace(" ", ""), self.size)
        if coord is None:
            return MoveOutcome(
                False, f"请输入 A1～{chr(64 + self.size)}{self.size} 的坐标，例如 H8。"
            )
        x, y = coord
        if self.board[y][x]:
            return MoveOutcome(False, "该交叉点已有棋子。")

        if self.opening_phase == "place_three":
            stone = (1, 2, 1)[self.opening_count]
        elif self.opening_phase == "place_two":
            stone = (2, 1)[self.opening_count]
        else:
            stone = 1 if self.turn == FIRST else 2
        self.board[y][x] = stone
        notation = coord_to_text(x, y)

        black_count = sum(row.count(1) for row in self.board)
        if self.rule == "renju" and stone == 1 and black_count >= 5:
            forbidden = self._forbidden_reason(x, y)
            if forbidden:
                self.board[y][x] = 0
                return MoveOutcome(False, f"黑方不能落在 {notation}：{forbidden}。")

        self.moves.append(notation)
        self.events.append({"kind": "place", "coord": notation})
        self.last_move = (None, (x, y))

        if self.opening_phase == "place_three":
            self.opening_count += 1
            if self.opening_count == 3:
                self.opening_count = 0
                self.opening_phase = "choice"
                self.turn = SECOND
            return MoveOutcome(True, self.opening_prompt or "", notation, target=(x, y))

        if self.opening_phase == "place_two":
            self.opening_count += 1
            if self.opening_count == 2:
                self.opening_count = 0
                self.opening_phase = "final_choice"
                self.turn = FIRST
            return MoveOutcome(True, self.opening_prompt or "", notation, target=(x, y))

        if self._is_win(x, y, stone):
            return MoveOutcome(
                True, "五子连珠，棋局结束。", notation, None, (x, y), True, self.turn
            )
        if len(self.moves) == self.size * self.size:
            return MoveOutcome(
                True, "棋盘已满，和棋。", notation, None, (x, y), True, None, True
            )
        self.turn = opponent(self.turn)
        return MoveOutcome(True, notation=notation, target=(x, y))

    def choose_opening(self, choice: str) -> MoveOutcome:
        value = choice.strip().lower()
        aliases = {
            "选白": "white",
            "white": "white",
            "选黑": "black",
            "black": "black",
            "交换": "swap",
            "swap": "swap",
            "加两子": "add_two",
            "add_two": "add_two",
        }
        action = aliases.get(value, value)
        if self.opening != "swap2":
            return MoveOutcome(False, "本局没有启用 Swap2。")
        swap_players = False
        if self.opening_phase == "choice":
            if action == "white":
                self.opening_phase = "normal"
                self.turn = SECOND
                self.normal_move_start = len(self.moves)
            elif action == "swap":
                self.opening_phase = "normal"
                self.turn = SECOND
                swap_players = True
                self.normal_move_start = len(self.moves)
            elif action == "add_two":
                self.opening_phase = "place_two"
                self.opening_count = 0
                self.turn = SECOND
            else:
                return MoveOutcome(False, "请选择 /选白、/交换 或 /加两子。")
        elif self.opening_phase == "final_choice":
            if action == "black":
                self.opening_phase = "normal"
                self.turn = SECOND
                self.normal_move_start = len(self.moves)
            elif action == "white":
                self.opening_phase = "normal"
                self.turn = SECOND
                swap_players = True
                self.normal_move_start = len(self.moves)
            else:
                return MoveOutcome(False, "请选择 /选黑 或 /选白。")
        else:
            return MoveOutcome(False, self.opening_prompt or "当前无需进行 Swap2 选择。")
        self.events.append({"kind": "choice", "choice": action})
        return MoveOutcome(
            True,
            self.opening_prompt or "Swap2 选色完成，白方继续行棋。",
            extra={"swap_players": swap_players},
        )

    def undo(self, steps: int = 1) -> bool:
        if steps < 1 or len(self.moves) < steps:
            return False
        place_indices = [
            index for index, event in enumerate(self.events) if event.get("kind") == "place"
        ]
        cut = place_indices[-steps]
        kept = self.events[:cut]
        self.__init__(
            self.size,
            rule=self.rule,
            opening=self.opening,
            events=kept,
        )
        return True

    def notation_history(self) -> list[str]:
        return list(self.moves)

    def to_dict(self) -> dict[str, Any]:
        return {
            "game_id": self.game_id,
            "size": self.size,
            "rule": self.rule,
            "opening": self.opening,
            "moves": list(self.moves),
            "events": [dict(event) for event in self.events],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> GomokuEngine:
        events = data.get("events")
        return cls(
            int(data.get("size", 15)),
            None if events is not None else list(data.get("moves", [])),
            rule=str(data.get("rule", "freestyle")),
            opening=str(data.get("opening", "normal")),
            events=[dict(event) for event in events] if events is not None else None,
        )

    def _would_win(self, x: int, y: int, stone: int) -> bool:
        self.board[y][x] = stone
        win = self._is_win(x, y, stone)
        if win and self.rule == "renju" and stone == 1:
            win = self._forbidden_reason(x, y) is None
        self.board[y][x] = 0
        return win

    def _point_score(self, x: int, y: int, stone: int) -> float:
        score = 0.0
        for dx, dy in ((1, 0), (0, 1), (1, 1), (1, -1)):
            run = count_direction(self.board, x, y, dx, dy, stone) + count_direction(
                self.board, x, y, -dx, -dy, stone
            )
            score += 10**run
        center = (self.size - 1) / 2
        return score - (abs(x - center) + abs(y - center)) * 0.1

    def analyze(self) -> Analysis:
        me = 1 if self.turn == FIRST else 2
        other = 3 - me
        empties = [
            (x, y)
            for y in range(self.size)
            for x in range(self.size)
            if self.board[y][x] == 0
        ]
        for x, y in empties:
            if self._would_win(x, y, me):
                return Analysis(
                    f"{self.rule_label}：当前存在一步制胜。",
                    coord_to_text(x, y),
                    82.0 if me == 1 else 18.0,
                )
        for x, y in empties:
            if self._would_win(x, y, other):
                return Analysis(
                    f"{self.rule_label}：对手下一手有成五威胁，建议立即封堵。",
                    coord_to_text(x, y),
                    43.0 if self.turn == FIRST else 57.0,
                )
        if not empties:
            return Analysis("棋盘已满。")
        best = max(
            empties,
            key=lambda p: (
                self._point_score(*p, me) + self._point_score(*p, other) * 0.8
            ),
        )
        return Analysis(
            f"{self.rule_label}：轻量威胁评估完成；优先兼顾己方连线与对手封堵。",
            coord_to_text(*best),
            50.0,
        )


class TicTacToeEngine(GameEngine):
    game_id = "tictactoe"
    display_name = "井字棋"
    side_names = ("X 方", "O 方")

    def __init__(self, moves: list[str] | None = None):
        self.size = 3
        self.board = [[0 for _ in range(3)] for _ in range(3)]
        self.moves: list[str] = []
        self.turn = FIRST
        self.last_move = None
        for move in moves or []:
            result = self.play(move)
            if not result.ok:
                raise ValueError(f"无法恢复井字棋走法: {move}")

    def move_candidate(self, text: str) -> bool:
        value, explicit = clean_move_text(text)
        return explicit or bool(re.fullmatch(r"(?:[A-Ca-c][1-3]|[1-9])", value))

    @staticmethod
    def _coord(value: str) -> tuple[int, int] | None:
        value = value.strip().upper()
        if value.isdigit() and 1 <= int(value) <= 9:
            pos = int(value) - 1
            return pos % 3, pos // 3
        return parse_letter_coord(value, 3)

    def _winner(self) -> int:
        lines = [*self.board]
        lines.extend([[self.board[y][x] for y in range(3)] for x in range(3)])
        lines.extend(
            [
                [self.board[i][i] for i in range(3)],
                [self.board[i][2 - i] for i in range(3)],
            ]
        )
        for line in lines:
            if line[0] and line.count(line[0]) == 3:
                return line[0]
        return 0

    def play(self, text: str) -> MoveOutcome:
        value, _ = clean_move_text(text)
        coord = self._coord(value.replace(" ", ""))
        if coord is None:
            return MoveOutcome(False, "请输入 1～9 或 A1～C3，例如 5 或 B2。")
        x, y = coord
        if self.board[y][x]:
            return MoveOutcome(False, "该格已有棋子。")
        self.board[y][x] = 1 if self.turn == FIRST else 2
        notation = coord_to_text(x, y)
        self.moves.append(notation)
        self.last_move = (None, (x, y))
        if self._winner():
            return MoveOutcome(
                True, "三子连线，棋局结束。", notation, None, (x, y), True, self.turn
            )
        if len(self.moves) == 9:
            return MoveOutcome(True, "和棋。", notation, None, (x, y), True, None, True)
        self.turn = opponent(self.turn)
        return MoveOutcome(True, notation=notation, target=(x, y))

    def undo(self, steps: int = 1) -> bool:
        if steps < 1 or len(self.moves) < steps:
            return False
        self.__init__(self.moves[:-steps])
        return True

    def notation_history(self) -> list[str]:
        return list(self.moves)

    def to_dict(self) -> dict[str, Any]:
        return {"game_id": self.game_id, "moves": list(self.moves)}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TicTacToeEngine:
        return cls(list(data.get("moves", [])))

    def _minimax(self, side: int) -> tuple[int, tuple[int, int] | None]:
        winner = self._winner()
        if winner:
            return (1 if winner == side else -1), None
        empties = [(x, y) for y in range(3) for x in range(3) if not self.board[y][x]]
        if not empties:
            return 0, None
        moving = 1 if self.turn == FIRST else 2
        maximizing = moving == side
        best_score = -2 if maximizing else 2
        best_move = None
        original_turn = self.turn
        for x, y in empties:
            self.board[y][x] = moving
            self.turn = opponent(self.turn)
            score, _ = self._minimax(side)
            self.turn = original_turn
            self.board[y][x] = 0
            if (maximizing and score > best_score) or (
                not maximizing and score < best_score
            ):
                best_score, best_move = score, (x, y)
        return best_score, best_move

    def analyze(self) -> Analysis:
        side = 1 if self.turn == FIRST else 2
        score, move = self._minimax(side)
        labels = {
            1: "当前方可强制获胜",
            0: "双方最优时为和棋",
            -1: "当前方已处于必败局面",
        }
        first_rate = {1: 100.0, 0: 50.0, -1: 0.0}[score]
        if self.turn == SECOND:
            first_rate = 100.0 - first_rate
        return Analysis(
            f"完全搜索：{labels[score]}。",
            coord_to_text(*move) if move else None,
            first_rate,
        )


class ReversiEngine(GameEngine):
    game_id = "reversi"
    display_name = "黑白棋"
    side_names = ("黑方", "白方")
    _DIRS = tuple((dx, dy) for dy in (-1, 0, 1) for dx in (-1, 0, 1) if dx or dy)

    def __init__(self, moves: list[str] | None = None):
        self.size = 8
        self.board = [[0 for _ in range(8)] for _ in range(8)]
        self.board[3][3] = self.board[4][4] = 2
        self.board[3][4] = self.board[4][3] = 1
        self.moves: list[str] = []
        self.turn = FIRST
        self.last_move = None
        for move in moves or []:
            result = self.play(move)
            if not result.ok:
                raise ValueError(f"无法恢复黑白棋走法: {move}")

    def move_candidate(self, text: str) -> bool:
        value, explicit = clean_move_text(text)
        return explicit or bool(re.fullmatch(r"[A-Ha-h][1-8]", value))

    def _flips(self, x: int, y: int, stone: int) -> list[tuple[int, int]]:
        if self.board[y][x]:
            return []
        captured: list[tuple[int, int]] = []
        for dx, dy in self._DIRS:
            line: list[tuple[int, int]] = []
            nx, ny = x + dx, y + dy
            while 0 <= nx < 8 and 0 <= ny < 8 and self.board[ny][nx] == 3 - stone:
                line.append((nx, ny))
                nx += dx
                ny += dy
            if line and 0 <= nx < 8 and 0 <= ny < 8 and self.board[ny][nx] == stone:
                captured.extend(line)
        return captured

    def legal_moves(self, stone: int) -> dict[tuple[int, int], list[tuple[int, int]]]:
        return {
            (x, y): flips
            for y in range(8)
            for x in range(8)
            if (flips := self._flips(x, y, stone))
        }

    def play(self, text: str) -> MoveOutcome:
        value, _ = clean_move_text(text)
        coord = parse_letter_coord(value.replace(" ", ""), 8)
        if coord is None:
            return MoveOutcome(False, "请输入 A1～H8，例如 D3。")
        stone = 1 if self.turn == FIRST else 2
        flips = self._flips(*coord, stone)
        if not flips:
            return MoveOutcome(False, "该位置不能夹住对方棋子。")
        x, y = coord
        self.board[y][x] = stone
        for fx, fy in flips:
            self.board[fy][fx] = stone
        notation = coord_to_text(x, y)
        self.moves.append(notation)
        self.last_move = (None, (x, y))

        next_side = opponent(self.turn)
        next_stone = 2 if stone == 1 else 1
        if self.legal_moves(next_stone):
            self.turn = next_side
            return MoveOutcome(
                True, notation=notation, target=(x, y), extra={"flips": len(flips)}
            )
        if self.legal_moves(stone):
            return MoveOutcome(
                True,
                "对方无合法着法，当前方继续。",
                notation,
                None,
                (x, y),
                extra={"flips": len(flips), "pass": True},
            )

        black = sum(row.count(1) for row in self.board)
        white = sum(row.count(2) for row in self.board)
        if black == white:
            return MoveOutcome(
                True,
                f"终局 {black}:{white}，和棋。",
                notation,
                None,
                (x, y),
                True,
                None,
                True,
            )
        winner = FIRST if black > white else SECOND
        return MoveOutcome(
            True,
            f"终局 黑 {black} : 白 {white}。",
            notation,
            None,
            (x, y),
            True,
            winner,
        )

    def undo(self, steps: int = 1) -> bool:
        if steps < 1 or len(self.moves) < steps:
            return False
        self.__init__(self.moves[:-steps])
        return True

    def notation_history(self) -> list[str]:
        return list(self.moves)

    def to_dict(self) -> dict[str, Any]:
        return {"game_id": self.game_id, "moves": list(self.moves)}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ReversiEngine:
        return cls(list(data.get("moves", [])))

    def analyze(self) -> Analysis:
        stone = 1 if self.turn == FIRST else 2
        legal = self.legal_moves(stone)
        if not legal:
            return Analysis("当前无合法着法。")
        corners = {(0, 0), (0, 7), (7, 0), (7, 7)}
        edges = {
            (x, y) for y in range(8) for x in range(8) if x in {0, 7} or y in {0, 7}
        }
        best = max(
            legal,
            key=lambda pos: (
                len(legal[pos]) + (100 if pos in corners else 8 if pos in edges else 0)
            ),
        )
        black = sum(row.count(1) for row in self.board)
        white = sum(row.count(2) for row in self.board)
        first_rate = 50.0 + max(-35, min(35, (black - white) * 1.8))
        return Analysis(
            "轻量评估优先角点、边线与本手翻子数；中盘子数胜率仅供参考。",
            coord_to_text(*best),
            first_rate,
        )
