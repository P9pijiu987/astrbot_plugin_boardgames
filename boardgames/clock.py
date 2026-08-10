from __future__ import annotations

import math
import re
import time
from typing import Any

from .base import FIRST, SECOND

_FISCHER_RE = re.compile(r"^(\d+(?:\.\d+)?)\s*(?:\+\s*(\d+))?$")
_BYOYOMI_RE = re.compile(
    r"^(\d+(?:\.\d+)?)\s*\|\s*(\d+)\s*[x×*]\s*(\d+)$",
    re.IGNORECASE,
)


def parse_time_control(value: str) -> dict[str, Any] | None:
    """Parse `15+10`, `20|3x30`, `10`, or an off switch."""
    text = value.strip().lower().replace("分钟", "").replace("分", "")
    if text in {"", "off", "none", "no", "不计时", "关闭"}:
        return None
    match = _BYOYOMI_RE.fullmatch(text)
    if match:
        main_minutes = float(match.group(1))
        periods = int(match.group(2))
        period_seconds = int(match.group(3))
        if main_minutes < 0 or periods < 1 or period_seconds < 1:
            raise ValueError("读秒格式应为 20|3x30，且次数和秒数必须大于 0")
        return {
            "mode": "byoyomi",
            "label": f"{main_minutes:g}|{periods}x{period_seconds}",
            "main_seconds": main_minutes * 60.0,
            "periods": periods,
            "period_seconds": period_seconds,
        }
    match = _FISCHER_RE.fullmatch(text)
    if match:
        minutes = float(match.group(1))
        increment = int(match.group(2) or 0)
        if minutes <= 0 or increment < 0:
            raise ValueError("计时格式应为 15+10，基础分钟必须大于 0")
        return {
            "mode": "fischer" if increment else "sudden",
            "label": f"{minutes:g}+{increment}" if increment else f"{minutes:g}",
            "main_seconds": minutes * 60.0,
            "increment_seconds": increment,
        }
    raise ValueError("无法识别计时；请使用 15+10、20|3x30、10 或 不计时")


def create_clock(spec: dict[str, Any] | None) -> dict[str, Any] | None:
    if spec is None:
        return None
    main = float(spec["main_seconds"])
    clock: dict[str, Any] = {
        **spec,
        "remaining": {FIRST: main, SECOND: main},
        "active": FIRST,
        "running": False,
        "turn_started_at": None,
        "started_at": None,
    }
    if spec["mode"] == "byoyomi":
        periods = int(spec["periods"])
        clock["periods_left"] = {FIRST: periods, SECOND: periods}
    return clock


def start_clock(clock: dict[str, Any] | None, now: float | None = None) -> None:
    if not clock:
        return
    moment = float(now if now is not None else time.time())
    clock["active"] = FIRST
    clock["running"] = True
    clock["turn_started_at"] = moment
    clock["started_at"] = clock.get("started_at") or moment


def _elapsed(clock: dict[str, Any], side: str, now: float) -> float:
    if not clock.get("running") or clock.get("active") != side:
        return 0.0
    started = clock.get("turn_started_at")
    return max(0.0, now - float(started)) if started is not None else 0.0


