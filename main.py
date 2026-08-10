from __future__ import annotations

import asyncio
import re
import time
from pathlib import Path
from typing import Any

import astrbot.api.message_components as Comp
from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star

from .boardgames.base import FIRST, SECOND, clean_move_text, opponent
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


class BoardGamesPlugin(Star):
    """群聊多棋种对弈：规则、房间、渲染和轻量分析彼此解耦。"""

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.store = SessionStore()
        self.stats: dict[str, Any] = {}
        self.renderer = BoardRenderer(Path(__file__).parent / "assets")
        self.timeout_tasks: dict[str, asyncio.Task] = {}

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
                    if session.status == "waiting":
                        self._schedule_wait_timeout(session)
            logger.info(f"多棋盘插件已载入，恢复 {len(self.store.sessions)} 局。")
        except Exception as exc:  # noqa: BLE001 - plugin startup must remain recoverable
            logger.exception(f"载入棋局数据失败，将从空状态启动: {exc}")

    async def terminate(self):
        for task in self.timeout_tasks.values():
            if not task.done():
                task.cancel()
        self.timeout_tasks.clear()
        await self._persist()

    def _key(self, event: AstrMessageEvent) -> str:
        return str(event.unified_msg_origin)

    @staticmethod
    def _player(event: AstrMessageEvent) -> Player:
        return Player(
            str(event.get_sender_id()),
            str(event.get_sender_name() or event.get_sender_id()),
        )

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

    @staticmethod
    def _image_result(event: AstrMessageEvent, data: bytes):
        # 故意只放图片组件：避免平台把说明文字和棋盘拼进同一条消息。
        return event.chain_result([Comp.Image.fromBytes(data)])

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
                or session.status != "waiting"
            ):
                return
            self.store.remove(key)
            await self._persist()
            from astrbot.api.event import MessageChain

            await self.context.send_message(
                key,
                MessageChain().message(
                    f"开局后 {minutes} 分钟无人加入，棋局已自动取消。"
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
            chosen_side = FIRST
            size = None
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
                side = self._parse_side(option, side_names)
                if side:
                    chosen_side = side
                    continue
                match = re.fullmatch(r"(\d{1,2})路?", option)
                if match:
                    size = int(match.group(1))
                    continue
                return (
                    f"无法识别开局参数“{option}”。可使用先手/后手、颜色、棋盘路数；五子棋还可选自由/标准/连珠及 Swap2。",
                    None,
                )
            if gomoku_opening == "swap2" and chosen_side != FIRST:
                return "Swap2 中开局者先摆黑白黑三子，不能预先选择后手或白方。", None
            try:
                engine = create_engine(
                    game_id,
                    size,
                    go_komi=float(self.config.get("go_komi", 6.5)),
                    gomoku_rule=gomoku_rule,
                    gomoku_opening=gomoku_opening,
                )
            except ValueError as exc:
                return str(exc), None
            players = {FIRST: None, SECOND: None}
            players[chosen_side] = self._player(event)
            session = GameSession(key, game_id, engine, players)
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
    ):
        """开一局棋。例如：/开局 国际象棋 黑方、/开局 围棋 13路。"""
        if not game_type:
            yield event.plain_result(
                "用法：/开局 [棋种] [先手/后手/颜色] [路数]\n"
                "五子棋示例：/开局 五子棋 连珠 Swap2"
            )
            return
        error, image = await self._start_game_impl(
            event, game_type, [option1, option2, option3, option4]
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
        error, image = await self._start_game_impl(event, match.group(1), options[:4])
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
            session.status = "playing"
            session.last_action_at = time.time()
            self._cancel_timeout(key)
            await self._persist()
            image = await self._render(session)
        yield self._image_result(event, image)

    async def _swap2_choice_impl(
        self, event: AstrMessageEvent, choice: str
    ) -> tuple[str | None, bytes | None]:
        key = self._key(event)
        async with self.store.lock(key):
            session = self.store.get(key)
            if (
                not session
                or session.status != "playing"
                or not isinstance(session.engine, GomokuEngine)
                or session.engine.opening != "swap2"
            ):
                return "当前没有进行 Swap2 开局的五子棋对局。", None
            side = session.side_for(str(event.get_sender_id()))
            if not side:
                return "只有本局选手可以进行 Swap2 选色。", None
            if side != session.engine.turn:
                return session.engine.opening_prompt or "现在不需要你进行选择。", None
            outcome = session.engine.choose_opening(choice)
            if not outcome.ok:
                return outcome.message, None
            if outcome.extra.get("swap_players"):
                session.players[FIRST], session.players[SECOND] = (
                    session.players[SECOND],
                    session.players[FIRST],
                )
            session.pending = None
            session.last_action_at = time.time()
            await self._persist()
            return None, await self._render(session)

    async def _yield_swap2_choice(self, event: AstrMessageEvent, choice: str):
        error, image = await self._swap2_choice_impl(event, choice)
        if error:
            yield event.plain_result(error)
        else:
            yield self._image_result(event, image)

    @filter.command("选白")
    async def swap2_choose_white(self, event: AstrMessageEvent):
        async for result in self._yield_swap2_choice(event, "white"):
            yield result

    @filter.command("选黑")
    async def swap2_choose_black(self, event: AstrMessageEvent):
        async for result in self._yield_swap2_choice(event, "black"):
            yield result

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
                yield event.plain_result("还在等待另一位玩家加入。")
                return
            if session.engine.turn != side:
                turn_name = session.engine.side_names[
                    0 if session.engine.turn == FIRST else 1
                ]
                yield event.plain_result(f"现在轮到{turn_name}。")
                return
            outcome = session.engine.play(cleaned)
            if not outcome.ok:
                yield event.plain_result(outcome.message)
                return
            session.last_action_at = time.time()
            session.pending = None
            image = await self._render(session)
            end_text = ""
            if outcome.ended:
                if outcome.draw:
                    end_text = outcome.message or "和棋。"
                    self._record_result(session, None)
                else:
                    winner = session.players[outcome.winner]
                    end_text = (
                        f"{outcome.message} {winner.name if winner else '胜方'}获胜。"
                    )
                    self._record_result(session, outcome.winner)
                self.store.remove(key)
            await self._persist()
        yield self._image_result(event, image)
        if end_text:
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

    def _record_result(self, session: GameSession, winner: str | None) -> None:
        now = int(time.time())
        max_history = max(1, int(self.config.get("max_history_per_user", 50)))
        for side in (FIRST, SECOND):
            player = session.players.get(side)
            rival = session.players.get(opponent(side))
            if not player:
                continue
            user = self.stats.setdefault(player.user_id, {})
            game = user.setdefault(
                session.game_id, {"wins": 0, "draws": 0, "losses": 0, "history": []}
            )
            result = "draw" if winner is None else "win" if winner == side else "loss"
            counter = {"win": "wins", "draw": "draws", "loss": "losses"}[result]
            game[counter] += 1
            game["history"].append(
                {
                    "time": now,
                    "result": result,
                    "side": side,
                    "opponent_id": rival.user_id if rival else "",
                    "opponent_name": rival.name if rival else "",
                    "moves": len(session.engine.notation_history()),
                }
            )
            game["history"] = game["history"][-max_history:]

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
        self._record_result(session, None)
        self.store.remove(session.key)
        await self._persist()
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
        if session.status == "waiting" and side:
            self.store.remove(key)
            self._cancel_timeout(key)
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
        self._record_result(session, winner_side)
        self.store.remove(session.key)
        await self._persist()
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
        minutes = max(1, int(self.config.get("turn_timeout_minutes", 10)))
        if session.engine.turn == side:
            yield event.plain_result("当前轮到你行棋，不能对自己申请超时胜。")
            return
        remaining = minutes * 60 - (time.time() - session.last_action_at)
        if remaining > 0:
            yield event.plain_result(
                f"对手尚未超时，还需等待约 {int(remaining // 60) + 1} 分钟。"
            )
            return
        self._record_result(session, side)
        self.store.remove(session.key)
        await self._persist()
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
            lines.append(
                f"{info['name']}：{record.get('wins', 0)}胜 {record.get('draws', 0)}和 {record.get('losses', 0)}负"
            )
        yield event.plain_result("\n".join(lines))

    @filter.command(
        "棋类帮助", alias={"棋盘游戏帮助", "国际象棋帮助", "国际象棋下棋帮助"}
    )
    async def help(self, event: AstrMessageEvent):
        """显示插件指令和各棋种走法。"""
        yield event.plain_result(
            "多棋盘插件\n"
            "开局：/开局 国际象棋 [白方/黑方]\n"
            "      /开局 围棋 [9路/13路/19路，默认19路]\n"
            "      /开局 中国象棋 | 井字棋 | 黑白棋\n"
            "      /开局 五子棋 [自由/标准/连珠] [Swap2] [13/15/19路]\n"
            "加入：/加入棋局\n"
            "走子：可用 /下棋 Nc3、/Nc3、Nc3；坐标可连写或空格分隔。\n"
            "国际象棋：Nc3 / b1c3\n"
            "围棋：D4 / pass（坐标跳过 I）\n"
            "中国象棋：炮二平五 / h2e2\n"
            "五子棋、黑白棋：H8；井字棋：1～9 或 A1～C3\n"
            "管理：/棋盘 /棋谱 /悔棋 /和棋 /流局 /认输 /强制胜利 /战绩\n"
            "Swap2：/选白 /选黑 /交换 /加两子（按棋盘提示使用）\n"
            "分析：/AI分析；围棋也可用 /势力范围（均需对手 /同意AI）\n"
            "      所有棋种均返回双方胜率与推荐着法，围棋另发纯图片势力图。\n"
            "说明：下棋以外的指令必须保留 /；裸走法只对当前行棋者生效。"
        )
