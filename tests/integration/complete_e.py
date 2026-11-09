"""Явно ограниченная E-часть прежнего стенда для проверки исправленного wire3."""
import argparse
import json
from unittest.mock import patch

from integration import intention_probe as probe


def run_e(source, output, **options):
    if options.get('mode', 'stub') == 'stub' and 'provider_factory' not in options:
        from iam.stubs import Reply, ScriptedProvider, rest
        def reply(request):
            return Reply(()) if request['purpose'] == 'appraise' else Reply((rest(),))
        options['provider_factory'] = lambda name, transport: ScriptedProvider([reply] * 4)
    # Ограничение входного набора, а не изменение постоянного ядра/истории.
    with patch.object(probe, 'STORIES', ('E',)):
        result = probe.run_probe(source, output, protocol='prospective', **options)
    if result['decisions'] > 3 or result['model_calls'] > 12:
        raise AssertionError('E completion exceeded its registered budget')
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--source', required=True)
    parser.add_argument('--output', required=True)
    parser.add_argument('--mode', choices=('stub', 'live'), default='stub')
    parser.add_argument('--auth-file')
    parser.add_argument('--image', default='iam-codex:1')
    args = parser.parse_args()
    result = run_e(args.source, args.output, mode=args.mode, auth_file=args.auth_file, image=args.image)
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result['status'] == 'completed' else 1


if __name__ == '__main__':
    raise SystemExit(main())
