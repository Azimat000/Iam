"""Запуск внешнего процесса: контекст — в stdin, результат — из stdout (R-116); никаких файлов обмена."""
from __future__ import annotations

import subprocess
from dataclasses import dataclass
from typing import Callable, Sequence


@dataclass(frozen=True)
class RunResult:
    returncode: int | None       # None — процесс убит по таймауту
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False


# Исполнитель: (argv, stdin, таймаут в секундах, argv очистки при таймауте) → результат.
Runner = Callable[[Sequence[str], str, float, "Sequence[str] | None"], RunResult]


def subprocess_runner(argv: Sequence[str], stdin: str, timeout_s: float, cleanup: Sequence[str] | None = None) -> RunResult:
    """Настоящий запуск. Не нашли программу — код 127 и пояснение в stderr (не исключение: цикл различает
    «не запустился» и «ответил ошибкой»). По таймауту процесс убивается, затем выполняется `cleanup`
    (например, `docker kill <имя>`: убийство клиента docker контейнер не останавливает)."""
    try:
        done = subprocess.run(list(argv), input=stdin, capture_output=True, text=True, timeout=timeout_s, check=False)
    except subprocess.TimeoutExpired:
        if cleanup:
            subprocess.run(list(cleanup), capture_output=True, text=True, timeout=30, check=False)
        return RunResult(None, "", "", True)
    except FileNotFoundError as e:
        return RunResult(127, "", f"не найдена программа: {e.filename}")
    return RunResult(done.returncode, done.stdout, done.stderr)