def clock_view(
    clock: dict[str, Any] | None, side: str, now: float | None = None
) -> dict[str, Any] | None:
    if not clock:
        return None
    moment = float(now if now is not None else time.time())
    elapsed = _elapsed(clock, side, moment)
    main = max(0.0, float(clock["remaining"].get(side, 0.0)))
    mode = str(clock["mode"])
    if mode != "byoyomi":
        remaining = main - elapsed
        return {
            "mode": mode,
            "main": max(0.0, remaining),
            "seconds_to_flag": remaining,
            "timed_out": remaining <= 0.0,
            "active": clock.get("active") == side and bool(clock.get("running")),
        }

    periods = max(0, int(clock["periods_left"].get(side, 0)))
    period_seconds = int(clock["period_seconds"])
    if elapsed < main:
        remaining_main = main - elapsed
        return {
            "mode": mode,
            "main": remaining_main,
            "periods": periods,
            "period_remaining": float(period_seconds),
            "seconds_to_flag": remaining_main + periods * period_seconds,
            "timed_out": False,
            "active": clock.get("active") == side and bool(clock.get("running")),
        }

    overtime = max(0.0, elapsed - main)
    total_overtime = periods * period_seconds
    timed_out = overtime >= total_overtime
    consumed = min(periods, int(overtime // period_seconds))
    periods_now = max(0, periods - consumed)
    into_period = overtime % period_seconds
    period_remaining = 0.0 if timed_out else period_seconds - into_period
    return {
        "mode": mode,
        "main": 0.0,
        "periods": periods_now,
        "period_remaining": period_remaining,
        "seconds_to_flag": total_overtime - overtime,
        "timed_out": timed_out,
        "active": clock.get("active") == side and bool(clock.get("running")),
    }


def timed_out_side(
    clock: dict[str, Any] | None, now: float | None = None
) -> str | None:
    if not clock or not clock.get("running"):
        return None
    side = str(clock.get("active", FIRST))
    view = clock_view(clock, side, now)
    return side if view and view["timed_out"] else None


def settle_and_switch(
    clock: dict[str, Any] | None,
    next_side: str,
    now: float | None = None,
    *,
    add_increment: bool = True,
) -> str | None:
    """Charge the active side and switch the running clock.

    Returns the side that ran out of time, otherwise None. If next_side equals the
    active side (such as a multi-placement opening), the clock keeps running without
    granting an increment.
    """
    if not clock or not clock.get("running"):
        return None
    moment = float(now if now is not None else time.time())
    active = str(clock.get("active", FIRST))
    elapsed = _elapsed(clock, active, moment)
    mode = str(clock["mode"])
    main = max(0.0, float(clock["remaining"].get(active, 0.0)))

    if mode == "byoyomi":
        if elapsed < main:
            clock["remaining"][active] = main - elapsed
        else:
            overtime = max(0.0, elapsed - main)
            clock["remaining"][active] = 0.0
            periods = max(0, int(clock["periods_left"].get(active, 0)))
            period_seconds = int(clock["period_seconds"])
            if overtime >= periods * period_seconds:
                clock["running"] = False
                return active
            consumed = int(overtime // period_seconds)
            clock["periods_left"][active] = max(0, periods - consumed)
    else:
        remaining = main - elapsed
        clock["remaining"][active] = max(0.0, remaining)
        if remaining <= 0.0:
            clock["running"] = False
            return active
        if next_side != active and add_increment:
            clock["remaining"][active] += float(clock.get("increment_seconds", 0))

    clock["active"] = next_side
    clock["turn_started_at"] = moment
    return None


def pause_clock(clock: dict[str, Any] | None, now: float | None = None) -> str | None:
    if not clock or not clock.get("running"):
        return None
    active = str(clock.get("active", FIRST))
    timed_out = settle_and_switch(clock, active, now, add_increment=False)
    clock["running"] = False
    return timed_out


def swap_clock_sides(clock: dict[str, Any] | None) -> None:
    """Keep each player's consumed time when a swap opening exchanges colours."""
    if not clock:
        return
    clock["remaining"][FIRST], clock["remaining"][SECOND] = (
        clock["remaining"][SECOND],
        clock["remaining"][FIRST],
    )
    if "periods_left" in clock:
        clock["periods_left"][FIRST], clock["periods_left"][SECOND] = (
            clock["periods_left"][SECOND],
            clock["periods_left"][FIRST],
        )
    clock["active"] = SECOND if clock.get("active") == FIRST else FIRST


def format_seconds(seconds: float) -> str:
    value = max(0, math.ceil(seconds))
    hours, rest = divmod(value, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def format_clock(
    clock: dict[str, Any] | None, side: str, now: float | None = None
) -> str:
    view = clock_view(clock, side, now)
    if view is None:
        return "不计时"
    prefix = "▶ " if view["active"] else ""
    if view["mode"] == "byoyomi" and view["main"] <= 0:
        return (
            f"{prefix}读秒 {format_seconds(view['period_remaining'])}"
            f" ×{view['periods']}"
        )
    return f"{prefix}{format_seconds(view['main'])}"


def parse_reminder_schedule(value: str) -> list[tuple[int, int]]:
    """Parse `300:60,120:30,60:10,30:5` as threshold/interval pairs."""
    result: list[tuple[int, int]] = []
    for part in value.replace("；", ",").replace("，", ",").split(","):
        token = part.strip()
        if not token:
            continue
        try:
            threshold_text, interval_text = token.split(":", 1)
            threshold, interval = int(threshold_text), int(interval_text)
        except ValueError as exc:
            raise ValueError("提醒规则应类似 300:60,120:30,60:10,30:5") from exc
        if threshold < 1 or interval < 1:
            raise ValueError("提醒阈值和间隔必须大于 0")
        result.append((threshold, interval))
    if not result:
        raise ValueError("提醒规则不能为空")
    return sorted(result)


def reminder_interval(
    seconds_to_flag: float, schedule: list[tuple[int, int]]
) -> int | None:
    remaining = max(0.0, seconds_to_flag)
    for threshold, interval in schedule:
        if remaining <= threshold:
            return interval
    return None


def crossed_reminder(
    previous: float,
    current: float,
    schedule: list[tuple[int, int]],
) -> int | None:
    """Return the most urgent whole-second reminder boundary just crossed."""
    if current >= previous:
        return None
    upper = max(1, math.floor(previous))
    lower = max(1, math.ceil(current))
    for second in range(lower, upper + 1):
        interval = reminder_interval(float(second), schedule)
        if interval and second % interval == 0 and current <= second < previous:
            return second
    return None
