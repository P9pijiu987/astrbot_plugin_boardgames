from __future__ import annotations

import asyncio
import re
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import astrbot.api.message_components as Comp
from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star

from .boardgames.base import FIRST, SECOND, clean_move_text, opponent
from .boardgames.clock import (
    clock_view,
    create_clock,
    crossed_reminder,
    format_clock,
    parse_reminder_schedule,
    parse_time_control,
    settle_and_switch,
    start_clock,
    swap_clock_sides,
    timed_out_side,
)
from .boardgames.go_engine import GoEngine
from .boardgames.grid_games import GomokuEngine
from .boardgames.registry import (
    GAME_INFO,
    create_engine,
    normalize_game,
    restore_engine,
)
from .boardgames.render import BoardRenderer
from .boardgames.session import GameSession, Player, SessionStore

DEFAULT_TIME_CONTROLS = {
    "chess": "15+10",
    "go": "60|3x30",
    "xiangqi": "10+5",
    "gomoku": "20+3",
    "tictactoe": "2+2",
    "reversi": "10",
}


class BoardGamesPlugin(Star):
    """群聊多棋种对弈：规则、房间、渲染和轻量分析彼此解耦。"""

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.store = SessionStore()
        self.stats: dict[str, Any] = {}
        self.renderer = BoardRenderer(Path(__file__).parent / "assets")
        self.timeout_tasks: dict[str, asyncio.Task] = {}
        self.clock_tasks: dict[str, asyncio.Task] = {}
        self.avatar_cache: dict[str, bytes | None] = {}

    @filter.on_astrbot_loaded()
    async def on_loaded(self):
        """恢复未结束棋局和战绩。"""
        try:
            self.stats = dict(await self.get_kv_data("stats", {}))
            if self.config.get("persist_active_games", True):
                errors = self.store.restore(
                    dict(await self.get_kv_data("active_games", {}))
                )
                for error in errors:
                    logger.warning(f"棋局恢复失败: {error}")
                for session in self.store.sessions.values():
                    if session.status in {"waiting", "choosing"}:
                        self._schedule_wait_timeout(session)
                    elif session.status == "playing":
                        if session.clock is None and not session.clock_disabled:
                            session.clock = self._create_game_clock(session.game_id, None)
                            start_clock(session.clock)
                        self._schedule_clock(session)
            logger.info(f"多棋盘插件已载入，恢复 {len(self.store.sessions)} 局。")
        except Exception as exc:  # noqa: BLE001 - plugin startup must remain recoverable
            logger.exception(f"载入棋局数据失败，将从空状态启动: {exc}")

    async def terminate(self):
        for task in self.timeout_tasks.values():
            if not task.done():
                task.cancel()
        self.timeout_tasks.clear()
        for task in self.clock_tasks.values():
            if not task.done():
                task.cancel()
        self.clock_tasks.clear()
        await self._persist()

    def _key(self, event: AstrMessageEvent) -> str:
        return str(event.unified_msg_origin)

    @staticmethod
    def _avatar_url(event: AstrMessageEvent) -> str:
        message_obj = getattr(event, "message_obj", None)
        sender = getattr(message_obj, "sender", None)
        for source in (sender, getattr(message_obj, "raw_message", None)):
            if source is None:
                continue
            if isinstance(source, dict):
                nested = source.get("sender")
                candidates = [source, nested] if isinstance(nested, dict) else [source]
                for item in candidates:
                    for key in ("avatar_url", "avatar", "icon", "head_url"):
                        value = item.get(key)
                        if isinstance(value, str) and value.startswith(("http://", "https://")):
                            return value
            else:
                for key in ("avatar_url", "avatar", "icon", "head_url"):
                    value = getattr(source, key, None)
                    if isinstance(value, str) and value.startswith(("http://", "https://")):
                        return value
        user_id = str(event.get_sender_id())
        origin = str(getattr(event, "unified_msg_origin", "")).lower()
        raw = getattr(message_obj, "raw_message", None)
        looks_like_qq = "qq" in origin or "aiocqhttp" in origin or (
            isinstance(raw, dict) and "post_type" in raw
        )
        if looks_like_qq and user_id.isdigit():
            return f"https://q1.qlogo.cn/g?b=qq&nk={user_id}&s=160"
        return ""

    @classmethod
    def _player(cls, event: AstrMessageEvent) -> Player:
        return Player(
            str(event.get_sender_id()),
            str(event.get_sender_name() or event.get_sender_id()),
            cls._avatar_url(event),
        )

    @staticmethod
    def _download_avatar(url: str) -> bytes | None:
        if not url:
            return None
        hostname = (urlparse(url).hostname or "").lower()
        allowed_hosts = (
            "qlogo.cn",
            "qpic.cn",
            "qq.com",
            "discord.com",
            "discordapp.com",
            "telegram.org",
            "t.me",
            "gravatar.com",
            "slack-edge.com",
            "larksuitecdn.com",
            "byteimg.com",
            "alicdn.com",
            "kookapp.cn",
        )
        if not any(hostname == host or hostname.endswith(f".{host}") for host in allowed_hosts):
            return None
        try:
            request = urllib.request.Request(
                url,
                headers={"User-Agent": "astrbot-plugin-boardgames/2.4"},
            )
            with urllib.request.urlopen(request, timeout=3) as response:
                data = response.read(2 * 1024 * 1024 + 1)
            return data if len(data) <= 2 * 1024 * 1024 else None
        except Exception:  # noqa: BLE001 - avatar failure must fall back to initials
            return None

    async def _avatars_for(self, session: GameSession) -> dict[str, bytes | None]:
        result: dict[str, bytes | None] = {}
        missing: list[tuple[str, str]] = []
        for side in (FIRST, SECOND):
            player = session.players.get(side)
            if not player:
                continue
            url = player.avatar_url
            if not url:
                result[side] = None
            elif url in self.avatar_cache:
                result[side] = self.avatar_cache[url]
            else:
                missing.append((side, url))
        if missing:
            downloaded = await asyncio.gather(
                *(asyncio.to_thread(self._download_avatar, url) for _, url in missing)
            )
            for (side, url), data in zip(missing, downloaded, strict=True):
                self.avatar_cache[url] = data
                result[side] = data
        return result

    @staticmethod
    def _is_group(event: AstrMessageEvent) -> bool:
        return bool(getattr(event.message_obj, "group_id", ""))

    async def _persist(self) -> None:
        try:
            await self.put_kv_data("stats", self.stats)
            if self.config.get("persist_active_games", True):
                await self.put_kv_data("active_games", self.store.to_dict())
        except Exception as exc:  # noqa: BLE001 - storage backends raise adapter-specific errors
            logger.exception(f"保存棋局数据失败: {exc}")

    async def _render(self, session: GameSession) -> bytes:
        return await asyncio.to_thread(self.renderer.render, session)

    async def _render_match_card(self, session: GameSession) -> bytes:
        avatars = await self._avatars_for(session)
        return await asyncio.to_thread(
            self.renderer.render_match_card, session, self.stats, avatars
        )

    async def _render_result_card(
        self,
        session: GameSession,
        winner: str | None,
        records: dict[str, dict[str, Any]],
        reason: str,
    ) -> bytes:
        avatars = await self._avatars_for(session)
        return await asyncio.to_thread(
            self.renderer.render_result_card,
            session,
            winner,
            records,
            avatars,
            reason,
        )

    @staticmethod
    def _image_result(event: AstrMessageEvent, data: bytes):
        # 故意只放图片组件：避免平台把说明文字和棋盘拼进同一条消息。
        return event.chain_result([Comp.Image.fromBytes(data)])

    def _create_game_clock(
        self, game_id: str, override: str | None
    ) -> dict[str, Any] | None:
        if not self.config.get("enable_clocks", True):
            return None
        profile = override
        if profile is None:
            profile = str(
                self.config.get(
                    f"clock_{game_id}", DEFAULT_TIME_CONTROLS.get(game_id, "10+5")
                )
            )
        return create_clock(parse_time_control(profile))

    def _reminder_schedule(self) -> list[tuple[int, int]]:
        raw = str(
            self.config.get(
                "clock_reminder_schedule",
                "86400:60,180:30,120:15,60:10,30:5",
            )
        )
        try:
            return parse_reminder_schedule(raw)
        except ValueError:
            logger.warning(f"无效的棋钟提醒规则 {raw!r}，已使用默认值。")
            return parse_reminder_schedule("86400:60,180:30,120:15,60:10,30:5")

    def _schedule_clock(self, session: GameSession) -> None:
        old = self.clock_tasks.pop(session.key, None)
        if old and not old.done():
            old.cancel()
        if not session.clock or not session.clock.get("running"):
            return
        self.clock_tasks[session.key] = asyncio.create_task(
            self._clock_watch(session.key)
        )

    def _cancel_clock(self, key: str) -> None:
        task = self.clock_tasks.pop(key, None)
        if task and not task.done() and task is not asyncio.current_task():
            task.cancel()

    async def _send_background_text(self, key: str, text: str) -> None:
        from astrbot.api.event import MessageChain

        await self.context.send_message(key, MessageChain().message(text))

    async def _send_background_image(self, key: str, data: bytes) -> None:
        from astrbot.api.event import MessageChain

        await self.context.send_message(
            key,
            MessageChain(chain=[Comp.Image.fromBytes(data)]),
        )

    @staticmethod
    def _clock_start_text(session: GameSession) -> str:
        label = session.clock.get("label") if session.clock else "不计时"
        if session.clock and session.clock.get("mode") == "byoyomi":
            explanation = f"本局读秒用时：{label}（基本用时 | 读秒次数×每次秒数）。"
        elif session.clock and session.clock.get("increment_seconds", 0):
            explanation = f"本局用时：{label}（每方基础分钟 + 每步加秒）。"
        else:
            explanation = f"本局用时：{label}。"
        return (
            f"{explanation}\n"
            "本局开始后不能修改棋钟；下次开局可追加 计时=15+10、"
            "计时=60|3x30 或 不计时。使用 /计时规则 查看完整说明。"
        )

    async def _clock_watch(self, key: str) -> None:
        schedule = self._reminder_schedule()
        previous = None
        turn_token = None
        try:
            while True:
                await asyncio.sleep(1)
                reminder = None
                timeout_text = None
                timeout_image = None
                async with self.store.lock(key):
                    session = self.store.get(key)
                    if (
                        not session
                        or session.status != "playing"
                        or not session.clock
                        or not session.clock.get("running")
                    ):
                        return
                    loser = timed_out_side(session.clock)
                    if loser:
                        winner_side = opponent(loser)
                        winner = session.players.get(winner_side)
                        loser_player = session.players.get(loser)
                        records = self._record_result(
                            session, winner_side, "timeout"
                        )
                        timeout_image = await self._render_result_card(
                            session,
                            winner_side,
                            records,
                            f"{loser_player.name if loser_player else '败方'}比赛用时耗尽",
                        )
                        self.store.remove(key)
                        await self._persist()
                        timeout_text = (
                            f"⏱ {loser_player.name if loser_player else session.engine.side_names[0 if loser == FIRST else 1]}"
                            f" 用时耗尽，{winner.name if winner else '对手'}获胜。"
                        )
                    else:
                        active = str(session.clock["active"])
                        view = clock_view(session.clock, active)
                        token = (active, session.clock.get("turn_started_at"))
                        current = float(view["seconds_to_flag"])
                        if token != turn_token:
                            turn_token = token
                            previous = current + 1.1
                        boundary = crossed_reminder(
                            float(previous), current, schedule
                        )
                        previous = current
                        if boundary is not None:
                            player = session.players.get(active)
                            side_name = session.engine.side_names[
                                0 if active == FIRST else 1
                            ]
                            reminder = (
                                f"⏱ {player.name if player else side_name}（{side_name}）"
                                f"剩余 {format_clock(session.clock, active).replace('▶ ', '')}"
                            )
                if timeout_text:
                    if timeout_image:
                        await self._send_background_image(key, timeout_image)
                    await self._send_background_text(key, timeout_text)
                    return
                if reminder:
                    await self._send_background_text(key, reminder)
        except asyncio.CancelledError:
            return
        except Exception as exc:  # noqa: BLE001 - background watcher must be recoverable
            logger.exception(f"棋钟任务失败: {exc}")
        finally:
            if self.clock_tasks.get(key) is asyncio.current_task():
                self.clock_tasks.pop(key, None)

    def _schedule_wait_timeout(self, session: GameSession) -> None:
        old = self.timeout_tasks.pop(session.key, None)
        if old and not old.done():
            old.cancel()
        self.timeout_tasks[session.key] = asyncio.create_task(
            self._wait_timeout(session.key, session.created_at)
        )

    async def _wait_timeout(self, key: str, created_at: float) -> None:
        minutes = max(1, int(self.config.get("waiting_timeout_minutes", 10)))
        elapsed = max(0.0, time.time() - created_at)
        try:
            await asyncio.sleep(max(0.0, minutes * 60 - elapsed))
            session = self.store.get(key)
            if (
                not session
                or session.created_at != created_at
                or session.status not in {"waiting", "choosing"}
            ):
                return
            self.store.remove(key)
            await self._persist()
            from astrbot.api.event import MessageChain

            await self.context.send_message(
                key,
                MessageChain().message(
                    f"开局后 {minutes} 分钟内未完成加入与选色，棋局已自动取消。"
                ),
            )
        except asyncio.CancelledError:
            return
        except Exception as exc:  # noqa: BLE001 - background tasks must not crash the plugin
            logger.exception(f"等待加入超时任务失败: {exc}")
        finally:
            self.timeout_tasks.pop(key, None)

    def _cancel_timeout(self, key: str) -> None:
        task = self.timeout_tasks.pop(key, None)
        if task and not task.done():
            task.cancel()

    def _parse_side(self, value: str, side_names: tuple[str, str]) -> str | None:
        token = value.strip().replace("棋", "").replace("方", "")
        first_name = side_names[0].replace("棋", "").replace("方", "").replace(" ", "")
        second_name = side_names[1].replace("棋", "").replace("方", "").replace(" ", "")
        if token in {"先", "先手", "第一", first_name}:
            return FIRST
        if token in {"后", "后手", "第二", second_name}:
            return SECOND
        return None

    async def _start_game_impl(
        self, event: AstrMessageEvent, game_text: str, options: list[str]
    ):
        if not self._is_group(event):
            return "请在群聊中开局。", None
        game_id = normalize_game(game_text)
        if not game_id:
            return (
                "不支持该棋种。可选：国际象棋、围棋、中国象棋、五子棋、井字棋、黑白棋。",
                None,
            )
        key = self._key(event)
        async with self.store.lock(key):
            if self.store.get(key):
                return "当前群已经有一局棋，请先结束或取消。", None
            side_names = tuple(GAME_INFO[game_id]["sides"])
            size = None
            clock_override = None
            gomoku_rule = "freestyle"
            gomoku_opening = "normal"
            gomoku_rules = {
                "自由": "freestyle",
                "自由规则": "freestyle",
                "无禁手": "freestyle",
                "freestyle": "freestyle",
                "标准": "standard",
                "标准规则": "standard",
                "恰好五连": "standard",
                "standard": "standard",
                "连珠": "renju",
                "连珠规则": "renju",
                "有禁手": "renju",
                "禁手": "renju",
                "renju": "renju",
            }
            gomoku_openings = {
                "swap2": "swap2",
                "交换2": "swap2",
                "交换二": "swap2",
                "平衡开局": "swap2",
            }
            for option in options:
                if not option:
                    continue
                token = option.strip().lower()
                if game_id == "gomoku" and token in gomoku_rules:
                    gomoku_rule = gomoku_rules[token]
                    continue
                if game_id == "gomoku" and token in gomoku_openings:
                    gomoku_opening = gomoku_openings[token]
                    continue
                if token in {"不计时", "关闭计时", "off"}:
                    clock_override = "不计时"
                    continue
                clock_match = re.fullmatch(r"(?:计时|用时)=(.+)", token)
                if clock_match:
                    clock_override = clock_match.group(1)
                    continue
                side = self._parse_side(option, side_names)
                if side:
                    return "颜色不再在开局时指定；第二位玩家加入后，任一选手使用 /选先 或 /选后，另一人会自动分到相反阵营。", None
                match = re.fullmatch(r"(\d{1,2})路?", option)
                if match:
                    size = int(match.group(1))
                    continue
                return (
                    f"无法识别开局参数“{option}”。可使用棋盘路数、计时=15+10、计时=60|3x30 或 不计时；五子棋还可选自由/标准/连珠及 Swap2。",
                    None,
                )
            try:
                engine = create_engine(
                    game_id,
                    size,
                    go_komi=float(self.config.get("go_komi", 6.5)),
                    gomoku_rule=gomoku_rule,
                    gomoku_opening=gomoku_opening,
                )
                game_clock = self._create_game_clock(game_id, clock_override)
            except ValueError as exc:
                return str(exc), None
            players = {FIRST: None, SECOND: None}
            # 等待/选色阶段两侧只是参与者槽位，不代表最终棋色。
            players[FIRST] = self._player(event)
            session = GameSession(
                key,
                game_id,
                engine,
                players,
                clock=game_clock,
                clock_disabled=game_clock is None,
            )
            self.store.put(session)
            self._schedule_wait_timeout(session)
            await self._persist()
            return None, await self._render(session)

    @filter.command("开局")
    async def start_game(
        self,
        event: AstrMessageEvent,
        game_type: str = "",
        option1: str = "",
        option2: str = "",
        option3: str = "",
        option4: str = "",
        option5: str = "",
        option6: str = "",
    ):
        """开一局棋。例如：/开局 国际象棋 计时=15+10、/开局 围棋 13路。"""
        if not game_type:
            yield event.plain_result(
                "用法：/开局 [棋种] [路数] [计时=15+10 / 计时=60|3x30 / 不计时]\n"
                "五子棋示例：/开局 五子棋 连珠 Swap2 计时=20+3"
            )
            return
        error, image = await self._start_game_impl(
            event, game_type, [option1, option2, option3, option4, option5, option6]
        )
        if error:
            yield event.plain_result(error)
        else:
            yield self._image_result(event, image)

    @filter.regex(
        r"^/开局(国际象棋|西洋棋|围棋|中国象棋|象棋|五子棋|井字棋|黑白棋)(?:\s+(.*))?$",
        priority=20,
    )
    async def start_game_compact(self, event: AstrMessageEvent, match: re.Match):
        """兼容 /开局国际象棋 这种紧凑写法。"""
        options = (match.group(2) or "").split()
        error, image = await self._start_game_impl(event, match.group(1), options[:6])
        event.stop_event()
        if error:
            yield event.plain_result(error)
        else:
            yield self._image_result(event, image)

    @filter.command("加入棋局", alias={"加入对局"})
    async def join_game(self, event: AstrMessageEvent):
        """加入当前群正在等待的棋局。"""
        key = self._key(event)
        async with self.store.lock(key):
            session = self.store.get(key)
            if not session or session.status != "waiting":
                yield event.plain_result("当前没有等待加入的棋局。")
                return
            player = self._player(event)
            if session.side_for(player.user_id):
                yield event.plain_result("不能自己和自己对弈。")
                return
            empty_side = FIRST if session.players[FIRST] is None else SECOND
            session.players[empty_side] = player
            is_swap2 = (
                isinstance(session.engine, GomokuEngine)
                and session.engine.opening == "swap2"
            )
            session.status = "playing" if is_swap2 else "choosing"
            session.last_action_at = time.time()
            if is_swap2:
                start_clock(session.clock)
                self._cancel_timeout(key)
                self._schedule_clock(session)
            await self._persist()
            board_image = await self._render(session)
            match_card = await self._render_match_card(session) if is_swap2 else None
            start_text = self._clock_start_text(session) if is_swap2 else ""
        if match_card:
            yield self._image_result(event, match_card)
        yield self._image_result(event, board_image)
        if start_text:
            yield event.plain_result(start_text)

    async def _choose_regular_side_impl(
        self, event: AstrMessageEvent, desired_side: str
    ) -> tuple[str | None, bytes | None, bytes | None, str]:
        key = self._key(event)
        async with self.store.lock(key):
            session = self.store.get(key)
            if not session or session.status != "choosing":
                return "当前没有等待选色的棋局。", None, None, ""
            provisional_side = session.side_for(str(event.get_sender_id()))
            if not provisional_side:
                return "只有本局两位参与者可以选色。", None, None, ""
            other = session.other_player(provisional_side)
            if not other:
                return "请等待第二位玩家加入后再选色。", None, None, ""
            user_id = str(event.get_sender_id())
            session.side_choices[user_id] = desired_side
            session.side_choices[other.user_id] = opponent(desired_side)
            chooser = session.players[provisional_side]
            session.players = {
                desired_side: chooser,
                opponent(desired_side): other,
            }
            session.status = "playing"
            session.last_action_at = time.time()
            start_clock(session.clock)
            self._cancel_timeout(key)
            self._schedule_clock(session)
            await self._persist()
            match_card = await self._render_match_card(session)
            board_image = await self._render(session)
            return (
                None,
                match_card,
                board_image,
                self._clock_start_text(session),
            )

    def _side_for_color(self, session: GameSession, color: str) -> str | None:
        for side, name in zip((FIRST, SECOND), session.engine.side_names, strict=True):
            if color.upper() in name.upper():
                return side
        return None

    async def _choose_named_side(self, event: AstrMessageEvent, color: str):
        session = self.store.get(self._key(event))
        if not session:
            yield event.plain_result("当前没有棋局。")
            return
        if session.status == "choosing":
            desired = self._side_for_color(session, color)
            if not desired:
                yield event.plain_result(
                    f"本棋种没有“{color}方”；请使用 /选先 或 /选后。"
                )
                return
            error, match_card, board_image, start_text = (
                await self._choose_regular_side_impl(event, desired)
            )
            if error:
                yield event.plain_result(error)
            else:
                yield self._image_result(event, match_card)
                yield self._image_result(event, board_image)
                yield event.plain_result(start_text)
            return
        if (
            color in {"白", "黑"}
            and isinstance(session.engine, GomokuEngine)
            and session.engine.opening == "swap2"
        ):
            async for result in self._yield_swap2_choice(
                event, "white" if color == "白" else "black"
            ):
                yield result
            return
        yield event.plain_result("当前不需要进行该选色操作。")

    async def _swap2_choice_impl(
        self, event: AstrMessageEvent, choice: str
    ) -> tuple[str | None, bytes | None, bytes | None]:
        key = self._key(event)
        async with self.store.lock(key):
            session = self.store.get(key)
            if (
                not session
                or session.status != "playing"
                or not isinstance(session.engine, GomokuEngine)
                or session.engine.opening != "swap2"
            ):
                return "当前没有进行 Swap2 开局的五子棋对局。", None, None
            side = session.side_for(str(event.get_sender_id()))
            if not side:
                return "只有本局选手可以进行 Swap2 选色。", None, None
            if side != session.engine.turn:
                return (
                    session.engine.opening_prompt or "现在不需要你进行选择。",
                    None,
                    None,
                )
            loser = timed_out_side(session.clock)
            if loser:
                winner_side = opponent(loser)
                records = self._record_result(session, winner_side, "timeout")
                result_card = await self._render_result_card(
                    session, winner_side, records, "Swap2 选色前比赛用时耗尽"
                )
                self.store.remove(key)
                self._cancel_clock(key)
                await self._persist()
                return "你的比赛用时已经耗尽，对手获胜。", None, result_card
            moment = time.time()
            outcome = session.engine.choose_opening(choice)
            if not outcome.ok:
                return outcome.message, None, None
            clock_loser = settle_and_switch(
                session.clock,
                session.engine.turn,
                moment,
                add_increment=False,
            )
            if clock_loser:
                winner_side = opponent(clock_loser)
                records = self._record_result(session, winner_side, "timeout")
                result_card = await self._render_result_card(
                    session, winner_side, records, "Swap2 选色时比赛用时耗尽"
                )
                self.store.remove(key)
                self._cancel_clock(key)
                await self._persist()
                return "你的比赛用时已经耗尽，对手获胜。", None, result_card
            if outcome.extra.get("swap_players"):
                session.players[FIRST], session.players[SECOND] = (
                    session.players[SECOND],
                    session.players[FIRST],
                )
                swap_clock_sides(session.clock)
                if session.clock:
                    session.clock["active"] = session.engine.turn
                    session.clock["turn_started_at"] = moment
            session.pending = None
            session.last_action_at = moment
            await self._persist()
            return None, await self._render(session), None

    async def _yield_swap2_choice(self, event: AstrMessageEvent, choice: str):
        error, image, result_card = await self._swap2_choice_impl(event, choice)
        if error:
            if result_card:
                yield self._image_result(event, result_card)
            yield event.plain_result(error)
        else:
            yield self._image_result(event, image)

    @filter.command("选白")
    async def choose_white(self, event: AstrMessageEvent):
        async for result in self._choose_named_side(event, "白"):
            yield result

    @filter.command("选黑")
    async def choose_black(self, event: AstrMessageEvent):
        async for result in self._choose_named_side(event, "黑"):
            yield result

    @filter.command("选红")
    async def choose_red(self, event: AstrMessageEvent):
        async for result in self._choose_named_side(event, "红"):
            yield result

    @filter.command("选先")
    async def choose_first(self, event: AstrMessageEvent):
        error, match_card, board_image, start_text = (
            await self._choose_regular_side_impl(event, FIRST)
        )
        if error:
            yield event.plain_result(error)
        else:
            yield self._image_result(event, match_card)
            yield self._image_result(event, board_image)
            yield event.plain_result(start_text)

    @filter.command("选后")
    async def choose_second(self, event: AstrMessageEvent):
        error, match_card, board_image, start_text = (
            await self._choose_regular_side_impl(event, SECOND)
        )
        if error:
            yield event.plain_result(error)
        else:
            yield self._image_result(event, match_card)
            yield self._image_result(event, board_image)
            yield event.plain_result(start_text)

    @filter.command("交换")
    async def swap2_swap(self, event: AstrMessageEvent):
        async for result in self._yield_swap2_choice(event, "swap"):
            yield result

    @filter.command("加两子")
    async def swap2_add_two(self, event: AstrMessageEvent):
        async for result in self._yield_swap2_choice(event, "add_two"):
            yield result

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE, priority=10)
    async def receive_bare_move(self, event: AstrMessageEvent):
        """接收 /Nc3、/b1 c3、Nc3、b1c3 以及各棋种坐标。"""
        key = self._key(event)
        session = self.store.get(key)
        if not session:
            return
        raw = (event.message_str or "").strip()
        if (
            not self.config.get("enable_bare_moves", True)
            and not raw.startswith("/")
            and not raw.startswith("下棋")
        ):
            return
        if not raw or not session.engine.move_candidate(raw):
            return
        cleaned, explicit = clean_move_text(raw)
        side = session.side_for(str(event.get_sender_id()))
        if not side:
            if explicit:
                event.stop_event()
                yield event.plain_result("你不是本局选手。")
            return
        event.stop_event()
        async with self.store.lock(key):
            session = self.store.get(key)
            if not session:
                yield event.plain_result("棋局已经结束。")
                return
            if session.status != "playing":
                if session.status == "choosing":
                    yield event.plain_result(
                        "尚未选色；任一选手使用 /选先 或 /选后，"
                        "对手会自动分配另一方。"
                    )
                else:
                    yield event.plain_result("还在等待另一位玩家加入。")
                return
            if session.engine.turn != side:
                turn_name = session.engine.side_names[
                    0 if session.engine.turn == FIRST else 1
                ]
                yield event.plain_result(f"现在轮到{turn_name}。")
                return
            loser = timed_out_side(session.clock)
            if loser:
                winner_side = opponent(loser)
                records = self._record_result(session, winner_side, "timeout")
                result_card = await self._render_result_card(
                    session,
                    winner_side,
                    records,
                    "行棋前比赛用时已经耗尽",
                )
                self.store.remove(key)
                self._cancel_clock(key)
                await self._persist()
                yield self._image_result(event, result_card)
                yield event.plain_result("你的比赛用时已经耗尽，对手获胜。")
                return
            moment = time.time()
            outcome = session.engine.play(cleaned)
            if not outcome.ok:
                yield event.plain_result(outcome.message)
                return
            clock_loser = settle_and_switch(session.clock, session.engine.turn, moment)
            if clock_loser:
                winner_side = opponent(clock_loser)
                records = self._record_result(session, winner_side, "timeout")
                result_card = await self._render_result_card(
                    session,
                    winner_side,
                    records,
                    "行棋完成前比赛用时耗尽",
                )
                self.store.remove(key)
                self._cancel_clock(key)
                await self._persist()
                yield self._image_result(event, result_card)
                yield event.plain_result("行棋完成前比赛用时耗尽，对手获胜。")
                return
            session.last_action_at = moment
            session.pending = None
            image = await self._render(session)
            end_text = ""
            result_card = None
            if outcome.ended:
                if outcome.draw:
                    end_text = outcome.message or "和棋。"
                    records = self._record_result(session, None, "rules")
                    result_card = await self._render_result_card(
                        session, None, records, end_text
                    )
                else:
                    winner = session.players[outcome.winner]
                    end_text = (
                        f"{outcome.message} {winner.name if winner else '胜方'}获胜。"
                    )
                    records = self._record_result(
                        session, outcome.winner, "rules"
                    )
                    result_card = await self._render_result_card(
                        session,
                        outcome.winner,
                        records,
                        outcome.message or "规则终局",
                    )
                self._cancel_clock(key)
                self.store.remove(key)
            await self._persist()
        yield self._image_result(event, image)
        if end_text:
            yield self._image_result(event, result_card)
            yield event.plain_result(end_text)

    @filter.command("下棋")
    async def move_command_fallback(
        self, event: AstrMessageEvent, part1: str = "", part2: str = ""
    ):
        """为缺少参数或尚未开局的 /下棋 提供明确提示。"""
        if not part1:
            yield event.plain_result("请在 /下棋 后输入走法，例如 /下棋 Nc3。")
            return
        if not self.store.get(self._key(event)):
            yield event.plain_result("当前没有棋局，请先使用 /开局 [棋种]。")

    def _record_result(
        self, session: GameSession, winner: str | None, reason: str = "completed"
    ) -> dict[str, dict[str, Any]]:
        now = int(time.time())
        max_history = max(1, int(self.config.get("max_history_per_user", 50)))
        records: dict[str, dict[str, Any]] = {}
        for side in (FIRST, SECOND):
            player = session.players.get(side)
            rival = session.players.get(opponent(side))
            if not player:
                continue
            user = self.stats.setdefault(player.user_id, {})
            game = user.setdefault(
                session.game_id,
                {
                    "wins": 0,
                    "draws": 0,
                    "losses": 0,
                    "rating": 1000,
                    "player_name": player.name,
                    "history": [],
                },
            )
            game.setdefault("rating", 1000)
            game.setdefault("wins", 0)
            game.setdefault("draws", 0)
            game.setdefault("losses", 0)
            game.setdefault("history", [])
            game["player_name"] = player.name
            game["avatar_url"] = player.avatar_url
            records[side] = game

        ratings_before = {
            side: float(records.get(side, {}).get("rating", 1000))
            for side in (FIRST, SECOND)
        }
        if FIRST in records and SECOND in records:
            score_first = 0.5 if winner is None else 1.0 if winner == FIRST else 0.0
            expected_first = 1.0 / (
                1.0 + 10 ** ((ratings_before[SECOND] - ratings_before[FIRST]) / 400.0)
            )
            delta = round(32 * (score_first - expected_first))
            records[FIRST]["rating"] = max(100, round(ratings_before[FIRST] + delta))
            records[SECOND]["rating"] = max(
                100, round(ratings_before[SECOND] - delta)
            )

        for side in (FIRST, SECOND):
            player = session.players.get(side)
            rival = session.players.get(opponent(side))
            game = records.get(side)
            if not player or not game:
                continue
            result = "draw" if winner is None else "win" if winner == side else "loss"
            counter = {"win": "wins", "draw": "draws", "loss": "losses"}[result]
            game[counter] += 1
            item = {
                    "time": now,
                    "result": result,
                    "side": side,
                    "opponent_id": rival.user_id if rival else "",
                    "opponent_name": rival.name if rival else "",
                    "moves": len(session.engine.notation_history()),
                    "duration_seconds": max(0, now - int(session.created_at)),
                    "reason": reason,
                    "rating_before": round(ratings_before[side]),
                    "rating_after": int(game.get("rating", 1000)),
                    "time_control": (
                        str(session.clock.get("label", ""))
                        if session.clock
                        else "不计时"
                    ),
                }
            game["history"].append(item)
            game["history"] = game["history"][-max_history:]
        return {
            side: dict(records[side]["history"][-1])
            for side in (FIRST, SECOND)
            if side in records and records[side].get("history")
        }

    def _require_player(
        self, event: AstrMessageEvent
    ) -> tuple[GameSession | None, str | None, str | None]:
        session = self.store.get(self._key(event))
        if not session or session.status != "playing":
            return None, None, "当前没有进行中的棋局。"
        side = session.side_for(str(event.get_sender_id()))
        if not side:
            return session, None, "只有本局选手可以操作。"
        return session, side, None

    @filter.command("悔棋", alias={"请求悔棋"})
    async def request_undo(self, event: AstrMessageEvent):
        """请求悔棋；需对手同意。"""
        session, side, error = self._require_player(event)
        if error:
            yield event.plain_result(error)
            return
        if (
            isinstance(session.engine, GomokuEngine)
            and session.engine.opening == "swap2"
            and (
                session.engine.opening_phase != "normal"
                or len(session.engine.moves) <= session.engine.normal_move_start
            )
        ):
            yield event.plain_result("Swap2 摆子和选色阶段不能悔棋；完成选色并正常行棋后才可请求。")
            return
        if not session.engine.notation_history():
            yield event.plain_result("当前没有可以撤回的走法。")
            return
        session.pending = {
            "type": "undo",
            "requester": str(event.get_sender_id()),
            "side": side,
            "created_at": time.time(),
        }
        await self._persist()
        yield event.plain_result("已请求悔棋。对手请使用 /同意悔棋 或 /拒绝悔棋。")

    @filter.command("同意悔棋")
    async def agree_undo(self, event: AstrMessageEvent):
        session, _, error = self._require_player(event)
        if error:
            yield event.plain_result(error)
            return
        pending = session.pending or {}
        if pending.get("type") != "undo" or pending.get("requester") == str(
            event.get_sender_id()
        ):
            yield event.plain_result("没有可同意的悔棋请求。")
            return
        requester_side = pending.get("side")
        if not session.engine.undo(1):
            yield event.plain_result("悔棋失败：历史步数不足。")
            return
        can_undo_again = bool(session.engine.notation_history())
        if isinstance(session.engine, GomokuEngine):
            can_undo_again = (
                len(session.engine.moves) > session.engine.normal_move_start
            )
        if session.engine.turn != requester_side and can_undo_again:
            session.engine.undo(1)
        clock_loser = settle_and_switch(
            session.clock, session.engine.turn, add_increment=False
        )
        if clock_loser:
            winner_side = opponent(clock_loser)
            records = self._record_result(session, winner_side, "timeout")
            result_card = await self._render_result_card(
                session,
                winner_side,
                records,
                "处理悔棋请求时比赛用时耗尽",
            )
            self._cancel_clock(session.key)
            self.store.remove(session.key)
            await self._persist()
            yield self._image_result(event, result_card)
            yield event.plain_result("处理悔棋请求时比赛用时耗尽，对手获胜。")
            return
        session.pending = None
        session.last_action_at = time.time()
        await self._persist()
        yield self._image_result(event, await self._render(session))

    @filter.command("拒绝悔棋")
    async def deny_undo(self, event: AstrMessageEvent):
        session, _, error = self._require_player(event)
        if error:
            yield event.plain_result(error)
            return
        pending = session.pending or {}
        if pending.get("type") != "undo" or pending.get("requester") == str(
            event.get_sender_id()
        ):
            yield event.plain_result("没有可拒绝的悔棋请求。")
            return
        session.pending = None
        await self._persist()
        yield event.plain_result("已拒绝悔棋，棋局继续。")

    @filter.command("和棋")
    async def offer_draw(self, event: AstrMessageEvent):
        """提议和棋；需对手同意。"""
        session, side, error = self._require_player(event)
        if error:
            yield event.plain_result(error)
            return
        session.pending = {
            "type": "draw",
            "requester": str(event.get_sender_id()),
            "side": side,
            "created_at": time.time(),
        }
        await self._persist()
        yield event.plain_result("已提议和棋。对手请使用 /同意和棋 或 /拒绝和棋。")

    @filter.command("同意和棋")
    async def agree_draw(self, event: AstrMessageEvent):
        session, _, error = self._require_player(event)
        if error:
            yield event.plain_result(error)
            return
        pending = session.pending or {}
        if pending.get("type") != "draw" or pending.get("requester") == str(
            event.get_sender_id()
        ):
            yield event.plain_result("没有可同意的和棋提议。")
            return
        records = self._record_result(session, None, "agreement")
        result_card = await self._render_result_card(
            session, None, records, "双方同意和棋"
        )
        self._cancel_clock(session.key)
        self.store.remove(session.key)
        await self._persist()
        yield self._image_result(event, result_card)
        yield event.plain_result("双方同意和棋，棋局结束。")

    @filter.command("拒绝和棋")
    async def deny_draw(self, event: AstrMessageEvent):
        session, _, error = self._require_player(event)
        if error:
            yield event.plain_result(error)
            return
        pending = session.pending or {}
        if pending.get("type") != "draw" or pending.get("requester") == str(
            event.get_sender_id()
        ):
            yield event.plain_result("没有可拒绝的和棋提议。")
            return
        session.pending = None
        await self._persist()
        yield event.plain_result("已拒绝和棋，棋局继续。")

    @filter.command("流局")
    async def abort_game(self, event: AstrMessageEvent):
        """等待阶段直接取消；对弈中需双方同意。"""
        key = self._key(event)
        session = self.store.get(key)
        if not session:
            yield event.plain_result("当前没有棋局。")
            return
        side = session.side_for(str(event.get_sender_id()))
        if session.status == "waiting":
            self.store.remove(key)
            self._cancel_timeout(key)
            self._cancel_clock(key)
            await self._persist()
            yield event.plain_result("等待加入的空房已取消。")
            return
        if session.status == "choosing" and side:
            self.store.remove(key)
            self._cancel_timeout(key)
            self._cancel_clock(key)
            await self._persist()
            yield event.plain_result("棋局已取消。")
            return
        if not side:
            yield event.plain_result("只有本局选手可以申请流局。")
            return
        session.pending = {
            "type": "abort",
            "requester": str(event.get_sender_id()),
            "created_at": time.time(),
        }
        await self._persist()
        yield event.plain_result("已申请流局。对手请使用 /同意流局 或 /拒绝流局。")

    @filter.command("同意流局")
    async def agree_abort(self, event: AstrMessageEvent):
        session, _, error = self._require_player(event)
        if error:
            yield event.plain_result(error)
            return
        pending = session.pending or {}
        if pending.get("type") != "abort" or pending.get("requester") == str(
            event.get_sender_id()
        ):
            yield event.plain_result("没有可同意的流局请求。")
            return
        self.store.remove(session.key)
        self._cancel_clock(session.key)
        await self._persist()
        yield event.plain_result("双方同意流局，棋局结束且不计战绩。")

    @filter.command("拒绝流局")
    async def deny_abort(self, event: AstrMessageEvent):
        session, _, error = self._require_player(event)
        if error:
            yield event.plain_result(error)
            return
        pending = session.pending or {}
        if pending.get("type") != "abort" or pending.get("requester") == str(
            event.get_sender_id()
        ):
            yield event.plain_result("没有可拒绝的流局请求。")
            return
        session.pending = None
        await self._persist()
        yield event.plain_result("已拒绝流局，棋局继续。")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("强制流局")
    async def force_abort(self, event: AstrMessageEvent):
        """管理员清理卡住的棋局，不计入战绩。"""
        key = self._key(event)
        if not self.store.get(key):
            yield event.plain_result("当前没有棋局。")
            return
        self.store.remove(key)
        self._cancel_timeout(key)
        self._cancel_clock(key)
        await self._persist()
        yield event.plain_result("管理员已强制结束棋局，本局不计战绩。")

    @filter.command("认输")
    async def resign(self, event: AstrMessageEvent):
        """认输并结束棋局。"""
        session, side, error = self._require_player(event)
        if error:
            yield event.plain_result(error)
            return
        winner_side = opponent(side)
        winner = session.players[winner_side]
        records = self._record_result(session, winner_side, "resign")
        result_card = await self._render_result_card(
            session,
            winner_side,
            records,
            f"{event.get_sender_name()} 认输",
        )
        self._cancel_clock(session.key)
        self.store.remove(session.key)
        await self._persist()
        yield self._image_result(event, result_card)
        yield event.plain_result(
            f"{event.get_sender_name()} 认输，{winner.name if winner else '对手'}获胜。"
        )

    @filter.command("强制胜利")
    async def force_win(self, event: AstrMessageEvent):
        """对手超时且正轮到对手时，判请求方胜。"""
        session, side, error = self._require_player(event)
        if error:
            yield event.plain_result(error)
            return
        if session.engine.turn == side:
            yield event.plain_result("当前轮到你行棋，不能对自己申请超时胜。")
            return
        if session.clock:
            loser = timed_out_side(session.clock)
            if loser is None:
                active = str(session.clock["active"])
                yield event.plain_result(
                    "比赛棋钟会在耗尽时自动判负，无需申请。"
                    f"对手当前剩余 {format_clock(session.clock, active).replace('▶ ', '')}。"
                )
                return
            winner_side = opponent(loser)
            records = self._record_result(session, winner_side, "timeout")
            result_card = await self._render_result_card(
                session, winner_side, records, "比赛用时耗尽"
            )
            self._cancel_clock(session.key)
            self.store.remove(session.key)
            await self._persist()
            winner = session.players.get(winner_side)
            yield self._image_result(event, result_card)
            yield event.plain_result(
                f"对手比赛用时耗尽，{winner.name if winner else '你'}获胜。"
            )
            return
        minutes = max(1, int(self.config.get("turn_timeout_minutes", 10)))
        remaining = minutes * 60 - (time.time() - session.last_action_at)
        if remaining > 0:
            yield event.plain_result(
                f"对手尚未超时，还需等待约 {int(remaining // 60) + 1} 分钟。"
            )
            return
        records = self._record_result(session, side, "turn_timeout")
        result_card = await self._render_result_card(
            session,
            side,
            records,
            f"对手超过 {minutes} 分钟未行棋",
        )
        self._cancel_clock(session.key)
        self.store.remove(session.key)
        await self._persist()
        yield self._image_result(event, result_card)
        yield event.plain_result(
            f"对手超过 {minutes} 分钟未行棋，判 {event.get_sender_name()} 获胜。"
        )

    @filter.command("AI分析", alias={"ai分析", "局势分析", "势力范围", "围棋势力"})
    async def request_analysis(self, event: AstrMessageEvent):
        """申请一次轻量局势评估；对手同意后执行。"""
        if not self.config.get("enable_ai_analysis", True):
            yield event.plain_result("管理员已关闭自动分析。")
            return
        session, side, error = self._require_player(event)
        if error:
            yield event.plain_result(error)
            return
        session.pending = {
            "type": "analysis",
            "requester": str(event.get_sender_id()),
            "side": side,
            "created_at": time.time(),
        }
        await self._persist()
        yield event.plain_result("已申请一次局势分析。对手请使用 /同意AI 或 /拒绝AI。")

    @filter.command("同意AI", alias={"同意ai"})
    async def agree_analysis(self, event: AstrMessageEvent):
        key = self._key(event)
        async with self.store.lock(key):
            session, _, error = self._require_player(event)
            if error:
                yield event.plain_result(error)
                return
            pending = session.pending or {}
            if pending.get("type") != "analysis" or pending.get("requester") == str(
                event.get_sender_id()
            ):
                yield event.plain_result("没有可同意的分析请求。")
                return
            # 在锁内复制局面，在线程中只分析副本，避免与同时到达的走子互相修改。
            analysis_engine = restore_engine(session.engine.to_dict())
            analysis_session = GameSession(
                session.key,
                session.game_id,
                analysis_engine,
                dict(session.players),
                status=session.status,
                created_at=session.created_at,
                last_action_at=session.last_action_at,
                side_choices=dict(session.side_choices),
                clock=dict(session.clock) if session.clock else None,
                clock_disabled=session.clock_disabled,
            )
            session.pending = None
            await self._persist()
        analysis = await asyncio.to_thread(analysis_engine.analyze)
        lines = [analysis.summary]
        if analysis.first_win_rate is not None:
            first_rate = max(0.0, min(100.0, analysis.first_win_rate))
            lines.append(
                f"胜率对比：{analysis_engine.side_names[0]} {first_rate:.1f}% · "
                f"{analysis_engine.side_names[1]} {100.0 - first_rate:.1f}%"
            )
        if analysis.recommended:
            lines.append(f"推荐走法：{analysis.recommended}")
        lines.append("说明：这是本地低开销启发式分析，不等同于 Stockfish、KataGo 等专业引擎。")
        yield event.plain_result("\n".join(lines))
        if isinstance(analysis_engine, GoEngine):
            influence_image = await asyncio.to_thread(
                self.renderer.render_go_influence, analysis_session
            )
            # 势力图独立成一条纯图片消息，不把说明文字混入消息链。
            yield self._image_result(event, influence_image)

    @filter.command("拒绝AI", alias={"拒绝ai"})
    async def deny_analysis(self, event: AstrMessageEvent):
        session, _, error = self._require_player(event)
        if error:
            yield event.plain_result(error)
            return
        pending = session.pending or {}
        if pending.get("type") != "analysis" or pending.get("requester") == str(
            event.get_sender_id()
        ):
            yield event.plain_result("没有可拒绝的分析请求。")
            return
        session.pending = None
        await self._persist()
        yield event.plain_result("已拒绝本次分析请求。")

    @filter.command("棋盘", alias={"棋局"})
    async def show_board(self, event: AstrMessageEvent):
        """重新发送当前棋盘（只发送图片）。"""
        key = self._key(event)
        async with self.store.lock(key):
            session = self.store.get(key)
            if not session:
                yield event.plain_result("当前没有棋局。")
                return
            image = await self._render(session)
        yield self._image_result(event, image)

    @filter.command("棋钟", alias={"时间", "剩余时间"})
    async def show_clock(self, event: AstrMessageEvent):
        """查看当前双方棋钟。"""
        session = self.store.get(self._key(event))
        if not session:
            yield event.plain_result("当前没有棋局。")
            return
        if not session.clock:
            yield event.plain_result("本局不计时。")
            return
        first = session.players.get(FIRST)
        second = session.players.get(SECOND)
        state = "运行中" if session.clock.get("running") else "尚未开始"
        yield event.plain_result(
            f"棋钟 {session.clock.get('label', '')} · {state}\n"
            f"{session.engine.side_names[0]} {first.name if first else '—'}："
            f"{format_clock(session.clock, FIRST)}\n"
            f"{session.engine.side_names[1]} {second.name if second else '—'}："
            f"{format_clock(session.clock, SECOND)}"
        )

    @filter.command("计时规则", alias={"棋钟帮助"})
    async def clock_help(self, event: AstrMessageEvent):
        """显示棋钟格式、默认值和提醒策略。"""
        yield event.plain_result(
            "棋钟在双方完成选边后启动，用尽自动判负，并显示在每张棋盘图顶部。\n"
            "开局可覆盖默认值：\n"
            "/开局 国际象棋 计时=15+10（每方15分钟，每步加10秒）\n"
            "/开局 围棋 计时=60|3x30（60分钟，随后3次30秒读秒）\n"
            "/开局 黑白棋 计时=10（每方包干10分钟）\n"
            "/开局 五子棋 不计时\n"
            "默认：国际象棋15+10、围棋60|3x30、中国象棋10+5、"
            "五子棋20+3、井字棋2+2、黑白棋10。管理员可在插件配置中逐项修改。\n"
            "默认提醒：全程每分钟、3分钟内每30秒、2分钟内每15秒、"
            "1分钟内每10秒、30秒内每5秒；提醒档位也可在插件配置中直接编辑。"
        )

    @filter.command("围棋计分", alias={"围棋和棋", "围棋数目"})
    async def go_scoring_help(self, event: AstrMessageEvent):
        """说明围棋终局和分析估分的区别。"""
        session = self.store.get(self._key(event))
        komi = (
            session.engine.komi
            if session and isinstance(session.engine, GoEngine)
            else float(self.config.get("go_komi", 6.5))
        )
        yield event.plain_result(
            "围棋双方连续 pass 后，插件按规则做面积数子：棋盘上的活子加其围住的空点，"
            f"白方另加 {komi:g} 贴目；这不是 AI 估算。只有最终分数完全相等才算和棋。\n"
            "注意：当前是自动简化数子，不会替双方判断争议死子；终局前应先把死子提净。"
            "局中 /AI分析 的胜率、目数和势力图才是低开销启发式估计。"
        )

    @filter.command("棋谱")
    async def show_record(self, event: AstrMessageEvent):
        """查看当前棋局的文字棋谱。"""
        session = self.store.get(self._key(event))
        if not session:
            yield event.plain_result("当前没有棋局。")
            return
        moves = session.engine.notation_history()
        if not moves:
            yield event.plain_result("当前还没有走子。")
            return
        lines = []
        for index in range(0, len(moves), 2):
            pair = moves[index : index + 2]
            lines.append(f"{index // 2 + 1}. " + "  ".join(pair))
        yield event.plain_result(
            f"{session.engine.display_name}棋谱\n" + "\n".join(lines)
        )

    @filter.command("战绩", alias={"国际象棋战绩"})
    async def show_stats(self, event: AstrMessageEvent):
        """查看自己的各棋种战绩。"""
        user = self.stats.get(str(event.get_sender_id()), {})
        if not user:
            yield event.plain_result("暂无已完成对局的战绩。")
            return
        lines = [f"{event.get_sender_name()} 的战绩"]
        for game_id, record in user.items():
            info = GAME_INFO.get(game_id, {"name": game_id})
            total = sum(
                int(record.get(key, 0)) for key in ("wins", "draws", "losses")
            )
            lines.append(
                f"{info['name']}：{record.get('wins', 0)}胜 {record.get('draws', 0)}和 "
                f"{record.get('losses', 0)}负 · {total}局 · 等级分 {record.get('rating', 1000)}"
            )
        lines.append("用 /对局记录 查看最近对局，用 /排行榜 [棋种] 看等级分榜。")
        yield event.plain_result("\n".join(lines))

    @filter.command("对局记录", alias={"历史战绩", "战绩记录"})
    async def show_history(self, event: AstrMessageEvent, game_type: str = ""):
        """查看自己或被提及玩家的汇总战绩与最近十局。"""
        requested = normalize_game(game_type) if game_type else None
        if game_type and not requested:
            yield event.plain_result(
                "无法识别棋种。用法：/对局记录、/对局记录 围棋、"
                "/对局记录 @某人、/对局记录 @某人 围棋。"
            )
            return
        target_id, mention_name = self._history_target(event)
        user = self.stats.get(target_id, {})
        target_name = mention_name or str(event.get_sender_name())
        for record in user.values():
            if record.get("player_name"):
                target_name = str(record["player_name"])
                break
        rows: list[tuple[int, str, dict[str, Any]]] = []
        summary: list[tuple[str, int, int, int, int]] = []
        for game_id, record in user.items():
            if requested and game_id != requested:
                continue
            wins = int(record.get("wins", 0))
            draws = int(record.get("draws", 0))
            losses = int(record.get("losses", 0))
            summary.append(
                (game_id, wins, draws, losses, int(record.get("rating", 1000)))
            )
            for item in record.get("history", []):
                rows.append((int(item.get("time", 0)), game_id, item))
        if not summary:
            scope = GAME_INFO[requested]["name"] if requested else "任何棋种"
            yield event.plain_result(
                f"{target_name} 暂无{scope}已完成对局。\n"
                "用法：/对局记录、/对局记录 围棋、/对局记录 @某人、"
                "/对局记录 @某人 围棋。"
            )
            return
        result_labels = {"win": "胜", "draw": "和", "loss": "负"}
        reason_labels = {
            "rules": "规则终局",
            "agreement": "协议和棋",
            "resign": "认输",
            "timeout": "棋钟超时",
            "turn_timeout": "单步超时",
            "completed": "正常结束",
        }
        total_wins = sum(item[1] for item in summary)
        total_draws = sum(item[2] for item in summary)
        total_losses = sum(item[3] for item in summary)
        total = total_wins + total_draws + total_losses
        scope = GAME_INFO[requested]["name"] if requested else "全部棋种"
        win_rate = total_wins / total * 100 if total else 0.0
        lines = [
            f"{target_name} · {scope}对局记录",
            f"总计 {total} 局：{total_wins}胜 {total_draws}和 {total_losses}负 · 胜率 {win_rate:.1f}%",
        ]
        for game_id, wins, draws, losses, rating in summary:
            game_total = wins + draws + losses
            rate = wins / game_total * 100 if game_total else 0.0
            lines.append(
                f"{GAME_INFO.get(game_id, {'name': game_id})['name']}："
                f"{wins}胜 {draws}和 {losses}负 · 胜率 {rate:.1f}% · 等级分 {rating}"
            )
        lines.append("\n最近 10 局")
        for timestamp, game_id, item in sorted(rows, reverse=True)[:10]:
            stamp = (
                datetime.fromtimestamp(timestamp, tz=timezone.utc)
                .astimezone()
                .strftime("%m-%d %H:%M")
            )
            name = GAME_INFO.get(game_id, {"name": game_id})["name"]
            duration = max(0, int(item.get("duration_seconds", 0)))
            duration_text = f"{duration // 60}分{duration % 60:02d}秒"
            result = result_labels.get(str(item.get("result")), "?")
            reason = reason_labels.get(
                str(item.get("reason", "completed")),
                str(item.get("reason", "正常结束")),
            )
            rating_before = int(item.get("rating_before", 1000))
            rating_after = int(item.get("rating_after", rating_before))
            lines.append(
                f"{stamp} {name} {result} vs {item.get('opponent_name') or '未知'} · "
                f"{reason} · {item.get('moves', 0)}手 · {duration_text} · "
                f"{rating_before}→{rating_after} · {item.get('time_control', '—')}"
            )
        yield event.plain_result("\n".join(lines))

    @staticmethod
    def _history_target(event: AstrMessageEvent) -> tuple[str, str]:
        message_obj = getattr(event, "message_obj", None)
        self_id = str(getattr(message_obj, "self_id", ""))
        for component in getattr(message_obj, "message", []) or []:
            if component.__class__.__name__ != "At" and not hasattr(component, "qq"):
                continue
            user_id = str(getattr(component, "qq", ""))
            if not user_id or user_id in {"all", self_id}:
                continue
            name = str(getattr(component, "name", "") or user_id)
            group = getattr(message_obj, "group", None)
            for member in getattr(group, "members", []) or []:
                if str(getattr(member, "user_id", "")) == user_id:
                    name = str(getattr(member, "nickname", "") or name)
                    break
            return user_id, name
        return str(event.get_sender_id()), str(event.get_sender_name())

    @filter.command("排行榜", alias={"等级分榜", "棋类排行"})
    async def show_ranking(self, event: AstrMessageEvent, game_type: str = ""):
        """显示各棋种 Elo 等级分榜。"""
        requested = normalize_game(game_type) if game_type else None
        if game_type and not requested:
            yield event.plain_result("无法识别棋种。")
            return
        game_ids = [requested] if requested else list(GAME_INFO)
        sections: list[str] = []
        for game_id in game_ids:
            entries = []
            for user_id, games in self.stats.items():
                record = games.get(game_id)
                if not record:
                    continue
                total = sum(
                    int(record.get(key, 0))
                    for key in ("wins", "draws", "losses")
                )
                entries.append(
                    (
                        int(record.get("rating", 1000)),
                        total,
                        str(record.get("player_name") or user_id),
                    )
                )
            if not entries:
                continue
            limit = 10 if requested else 3
            lines = [f"{GAME_INFO[game_id]['name']}等级分榜"]
            for rank, (rating, total, name) in enumerate(
                sorted(entries, key=lambda row: (-row[0], -row[1], row[2]))[:limit],
                1,
            ):
                lines.append(f"{rank}. {name} · {rating}分 · {total}局")
            sections.append("\n".join(lines))
        if not sections:
            yield event.plain_result("暂无已完成对局，排行榜还是空的。")
            return
        yield event.plain_result("\n\n".join(sections))

    @filter.command("棋类帮助", alias={"棋盘游戏帮助"})
    async def help(self, event: AstrMessageEvent):
        """显示插件总览和分棋种帮助入口。"""
        yield event.plain_result(
            "多棋盘插件\n"
            "开局：/开局 国际象棋 [计时=15+10]\n"
            "      /开局 围棋 [9路/13路/19路，默认19路] [计时=60|3x30]\n"
            "      /开局 中国象棋 | 井字棋 | 黑白棋\n"
            "      /开局 五子棋 [自由/标准/连珠] [Swap2] [13/15/19路]\n"
            "加入：/加入棋局；任一选手选边后，对手自动分到另一方。\n"
            "选边：/选先 /选后，或对应棋种的 /选黑 /选白 /选红。\n"
            "走子格式：/下棋帮助\n"
            "管理：/棋盘 /棋谱 /棋钟 /计时规则 /悔棋 /和棋 /流局 /认输\n"
            "战绩：/战绩 /对局记录 [@玩家] [棋种] /排行榜 [棋种]\n"
            "Swap2：/选白 /选黑 /交换 /加两子（按棋盘提示使用）\n"
            "分析：/AI分析；围棋也可用 /势力范围（均需对手 /同意AI）\n"
            "分棋种：/国际象棋帮助 /围棋帮助 /中国象棋帮助 /五子棋帮助 "
            "/井字棋帮助 /黑白棋帮助"
        )

    @filter.command("下棋帮助", alias={"走子帮助", "国际象棋下棋帮助"})
    async def move_help(self, event: AstrMessageEvent):
        yield event.plain_result(
            "下棋指令\n"
            "轮到自己时可发送：/下棋 Nc3、/Nc3 或直接 Nc3。\n"
            "坐标可连写或空格分隔，例如 /b1c3、/b1 c3、b1 c3。\n"
            "国际象棋：Nc3、b1c3、O-O；围棋：D4、pass（跳过 I）；\n"
            "中国象棋：炮二平五、h2e2；五子棋/黑白棋：H8；"
            "井字棋：1～9 或 A1～C3。\n"
            "裸走法仅在本群有棋局、发送者是当前方且文本能识别为合法格式时接管；"
            "认输、悔棋、AI分析等非走子指令仍必须带 /。\n"
            "需要规则细节时使用对应的 /xx棋帮助。"
        )

    @filter.command("国际象棋帮助")
    async def chess_help(self, event: AstrMessageEvent):
        yield event.plain_result(
            "国际象棋帮助\n"
            "开局：/开局 国际象棋（默认 15+10）→ /加入棋局 → 任一人 /选白 或 /选黑。\n"
            "走法支持 SAN：Nc3、Nxf7+、O-O、e8=Q；也支持 UCI：b1c3、e2 e4。\n"
            "完整处理王车易位、吃过路兵、升变、将军、将死、逼和与规则和棋。\n"
            "可用 /棋谱、/悔棋、/和棋、/认输、/AI分析。"
        )

    @filter.command("围棋帮助")
    async def go_help(self, event: AstrMessageEvent):
        yield event.plain_result(
            "围棋帮助\n"
            "开局：/开局 围棋 [9路/13路/19路]，默认标准 19 路、60|3x30。\n"
            "走子：D4；字母跳过 I。停一手：pass。任一人 /选黑 或 /选白。\n"
            "支持提子、禁入点和全局同形。双方连续 pass 后按面积数子终局；"
            "默认白贴 6.5 目，详见 /围棋计分。\n"
            "/AI分析 或 /势力范围 经对手同意后给出估算胜率、推荐着点和势力图。"
        )

    @filter.command("中国象棋帮助", alias={"象棋帮助"})
    async def xiangqi_help(self, event: AstrMessageEvent):
        yield event.plain_result(
            "中国象棋帮助\n"
            "开局：/开局 中国象棋（默认 10+5），任一人 /选红 或 /选黑。\n"
            "支持中文记谱：炮二平五、马八进七；支持坐标：h2e2、h2 e2。\n"
            "包含蹩马腿、塞象眼、炮架、九宫、将帅照面、将军与送将校验。\n"
            "同路同名棋子难以区分时建议使用坐标走法。"
        )

    @filter.command("五子棋帮助")
    async def gomoku_help(self, event: AstrMessageEvent):
        yield event.plain_result(
            "五子棋帮助\n"
            "开局：/开局 五子棋 [自由/标准/连珠] [Swap2] [13/15/19路]。\n"
            "自由：五子及以上胜；标准：必须恰好五子；连珠：15路，黑有长连、四四、三三禁手。\n"
            "走子：H8。普通开局任一人 /选黑 或 /选白。\n"
            "Swap2 按棋盘提示使用 /选白、/交换、/加两子，最终再 /选黑 或 /选白。"
        )

    @filter.command("井字棋帮助")
    async def tictactoe_help(self, event: AstrMessageEvent):
        yield event.plain_result(
            "井字棋帮助\n"
            "开局：/开局 井字棋（默认 2+2），任一人 /选先 或 /选后。\n"
            "走子可用数字 1～9（从左上到右下），或 A1～C3。\n"
            "任意横、竖、斜线连成三子获胜；棋盘填满且无人连线则和棋。"
        )

    @filter.command("黑白棋帮助", alias={"奥赛罗帮助"})
    async def reversi_help(self, event: AstrMessageEvent):
        yield event.plain_result(
            "黑白棋帮助\n"
            "开局：/开局 黑白棋（默认每方 10 分钟包干），任一人 /选黑 或 /选白。\n"
            "走子：D3、C4 等坐标；落子必须夹住并翻转至少一枚对方棋子。\n"
            "无合法着法时自动跳过；双方都无合法着法或棋盘填满后，棋子更多者获胜。"
        )
