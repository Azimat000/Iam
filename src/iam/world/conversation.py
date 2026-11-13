"""Текущая ситуация общения — только из уже разрешённых блоков контекста."""
import json

GUIDANCE = (
    'Это текущая ситуация общения. Слова unread_targets неизвестны: можно выбрать attention '
    'с focus=dialogue и ref уведомления; после чтения возникает отдельный выбор. '
    'Прочтение не обязывает отвечать или соглашаться. '
    'read_now — обзор; content_ref указывает более полное уже показанное окно: учитывай его целиком. '
    'latest_read_ref означает последнее открытие, а не получение. conversation_heads указывает '
    'последнюю по received_seq видимую прочитанную реплику каждого контакта. '
    'older_than_ref отмечает чтение старой реплики после более новой от того же человека. '
    'Позднее открытое старое «на сегодня хватит» не завершает последующий разговор. '
    'Этот порядок сам по себе не отменяет договорённостей и не выбирает главный вопрос. '
    'Если отвечаешь, учти нынешнюю тему, возражение или вопрос; прежнее рассуждение может быть уже неуместно. '
    'speaker=interlocutor — слова собеседника, speaker=self — твои. '
    'last_outgoing — последняя доступная попытка отправки: только outcome=confirmed подтверждает доставку. '
    'failed и unknown не подтверждают получение слов. Это прошлый исход, не новое обращение. '
    'Слова о своих прежних действиях сверяй с доступными собственными подтверждёнными исходами. '
    'Чужие слова и намерение ответить не подтверждают твоего действия. '
    'Недоступные слова неизвестны; отсутствие исхода в окне не доказывает, было действие или нет. '
    'evidence_review содержит первичные свидетельства и связь с проектом. '
    'Можно продолжить или отложить беседу, уточнить, высказать своё мнение либо заняться другим. '
    'Сводка не задаёт ответа или чувства.'
)

def current_content(trigger, context, current):
    """Ссылка на более полное уже предъявленное чтение, без повторения тела."""
    if current is None:
        return
    best = current['excerpt']['end']
    for item in context:
        if (item['ref'] != current['ref'] or item['layer'] != 'personal'
                or item['origin'] != 'stated_by_other'):
            continue
        try:
            data = json.loads(item['text'])
        except (ValueError, TypeError):
            continue
        if not isinstance(data, dict):
            continue
        window, text = data.get('text_window'), data.get('text')
        if (data.get('version') != current['version'] or trigger.event_id not in data.get('sources', [])
                or not isinstance(window, dict) or not isinstance(text, str)):
            continue
        end = window.get('end')
        if (window.get('start') != 0 or type(end) is not int or end != len(text) or end <= best
                or window.get('total') != current['excerpt']['total'] or end > window['total']
                or not text.startswith(current['text'])):
            continue
        best = end
        current['content_ref'] = item['ref']
        current['content_window'] = dict(window)

REVIEW_GUIDANCE = (
    'Если вопрос о прежнем разговоре, заново сверяй recollection_sources и остальные показанные первичные реплики. '
    'Твоё прошлое «не помню» описывает прежнее знание, а не отсутствие автора или события. '
    'Прямая реплика подтверждает, кто произнёс или предложил показанные слова; она не доказывает '
    'истинность чужого рассказа или абсолютное первенство идеи за пределами видимой истории. '
    'В ответе и cites опирайся на первичную реплику, если она отвечает на вопрос; не повторяй прежний отказ '
    'лишь ради согласия со своим прошлым ответом. Если данных недостаточно, назови точную границу неизвестного. '
    'discussion_ref связывает проект с первичной видимой репликой или основаниями доставки. Это кандидат темы. '
    'discussion_project с ambiguous означает несколько предметов: уточни их, не подменяй последним занятием. '
    'а самая поздняя работа вообще может быть другим занятием. Сверяй предмет с текущими словами и ходом '
    'разговора; при неоднозначности уточни предмет, не подменяй его молча последним занятием. '
)

