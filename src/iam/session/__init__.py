"""Ограниченный ручной сеанс поверх неизменённого контроллера состояния."""
from .runner import SessionConflict, SessionRunner

__all__ = ["SessionConflict", "SessionRunner"]
