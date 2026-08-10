from __future__ import annotations

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


astrbot = types.ModuleType("astrbot")
api = types.ModuleType("astrbot.api")
event = types.ModuleType("astrbot.api.event")
star = types.ModuleType("astrbot.api.star")
components = types.ModuleType("astrbot.api.message_components")
api.AstrBotConfig = dict
api.logger = _Logger()
event.AstrMessageEvent = object
event.filter = _Filter()
event.MessageChain = object
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


class _Event:
    def __init__(self, user_id: str, name: str, message: str = ""):
        self._user_id = user_id
        self._name = name
        self.message_str = message
        self.message_obj = SimpleNamespace(group_id="100")
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
        await _collect(plugin.join_game(joiner))
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
        chosen = await _collect(plugin.swap2_choose_white(starter))
        self.assertEqual(chosen[0][0], "chain")
        session = plugin.store.get(starter.unified_msg_origin)
        self.assertEqual(session.players["second"].user_id, "1")
        self.assertEqual(session.players["first"].user_id, "2")
        self.assertEqual(session.engine.turn, "second")
        await plugin.terminate()


if __name__ == "__main__":
    unittest.main()
