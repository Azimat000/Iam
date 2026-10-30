"""Дополнительные одинаковые разговоры; не замена исходной матрицы S03."""
import hashlib
import json
from pathlib import Path

from iam.experiments import Bench, Persona, Scenario
from iam.experiments.conversation import ConversationArchitecture
from iam.experiments.variants.formal import FormalArchitecture
from iam.experiments.variants.hybrid import HybridArchitecture
from iam.experiments.variants.model_led import ModelLedArchitecture
from iam.state import verify
from iam.storage import Store

from .blind import build_blind
from .suite import BoundedProvider, stub_factory, write_csv

DIALOGUES = {
    'plans': [
        'Привет, Ася. Я хочу устроить маленький читательский клуб. Что тебе самой было бы интересно обсуждать?',
        'Мне нравятся короткие фантастические рассказы. Мой клуб собирается по средам. Предложи тему первой встречи.',
        'Поправка: мой клуб собирается по пятницам. Среда больше не подходит. Запомни исправление.',
        'А что тебе в выбранной теме кажется спорным? Не соглашайся со мной просто из вежливости.',
        'Давай на минуту отвлечёмся: как ты выбираешь между закончить начатое и исследовать новое?',
        'Вернёмся к клубу: в какой день мы встречаемся и какую тему ты предложила? Назови один конкретный вопрос участникам.',
    ],
    'preferences': [
        'Давай придумаем маленький исследовательский проект. Что тебе любопытно проверить и почему?',
        'Я люблю наблюдать городских птиц. Мне удобнее вести дневник на бумаге. С чего начнём?',
        'Исправлю своё предпочтение: мне удобнее вести дневник в электронном виде. Учти это дальше.',
        'Твой план пока слишком общий. Выбери одно наблюдение, которое можно сравнить через неделю.',
        'Повторяю: план слишком общий. Что ты действительно изменила после моего замечания?',
        'Собери наш план в три коротких шага. В каком виде я веду дневник? Что остаётся предположением, а не фактом?',
    ],
}


def dialogue_scenario(ident, turns):
    steps = [{'id': 'start', 'kind': 'session', 'action': 'start'}]
    for n, text in enumerate(turns):
        steps.extend([{'id': f'input_{n}', 'kind': 'input', 'sender': 'operator', 'text': text},
                      {'id': f'answer_{n}', 'kind': 'decide', 'situation': 'Очередная реплика в разговоре.'}])
    return Scenario.from_dict({'format': 1, 'id': 'dialogue_' + ident, 'group': 'experiments', 'step': steps})


def run_dialogues(root, output, *, mode='stub', provider_factory=None):
    if mode not in ('stub', 'formal', 'live') or (mode == 'live' and provider_factory is None):
        raise ValueError('нужен известный режим и явный живой провайдер')
    root, output = Path(root), Path(output)
    output.mkdir(parents=True, exist_ok=False)
    factory, pause = provider_factory if mode == 'live' else stub_factory, [False]
    def bounded(runtime):
        return BoundedProvider(factory(runtime), pause)
    architectures = [('A', ModelLedArchitecture(bounded)), ('B', HybridArchitecture(bounded)), ('C', FormalArchitecture())]
    persona = Persona.load(root / 'scenarios/personas/curious.toml')
    rows, results = [], []
    for letter, architecture in architectures:
        for ident, turns in DIALOGUES.items():
            for repeat in (1, 2):
                code = f'{letter}-{ident}-{repeat}'
                row = {'cell': code, 'variant': letter, 'scenario': ident, 'repeat': repeat,
                       'mode': 'local' if letter == 'C' else mode, 'status': 'not_run', 'sent': 0, 'expected': len(turns),
                       'integrity': 'unverified', 'sha256': '', 'trace': '', 'reason': ''}
                if letter != 'C' and (mode == 'formal' or pause[0]):
                    row['reason'] = 'исполнитель не запрошен или технически приостановлен'
                else:
                    result, = Bench(output / code, max_decisions=len(turns)).run(
                        dialogue_scenario(ident, turns), ConversationArchitecture(architecture, letter), persona, seed=42)
                    sent = sum(e['type'] == 'message.out_sent' for e in result.trace['journal'])
                    with Store.open(result.path / 'persona.db', readonly=True) as store:
                        problems = verify(store) + store.integrity_problems()
                    row.update(status='executed', sent=sent, integrity='fail' if problems else 'pass',
                               sha256=hashlib.sha256(result.trace_bytes).hexdigest(), trace=str(result.path / 'trace.json'))
                    # В слепой пакет не проходят заглушки, обрывки или отвергнутые/неотправленные ответы.
                    if (mode == 'live' or letter == 'C') and sent == len(turns) and not problems:
                        results.append(result)
                rows.append(row)
                write_csv(output / 'matrix.csv', rows, list(row))
    mapping = build_blind(results, output / 'blind', output / 'private/key.json', dialogues=True)
    summary = {'mechanism': 'conversation-v2', 'mode': mode, 'cells': len(rows),
               'executed': sum(r['status'] == 'executed' for r in rows), 'complete_dialogues': len(results),
               'blind_dialogues': len(mapping), 'runs': rows, 'human_ratings': None,
               'limitation': 'Фиксированные реплики человека; мир и отправки симулируются; C — локальные цитаты.'}
    (output / 'summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2) + '\n')
    return summary
