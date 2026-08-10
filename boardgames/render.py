from __future__ import annotations

import io
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageOps

from .base import FIRST, SECOND
from .chess_engine import ChessEngine
from .clock import format_clock
from .go_engine import _GO_LETTERS, GoEngine
from .grid_games import GomokuEngine, ReversiEngine, TicTacToeEngine
from .session import GameSession, Player
from .xiangqi_engine import _PIECE_NAMES, XiangqiEngine


class BoardRenderer:
    WIDTH = 760
    BG = "#F4F1EA"
    INK = "#242424"
    ORIGIN = "#F4B942"
    TARGET = "#5FB36A"
    FRAME = "#5D4632"

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

    @staticmethod
    def _coord_font(size: int):
        """棋盘边缘专用的清晰无衬线字体，不影响正文的美术字体。"""
        try:
            return ImageFont.truetype("DejaVuSans.ttf", size)
        except OSError:
            try:
                return ImageFont.load_default(size=size)
            except TypeError:  # Pillow 10.0 的兼容回退
                return ImageFont.load_default()

    @staticmethod
    def _latin_coord(index: int) -> str:
        return chr(65 + index)

    @classmethod
    def _board_frame(
        cls,
        draw: ImageDraw.ImageDraw,
        rect: tuple[float, float, float, float],
        color: str | None = None,
    ) -> None:
        x0, y0, x1, y1 = rect
        draw.rounded_rectangle(
            (x0 - 6, y0 - 6, x1 + 6, y1 + 6),
            radius=7,
            outline="#D8C8AA",
            width=2,
        )
        draw.rounded_rectangle(
            (x0 - 4, y0 - 4, x1 + 4, y1 + 4),
            radius=5,
            outline=color or cls.FRAME,
            width=4,
        )

    def _header(self, draw: ImageDraw.ImageDraw, session: GameSession) -> None:
        title_font = self._font(30)
        info_font = self._font(19)
        draw.text((32, 20), session.engine.display_name, font=title_font, fill=self.INK)
        side = session.engine.turn
        side_name = session.engine.side_names[0 if side == FIRST else 1]
        if session.status == "choosing":
            status = "等待任一选手选色"
        elif (
            session.status == "playing"
            and isinstance(session.engine, GomokuEngine)
            and session.engine.opening_prompt
        ):
            status = f"Swap2 · {session.engine.opening_prompt}"
        elif session.status == "waiting":
            control = session.clock.get("label") if session.clock else "不计时"
            status = f"等待加入 · 用时 {control}"
        else:
            in_check = (
                isinstance(session.engine, ChessEngine)
                and session.engine.board.is_check()
            ) or (
                isinstance(session.engine, XiangqiEngine)
                and session.engine._in_check(session.engine.turn)
            )
            perspective = (
                ""
                if isinstance(session.engine, TicTacToeEngine)
                else f" · {side_name}视角"
            )
            status = f"轮到 {side_name}{perspective}" + (
                " · 将军" if in_check else ""
            )
        draw.text((728, 27), status, font=info_font, fill="#555555", anchor="ra")
        first = session.players.get(FIRST)
        second = session.players.get(SECOND)
        if session.status == "choosing":
            first_choice = session.side_choices.get(first.user_id) if first else None
            second_choice = session.side_choices.get(second.user_id) if second else None

            def choice_text(choice: str | None) -> str:
                if not choice:
                    return "未选"
                return session.engine.side_names[0 if choice == FIRST else 1]

            draw.text(
                (32, 68),
                f"玩家  {first.name if first else '—'} · {choice_text(first_choice)}",
                font=info_font,
                fill="#9C2F2F",
            )
            draw.text(
                (728, 68),
                f"玩家  {second.name if second else '—'} · {choice_text(second_choice)}",
                font=info_font,
                fill="#333333",
                anchor="ra",
            )
            return
        first_name = first.name if first else "等待加入"
        second_name = second.name if second else "等待加入"
        first_clock = (
            f"  ⏱ {format_clock(session.clock, FIRST)}"
            if session.status == "playing"
            else ""
        )
        second_clock = (
            f"  ⏱ {format_clock(session.clock, SECOND)}"
            if session.status == "playing"
            else ""
        )
        draw.text(
            (32, 68),
            f"{session.engine.side_names[0]}  {first_name}{first_clock}",
            font=info_font,
            fill="#9C2F2F",
        )
        draw.text(
            (728, 68),
            f"{session.engine.side_names[1]}  {second_name}{second_clock}",
            font=info_font,
            fill="#333333",
            anchor="ra",
        )

    def _footer(
        self, draw: ImageDraw.ImageDraw, height: int, session: GameSession
    ) -> None:
        font = self._font(16)
        if session.status == "choosing":
            draw.text(
                (32, height - 40),
                "任一选手选择一方，对手自动分配另一方",
                font=font,
                fill="#666666",
            )
            draw.text(
                (728, height - 40),
                "使用 /选先 /选后，或对应棋种的 /选白 /选黑 /选红",
                font=font,
                fill="#666666",
                anchor="ra",
            )
            return
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

    @staticmethod
    def _flipped(session: GameSession) -> bool:
        # 井字棋的 1～9 格位是固定键盘布局；翻转会让同一个数字在双方眼中
        # 指向不同位置，因此它始终保持 X 方视角。
        return (
            not isinstance(session.engine, TicTacToeEngine)
            and session.status == "playing"
            and session.engine.turn == SECOND
        )

    @staticmethod
    def _oriented_point(
        x: int, y: int, width: int, height: int, flipped: bool
    ) -> tuple[int, int]:
        if flipped:
            return width - 1 - x, height - 1 - y
        return x, y

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

    @staticmethod
    def _game_record(
        stats: dict, player: Player | None, game_id: str
    ) -> dict:
        if not player:
            return {}
        return dict(stats.get(player.user_id, {}).get(game_id, {}))

    @staticmethod
    def _totals(stats: dict, player: Player | None) -> tuple[int, int, int]:
        wins = draws = losses = 0
        if player:
            for record in stats.get(player.user_id, {}).values():
                wins += int(record.get("wins", 0))
                draws += int(record.get("draws", 0))
                losses += int(record.get("losses", 0))
        return wins, draws, losses

    @staticmethod
    def _h2h(stats: dict, player: Player | None, rival: Player | None, game_id=None):
        wins = draws = losses = 0
        if not player or not rival:
            return wins, draws, losses
        games = stats.get(player.user_id, {})
        for current_game, record in games.items():
            if game_id and current_game != game_id:
                continue
            for item in record.get("history", []):
                if str(item.get("opponent_id", "")) != rival.user_id:
                    continue
                result = item.get("result")
                wins += result == "win"
                draws += result == "draw"
                losses += result == "loss"
        return int(wins), int(draws), int(losses)

    @staticmethod
    def _rate(wins: int, draws: int, losses: int) -> str:
        total = wins + draws + losses
        return f"{wins / total * 100:.1f}%" if total else "—"

    def _draw_avatar(
        self,
        image: Image.Image,
        player: Player | None,
        data: bytes | None,
        center: tuple[int, int],
        size: int,
        color: str,
    ) -> None:
        left, top = center[0] - size // 2, center[1] - size // 2
        mask = Image.new("L", (size, size), 0)
        ImageDraw.Draw(mask).ellipse((0, 0, size - 1, size - 1), fill=255)
        avatar = None
        if data:
            try:
                avatar = Image.open(io.BytesIO(data)).convert("RGB")
                avatar = ImageOps.fit(avatar, (size, size), method=Image.Resampling.LANCZOS)
            except Exception:  # noqa: BLE001 - malformed avatar uses initials
                avatar = None
        if avatar is None:
            avatar = Image.new("RGB", (size, size), color)
            draw = ImageDraw.Draw(avatar)
            initial = (player.name.strip()[:1] if player else "?") or "?"
            draw.text(
                (size / 2, size / 2 - 2),
                initial,
                font=self._font(size // 2),
                fill="#FFFFFF",
                anchor="mm",
            )
        image.paste(avatar, (left, top), mask)
        ImageDraw.Draw(image).ellipse(
            (left - 3, top - 3, left + size + 2, top + size + 2),
            outline=color,
            width=5,
        )

    def _game_detail(self, session: GameSession) -> str:
        engine = session.engine
        if isinstance(engine, GoEngine):
            return f"{engine.size} 路 · 白贴 {engine.komi:g} 目"
        if isinstance(engine, GomokuEngine):
            opening = " · Swap2" if engine.opening == "swap2" else ""
            return f"{engine.size} 路 · {engine.rule_label}{opening}"
        return "标准规则"

    def render_match_card(
        self,
        session: GameSession,
        stats: dict,
        avatars: dict[str, bytes | None],
    ) -> bytes:
        image = Image.new("RGB", (self.WIDTH, 650), "#F2EEE5")
        draw = ImageDraw.Draw(image)
        draw.rounded_rectangle(
            (24, 24, 736, 626), radius=28, fill="#FFFDF8", outline="#D8CDBA", width=3
        )
        draw.text((380, 58), "对局成立", font=self._font(34), fill=self.INK, anchor="mm")
        control = session.clock.get("label") if session.clock else "不计时"
        draw.text(
            (380, 100),
            f"{session.engine.display_name} · {self._game_detail(session)} · 用时 {control}",
            font=self._font(18),
            fill="#66605A",
            anchor="mm",
        )
        first, second = session.players.get(FIRST), session.players.get(SECOND)
        self._draw_avatar(image, first, avatars.get(FIRST), (190, 205), 126, "#B43B32")
        self._draw_avatar(image, second, avatars.get(SECOND), (570, 205), 126, "#334C73")
        draw.text((380, 205), "VS", font=self._font(42), fill="#A5834E", anchor="mm")

        for x, side, player, align in (
            (190, FIRST, first, "mm"),
            (570, SECOND, second, "mm"),
        ):
            record = self._game_record(stats, player, session.game_id)
            wins = int(record.get("wins", 0))
            draws = int(record.get("draws", 0))
            losses = int(record.get("losses", 0))
            draw.text((x, 292), player.name if player else "—", font=self._font(26), fill=self.INK, anchor=align)
            draw.text(
                (x, 330),
                f"{session.engine.side_names[0 if side == FIRST else 1]} · 等级分 {record.get('rating', 1000)}",
                font=self._font(17),
                fill="#5C5650",
                anchor=align,
            )
            draw.text(
                (x, 364),
                f"本棋种 {wins}胜 {draws}和 {losses}负 · 胜率 {self._rate(wins, draws, losses)}",
                font=self._font(15),
                fill="#6B655F",
                anchor=align,
            )
            all_w, all_d, all_l = self._totals(stats, player)
            draw.text(
                (x, 395),
                f"总战绩 {all_w}胜 {all_d}和 {all_l}负 · 胜率 {self._rate(all_w, all_d, all_l)}",
                font=self._font(15),
                fill="#6B655F",
                anchor=align,
            )

        game_h2h = self._h2h(stats, first, second, session.game_id)
        all_h2h = self._h2h(stats, first, second)
        draw.rounded_rectangle((85, 445, 675, 575), radius=18, fill="#F6F0E5")
        draw.text((380, 475), "历史交手（左方视角）", font=self._font(19), fill="#5B4934", anchor="mm")
        draw.text(
            (380, 514),
            f"本棋种：{game_h2h[0]}胜 {game_h2h[1]}和 {game_h2h[2]}负　·　全部：{all_h2h[0]}胜 {all_h2h[1]}和 {all_h2h[2]}负",
            font=self._font(17),
            fill=self.INK,
            anchor="mm",
        )
        draw.text((380, 598), "双方选手已就位，比赛开始", font=self._font(16), fill="#85796B", anchor="mm")
        stream = io.BytesIO()
        image.save(stream, format="PNG", optimize=True)
        return stream.getvalue()

    def render_result_card(
        self,
        session: GameSession,
        winner: str | None,
        records: dict[str, dict],
        avatars: dict[str, bytes | None],
        reason: str,
    ) -> bytes:
        image = Image.new("RGB", (self.WIDTH, 570), "#EEE9DF")
        draw = ImageDraw.Draw(image)
        draw.rounded_rectangle(
            (24, 24, 736, 546), radius=28, fill="#FFFDF8", outline="#D8CDBA", width=3
        )
        result_title = "和棋" if winner is None else f"{session.players[winner].name} 获胜"
        draw.text((380, 68), "对局结束", font=self._font(29), fill="#6A625A", anchor="mm")
        draw.text((380, 112), result_title, font=self._font(35), fill="#9C3D32", anchor="mm")
        first, second = session.players.get(FIRST), session.players.get(SECOND)
        self._draw_avatar(image, first, avatars.get(FIRST), (190, 225), 118, "#B43B32")
        self._draw_avatar(image, second, avatars.get(SECOND), (570, 225), 118, "#334C73")
        score = "和" if winner is None else "1 : 0" if winner == FIRST else "0 : 1"
        draw.text(
            (380, 225),
            score,
            font=self._font(34),
            fill=self.INK,
            anchor="mm",
        )
        for x, side, player in ((190, FIRST, first), (570, SECOND, second)):
            item = records.get(side, {})
            before = int(item.get("rating_before", 1000))
            after = int(item.get("rating_after", before))
            delta = after - before
            draw.text((x, 306), player.name if player else "—", font=self._font(24), fill=self.INK, anchor="mm")
            draw.text(
                (x, 345),
                f"等级分 {before} → {after}（{delta:+d}）",
                font=self._font(17),
                fill="#2D7A45" if delta > 0 else "#A1423A" if delta < 0 else "#666666",
                anchor="mm",
            )
        draw.rounded_rectangle((100, 400, 660, 494), radius=16, fill="#F4EEE3")
        reason_text = reason if len(reason) <= 28 else f"{reason[:27]}…"
        draw.text(
            (380, 428),
            reason_text,
            font=self._font(20),
            fill="#5C5248",
            anchor="mm",
        )
        draw.text(
            (380, 465),
            f"{session.engine.display_name} · 共 {len(session.engine.notation_history())} 手",
            font=self._font(16),
            fill="#7A7168",
            anchor="mm",
        )
        draw.text((380, 522), "战绩与等级分已记录", font=self._font(15), fill="#8B8177", anchor="mm")
        stream = io.BytesIO()
        image.save(stream, format="PNG", optimize=True)
        return stream.getvalue()

    def combine_end_images(self, board_data: bytes, result_data: bytes) -> bytes:
        """把终局棋盘和结算卡合成一张图，避免平台漏发第二张图片。"""
        with Image.open(io.BytesIO(board_data)) as board_source:
            board = board_source.convert("RGB")
        with Image.open(io.BytesIO(result_data)) as result_source:
            result = result_source.convert("RGB")
        gap = 14
        width = max(board.width, result.width)
        image = Image.new("RGB", (width, board.height + gap + result.height), self.BG)
        image.paste(board, ((width - board.width) // 2, 0))
        image.paste(result, ((width - result.width) // 2, board.height + gap))
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
        flipped = self._flipped(session)
        coord_font = self._coord_font(18)
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
                file_index = 7 - col if flipped else col
                rank_index = row if flipped else 7 - row
                square = __import__("chess").square(file_index, rank_index)
                piece = engine.board.piece_at(square)
                if piece:
                    draw.text(
                        (x0 + cell / 2, y0 + cell / 2 - 2),
                        symbols[piece.symbol()],
                        font=piece_font,
                        fill="#111111",
                        anchor="mm",
                    )
        self._board_frame(
            draw, (left, top, left + board_size, top + board_size), "#65472F"
        )
        if engine.last_move:
            origin, target = engine.last_move
            for pos, color in ((origin, self.ORIGIN), (target, self.TARGET)):
                if pos:
                    x, rank = pos
                    view_x, view_rank = self._oriented_point(
                        x, rank, 8, 8, flipped
                    )
                    row = 7 - view_rank
                    self._highlight_rect(
                        draw,
                        (
                            left + view_x * cell + 3,
                            top + row * cell + 3,
                            left + (view_x + 1) * cell - 3,
                            top + (row + 1) * cell - 3,
                        ),
                        color,
                    )
        for i in range(8):
            file_label = self._latin_coord(7 - i if flipped else i)
            rank_label = str((i + 1) if flipped else (8 - i))
            draw.text(
                (left + i * cell + cell / 2, top + board_size + 8),
                file_label,
                font=coord_font,
                fill=self.INK,
                anchor="mm",
            )
            draw.text(
                (left + i * cell + cell / 2, top - 12),
                file_label,
                font=coord_font,
                fill=self.INK,
                anchor="mm",
            )
            draw.text(
                (left - 12, top + i * cell + cell / 2),
                rank_label,
                font=coord_font,
                fill=self.INK,
                anchor="rm",
            )
            draw.text(
                (left + board_size + 12, top + i * cell + cell / 2),
                rank_label,
                font=coord_font,
                fill=self.INK,
                anchor="lm",
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
        flipped = self._flipped(session)
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
        left_river, right_river = ("汉 界", "楚 河") if flipped else ("楚 河", "汉 界")
        draw.text(
            (left + 1.5 * cell, top + 4.5 * cell),
            left_river,
            font=river_font,
            fill="#6B4423",
            anchor="mm",
        )
        draw.text(
            (left + 6.5 * cell, top + 4.5 * cell),
            right_river,
            font=river_font,
            fill="#6B4423",
            anchor="mm",
        )
        self._board_frame(
            draw, (left, top, left + width, top + board_height), "#6B4423"
        )
        piece_font = self._font(28)
        for y in range(10):
            for x in range(9):
                piece = engine.board[y][x]
                if not piece:
                    continue
                view_x, view_y = self._oriented_point(x, y, 9, 10, flipped)
                px, py = left + view_x * cell, top + (9 - view_y) * cell
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
                    view_x, view_y = self._oriented_point(x, y, 9, 10, flipped)
                    px, py = left + view_x * cell, top + (9 - view_y) * cell
                    draw.ellipse(
                        (px - 29, py - 29, px + 29, py + 29), outline=color, width=6
                    )
        coord_font = self._coord_font(17)
        for screen_x in range(9):
            logical_x = 8 - screen_x if flipped else screen_x
            label = self._latin_coord(logical_x)
            draw.text(
                (left + screen_x * cell, top + board_height + 40),
                label,
                font=coord_font,
                fill="#684420",
                anchor="mm",
            )
            draw.text(
                (left + screen_x * cell, top - 40),
                label,
                font=coord_font,
                fill="#684420",
                anchor="mm",
            )
        for screen_row in range(10):
            logical_y = screen_row if flipped else 9 - screen_row
            label = str(logical_y)
            draw.text(
                (left - 42, top + screen_row * cell),
                label,
                font=coord_font,
                fill="#684420",
                anchor="mm",
            )
            draw.text(
                (left + width + 42, top + screen_row * cell),
                label,
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
        flipped = self._flipped(session)
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
                    view_x, view_y = self._oriented_point(
                        x, y, engine.size, engine.size, flipped
                    )
                    px = left + view_x * cell
                    py = top + (engine.size - 1 - view_y) * cell
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
        self._board_frame(
            draw, (left, top, left + extent, top + extent), "#6B4423"
        )
        for y in range(engine.size):
            for x in range(engine.size):
                stone = engine.board[y][x]
                if not stone:
                    continue
                view_x, view_y = self._oriented_point(
                    x, y, engine.size, engine.size, flipped
                )
                px = left + view_x * cell
                py = top + (engine.size - 1 - view_y) * cell
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
            view_x, view_y = self._oriented_point(
                x, y, engine.size, engine.size, flipped
            )
            px = left + view_x * cell
            py = top + (engine.size - 1 - view_y) * cell
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
        coord_font = self._coord_font(15)
        for screen_i in range(engine.size):
            logical_i = engine.size - 1 - screen_i if flipped else screen_i
            letter = _GO_LETTERS[logical_i]
            number = str((screen_i + 1) if flipped else (engine.size - screen_i))
            draw.text(
                (left + screen_i * cell, top + extent + cell * 0.65),
                letter,
                font=coord_font,
                fill=self.INK,
                anchor="mm",
            )
            draw.text(
                (left + screen_i * cell, top - cell * 0.65),
                letter,
                font=coord_font,
                fill=self.INK,
                anchor="mm",
            )
            draw.text(
                (left - cell * 0.65, top + screen_i * cell),
                number,
                font=coord_font,
                fill=self.INK,
                anchor="mm",
            )
            draw.text(
                (left + extent + cell * 0.65, top + screen_i * cell),
                number,
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
        flipped = self._flipped(session)
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
        self._board_frame(
            draw, (left, top, left + extent, top + extent), "#6B4423"
        )
        for y in range(engine.size):
            for x in range(engine.size):
                stone = engine.board[y][x]
                if not stone:
                    continue
                view_x, view_y = self._oriented_point(
                    x, y, engine.size, engine.size, flipped
                )
                px = left + view_x * cell
                py = top + (engine.size - 1 - view_y) * cell
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
            view_x, view_y = self._oriented_point(
                x, y, engine.size, engine.size, flipped
            )
            px = left + view_x * cell
            py = top + (engine.size - 1 - view_y) * cell
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
        coord_font = self._coord_font(15)
        for screen_i in range(engine.size):
            logical_i = engine.size - 1 - screen_i if flipped else screen_i
            letter = self._latin_coord(logical_i)
            number = str((screen_i + 1) if flipped else (engine.size - screen_i))
            draw.text(
                (left + screen_i * cell, top + extent + cell * 0.65),
                letter,
                font=coord_font,
                fill=self.INK,
                anchor="mm",
            )
            draw.text(
                (left + screen_i * cell, top - cell * 0.65),
                letter,
                font=coord_font,
                fill=self.INK,
                anchor="mm",
            )
            draw.text(
                (left - cell * 0.65, top + screen_i * cell),
                number,
                font=coord_font,
                fill=self.INK,
                anchor="mm",
            )
            draw.text(
                (left + extent + cell * 0.65, top + screen_i * cell),
                number,
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
        flipped = self._flipped(session)
        for y in range(8):
            for x in range(8):
                x0, y0 = left + x * cell, top + y * cell
                draw.rectangle(
                    (x0, y0, x0 + cell, y0 + cell),
                    fill="#278A4B",
                    outline="#173B25",
                    width=2,
                )
                logical_x, logical_y = self._oriented_point(x, y, 8, 8, flipped)
                stone = engine.board[logical_y][logical_x]
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
            x, y = self._oriented_point(x, y, 8, 8, flipped)
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
        self._board_frame(
            draw, (left, top, left + size, top + size), "#173B25"
        )
        font = self._coord_font(17)
        for i in range(8):
            logical_i = 7 - i if flipped else i
            draw.text(
                (left + i * cell + cell / 2, top + size + 8),
                self._latin_coord(logical_i),
                font=font,
                fill=self.INK,
                anchor="ma",
            )
            draw.text(
                (left - 12, top + i * cell + cell / 2),
                str(logical_i + 1),
                font=font,
                fill=self.INK,
                anchor="rm",
            )
            draw.text(
                (left + i * cell + cell / 2, top - 8),
                self._latin_coord(logical_i),
                font=font,
                fill=self.INK,
                anchor="md",
            )
            draw.text(
                (left + size + 12, top + i * cell + cell / 2),
                str(logical_i + 1),
                font=font,
                fill=self.INK,
                anchor="lm",
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
        flipped = self._flipped(session)
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
        self._board_frame(
            draw, (left, top, left + size, top + size), "#4D4D4D"
        )
        font = self._font(105)
        for y in range(3):
            for x in range(3):
                logical_x, logical_y = self._oriented_point(x, y, 3, 3, flipped)
                value = engine.board[logical_y][logical_x]
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
                    str(logical_y * 3 + logical_x + 1),
                    font=self._font(16),
                    fill="#888",
                )
        if engine.last_move and engine.last_move[1]:
            x, y = engine.last_move[1]
            x, y = self._oriented_point(x, y, 3, 3, flipped)
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
        coord_font = self._coord_font(17)
        for i in range(3):
            logical_i = 2 - i if flipped else i
            draw.text(
                (left + i * cell + cell / 2, top - 12),
                self._latin_coord(logical_i),
                font=coord_font,
                fill=self.INK,
                anchor="md",
            )
            draw.text(
                (left + i * cell + cell / 2, top + size + 12),
                self._latin_coord(logical_i),
                font=coord_font,
                fill=self.INK,
                anchor="ma",
            )
            draw.text(
                (left - 12, top + i * cell + cell / 2),
                str(logical_i + 1),
                font=coord_font,
                fill=self.INK,
                anchor="rm",
            )
            draw.text(
                (left + size + 12, top + i * cell + cell / 2),
                str(logical_i + 1),
                font=coord_font,
                fill=self.INK,
                anchor="lm",
            )
        self._footer(draw, height, session)
        return image
