from __future__ import annotations

import asyncio
import importlib
import sys
import types
import unittest
from pathlib import Path
from types import SimpleNamespace

OUTPUTS = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(OUTPUTS))


def _decorator(*_args, **_kwargs):
    return lambda function: function


class _Filter:
    EventMessageType = types.SimpleNamespace(GROUP_MESSAGE="group")
    PermissionType = types.SimpleNamespace(ADMIN="admin")
    command = staticmethod(_decorator)
    regex = staticmethod(_decorator)
    event_message_type = staticmethod(_decorator)
    permission_type = staticmethod(_decorator)
    on_astrbot_loaded = staticmethod(_decorator)


class _Star:
    def __init__(self, context):
        self.context = context
        self._kv = {}

    async def get_kv_data(self, key, default=None):
        return self._kv.get(key, default)

    async def put_kv_data(self, key, value):
        self._kv[key] = value


class _Image:
    @staticmethod
    def fromBytes(data):
        return ("image", data)


class _Logger:
    def __getattr__(self, _name):
        return lambda *_args, **_kwargs: None


class _MessageChain:
    def __init__(self, chain=None):
        self.text = ""
        self.chain = list(chain or [])

    def message(self, text):
        self.text = text
        return self


class _At:
    def __init__(self, qq: str, name: str = ""):
        self.qq = qq
        self.name = name


astrbot = types.ModuleType("astrbot")
api = types.ModuleType("astrbot.api")
event = types.ModuleType("astrbot.api.event")
star = types.ModuleType("astrbot.api.star")
components = types.ModuleType("astrbot.api.message_components")
api.AstrBotConfig = dict
api.logger = _Logger()
event.AstrMessageEvent = object
event.filter = _Filter()
event.MessageChain = _MessageChain
star.Context = object
star.Star = _Star
components.Image = _Image
sys.modules.update(
    {
        "astrbot": astrbot,
        "astrbot.api": api,
        "astrbot.api.event": event,
        "astrbot.api.star": star,
        "astrbot.api.message_components": components,
    }
)


class PluginImportTests(unittest.TestCase):
    def test_main_module_imports_and_registers_handlers(self):
        module = importlib.import_module("astrbot_plugin_boardgames.main")
        plugin = module.BoardGamesPlugin(object(), {})
        self.assertTrue(callable(plugin.start_game))
        self.assertTrue(callable(plugin.receive_bare_move))
        self.assertTrue(callable(plugin.agree_analysis))
        for handler in (
            "move_help",
            "chess_help",
            "go_help",
            "xiangqi_help",
            "gomoku_help",
            "tictactoe_help",
            "reversi_help",
        ):
            self.assertTrue(callable(getattr(plugin, handler)))


class _Event:
    def __init__(self, user_id: str, name: str, message: str = ""):
        self._user_id = user_id
        self._name = name
        self.message_str = message
        self.message_obj = SimpleNamespace(
            group_id="100",
            message=[],
            self_id="bot",
            raw_message=None,
            sender=None,
        )
        self.unified_msg_origin = "test:group:100"
        self.stopped = False

    def get_sender_id(self):
        return self._user_id

    def get_sender_name(self):
        return self._name

    def plain_result(self, text):
        return ("plain", text)

    def chain_result(self, chain):
        return ("chain", chain)

    def stop_event(self):
        self.stopped = True


async def _collect(generator):
    return [item async for item in generator]


