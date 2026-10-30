"""Неизменные билеты и варианты; каждый вложенный прогон получает отдельный бюджет."""
import csv
import hashlib
import json
import time
from pathlib import Path

from iam.experiments import Bench, Persona, Scenario
from iam.experiments.results import RunResult
from iam.experiments.conversation import ConversationArchitecture
from iam.experiments.variants.formal import FormalArchitecture
from iam.experiments.variants.hybrid import HybridArchitecture
from iam.experiments.variants.model_led import ModelLedArchitecture
from iam.providers.errors import SessionLimitReached, ProviderFormatError
from iam.interfaces import ProviderError
from iam.state import verify
from iam.storage import Store
from iam.stubs import Reply, ScriptedProvider, rest

from .blind import build_blind
from .evaluation import assess, rank


class BoundedProvider:
    def __init__(self, provider, pause=None):
        if getattr(provider, 'max_format_retries', 0) != 0:
            raise ValueError('единственный повтор формата ведёт ядро; max_format_retries должен быть 0')
        self.provider, self.calls, self.started = provider, 0, time.monotonic()
        self.pause = pause if pause is not None else [False]

    def propose(self, request):
        if self.pause[0] or self.calls >= 10 or time.monotonic() - self.started >= 1800:
            raise SessionLimitReached('calls', 'предел экспериментального сеанса')
        self.calls += 1
        try:
            return self.provider.propose(request)
        except ProviderFormatError:
            raise
        except ProviderError:
            self.pause[0] = True
            raise


def stub_factory(runtime):
    """Контроль формы и соединений. Заглушка не понимает билет и не создаёт диалогов."""
    class Stub(ScriptedProvider):
        def propose(self, request):
            refs = list(dict.fromkeys(c['ref'] for c in request['context'] if c['ref'].startswith(('evt_', 'mem_'))))
            self.queue(Reply(proposals=(rest('Техническая заглушка: доступный опыт учтён только как вход.', cites=refs),)))
            return super().propose(request)
    return Stub()


def complexity(root, variant):
    directory = root / 'src/iam/experiments/variants' / variant
    files = sorted(directory.glob('*.py'))
    return {'files': len(files), 'lines': sum(len(p.read_text().splitlines()) for p in files),
            'dependencies': 'stdlib + общее ядро' + (' + Codex/Docker/подписка' if variant != 'formal' else ''),
            'scope': 'только исходники варианта; общие модули и тесты исключены'}


def write_csv(path, rows, fields):
    with path.open('w', encoding='utf-8', newline='') as out:
        writer = csv.DictWriter(out, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)



def eligibility(letter, matrix, runs, technical, mode):
    """Незавершённая матрица и повреждённая база не получают допуск даже при зелёных билетах."""
    cells = [r for r in matrix if r['variant'] == letter]
    selected = [r for r in runs if r['cell'].startswith(letter + '-')]
    checks = list(technical)
    if len(cells) != 16 or any(c['status'] != 'executed' for c in cells):
        checks.append('unverified')
    if letter != 'C' and mode != 'live':
        checks.append('unverified')
    for run in selected:
        checks.append(run['integrity'])
        if run['status'] != 'completed' or any(o in ('no_decision', 'failed', 'refused') for o in run['outcomes']):
            checks.append('fail')
    if not selected:
        checks.append('unverified')
    return checks

