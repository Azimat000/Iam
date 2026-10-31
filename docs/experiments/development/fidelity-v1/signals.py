"""Прежняя независимая арифметика Scope без изменений."""
from pathlib import Path
import runpy
check_signals=runpy.run_path(str(Path(__file__).resolve().parent.parent/'scope-v1/signals.py'))['check_signals']
