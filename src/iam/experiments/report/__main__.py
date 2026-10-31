"""python -m iam.experiments.report --output /новый/каталог [--mode live --auth-file ...]."""
import argparse
from pathlib import Path

from iam.providers.codex import CodexProvider, CodexSandboxConfig
from iam.providers.run import subprocess_runner
from .suite import run_suite
from .dialogues import run_dialogues
from .life import run_life
from .development import run_development


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--mode', choices=('stub', 'formal', 'live'), default='stub')
    parser.add_argument('--auth-file')
    parser.add_argument('--persist-auth-updates', action='store_true')
    parser.add_argument('--conversational', action='store_true')
    kind = parser.add_mutually_exclusive_group()
    kind.add_argument('--dialogues', action='store_true')
    kind.add_argument('--life', action='store_true')
    kind.add_argument('--development', action='store_true')
    parser.add_argument('--repeats', type=int, choices=(1, 2), default=2)
    parser.add_argument('--variants', nargs='+', choices=('A', 'B', 'C'))
    parser.add_argument('--development-profile', choices=('legacy', 'agency-v1'), default='legacy')
    args = parser.parse_args(argv)
    if args.mode == 'live' and not args.auth_file:
        parser.error('нужен внешний --auth-file')
    if args.persist_auth_updates and args.mode != 'live':
        parser.error('сохранение входа доступно только в live')
    if args.development_profile != 'legacy' and (not args.development or args.mode == 'formal'
                                                or args.variants not in (None, ['A'])):
        parser.error('agency-v1 требует --development, только A и режим stub/live')
    factory = None
    if args.mode == 'live':
        config = CodexSandboxConfig(auth_file=args.auth_file, persist_auth_updates=args.persist_auth_updates)
        factory = lambda runtime: CodexProvider(subprocess_runner, config, max_format_retries=0)
    runner = run_development if args.development else run_life if args.life else run_dialogues if args.dialogues else run_suite
    kwargs = ({'repeats': args.repeats, 'variants': args.variants or ('A', 'B', 'C')} if args.development else {}
              if args.life or args.dialogues or not args.conversational else {'conversational': True})
    if args.development_profile == 'agency-v1':
        kwargs.update(profile='agency-v1', variants=('A',))
    summary = runner(Path(__file__).resolve().parents[4], args.output, mode=args.mode, provider_factory=factory, **kwargs)
    if args.development:
        print(f"Биографий: {summary['cells']}; исполнено: {summary['executed']}; допущено к оценке: {summary['human_packet']}")
        return 0
    if args.life:
        print(f"Биографий: {summary['cells']}; исполнено: {summary['executed']}; в пакете: {summary['episodes']}")
        return 0
    if args.dialogues:
        print(f"Разговоров: {summary['cells']}; исполнено: {summary['executed']}; в пакете: {summary['blind_dialogues']}")
        return 0
    print(f"Ячеек: {summary['matrix_cells']}; исполнено: {summary['executed']}; вложенных прогонов: {summary['subruns']}")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
