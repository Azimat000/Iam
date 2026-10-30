"""Ручной запуск учебного стенда без обращения к подписке и без Telegram."""
import argparse

from . import Bench
from .toy import ToyArchitecture


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", required=True)
    parser.add_argument("--scenario", required=True)
    parser.add_argument("--persona", required=True)
    parser.add_argument("--seed", type=int)
    parser.add_argument("--memory-off", action="store_true")
    parser.add_argument("--motivation-off", action="store_true")
    args = parser.parse_args(argv)
    bench = Bench(args.data_root)
    results = bench.run(args.scenario, ToyArchitecture(), args.persona, seed=args.seed,
                        memory_off=args.memory_off, motivation_off=args.motivation_off)
    for result in results:
        print(result.path / "trace.json")
    print(bench.results.table(), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