class PluginFlowTests(unittest.IsolatedAsyncioTestCase):
    async def test_start_join_and_bare_move_emit_image_only(self):
        module = importlib.import_module("astrbot_plugin_boardgames.main")
        context = SimpleNamespace(send_message=lambda *_args, **_kwargs: None)
        plugin = module.BoardGamesPlugin(context, {})

        starter = _Event("1", "甲")
        started = await _collect(plugin.start_game(starter, "国际象棋"))
        self.assertEqual(started[0][0], "chain")
        self.assertEqual(len(started[0][1]), 1)
        self.assertEqual(started[0][1][0][0], "image")

        joiner = _Event("2", "乙")
        joined = await _collect(plugin.join_game(joiner))
        self.assertEqual(joined[0][0], "chain")
        self.assertEqual(len(joined[0][1]), 1)

        selected = await _collect(plugin.choose_first(starter))
        self.assertEqual(selected[0][0], "chain")
        self.assertEqual(selected[1][0], "chain")
        self.assertEqual(selected[2][0], "plain")
        self.assertIn("本局用时", selected[2][1])
        session = plugin.store.get(starter.unified_msg_origin)
        self.assertEqual(session.status, "playing")
        self.assertEqual(session.players["first"].user_id, "1")
        self.assertEqual(session.players["second"].user_id, "2")
        self.assertTrue(session.clock["running"])

        move = _Event("1", "甲", "Nc3")
        moved = await _collect(plugin.receive_bare_move(move))
        self.assertTrue(move.stopped)
        self.assertEqual(moved[0][0], "chain")
        self.assertEqual(len(moved[0][1]), 1)
        self.assertEqual(
            plugin.store.get(move.unified_msg_origin).engine.moves, ["b1c3"]
        )
        await plugin.terminate()

    async def test_go_analysis_returns_rates_recommendation_and_pure_influence_image(self):
        module = importlib.import_module("astrbot_plugin_boardgames.main")
        context = SimpleNamespace(send_message=lambda *_args, **_kwargs: None)
        plugin = module.BoardGamesPlugin(context, {})
        starter = _Event("1", "甲")
        joiner = _Event("2", "乙")
        await _collect(plugin.start_game(starter, "围棋", "9路"))
        self.assertEqual(
            plugin.store.get(starter.unified_msg_origin).clock["label"],
            "60|3x30",
        )
        await _collect(plugin.join_game(joiner))
        await _collect(plugin.choose_first(starter))
        await _collect(plugin.request_analysis(starter))
        results = await _collect(plugin.agree_analysis(joiner))
        self.assertEqual(results[0][0], "plain")
        self.assertIn("胜率对比", results[0][1])
        self.assertIn("推荐走法", results[0][1])
        self.assertEqual(results[1][0], "chain")
        self.assertEqual(len(results[1][1]), 1)
        self.assertEqual(results[1][1][0][0], "image")
        await plugin.terminate()

    async def test_swap2_add_two_and_choose_white_swaps_players(self):
        module = importlib.import_module("astrbot_plugin_boardgames.main")
        context = SimpleNamespace(send_message=lambda *_args, **_kwargs: None)
        plugin = module.BoardGamesPlugin(context, {})
        starter = _Event("1", "甲")
        joiner = _Event("2", "乙")
        await _collect(plugin.start_game(starter, "五子棋", "连珠", "Swap2"))
        await _collect(plugin.join_game(joiner))
        for move in ("H8", "I8", "H9"):
            await _collect(plugin.receive_bare_move(_Event("1", "甲", move)))
        added = await _collect(plugin.swap2_add_two(joiner))
        self.assertEqual(added[0][0], "chain")
        for move in ("G8", "G9"):
            await _collect(plugin.receive_bare_move(_Event("2", "乙", move)))
        chosen = await _collect(plugin.choose_white(starter))
        self.assertEqual(chosen[0][0], "chain")
        session = plugin.store.get(starter.unified_msg_origin)
        self.assertEqual(session.players["second"].user_id, "1")
        self.assertEqual(session.players["first"].user_id, "2")
        self.assertEqual(session.engine.turn, "second")
        await plugin.terminate()

    async def test_explicit_no_clock_survives_persistence(self):
        module = importlib.import_module("astrbot_plugin_boardgames.main")
        context = SimpleNamespace(send_message=lambda *_args, **_kwargs: None)
        plugin = module.BoardGamesPlugin(context, {})
        starter = _Event("1", "甲")
        await _collect(plugin.start_game(starter, "黑白棋", "不计时"))
        session = plugin.store.get(starter.unified_msg_origin)
        self.assertIsNone(session.clock)
        self.assertTrue(session.clock_disabled)
        restored = module.SessionStore()
        self.assertEqual(restored.restore(plugin.store.to_dict()), [])
        restored_session = restored.get(starter.unified_msg_origin)
        self.assertIsNone(restored_session.clock)
        self.assertTrue(restored_session.clock_disabled)
        await plugin.terminate()

    async def test_running_clock_automatically_records_timeout(self):
        module = importlib.import_module("astrbot_plugin_boardgames.main")
        sent = []

        async def send_message(key, chain):
            sent.append((key, chain))

        plugin = module.BoardGamesPlugin(SimpleNamespace(send_message=send_message), {})
        starter = _Event("1", "甲")
        joiner = _Event("2", "乙")
        await _collect(plugin.start_game(starter, "井字棋", "计时=0.001"))
        await _collect(plugin.join_game(joiner))
        await _collect(plugin.choose_first(starter))
        await asyncio.sleep(1.15)
        self.assertIsNone(plugin.store.get(starter.unified_msg_origin))
        self.assertEqual(plugin.stats["2"]["tictactoe"]["wins"], 1)
        self.assertTrue(sent)
        await plugin.terminate()

    async def test_anyone_can_abort_an_unjoined_room(self):
        module = importlib.import_module("astrbot_plugin_boardgames.main")
        plugin = module.BoardGamesPlugin(SimpleNamespace(send_message=None), {})
        starter = _Event("1", "甲")
        outsider = _Event("3", "路人")
        await _collect(plugin.start_game(starter, "围棋"))
        results = await _collect(plugin.abort_game(outsider))
        self.assertIn("空房已取消", results[0][1])
        self.assertIsNone(plugin.store.get(starter.unified_msg_origin))
        await plugin.terminate()

    async def test_history_supports_mention_and_game_filter(self):
        module = importlib.import_module("astrbot_plugin_boardgames.main")
        plugin = module.BoardGamesPlugin(SimpleNamespace(send_message=None), {})
        plugin.stats = {
            "2": {
                "go": {
                    "player_name": "乙",
                    "wins": 3,
                    "draws": 1,
                    "losses": 1,
                    "rating": 1042,
                    "history": [
                        {
                            "time": 1_700_000_000,
                            "result": "win",
                            "opponent_name": "甲",
                            "reason": "rules",
                            "moves": 120,
                            "duration_seconds": 3600,
                            "rating_before": 1026,
                            "rating_after": 1042,
                            "time_control": "60|3x30",
                        }
                    ],
                }
            }
        }
        event = _Event("3", "查询者")
        event.message_obj.message = [_At("2", "乙")]
        results = await _collect(plugin.show_history(event, "围棋"))
        self.assertIn("乙 · 围棋对局记录", results[0][1])
        self.assertIn("3胜 1和 1负", results[0][1])
        self.assertIn("胜率 60.0%", results[0][1])
        self.assertIn("最近 10 局", results[0][1])
        await plugin.terminate()

    async def test_resignation_sends_result_card_before_text(self):
        module = importlib.import_module("astrbot_plugin_boardgames.main")
        plugin = module.BoardGamesPlugin(SimpleNamespace(send_message=None), {})
        starter = _Event("1", "甲")
        joiner = _Event("2", "乙")
        await _collect(plugin.start_game(starter, "国际象棋"))
        await _collect(plugin.join_game(joiner))
        await _collect(plugin.choose_first(starter))
        results = await _collect(plugin.resign(starter))
        self.assertEqual(results[0][0], "chain")
        self.assertEqual(results[0][1][0][0], "image")
        self.assertEqual(results[1][0], "plain")
        await plugin.terminate()

    async def test_game_specific_and_move_help_are_distinct(self):
        module = importlib.import_module("astrbot_plugin_boardgames.main")
        plugin = module.BoardGamesPlugin(SimpleNamespace(send_message=None), {})
        event = _Event("1", "甲")
        move_help = await _collect(plugin.move_help(event))
        go_help = await _collect(plugin.go_help(event))
        self.assertIn("/下棋 Nc3", move_help[0][1])
        self.assertIn("默认标准 19 路、60|3x30", go_help[0][1])
        self.assertIn("面积数子", go_help[0][1])
        await plugin.terminate()


if __name__ == "__main__":
    unittest.main()
