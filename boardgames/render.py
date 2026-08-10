from __future__ import annotations

import io
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from .base import FIRST, SECOND
from .chess_engine import ChessEngine
from .go_engine import _GO_LETTERS, GoEngine
from .grid_games import GomokuEngine, ReversiEngine, TicTacToeEngine
from .session import GameSession
from .xiangqi_engine import _PIECE_NAMES, XiangqiEngine


class BoardRenderer:
    WIDTH = 760
    BG = "#F4F1EA"
    INK = "#242424"
    ORIGIN = "#F4B942"
    TARGET = "#5FB36A"

    def __init__(self, assets_dir: str | Path):
        assets = Path(assets_dir)
        self.text_font_path = assets / "chinese_font.ttf"
        self.piece_font_path = assets / "apple_symbols.ttf"

    def _font(self, size: int, *, pieces: bool = False):
        path = self.piece_font_path if pieces else self.text_font_path
        try:
            return ImageFont.truetype(str(path), size)
        except OSError:
            return ImageFont.load_default()

    def _header(self, draw: ImageDraw.ImageDraw, session: GameSession) -> None:
        title_font = self._font(30)
        info_font = self._font(19)
        draw.text((32, 20), session.engine.display_name, font=title_font, fill=self.INK)
        side = session.engine.turn
        side_name = session.engine.side_names[0 if side == FIRST else 1]
        if (
            session.status == "playing"
            and isinstance(session.engine, GomokuEngine)
            and session.engine.opening_prompt
        ):
            status = f"Swap2 · {session.engine.opening_prompt}"
        elif session.status == "waiting":
            status = f"等待加入 · {side_name}"
        else:
            in_check = (
                isinstance(session.engine, ChessEngine)
                and session.engine.board.is_check()
            ) or (
                isinstance(session.engine, XiangqiEngine)
                and session.engine._in_check(session.engine.turn)
            )
            status = f"轮到 {side_name}" + (" · 将军" if in_check else "")
        draw.text((728, 27), status, font=info_font, fill="#555555", anchor="ra")
        first = session.players.get(FIRST)
        second = session.players.get(SECOND)
        first_name = first.name if first else "等待加入"
        second_name = second.name if second else "等待加入"
        draw.text(
            (32, 68),
            f"{session.engine.side_names[0]}  {first_name}",
            font=info_font,
            fill="#9C2F2F",
        )
        draw.text(
            (728, 68),
            f"{session.engine.side_names[1]}  {second_name}",
            font=info_font,
            fill="#333333",
            anchor="ra",
        )

    def _footer(
        self, draw: ImageDraw.ImageDraw, height: int, session: GameSession
    ) -> None:
        font = self._font(16)
        if session.status == "waiting":
            draw.text(
                (32, height - 40),
                "使用 /加入棋局 加入对局",
                font=font,
                fill="#666666",
            )
            draw.text(
                (728, height - 40),
                f"{session.engine.side_names[0]}先行",
                font=font,
                fill="#666666",
                anchor="ra",
            )
            return
        moves = len(session.engine.notation_history())
        last = session.engine.notation_history()[-1] if moves else "—"
        draw.text(
            (32, height - 40),
            f"第 {moves} 手 · 上一步 {last}",
            font=font,
            fill="#666666",
        )
        draw.text(
            (728, height - 40),
            "橙色：起点  绿色：终点",
            font=font,
            fill="#666666",
            anchor="ra",
        )

    @staticmethod
    def _highlight_rect(
        draw: ImageDraw.ImageDraw,
        rect: tuple[float, float, float, float],
        color: str,
    ) -> None:
        draw.rectangle(rect, outline=color, width=6)

    def render(self, session: GameSession) -> bytes:
        engine = session.engine
        if isinstance(engine, ChessEngine):
            image = self._render_chess(session, engine)
        elif isinstance(engine, XiangqiEngine):
            image = self._render_xiangqi(session, engine)
        elif isinstance(engine, GoEngine):
            image = self._render_go(session, engine)
        elif isinstance(engine, GomokuEngine):
            image = self._render_gomoku(session, engine)
        elif isinstance(engine, ReversiEngine):
            image = self._render_reversi(session, engine)
        elif isinstance(engine, TicTacToeEngine):
            image = self._render_tictactoe(session, engine)
        else:
            raise TypeError(f"没有适配的渲染器: {type(engine)!r}")
        stream = io.BytesIO()
        image.save(stream, format="PNG", optimize=True)
        return stream.getvalue()

    def render_go_influence(self, session: GameSession) -> bytes:
        if not isinstance(session.engine, GoEngine):
            raise TypeError("势力范围图仅适用于围棋")
        image = self._render_go(session, session.engine, show_influence=True)
        stream = io.BytesIO()
        image.save(stream, format="PNG", optimize=True)
        return stream.getvalue()

    def _render_chess(self, session: GameSession, engine: ChessEngine) -> Image.Image:
        cell, left, top = 70, 100, 112
        board_size = cell * 8
        height = top + board_size + 74
        image = Image.new("RGB", (self.WIDTH, height), self.BG)
        draw = ImageDraw.Draw(image)
        self._header(draw, session)
        coord_font = self._font(16)
        piece_font = self._font(49, pieces=True)
        symbols = {
            "r": "♜",
            "n": "♞",
            "b": "♝",
            "q": "♛",
            "k": "♚",
            "p": "♟",
            "R": "♖",
            "N": "♘",
            "B": "♗",
            "Q": "♕",
            "K": "♔",
            "P": "♙",
        }
        for row in range(8):
            for col in range(8):
                x0, y0 = left + col * cell, top + row * cell
                color = "#F0D9B5" if (row + col) % 2 == 0 else "#B58863"
                draw.rectangle((x0, y0, x0 + cell, y0 + cell), fill=color)
                square = __import__("chess").square(col, 7 - row)
                piece = engine.board.piece_at(square)
                if piece:
                    draw.text(
                        (x0 + cell / 2, y0 + cell / 2 - 2),
                        symbols[piece.symbol()],
                        font=piece_font,
                        fill="#111111",
                        anchor="mm",
                    )
        if engine.last_move:
            origin, target = engine.last_move
            for pos, color in ((origin, self.ORIGIN), (target, self.TARGET)):
                if pos:
                    x, rank = pos
                    row = 7 - rank
                    self._highlight_rect(
                        draw,
                        (
                            left + x * cell + 3,
                            top + row * cell + 3,
                            left + (x + 1) * cell - 3,
                            top + (row + 1) * cell - 3,
                        ),
                        color,
                    )
        for i in range(8):
            draw.text(
                (left + i * cell + cell / 2, top + board_size + 8),
                chr(97 + i),
                font=coord_font,
                fill=self.INK,
                anchor="ma",
            )
            draw.text(
                (left - 12, top + i * cell + cell / 2),
                str(8 - i),
                font=coord_font,
                fill=self.INK,
                anchor="rm",
            )
        self._footer(draw, height, session)
        return image

    def _render_xiangqi(
        self, session: GameSession, engine: XiangqiEngine
    ) -> Image.Image:
        cell, left, top = 60, 140, 120
        width, board_height = cell * 8, cell * 9
        height = top + board_height + 88
        image = Image.new("RGB", (self.WIDTH, height), self.BG)
        draw = ImageDraw.Draw(image)
        self._header(draw, session)
        draw.rectangle(
            (left - 32, top - 32, left + width + 32, top + board_height + 32),
            fill="#E9C887",
            outline="#8A5A2B",
            width=3,
        )
        for i in range(10):
            y = top + i * cell
            draw.line((left, y, left + width, y), fill="#5A371F", width=2)
        for i in range(9):
            x = left + i * cell
            if i in {0, 8}:
                draw.line((x, top, x, top + board_height), fill="#5A371F", width=2)
            else:
                draw.line((x, top, x, top + 4 * cell), fill="#5A371F", width=2)
                draw.line(
                    (x, top + 5 * cell, x, top + board_height), fill="#5A371F", width=2
                )
        draw.line(
            (left + 3 * cell, top, left + 5 * cell, top + 2 * cell),
            fill="#5A371F",
            width=2,
        )
        draw.line(
            (left + 5 * cell, top, left + 3 * cell, top + 2 * cell),
            fill="#5A371F",
            width=2,
        )
        draw.line(
            (left + 3 * cell, top + 7 * cell, left + 5 * cell, top + 9 * cell),
            fill="#5A371F",
            width=2,
        )
        draw.line(
            (left + 5 * cell, top + 7 * cell, left + 3 * cell, top + 9 * cell),
            fill="#5A371F",
            width=2,
        )
        river_font = self._font(26)
        draw.text(
            (left + 1.5 * cell, top + 4.5 * cell),
            "楚 河",
            font=river_font,
            fill="#6B4423",
            anchor="mm",
        )
        draw.text(
            (left + 6.5 * cell, top + 4.5 * cell),
            "汉 界",
            font=river_font,
            fill="#6B4423",
            anchor="mm",
        )
        piece_font = self._font(28)
        for y in range(10):
            for x in range(9):
                piece = engine.board[y][x]
                if not piece:
                    continue
                px, py = left + x * cell, top + (9 - y) * cell
                fill = "#F7E1A8"
                outline = "#B43B32" if piece.isupper() else "#333333"
                draw.ellipse(
                    (px - 24, py - 24, px + 24, py + 24),
                    fill=fill,
                    outline=outline,
                    width=3,
                )
                draw.text(
                    (px, py - 1),
                    _PIECE_NAMES[piece],
                    font=piece_font,
                    fill=outline,
                    anchor="mm",
                )
        if engine.last_move:
            origin, target = engine.last_move
            for pos, color in ((origin, self.ORIGIN), (target, self.TARGET)):
                if pos:
                    x, y = pos
                    px, py = left + x * cell, top + (9 - y) * cell
                    draw.ellipse(
                        (px - 29, py - 29, px + 29, py + 29), outline=color, width=6
                    )
        coord_font = self._font(15)
        for x in range(9):
            draw.text(
                (left + x * cell, top + board_height + 40),
                chr(97 + x),
                font=coord_font,
                fill="#684420",
                anchor="mm",
            )
        for y in range(10):
            draw.text(
                (left - 42, top + (9 - y) * cell),
                str(y),
                font=coord_font,
                fill="#684420",
                anchor="mm",
            )
        self._footer(draw, height, session)
        return image

    @staticmethod
    def _mix_color(base: tuple[int, int, int], tint: tuple[int, int, int], alpha: float):
        return tuple(round(a * (1.0 - alpha) + b * alpha) for a, b in zip(base, tint))

    def _render_go(
        self,
        session: GameSession,
        engine: GoEngine,
        *,
        show_influence: bool = False,
    ) -> Image.Image:
        cell = 30 if engine.size == 19 else 42 if engine.size == 13 else 56
        extent = cell * (engine.size - 1)
        left = (self.WIDTH - extent) // 2
        top = 120
        height = top + extent + 90
        image = Image.new("RGB", (self.WIDTH, height), self.BG)
        draw = ImageDraw.Draw(image)
        self._header(draw, session)
        draw.rectangle(
            (left - cell, top - cell, left + extent + cell, top + extent + cell),
            fill="#D9A85B",
            outline="#8B5A2B",
            width=3,
        )
        if show_influence:
            influence = engine.influence_map()
            base = (217, 168, 91)
            for y in range(engine.size):
                for x in range(engine.size):
                    if engine.board[y][x]:
                        continue
                    value = influence[y][x]
                    if abs(value) < 0.08:
                        continue
                    tint = (55, 48, 42) if value > 0 else (205, 232, 244)
                    color = self._mix_color(base, tint, min(0.78, abs(value) * 0.72))
                    px = left + x * cell
                    py = top + (engine.size - 1 - y) * cell
                    radius = max(5, cell * 0.43)
                    draw.rectangle(
                        (px - radius, py - radius, px + radius, py + radius),
                        fill=color,
                    )
        for i in range(engine.size):
            draw.line(
                (left, top + i * cell, left + extent, top + i * cell),
                fill="#3B2A1D",
                width=1,
            )
            draw.line(
                (left + i * cell, top, left + i * cell, top + extent),
                fill="#3B2A1D",
                width=1,
            )
        hoshi = {9: [2, 4, 6], 13: [3, 6, 9], 19: [3, 9, 15]}[engine.size]
        for x in hoshi:
            for y in hoshi:
                draw.ellipse(
                    (
                        left + x * cell - 4,
                        top + y * cell - 4,
                        left + x * cell + 4,
                        top + y * cell + 4,
                    ),
                    fill="#3B2A1D",
                )
        for y in range(engine.size):
            for x in range(engine.size):
                stone = engine.board[y][x]
                if not stone:
                    continue
                px, py = left + x * cell, top + (engine.size - 1 - y) * cell
                fill, outline = (
                    ("#171717", "#000000") if stone == 1 else ("#F7F7F7", "#555555")
                )
                radius = cell * 0.44
                draw.ellipse(
                    (px - radius, py - radius, px + radius, py + radius),
                    fill=fill,
                    outline=outline,
                    width=2,
                )
        if engine.last_move and engine.last_move[1]:
            x, y = engine.last_move[1]
            px, py = left + x * cell, top + (engine.size - 1 - y) * cell
            draw.ellipse(
                (
                    px - cell * 0.18,
                    py - cell * 0.18,
                    px + cell * 0.18,
                    py + cell * 0.18,
                ),
                outline=self.TARGET,
                width=4,
            )
        coord_font = self._font(14)
        for i in range(engine.size):
            draw.text(
                (left + i * cell, top + extent + cell * 0.65),
                _GO_LETTERS[i],
                font=coord_font,
                fill=self.INK,
                anchor="mm",
            )
            draw.text(
                (left - cell * 0.65, top + (engine.size - 1 - i) * cell),
                str(i + 1),
                font=coord_font,
                fill=self.INK,
                anchor="mm",
            )
        self._footer(draw, height, session)
        if show_influence:
            font = self._font(16)
            draw.rectangle((360, height - 58, 740, height - 20), fill=self.BG)
            draw.text(
                (728, height - 40),
                "深色：黑方势力  浅蓝：白方势力",
                font=font,
                fill="#555555",
                anchor="ra",
            )
        return image

    def _render_gomoku(self, session: GameSession, engine: GomokuEngine) -> Image.Image:
        cell = 38 if engine.size == 15 else 29 if engine.size == 19 else 43
        extent = cell * (engine.size - 1)
        left, top = (self.WIDTH - extent) // 2, 120
        height = top + extent + 90
        image = Image.new("RGB", (self.WIDTH, height), self.BG)
        draw = ImageDraw.Draw(image)
        self._header(draw, session)
        draw.rectangle(
            (left - cell, top - cell, left + extent + cell, top + extent + cell),
            fill="#D9A85B",
            outline="#8B5A2B",
            width=3,
        )
        for i in range(engine.size):
            draw.line(
                (left, top + i * cell, left + extent, top + i * cell), fill="#3B2A1D"
            )
            draw.line(
                (left + i * cell, top, left + i * cell, top + extent), fill="#3B2A1D"
            )
        for y in range(engine.size):
            for x in range(engine.size):
                stone = engine.board[y][x]
                if not stone:
                    continue
                px, py = left + x * cell, top + (engine.size - 1 - y) * cell
                fill, outline = (
                    ("#151515", "#000") if stone == 1 else ("#FAFAFA", "#666")
                )
                r = cell * 0.43
                draw.ellipse(
                    (px - r, py - r, px + r, py + r),
                    fill=fill,
                    outline=outline,
                    width=2,
                )
        if engine.last_move and engine.last_move[1]:
            x, y = engine.last_move[1]
            px, py = left + x * cell, top + (engine.size - 1 - y) * cell
            draw.ellipse(
                (
                    px - cell * 0.16,
                    py - cell * 0.16,
                    px + cell * 0.16,
                    py + cell * 0.16,
                ),
                outline=self.TARGET,
                width=4,
            )
        coord_font = self._font(14)
        for i in range(engine.size):
            draw.text(
                (left + i * cell, top + extent + cell * 0.65),
                chr(65 + i),
                font=coord_font,
                fill=self.INK,
                anchor="mm",
            )
            draw.text(
                (left - cell * 0.65, top + (engine.size - 1 - i) * cell),
                str(i + 1),
                font=coord_font,
                fill=self.INK,
                anchor="mm",
            )
        self._footer(draw, height, session)
        font = self._font(15)
        draw.rectangle((300, height - 58, 740, height - 20), fill=self.BG)
        footer = engine.opening_prompt or engine.rule_label
        draw.text(
            (728, height - 40),
            footer,
            font=font,
            fill="#555555",
            anchor="ra",
        )
        return image

    def _render_reversi(
        self, session: GameSession, engine: ReversiEngine
    ) -> Image.Image:
        cell, left, top = 68, 108, 120
        size = cell * 8
        height = top + size + 80
        image = Image.new("RGB", (self.WIDTH, height), self.BG)
        draw = ImageDraw.Draw(image)
        self._header(draw, session)
        for y in range(8):
            for x in range(8):
                x0, y0 = left + x * cell, top + y * cell
                draw.rectangle(
                    (x0, y0, x0 + cell, y0 + cell),
                    fill="#278A4B",
                    outline="#173B25",
                    width=2,
                )
                stone = engine.board[y][x]
                if stone:
                    fill, outline = (
                        ("#171717", "#000") if stone == 1 else ("#F7F7F7", "#777")
                    )
                    draw.ellipse(
                        (x0 + 8, y0 + 8, x0 + cell - 8, y0 + cell - 8),
                        fill=fill,
                        outline=outline,
                        width=2,
                    )
        if engine.last_move and engine.last_move[1]:
            x, y = engine.last_move[1]
            self._highlight_rect(
                draw,
                (
                    left + x * cell + 3,
                    top + y * cell + 3,
                    left + (x + 1) * cell - 3,
                    top + (y + 1) * cell - 3,
                ),
                self.TARGET,
            )
        font = self._font(15)
        for i in range(8):
            draw.text(
                (left + i * cell + cell / 2, top + size + 8),
                chr(65 + i),
                font=font,
                fill=self.INK,
                anchor="ma",
            )
            draw.text(
                (left - 12, top + i * cell + cell / 2),
                str(i + 1),
                font=font,
                fill=self.INK,
                anchor="rm",
            )
        self._footer(draw, height, session)
        return image

    def _render_tictactoe(
        self, session: GameSession, engine: TicTacToeEngine
    ) -> Image.Image:
        cell, left, top = 160, 140, 130
        size = cell * 3
        height = top + size + 90
        image = Image.new("RGB", (self.WIDTH, height), self.BG)
        draw = ImageDraw.Draw(image)
        self._header(draw, session)
        for i in (1, 2):
            draw.line(
                (left + i * cell, top, left + i * cell, top + size),
                fill="#555",
                width=8,
            )
            draw.line(
                (left, top + i * cell, left + size, top + i * cell),
                fill="#555",
                width=8,
            )
        font = self._font(105)
        for y in range(3):
            for x in range(3):
                value = engine.board[y][x]
                if value:
                    draw.text(
                        (left + x * cell + cell / 2, top + y * cell + cell / 2),
                        "X" if value == 1 else "O",
                        font=font,
                        fill="#B43B32" if value == 1 else "#2E65A7",
                        anchor="mm",
                    )
                draw.text(
                    (left + x * cell + 18, top + y * cell + 12),
                    str(y * 3 + x + 1),
                    font=self._font(16),
                    fill="#888",
                )
        if engine.last_move and engine.last_move[1]:
            x, y = engine.last_move[1]
            self._highlight_rect(
                draw,
                (
                    left + x * cell + 5,
                    top + y * cell + 5,
                    left + (x + 1) * cell - 5,
                    top + (y + 1) * cell - 5,
                ),
                self.TARGET,
            )
        self._footer(draw, height, session)
        return image
