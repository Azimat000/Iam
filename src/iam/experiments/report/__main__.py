"""python -m iam.experiments.report --output /новый/каталог [--mode live --auth-file ...]."""
import argparse
from pathlib import Path

from iam.providers.codex import CodexProvider, CodexSandboxConfig
from iam.providers.run import subprocess_runner
from .suite import run_suite


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--mode', choices=('stub', 'formal', 'live'), default='stub')
    parser.add_argument('--auth-file')
    args = parser.parse_args(argv)
    if args.mode == 'live' and not args.auth_file:
        parser.error('нужен внешний --auth-file')
    factory = None
    if args.mode == 'live':
        config = CodexSandboxConfig(auth_file=args.auth_file)
        factory = lambda runtime: CodexProvider(subprocess_runner, config, max_format_retries=0)
    summary = run_suite(Path(__file__).resolve().parents[4], args.output, mode=args.mode, provider_factory=factory)
    print(f"Ячеек: {summary['matrix_cells']}; исполнено: {summary['executed']}; вложенных прогонов: {summary['subruns']}")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
