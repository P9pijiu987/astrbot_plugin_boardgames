from __future__ import annotations

import copy
import sys
import unittest
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN_ROOT))

from boardgames.base import FIRST, SECOND
from boardgames.clock import (
    clock_view,
    create_clock,
    crossed_reminder,
    extend_clock,
    parse_reminder_schedule,
    parse_time_control,
    settle_and_switch,
    start_clock,
    timed_out_side,
)


class ClockTests(unittest.TestCase):
    def test_parse_supported_profiles(self):
        self.assertEqual(parse_time_control("15+10")["mode"], "fischer")
        self.assertEqual(parse_time_control("10")["mode"], "sudden")
        self.assertEqual(parse_time_control("20|3x30")["periods"], 3)
        self.assertIsNone(parse_time_control("不计时"))
        with self.assertRaises(ValueError):
            parse_time_control("随便")

    def test_fischer_charges_elapsed_and_adds_increment(self):
        clock = create_clock(parse_time_control("1+10"))
        start_clock(clock, 100.0)
        self.assertIsNone(settle_and_switch(clock, SECOND, 130.0))
        self.assertEqual(clock["remaining"][FIRST], 40.0)
        self.assertEqual(clock["active"], SECOND)
        self.assertAlmostEqual(clock_view(clock, SECOND, 151.0)["main"], 39.0)

    def test_timeout_is_detected(self):
        clock = create_clock(parse_time_control("1"))
        start_clock(clock, 100.0)
        self.assertIsNone(timed_out_side(clock, 159.9))
        self.assertEqual(timed_out_side(clock, 160.0), FIRST)

    def test_japanese_byoyomi_consumes_periods(self):
        clock = create_clock(parse_time_control("0|3x30"))
        start_clock(clock, 100.0)
        self.assertIsNone(settle_and_switch(clock, SECOND, 131.0))
        self.assertEqual(clock["periods_left"][FIRST], 2)
        self.assertEqual(clock["active"], SECOND)

    def test_staged_reminder_boundaries(self):
        schedule = parse_reminder_schedule("86400:60,180:30,120:15,60:10,30:5")
        self.assertEqual(crossed_reminder(3541.2, 3539.8, schedule), 3540)
        self.assertEqual(crossed_reminder(241.2, 239.8, schedule), 240)
        self.assertEqual(crossed_reminder(151.2, 149.8, schedule), 150)
        self.assertEqual(crossed_reminder(121.2, 119.8, schedule), 120)
        self.assertEqual(crossed_reminder(61.2, 59.8, schedule), 60)
        self.assertEqual(crossed_reminder(31.2, 29.8, schedule), 30)
        self.assertIsNone(crossed_reminder(29.9, 26.0, schedule))

    def test_extension_adds_to_each_remaining_clock_without_increment(self):
        clock = create_clock(parse_time_control("1+10"))
        start_clock(clock, 100.0)
        settle_and_switch(clock, SECOND, 130.0)
        self.assertEqual(extend_clock(clock, 5, 140.0), "minutes")
        self.assertEqual(clock_view(clock, FIRST, 140.0)["main"], 340)
        self.assertEqual(clock_view(clock, SECOND, 140.0)["main"], 350)
        self.assertEqual(clock["active"], SECOND)
        self.assertEqual(clock["turn_started_at"], 130.0)
        self.assertEqual(clock["increment_seconds"], 10)

    def test_byoyomi_main_time_extension_uses_minutes(self):
        clock = create_clock(parse_time_control("1|3x30"))
        start_clock(clock, 100.0)
        self.assertEqual(extend_clock(clock, 3, 130.0), "minutes")
        self.assertEqual(clock_view(clock, FIRST, 130.0)["main"], 210)
        self.assertEqual(clock_view(clock, SECOND, 130.0)["main"], 240)
        self.assertEqual(clock["periods_left"], {FIRST: 3, SECOND: 3})

    def test_byoyomi_extension_preserves_partially_consumed_period(self):
        clock = create_clock(parse_time_control("0|3x30"))
        start_clock(clock, 100.0)
        before = clock_view(clock, FIRST, 131.0)
        self.assertEqual(extend_clock(clock, 9, 131.0), "periods")
        after = clock_view(clock, FIRST, 131.0)
        self.assertEqual(after["periods"], before["periods"] + 2)
        self.assertEqual(after["period_remaining"], before["period_remaining"])
        self.assertEqual(after["seconds_to_flag"], before["seconds_to_flag"] + 60)
        self.assertEqual(clock_view(clock, SECOND, 131.0)["periods"], 5)
        self.assertEqual(clock["remaining"], {FIRST: 0, SECOND: 0})

    def test_byoyomi_extension_when_only_inactive_player_is_in_overtime(self):
        clock = create_clock(parse_time_control("1|3x30"))
        start_clock(clock, 100.0)
        settle_and_switch(clock, SECOND, 165.0)
        self.assertEqual(extend_clock(clock, 3, 170.0), "periods")
        self.assertEqual(clock_view(clock, SECOND, 170.0)["main"], 55)
        self.assertEqual(clock["periods_left"], {FIRST: 5, SECOND: 5})

    def test_extension_cannot_revive_expired_clock_or_accept_invalid_minutes(self):
        for profile, moment in (("1", 160), ("0|3x30", 190)):
            clock = create_clock(parse_time_control(profile))
            start_clock(clock, 100.0)
            original = copy.deepcopy(clock)
            with self.assertRaises(ValueError):
                extend_clock(clock, 3, moment)
            self.assertEqual(clock, original)
        for amount in (0, -1, float("nan"), float("inf"), 1e308):
            with self.assertRaises(ValueError):
                extend_clock(clock, amount, 101.0)


if __name__ == "__main__":
    unittest.main()
