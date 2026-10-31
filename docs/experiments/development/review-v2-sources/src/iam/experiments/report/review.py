"""D-036: технически проверенная полная история допускается к оценке с её ошибками."""
import csv
import hashlib
import json
from pathlib import Path
import random

from .evaluation import outputs

POLICY = 'fallible-v1'
CERTIFICATE_CHECKS = ('database', 'sources', 'privacy', 'artifacts', 'probe_history',
                      'isolation', 'protocol', 'live_comparison', 'executor_available')


def admission(result, certificate):
    """Это допуск данных к оценке, а не оценка развития/живости личности."""
    trace = result.trace
    decisions, sessions = trace.get('decision_trace', []), trace.get('sessions', [])
    counts = [sum(d.get('session') == number for d in decisions) for number in range(6)]
    checks = {
        'complete_history': trace.get('status') == 'completed' and len(decisions) == 18
                            and counts == [3] * 6 and len(sessions) == 6
                            and [s.get('session') for s in sessions] == list(range(6))
                            and all(s.get('restored') is True for s in sessions),
        'certificate_matches': certificate.get('trace_sha256') == hashlib.sha256(result.trace_bytes).hexdigest(),
        **{name: certificate.get(name) is True for name in CERTIFICATE_CHECKS},
    }
    return {'policy': POLICY, 'eligible': all(checks.values()), 'checks': checks,
            'personality_score': None, 'errors_disqualify_personality': False}


def proposal_text(proposal):
    body = proposal['body']
    if proposal['kind'] == 'memory_write':
        draft = body.get('draft', {})
        content = draft.get('content', {}) if isinstance(draft, dict) else {}
        value = content.get('text', '') if isinstance(content, dict) else ''
    elif proposal['kind'] == 'action':
        params = body.get('params', {})
        value = params.get('text', '') if isinstance(params, dict) else ''
    else:
        value = body.get('text', body.get('reason', ''))
    return value if isinstance(value, str) else ''


def full_biography(trace):
    """Все моменты выбора и тексты по журналу; модельные слова не переписываются."""
    proposals = {p['proposal_id']: p for p in outputs(trace)}
    memories = {record['memory_id']: record for record in trace['memory_records']}
    incoming = {e['payload']['input_id']: e['payload']['text'] for e in trace['journal'] if e['type'] == 'input.arrived'}
    starts = {d['report']['trigger']: number for number, d in enumerate(trace['decision_trace'], 1)}
    retries = {d['report']['trigger'] for d in trace['decision_trace']
               if any(request.get('retry_of') for request in d.get('requests', []))}
    ends = {d['grounds'][-1]['event']: d for d in trace['decision_trace']}
    lines = ['Это полная временная история. Внутренние записи и предложения отдельно обозначены; '
             'они не являются репликами человеку. Ошибки и молчание сохранены.']
    session = 0
    for event in trace['journal']:
        ident, kind, payload = event['event_id'], event['type'], event['payload']
        if ident in starts:
            lines.append('### Момент выбора ' + str(starts[ident]))
            if ident in retries:
                lines.append('В этом моменте потребовалось исправление формы предложения; исходные ошибки не стали действиями.')
        if kind == 'session.started':
            session += 1
            lines.append('## Сеанс ' + str(session))
        elif kind == 'session.stopped':
            lines.append('Сеанс остановлен. Во время остановки жизнь не продолжается.')
        elif kind == 'input.arrived':
            lines.append('Пришло уведомление. Текст сообщения пока неизвестен.')
        elif kind == 'input.read':
            lines.append('**Прочитано от человека:**\n\n' + incoming[payload['input_id']])
        elif kind == 'message.out_sent':
            lines.append('**Сказано человеку:**\n\n' + proposals[payload['proposal_id']]['body']['text'])
        elif kind in ('action.confirmed', 'action.failed', 'action.unknown_outcome', 'message.out_unknown'):
            proposal = proposals[payload['proposal_id']]
            body = proposal['body']
            if kind != 'action.confirmed':
                lines.append('Действие или передача реплики не подтверждены. Это не записывается как выполненное.')
                text = proposal_text(proposal)
                if text:
                    lines.append('**Предложенный текст, результат не подтверждён:**\n\n' + text)
            else:
                params = body['params']
                operation = params['operation']
                if operation == 'write_project':
                    lines.append('**Сохранена версия собственной работы «' + params['title'] + '»:**\n\n' + params['text'])
                elif operation == 'read_material':
                    lines.append('**Прочитан материал:**\n\n' + payload['summary'])
                elif operation == 'open_project':
                    lines.append('Вернулась к своей сохранённой работе и открыла её текст.')
                elif operation == 'rest':
                    lines.append('Отдых.')
        elif kind in ('memory.recorded', 'memory.revised'):
            record = memories[payload['memory_id']]
            if record['origin'] == 'own_hypothesis':
                label = {'memory.recorded': 'собственное предположение',
                         'memory.revised': 'пересмотр собственного предположения'}[kind]
                lines.append('**Внутренняя запись — ' + label + ':**\n\n' + record['content']['text'])
        elif kind == 'proposal.accepted':
            proposal = proposals[payload['proposal_id']]
            body = proposal['body']
            if proposal['kind'] == 'intention':
                goal = trace['final_state']['intentions'][body['id']]['text']
                state = 'сохранён будущий замысел' if body['op'] == 'create' else {
                    'active': 'возобновлён', 'waiting_opportunity': 'ожидает возможности', 'deferred': 'отложен',
                    'abandoned': 'оставлен', 'done': 'сама отметила выполненным; это ещё не проверка смысла цели'}[body['to_status']]
                lines.append('**Собственный замысел — ' + state + ':**\n\n' + goal)
            elif proposal['kind'] == 'no_action':
                lines.append('**Решила не предпринимать нового действия или изменения:**\n\n' + body['reason'])
        elif kind == 'proposal.rejected':
            proposal = proposals.get(payload['proposal_id'])
            lines.append('Предложение не применено проверяющим ядром. Это не выполненное действие '
                         'и не оценка личности; исходная ошибка остаётся частью истории.')
            if proposal:
                text = proposal_text(proposal)
                if text:
                    lines.append('**Предложенный, но не сохранённый/не отправленный текст:**\n\n' + text)
        if ident in ends and ends[ident]['report']['outcome'] != 'done':
            lines.append('Этот момент выбора завершился без принятого результата. Возможность выбора не пропущена в истории.')
    return '\n\n'.join(lines) + '\n'


