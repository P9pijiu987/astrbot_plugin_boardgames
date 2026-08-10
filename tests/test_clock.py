from __future__ import annotations

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
        self.assertEqual(crossed_reminder(241.2, 239.8, schedule), 240)
        self.assertEqual(crossed_reminder(151.2, 149.8, schedule), 150)
        self.assertEqual(crossed_reminder(121.2, 119.8, schedule), 120)
        self.assertEqual(crossed_reminder(61.2, 59.8, schedule), 60)
        self.assertEqual(crossed_reminder(31.2, 29.8, schedule), 30)
        self.assertIsNone(crossed_reminder(29.9, 26.0, schedule))


if __name__ == "__main__":
    unittest.main()
