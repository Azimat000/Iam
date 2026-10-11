"""Заглушки интерфейсов (S07): весь цикл проходит без модели, мира, Telegram и настоящей памяти."""
from .capabilities import StubCapabilities
from .creator import StubCreatorBridge
from .memory import READABLE_LAYERS, StoreMemory, StubMemory
from .provider import Reply, ScriptExhausted, ScriptedProvider, act, ask, attend, change, rest, say
from .world import BEHAVIOURS, StubWorld, WorldCrash

__all__ = [
    "StubCapabilities", "StubCreatorBridge", "READABLE_LAYERS", "StoreMemory", "StubMemory", "Reply", "ScriptExhausted",
    "ScriptedProvider", "act", "ask", "attend", "change", "rest", "say", "BEHAVIOURS", "StubWorld", "WorldCrash",
]
