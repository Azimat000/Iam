"""python -m iam.experiments.report --output /новый/каталог [--mode live --auth-file ...]."""
import argparse
from pathlib import Path

from iam.providers.codex import CodexProvider, CodexSandboxConfig
from iam.providers.run import subprocess_runner
from .suite import run_suite
from .dialogues import run_dialogues


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--mode', choices=('stub', 'formal', 'live'), default='stub')
    parser.add_argument('--auth-file')
    parser.add_argument('--persist-auth-updates', action='store_true')
    parser.add_argument('--conversational', action='store_true')
    parser.add_argument('--dialogues', action='store_true')
    args = parser.parse_args(argv)
    if args.mode == 'live' and not args.auth_file:
        parser.error('нужен внешний --auth-file')
    if args.persist_auth_updates and args.mode != 'live':
        parser.error('сохранение входа доступно только в live')
    factory = None
    if args.mode == 'live':
        config = CodexSandboxConfig(auth_file=args.auth_file, persist_auth_updates=args.persist_auth_updates)
        factory = lambda runtime: CodexProvider(subprocess_runner, config, max_format_retries=0)
    runner = run_dialogues if args.dialogues else run_suite
    kwargs = {} if args.dialogues or not args.conversational else {'conversational': True}
    summary = runner(Path(__file__).resolve().parents[4], args.output, mode=args.mode, provider_factory=factory, **kwargs)
    if args.dialogues:
        print(f"Разговоров: {summary['cells']}; исполнено: {summary['executed']}; в пакете: {summary['blind_dialogues']}")
        return 0
    print(f"Ячеек: {summary['matrix_cells']}; исполнено: {summary['executed']}; вложенных прогонов: {summary['subruns']}")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
