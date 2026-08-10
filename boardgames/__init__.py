"""Rule engines and shared services for the AstrBot board-games plugin."""

from .registry import GAME_ALIASES, GAME_INFO, create_engine, restore_engine

__all__ = ["GAME_ALIASES", "GAME_INFO", "create_engine", "restore_engine"]
