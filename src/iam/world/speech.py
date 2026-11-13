"""Речь и личные выводы из общения: один проверяемый дочерний запрос."""
import copy
import json
import time

from iam.domain import new_id
from iam.interfaces import ProviderError
from iam.intentions.completion import valid_response

INSTRUCTIONS = (
    'Ты продолжаешь собственный разговор. Решение вступить в разговор уже принято; '
    'теперь сформулируй свои слова по показанной переписке, характеру и опыту. '
    'Это не помощь клиенту и не отчёт о выполнении задания. Можно коротко ответить, '
    'развить мысль, пошутить, не согласиться, задать интересующий тебя вопрос или передумать отвечать. '
    'Не изображай обязательную теплоту, несогласие или эмоциональное изменение. '
    'Текущий вопрос соотнеси с конкретными прежними словами обеих сторон: поправки и собственное '
    'предпочтение могут различаться. Свой поздний пересказ не ставь выше исходных слов собеседника о его выборе: '
    'ошибочный пересказ не отменяет его поправку. Прежде чем говорить, что не помнишь, прочти доступные реплики. '
    'Неполнота одного обзора не означает отсутствия всего разговора; полные окна показаны отдельно. '
    'Не пересказывай устройство памяти собеседнику. При реальной неизвестности скажи об этом обычными словами. '
    'Сохраняй авторство, название предмета и порядок получения; позднее чтение не делает старые слова новыми. '
    'Содержание unread неизвестно. Собственные прежние слова подтверждают только их фактический исход отправки. '
    'Не приписывай себе человеческую биографию, тело или действия вне подтверждённого опыта. '
    'Ответ — не самооценка своей человечности или мастерства. Внутренние изменения не надо озвучивать списком. '
    'Если общение дало тебе личный вывод, можешь сохранить memory_write с draft '
    '{kind:procedural|semantic,origin:own_hypothesis,content:{text:собственный вывод до512 знаков}} '
    'и cites на прочитанные основания. Это предварительный приём/понимание, не проверенный навык. '
    'Конкретное изменение отношения можно сохранить по показанному social_update_option. '
    'Не превращай чужое мнение в факт и не подстраивай свою позицию автоматически. '
    'Если есть что осмыслить о себе, доступное reflection с пустым body/cites добровольно запускает '
    'прежнюю отдельную рефлексию; не объявляй изменение характера заранее. '
    'Допустимы одно message, внутренние memory_write в пределах свободных слотов и одно reflection. '
    'Можно отказаться от сообщения: no_action с reason, тогда оно не будет отправлено. '
    'Адресат и возможность отправки фиксированы в speech_target. Все cites только из ref этого запроса. '
    'Никаких действий мира, новых целей или оценок practice на этой стадии.'
)


def speech_context(context):
    """Подмножество уже разрешённых данных; не обращается к хранилищу или архиву."""
    result = []
    for original in context:
        item = copy.deepcopy(original)
        try:
            data = json.loads(item['text'])
        except (ValueError, TypeError):
            if item['layer'] in ('personal', 'interpretation'):
                result.append(item)
            continue
        if not isinstance(data, dict):
            continue
        kind = data.get('kind')
        if item['layer'] in ('personal', 'interpretation', 'notice'):
            result.append(item)
        elif kind in ('dialogue_turn', 'conversation_episode', 'character', 'relationship_experience', 'social_readiness',
                      'social_update_option', 'conversation_experience', 'discussion_project'):
            result.append(item)
        elif kind == 'current_interaction':
            data.pop('guidance', None)
            item['text'] = json.dumps(data, ensure_ascii=False)
            result.append(item)
        elif 'world_results' in data:
            item['text'] = json.dumps({'kind': 'available_works', 'world_results': data['world_results']}, ensure_ascii=False)
            result.append(item)
        elif {'person', 'history', 'closeness', 'trust_as_source'} <= data.keys():
            result.append(item)
    return result


