from __future__ import annotations

import copy
import json
import os
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

STATE_SCHEMA_VERSION = 1


class StateFileError(RuntimeError):
    """Raised when an existing state file cannot be read safely."""


@dataclass(frozen=True, slots=True)
class LoadedState:
    stats: dict[str, Any]
    active_games: dict[str, Any]
    migrations: tuple[str, ...] = ()
    recovered_from_backup: bool = False


class JsonStateStore:
    """Atomic, human-readable storage below AstrBot's plugin_data directory."""

    def __init__(self, path: Path):
        self.path = path
        self.backup_path = path.with_suffix(path.suffix + ".bak")

    @staticmethod
    def _decode(path: Path) -> LoadedState:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise StateFileError(f"无法读取 {path}: {exc}") from exc
        if not isinstance(payload, dict):
            raise StateFileError(f"{path} 顶层必须是 JSON 对象")
        version = payload.get("schema_version")
        if version != STATE_SCHEMA_VERSION:
            raise StateFileError(f"{path} 使用不支持的数据版本 {version!r}")
        stats = payload.get("stats", {})
        active_games = payload.get("active_games", {})
        migrations = payload.get("migrations", [])
        if (
            not isinstance(stats, dict)
            or not isinstance(active_games, dict)
            or not isinstance(migrations, list)
        ):
            raise StateFileError(f"{path} 的 stats/active_games 必须是 JSON 对象")
        return LoadedState(
            dict(stats),
            dict(active_games),
            tuple(str(item) for item in migrations),
        )

    def load(self) -> LoadedState | None:
        if self.path.exists():
            try:
                return self._decode(self.path)
            except StateFileError as primary_error:
                if not self.backup_path.exists():
                    raise
                try:
                    backup = self._decode(self.backup_path)
                except StateFileError as backup_error:
                    raise StateFileError(
                        f"主文件和备份都已损坏：{primary_error}; {backup_error}"
                    ) from backup_error
                return LoadedState(
                    backup.stats,
                    backup.active_games,
                    backup.migrations,
                    recovered_from_backup=True,
                )
        if self.backup_path.exists():
            backup = self._decode(self.backup_path)
            return LoadedState(
                backup.stats,
                backup.active_games,
                backup.migrations,
                recovered_from_backup=True,
            )
        return None

    def save(
        self,
        stats: dict[str, Any],
        active_games: dict[str, Any],
        migrations: list[str] | tuple[str, ...] = (),
        backup_existing: bool = True,
    ) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema_version": STATE_SCHEMA_VERSION,
            "saved_at": int(time.time()),
            "stats": stats,
            "active_games": active_games,
            "migrations": sorted(set(migrations)),
        }
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        try:
            with temporary.open("w", encoding="utf-8", newline="\n") as stream:
                json.dump(payload, stream, ensure_ascii=False, indent=2, sort_keys=True)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            if backup_existing and self.path.exists():
                shutil.copy2(self.path, self.backup_path)
            os.replace(temporary, self.path)
        finally:
            if temporary.exists():
                temporary.unlink()


def merge_stats(
    destination: dict[str, Any],
    incoming: dict[str, Any],
    *,
    history_limit: int = 50,
) -> dict[str, Any]:
    """Merge independent stats sources once, preserving totals and unique history."""

    merged = copy.deepcopy(destination)
    for user_id, games in incoming.items():
        if not isinstance(games, dict):
            continue
        target_games = merged.setdefault(str(user_id), {})
        if not isinstance(target_games, dict):
            target_games = {}
            merged[str(user_id)] = target_games
        for game_id, record in games.items():
            if not isinstance(record, dict):
                continue
            current = target_games.get(str(game_id))
            if not isinstance(current, dict):
                target_games[str(game_id)] = copy.deepcopy(record)
                continue
            if current == record:
                continue
            current_total = sum(
                int(current.get(field, 0)) for field in ("wins", "draws", "losses")
            )
            for field in ("wins", "draws", "losses"):
                current[field] = int(current.get(field, 0)) + int(record.get(field, 0))
            if not current.get("player_name") and record.get("player_name"):
                current["player_name"] = record["player_name"]
            if current_total == 0 and "rating" in record:
                current["rating"] = int(record.get("rating", 1000))
            current.setdefault("rating", 1000)
            history: list[dict[str, Any]] = []
            seen: set[str] = set()
            for item in [*current.get("history", []), *record.get("history", [])]:
                if not isinstance(item, dict):
                    continue
                signature = json.dumps(item, ensure_ascii=False, sort_keys=True)
                if signature in seen:
                    continue
                seen.add(signature)
                history.append(copy.deepcopy(item))
            history.sort(key=lambda item: float(item.get("time", 0)))
            current["history"] = history[-max(1, history_limit) :]
    return merged


def convert_legacy_chess_stats(data: dict[str, Any]) -> dict[str, Any]:
    """Convert the original root-level chess_stats.json into the new schema."""

    converted: dict[str, Any] = {}
    for user_id, old_record in data.items():
        if not isinstance(old_record, dict):
            continue
        history = []
        for item in old_record.get("history", []):
            if not isinstance(item, dict):
                continue
            moves = item.get("moves", [])
            history.append(
                {
                    "time": int(float(item.get("ts", 0))),
                    "result": str(item.get("result", "draw")),
                    "opponent_id": str(item.get("opponent_id", "")),
                    "opponent_name": str(item.get("opponent_name", "未知")),
                    "reason": "旧版记录",
                    "moves": len(moves) if isinstance(moves, list) else 0,
                    "duration_seconds": 0,
                    "rating_before": 1000,
                    "rating_after": 1000,
                    "time_control": "旧版未记录",
                }
            )
        converted[str(user_id)] = {
            "chess": {
                "player_name": str(old_record.get("name", user_id)),
                "wins": int(old_record.get("wins", 0)),
                "draws": int(old_record.get("draws", 0)),
                "losses": int(old_record.get("losses", 0)),
                "rating": 1000,
                "history": history,
            }
        }
    return converted
