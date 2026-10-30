"""S16: единая матрица, проверяемые оценки и раздельный пакет человеческой оценки."""
from .evaluation import assess, rank
from .suite import run_suite

__all__ = ['assess', 'rank', 'run_suite']