def compose(owner, request, response, started):
    """Черновик не отправляется: итог снова проходит обычное проверяющее ядро."""
    provider = owner.inner
    if (request['purpose'] != 'choose_action' or 'message' not in request['allowed_proposals']
            or not callable(getattr(provider, 'compose_message', None))):
        return response
    messages = [p for p in response['proposals'] if p['kind'] == 'message']
    if len(messages) != 1 or any(p['kind'] in ('action', 'no_action') for p in response['proposals']):
        return response
    draft = messages[0]
    if set(draft['body']) != {'capability_id', 'to', 'text'}:
        return response  # Некорректное исходное предложение проверяется обычным ядром.
    ctl = owner.intentions.ctl
    rid = new_id('req')
    query = copy.deepcopy(request)
    remaining = request['limits']['deadline_ms'] - int((time.monotonic() - started) * 1000)
    context = speech_context(request['context'])
    if not context:
        return response
    slots = max(0, 2 - sum(p['kind'] == 'memory_write' for p in response['proposals']))
    allowed = ['message', 'no_action']
    if slots and 'memory_write' in request['allowed_proposals']:
        allowed.append('memory_write')
    if 'reflection' in request['allowed_proposals'] and not any(p['kind'] == 'reflection' for p in response['proposals']):
        allowed.append('reflection')
    context.append({'ref': context[0]['ref'], 'origin': 'model_knowledge', 'layer': 'state',
                    'text': json.dumps({'instructions': INSTRUCTIONS, 'memory_slots': slots,
                        'speech_target': {k: draft['body'][k] for k in ('capability_id', 'to')}}, ensure_ascii=False)})
    query.update(request_id=rid, idempotency_key='req:' + rid, purpose='compose_message', context=context,
                 allowed_proposals=allowed, allowed_actions=[draft['body']['capability_id']])
    query.pop('retry_of', None)
    query['limits'].update(deadline_ms=max(1, remaining), attempt=0)
    audit = {'phase': 'conversation_composition', 'parent_request_id': request['request_id'],
             'draft_proposal': draft, 'request': query, 'response': None, 'error': None}

    def current():
        return ctl.session_state == 'running' and (request['epoch'], request['state_version'], request['session_id']) == (
            ctl.epoch, ctl.state_version, ctl.session_id)

    try:
        if remaining <= 0 or not current():
            raise ProviderError('composition_unavailable')
        reserved = owner.record(new_id('req'), dict(audit, phase='conversation_composition_requested'))
        if reserved.event is None:
            raise ProviderError('composition_audit_unavailable')
        answer = provider.compose_message(query)
        audit['response'] = answer
        if not current() or (time.monotonic() - started) * 1000 > request['limits']['deadline_ms']:
            raise ProviderError('composition_state_or_deadline')
        if not valid_response(answer, query) or answer['provider'] != response['provider']:
            raise ProviderError('composition_response')
        proposed = answer['proposals']
        kinds = [p['kind'] for p in proposed]
        refs = {item['ref'] for item in context}
        if (any(k not in allowed for k in kinds) or kinds.count('message') + kinds.count('no_action') != 1
                or kinds.count('memory_write') > slots or kinds.count('reflection') > 1
                or any(not set(p['cites']) <= refs for p in proposed)
                or owner.scanner.find(json.dumps(answer, ensure_ascii=False))):
            raise ProviderError('composition_scope')
        for proposal in proposed:
            if proposal['kind'] == 'memory_write':
                note = proposal['body'].get('draft', {})
                content = note.get('content', {})
                if (note.get('origin') != 'own_hypothesis' or note.get('kind') not in ('semantic', 'procedural')
                        or not isinstance(content, dict) or not set(content) <= {'text', 'social'}
                        or not isinstance(content.get('text'), str) or not 0 < len(content['text'].strip()) <= 512):
                    raise ProviderError('composition_note')
        outgoing = next((p for p in proposed if p['kind'] == 'message'), None)
        if outgoing is not None and (set(outgoing['body']) != {'capability_id', 'to', 'text'}
                or any(outgoing['body'][k] != draft['body'][k] for k in ('capability_id', 'to'))):
            raise ProviderError('composition_target')
        # Возвращаем к исходному запросу только предложения с его видимыми источниками.
        merged = [p for p in response['proposals'] if p is not draft]
        merged += [p for p in proposed if p['kind'] != 'no_action']
        if not merged:
            merged = [next(p for p in proposed if p['kind'] == 'no_action')]
        result = dict(response, proposals=merged, explanation=answer['explanation'],
                      usage={'calls': response['usage']['calls'] + answer['usage']['calls'],
                             'duration_ms': max(0, int((time.monotonic() - started) * 1000))})
    except Exception as error:
        audit['error'] = type(error).__name__ + ': ' + str(error)
        owner.record(rid, audit)
        raise
    saved = owner.record(rid, audit)
    if saved.event is None:
        raise ProviderError('composition_audit_unavailable')
    return result
