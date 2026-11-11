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
    'Можно прямо обозначить желание продолжить или отложить беседу, уточнить непонятное, '
    'выразить собственное мнение либо выбрать другое занятие. Ни готового ответа, ни чувства эта сводка не задаёт.'
)

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
        'unread_targets':[ref for ref,data in notices[:5]],
        'more_unread_targets':len(notices)>5,
        'guidance':GUIDANCE,
    }
    return [{'ref':trigger.event_id,'origin':'model_knowledge','layer':'state',
             'text':json.dumps(frame,ensure_ascii=False)}]
