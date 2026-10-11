"""Реэкспорт мини-проверки JSON Schema из пакета (с S04 код живёт в src/iam/domain/jsonschema.py, D-011)."""
import sys
from pathlib import Path

_SRC = str(Path(__file__).resolve().parent.parent / "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from iam.domain.jsonschema import KNOWN, TYPES, check  # noqa: E402,F401
