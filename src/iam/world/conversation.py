"""Текущая ситуация общения — только из уже разрешённых блоков контекста."""
import json

GUIDANCE = (
    'Это текущая ситуация разговора, отдельно от фоновых работ и прежней переписки. '
    'Непрочитанное уведомление означает, что собеседник уже прислал сообщение, а ты ещё не знаешь его слов. '
    'Чтобы узнать их, можно выбрать attention с focus=dialogue и ref из unread_targets; '
    'после принятого чтения возникает отдельный выбор. Слова видны лишь пока запись доступна в памяти. '
    'Прочтение не обязывает соглашаться или отвечать. '
    'read_now — доступный фрагмент прочитанного именно сейчас сообщения. Время чтения не делает '
    'его новым по времени получения: received_seq сохраняет исходный порядок. '
    'Если выбираешь ответить, сначала учти, что собеседник говорит сейчас: он мог сменить тему, '
    'возразить, спросить о тебе или попросить восстановить разговор. Продолжение прежнего рассуждения '
    'само по себе не отвечает на такое обращение. speaker=interlocutor — слова собеседника, speaker=self — твои. '
    'last_outgoing — последняя доступная в этом окне попытка отправки: '
    'только outcome=confirmed подтверждает доставку; failed и unknown не означают, что собеседник получил слова. '
    'Это прошлый исход, а не новое обращение собеседника. Утверждения о том, что ты уже сказала или сделала, '
    'в ответе и в его объяснении сверяй с доступными собственными подтверждёнными исходами. '
    'Чужие слова и текущее намерение ответить не подтверждают твоего прежнего действия. '
    'Если нужного исхода нет в ограниченном окне или его слова недоступны, это неизвестность, '
    'а не доказательство, что действие было или никогда не было. '
    'Если вопрос о прежнем разговоре, заново сверяй recollection_sources и остальные показанные первичные реплики. '
    'Твоё прошлое «не помню» описывает прежнее знание, а не отсутствие автора или события. '
    'Прямая реплика подтверждает, кто произнёс или предложил показанные слова; она не доказывает '
    'истинность чужого рассказа или абсолютное первенство идеи за пределами видимой истории. '
    'В ответе и cites опирайся на первичную реплику, если она отвечает на вопрос; не повторяй прежний отказ '
    'лишь ради согласия со своим прошлым ответом. Если данных недостаточно, назови точную границу неизвестного. '
    'discussion_ref у результата связывает проект с основаниями видимой реплики. Это кандидат темы, '
    'а самая поздняя работа вообще может быть другим занятием. Сверяй предмет с текущими словами и ходом '
    'разговора; при неоднозначности уточни предмет, не подменяй его молча последним занятием. '
    'Можно прямо обозначить желание продолжить или отложить беседу, уточнить непонятное, '
    'выразить собственное мнение либо выбрать другое занятие. Ни готового ответа, ни чувства эта сводка не задаёт.'
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
    current = next(({'ref':ref, 'speaker':'interlocutor', 'version':turn['version'], 'received_seq':turn['received_seq'],
                      'text':turn['text'], 'excerpt':turn['excerpt']}
                    for ref,turn in turns if turn.get('current_read') is True), None)
    frame = {
        'kind':'current_interaction', 'occasion':trigger.type,
        'read_now':current,
        'latest_read_ref':next((ref for ref,t in turns if t.get('direction')=='incoming' and t.get('latest_in_direction')),None),
        'last_outgoing':next(({'ref':ref, 'speaker':'self', 'version':t['version'], 'seq':t['seq'],
                              'outcome':t['outcome'], 'content_available':t['content_available'],
                              'text':t['text'] if t['content_available'] else None,
                              'excerpt':t['excerpt'] if t['content_available'] else None}
                              for ref,t in turns if t.get('direction')=='outgoing' and t.get('latest_in_direction')),None),
        'recollection_sources': recollection_sources(context, current['ref']) if current else [],
        'unread_targets':[ref for ref,data in notices[:5]],
        'more_unread_targets':len(notices)>5,
        'guidance':GUIDANCE,
    }
    return [{'ref':trigger.event_id,'origin':'model_knowledge','layer':'state',
             'text':json.dumps(frame,ensure_ascii=False)}]
