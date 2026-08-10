from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN_ROOT))

from boardgames.storage import (
    JsonStateStore,
    convert_legacy_chess_stats,
    merge_stats,
)


class StorageTests(unittest.TestCase):
    def test_atomic_state_file_and_backup_recovery(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "plugin_data" / "state.json"
            store = JsonStateStore(path)
            store.save({"1": {"go": {"wins": 1}}}, {}, ["kv:current"])
            first = store.load()
            self.assertEqual(first.stats["1"]["go"]["wins"], 1)
            self.assertEqual(first.migrations, ("kv:current",))

            store.save({"1": {"go": {"wins": 2}}}, {}, ["kv:current"])
            path.write_text("{broken", encoding="utf-8")
            recovered = store.load()
            self.assertTrue(recovered.recovered_from_backup)
            self.assertEqual(recovered.stats["1"]["go"]["wins"], 1)
            store.save(
                recovered.stats,
                recovered.active_games,
                recovered.migrations,
                backup_existing=False,
            )
            self.assertEqual(store.load().stats["1"]["go"]["wins"], 1)
            self.assertEqual(
                JsonStateStore._decode(store.backup_path).stats["1"]["go"]["wins"],
                1,
            )

    def test_merge_stats_sums_totals_and_deduplicates_history(self):
        shared = {"time": 10, "result": "win"}
        destination = {
            "1": {
                "chess": {
                    "wins": 1,
                    "draws": 0,
                    "losses": 0,
                    "rating": 1016,
                    "history": [shared],
                }
            }
        }
        incoming = {
            "1": {
                "chess": {
                    "wins": 0,
                    "draws": 1,
                    "losses": 1,
                    "rating": 980,
                    "history": [shared, {"time": 20, "result": "loss"}],
                }
            }
        }
        merged = merge_stats(destination, incoming)
        record = merged["1"]["chess"]
        self.assertEqual(
            (record["wins"], record["draws"], record["losses"]),
            (1, 1, 1),
        )
        self.assertEqual(record["rating"], 1016)
        self.assertEqual(len(record["history"]), 2)
        unchanged = merge_stats(destination, destination)
        self.assertEqual(unchanged["1"]["chess"]["wins"], 1)

    def test_original_chess_stats_conversion(self):
        converted = convert_legacy_chess_stats(
            {
                "7": {
                    "name": "旧玩家",
                    "wins": 2,
                    "draws": 1,
                    "losses": 3,
                    "history": [
                        {
                            "ts": 123,
                            "result": "win",
                            "opponent_id": "8",
                            "opponent_name": "对手",
                            "moves": ["e2e4", "e7e5"],
                        }
                    ],
                }
            }
        )
        record = converted["7"]["chess"]
        self.assertEqual(record["player_name"], "旧玩家")
        self.assertEqual(record["wins"], 2)
        self.assertEqual(record["history"][0]["moves"], 2)
        self.assertEqual(record["history"][0]["time"], 123)
        json.dumps(converted, ensure_ascii=False)


if __name__ == "__main__":
    unittest.main()