def run_suite(root, output, *, mode='stub', provider_factory=None, conversational=False):
    root, output = Path(root).resolve(), Path(output).resolve()
    if mode not in ('stub', 'live', 'formal'):
        raise ValueError('неизвестный режим')
    if mode == 'live' and provider_factory is None:
        raise ValueError('живой режим требует явного провайдера')
    output.mkdir(parents=True, exist_ok=False)
    factory = provider_factory if mode == 'live' else stub_factory
    providers, pause = [], [False]
    def bounded(runtime):
        provider = BoundedProvider(factory(runtime), pause)
        providers.append(provider)
        return provider
    architectures = [('A', ModelLedArchitecture(bounded), 'model_led'),
                     ('B', HybridArchitecture(bounded), 'hybrid'), ('C', FormalArchitecture(), 'formal')]
    if conversational:
        architectures = [(letter, ConversationArchitecture(arch, letter), directory) for letter, arch, directory in architectures]
    persona = Persona.load(root / 'scenarios/personas/curious.toml')
    scenarios = sorted((root / 'scenarios/experiments').glob('*.toml'))
    if len(scenarios) != 8:
        raise ValueError('ожидается ровно восемь исходных билетов')
    matrix, checks, runs, blind_results = [], [], [], []
    stopped = False
    for letter, architecture, directory in architectures:
        for path in scenarios:
            scenario = Scenario.load(path)
            for repeat in (1, 2):
                cell = f'{letter}-{path.stem}-{repeat}'
                row = {'cell': cell, 'variant': letter, 'scenario': path.stem, 'repeat': repeat,
                       'source_sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
                       'mode': 'local' if letter == 'C' else mode, 'status': 'not_run', 'reason': '',
                       'runs': 0, 'decisions': 0, 'calls': 0, 'wall_ms': 0, 'calls_attempted': 0}
                if (mode == 'formal' or stopped) and letter != 'C':
                    row['reason'] = 'языковой исполнитель не запрошен' if mode == 'formal' else 'техническая пауза исполнителя'
                else:
                    providers.clear()
                    started = time.monotonic()
                    results = Bench(output / cell).run(scenario, architecture, persona, seed=42)
                    row.update(status='executed', runs=len(results),
                               decisions=sum(r.trace['decisions'] for r in results),
                               wall_ms=round((time.monotonic() - started) * 1000))
                    evaluated = assess(scenario, results)
                    checks.extend({'cell': cell, **check} for check in evaluated)
                    for result in results:
                        with Store.open(result.path / 'persona.db', readonly=True) as store:
                            integrity = verify(store) + store.integrity_problems()
                        technical = [c['status'] for c in evaluated if c['run'] == result.trace['run'] and c['class'] == 'technical']
                        decisions = result.trace['decision_trace']
                        calls = sum(p['response'].get('usage', {}).get('calls', 0)
                                    for d in decisions for p in d['proposals'])
                        # Ответы с ошибкой могут отсутствовать. Запрошенные попытки учитываются отдельно.
                        attempts = sum(len(d['requests']) for d in decisions)
                        unavailable = any(d['report']['outcome'] == 'no_decision' for d in decisions)
                        row['calls'] += calls
                        run = {'cell': cell, 'run': result.trace['run'], 'trace': str(result.path / 'trace.json'),
                               'sha256': hashlib.sha256(result.trace_bytes).hexdigest(), 'calls_reported': calls,
                               'protocol_requests': attempts, 'status': result.trace['status'], 'integrity': 'pass' if not integrity else 'fail',
                               'technical_pass': technical.count('pass'), 'technical_fail': technical.count('fail'),
                               'technical_unverified': technical.count('unverified'),
                               'outcomes': [d['report']['outcome'] for d in decisions],
                               'memory_off': result.trace['switches']['memory_off'],
                               'motivation_off': result.trace['switches']['motivation_off']}
                        runs.append(run)
                        if mode == 'live' or letter == 'C':
                            blind_results.append(result)
                        if unavailable and letter != 'C':
                            stopped = True
                            errors = [s['detail']['error'] for d in decisions for s in d['report']['stages']
                                      if 'error' in s['detail']]
                            row['reason'] = 'нет решения: ' + (errors[0] if errors else 'provider_error')
                    row['calls_attempted'] = sum(p.calls for p in providers)
                    # Пишем промежуточный журнал после каждой ячейки, чтобы обрыв не потерял выполненное.
                    (output / 'runs.json').write_text(json.dumps(runs, ensure_ascii=False, indent=2) + '\n')
                matrix.append(row)
                write_csv(output / 'matrix.csv', matrix, list(row))
    write_csv(output / 'checks.csv', checks, ['cell', 'run', 'assertion', 'class', 'status', 'reason', 'trace'])
    assessments = {}
    for letter, _, _ in architectures:
        selected = [c for c in checks if c['cell'].startswith(letter + '-')]
        technical = [c['status'] for c in selected if c['class'] == 'technical']
        causal = [c['status'] for c in selected if c['class'] == 'causal']
        complete = len([r for r in matrix if r['variant'] == letter and r['status'] == 'executed']) == 16
        score = None if not complete or not causal or 'unverified' in causal else 1 + 4 * causal.count('pass') / len(causal)
        assessments[letter] = {'technical_pass': technical.count('pass'),
                               'technical_fail': technical.count('fail'),
                               'technical_unverified': technical.count('unverified'),
                               'causal_pass': causal.count('pass'), 'causal_fail': causal.count('fail'),
                               'causal_unverified': causal.count('unverified'),
                               'causality': score, 'liveliness': None, **rank(eligibility(letter, matrix, runs, technical, mode), score, None)}
    summary = {'mechanism': 'conversation-v1' if conversational else 'original', 'assessments': assessments, 'mode': mode, 'matrix_cells': len(matrix), 'executed': sum(r['status'] == 'executed' for r in matrix),
               'subruns': len(runs), 'decisions': sum(r['decisions'] for r in matrix),
               'complexity': {letter: complexity(root, directory) for letter, _, directory in architectures},
               'runs': runs}
    (output / 'summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2) + '\n')
    if blind_results:
        build_blind(blind_results, output / 'blind', output / 'private' / 'key.json')
    return summary