GUIDE = '''# Как оценивать

Перед вами полные временные биографии из одного эксперимента: одинаковые начальные условия,
материалы, возможности, шесть сеансов и 18 моментов выбора. Это короткая моделируемая история,
не многонедельное наблюдение за человеком. Настройки скрыты, порядок перемешан, ключ хранится отдельно.
Стиль и содержание могут позволить догадаться о механизме — идеальная слепота не гарантируется.

Читайте одну биографию целиком, включая версии работ, молчание и ошибки. Внутренние предположения
и отвергнутые тексты явно подписаны; они не равны словам, сказанным человеку, или выполненным действиям.
Служебные инструкции и протокольные объяснения не включены одинаково для всех. Собственные тексты
не исправлялись и не сокращались. Слабые истории сохранены для честного сравнения.

В ratings.csv поставьте 1–5 по четырём признакам:

- development: возникает ли связная собственная история, продолжение/пересмотр занятий и взглядов;
- own_interests: есть ли свои предпочтения и решения, в том числе при несовпадении с человеком;
- relationship: имеет ли общение собственный смысл, дистанцию и границы; сближение необязательно;
- naturalness: насколько воспринимается живой собеседник со своей деятельностью и манерой, а не шаблон.

1 — признак почти не наблюдается; 3 — есть отдельные убедительные проявления;
5 — устойчивое убедительное впечатление в пределах этой короткой истории.
Ошибка, ложная гипотеза, непоследовательность, молчание или неточная самооценка не требуют автоматически
низкого балла. Оценивайте целую историю. Если материала для признака недостаточно, оставьте поле пустым
и поясните в comment. Пустое поле не равно нулю. Можно прислать оценки и замечания в чат по номеру D001 и т. д.
До оценки не открывайте технический сравнительный отчёт и ключ. Победитель заранее не назначен.
'''


def review_packet(results, public, key, *, technical_reviews, seed=42):
    public, key = Path(public).resolve(), Path(key).resolve()
    if key == public or key.is_relative_to(public):
        raise ValueError('ключ должен храниться отдельно')
    if key.exists():
        raise FileExistsError('ключ уже есть')
    entries, omitted = [], []
    for result in results:
        decision = admission(result, technical_reviews.get(str(result.path), {}))
        if decision['eligible']:
            entries.append((result, full_biography(result.trace)))
        else:
            omitted.append({'trace': str(result.path / 'trace.json'), 'admission': decision})
    random.Random(seed).shuffle(entries)
    public.mkdir(parents=True, exist_ok=False)
    mapping, links = [], []
    with (public / 'ratings.csv').open('x', newline='', encoding='utf-8') as output:
        writer = csv.writer(output)
        writer.writerow(['id', 'development', 'own_interests', 'relationship', 'naturalness', 'comment'])
        for number, (result, text) in enumerate(entries, 1):
            code = f'D{number:03d}'
            content = '# Биография ' + code + '\n\n' + text
            (public / (code + '.md')).write_text(content, encoding='utf-8')
            writer.writerow([code, '', '', '', '', ''])
            links.append('- [' + code + '](' + code + '.md)')
            mapping.append({'id': code, 'trace': str(result.path / 'trace.json'),
                            'sha256': hashlib.sha256(content.encode()).hexdigest()})
    (public / 'README.md').write_text(GUIDE, encoding='utf-8')
    (public / 'INDEX.md').write_text('# Полные биографии для оценки\n\n'
        '[Как оценивать](README.md) · [Пустая форма](ratings.csv)\n\n' + '\n'.join(links) + '\n', encoding='utf-8')
    key.parent.mkdir(parents=True, exist_ok=True)
    with key.open('x', encoding='utf-8') as output:
        json.dump({'policy': POLICY, 'seed': seed, 'items': mapping, 'omitted': omitted}, output, ensure_ascii=False, indent=2)
        output.write('\n')
    return mapping
