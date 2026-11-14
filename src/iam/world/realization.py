"""Речь по отобранным свидетельствам; сырые старые реплики уже прочитаны отдельно."""
import copy
import json


def realization_context(context, readings):
    if not readings:
        return copy.deepcopy(context)
    current = readings[0]['ref']
    result = []
    for original in context:
        item = copy.deepcopy(original)
        try:
            data = json.loads(item['text'])
        except (ValueError, TypeError):
            result.append(item)
            continue
        if isinstance(data, dict):
            utterance = (item['layer'] == 'state' and data.get('kind') in ('dialogue_turn', 'conversation_episode')
                         or item['layer'] == 'personal' and data.get('statement_scope') == 'utterance_only')
            if utterance and item['ref'] != current:
                continue
            if data.get('kind') == 'current_interaction' and isinstance(data.get('last_outgoing'), dict):
                for key in ('text', 'excerpt'):
                    data['last_outgoing'].pop(key, None)
                item['text'] = json.dumps(data, ensure_ascii=False)
        result.append(item)
    for reading in readings:
        result.append(copy.deepcopy(reading))
        data = json.loads(reading['text'])
        for finding in data['findings']:
            for evidence in finding['evidence']:
                # Собственный верхний ref сохраняет право сослаться на исходные слова.
                result.append({'ref': evidence['ref'],
                               'origin': 'stated_by_other' if evidence['speaker'] == 'interlocutor' else 'observation',
                               'layer': 'state', 'text': json.dumps({
                                   'kind': 'conversation_quote', 'finding_scope': finding['scope'],
                                   'selection_is_hypothesis': True, **evidence}, ensure_ascii=False)})
    return result