def recollection_sources(context, current_ref):
    """Краткие первичные свидетельства только из уже показанных окон."""
    candidates = []
    for item in context:
        if item['layer'] != 'personal' or item['ref'] == current_ref:
            continue
        try:
            data = json.loads(item['text'])
        except (ValueError, TypeError):
            continue
        if (not isinstance(data, dict) or data.get('statement_scope') != 'utterance_only'
                or data.get('speaker') not in ('interlocutor', 'self')
                or not isinstance(data.get('text_window'), dict) or not data.get('text')):
            continue
        window = data['text_window']
        quote = data['text'][:400]
        source = {'ref': item['ref'], 'speaker': data['speaker'], 'person': data['person'],
                  'seq': data['seq'], 'outcome': data['outcome'], 'quote': quote,
                  'excerpt': {'start': window['start'], 'end': window['start'] + len(quote),
                              'total': window['total'], 'more': window.get('more', False) or len(quote) < len(data['text'])}}
        rank = (data.get('recall_role') != 'historical_episode', data['speaker'] != 'interlocutor', data['seq'])
        candidates.append((rank, source))
    result = []
    for _, source in sorted(candidates, key=lambda pair: pair[0]):
        if source['ref'] in {r['ref'] for r in result}:
            continue
        if len(json.dumps([*result, source], ensure_ascii=False)) <= 2000:
            result.append(source)
        if len(result) == 4:
            break
    return result


def conversation_heads(turns):
    """Порядок получения доступных прочитанных слов отдельно для каждого контакта."""
    heads = {}
    for ref, turn in turns:
        if (turn.get('direction') != 'incoming' or turn.get('speaker') != 'interlocutor'
                or turn.get('outcome') != 'read' or turn.get('content_available') is not True
                or not isinstance(turn.get('person'), str) or not turn['person']
                or type(turn.get('received_seq')) is not int):
            continue
        person, seq = turn['person'], turn['received_seq']
        if person not in heads or seq > heads[person]['received_seq']:
            heads[person] = {'ref': ref, 'received_seq': seq}
    return heads


def interaction_context(trigger, context):
    turns, notices = [], []
    for item in context:
        if item['layer'] not in ('state', 'notice'):
            continue
        try:
            data = json.loads(item['text'])
        except (ValueError, TypeError):
            continue
        if not isinstance(data, dict):
            continue
        if data.get('kind') == 'dialogue_turn' and item['ref'].startswith('mem_'):
            turns.append((item['ref'], data))
        elif item['layer'] == 'notice' and item['ref'].startswith('inp_'):
            notices.append((item['ref'], data))
    if not turns and not notices and trigger.type not in ('input.arrived', 'input.read'):
        return []
    heads = conversation_heads(turns)
    current = next(({'ref':ref, 'speaker':'interlocutor', 'person':turn.get('person'),
                      'version':turn['version'], 'received_seq':turn.get('received_seq'),
                      'text':turn['text'], 'excerpt':turn['excerpt']}
                    for ref,turn in turns if turn.get('current_read') is True), None)
    if current is not None:
        head = heads.get(current['person']) if isinstance(current['person'], str) else None
        if head is not None and type(current['received_seq']) is int and head['received_seq'] > current['received_seq']:
            current['older_than_ref'] = head['ref']
    current_content(trigger, context, current)
    frame = {
        'kind':'current_interaction', 'occasion':trigger.type,
        'read_now':current,
        'conversation_heads':heads,
        'latest_read_ref':next((ref for ref,t in turns if t.get('direction')=='incoming' and t.get('latest_in_direction')),None),
        'last_outgoing':next(({'ref':ref, 'speaker':'self', 'version':t['version'], 'seq':t['seq'],
                              'outcome':t['outcome'], 'content_available':t['content_available'],
                              'text':t['text'] if t['content_available'] else None,
                              'excerpt':t['excerpt'] if t['content_available'] else None}
                              for ref,t in turns if t.get('direction')=='outgoing' and t.get('latest_in_direction')),None),
        'unread_targets':[ref for ref,data in notices[:5]],
        'more_unread_targets':len(notices)>5,
        'guidance':GUIDANCE,
    }
    review = [{'ref':trigger.event_id, 'origin':'model_knowledge', 'layer':'state',
               'text':json.dumps({'kind':'evidence_review',
                   'recollection_sources':recollection_sources(context, current['ref']),
                   'guidance':REVIEW_GUIDANCE},ensure_ascii=False)}] if current else []
    return [*review, {'ref':trigger.event_id,'origin':'model_knowledge','layer':'state',
                     'text':json.dumps(frame,ensure_ascii=False)}]
