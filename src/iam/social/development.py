"""Личные основания отношения видны рядом с разговором, а не только числа состояния."""
import json


def relationship_context(memory, context, trigger):
    if memory.off:
        return []
    visible = {item['ref'] for item in context}
    rows = {row.memory_id: row for row in memory.records()}
    people = {person: profile for person, profile in memory.history().items()
              if any(ref in visible for ref in profile['history'])}
    result = []
    for person, profile in list(people.items())[-2:]:
        notes = [row for row in rows.values() if row.version == 1
                 and row.origin == 'own_hypothesis'
                 and row.content.get('interpretation', {}).get('kind') == 'social'
                 and row.content['interpretation'].get('person') == person]
        for row in sorted(notes, key=lambda r: (r.created_life_ms, r.memory_id))[-2:]:
            packet = {'kind': 'relationship_experience', 'person': person, 'text': row.text,
                      'interpretation': row.content['interpretation'], 'sources': list(row.sources),
                      'meaning': 'Твоё прежнее отношение и его основания. Оно может изменяться; это не обязанность соглашаться.'}
            detail = json.dumps(packet, ensure_ascii=False)
            if len(detail) <= memory.text_limit and memory._safe_item(packet):
                result.append({'ref': row.memory_id, 'origin': row.origin,
                               'layer': 'interpretation', 'text': detail})
        if getattr(trigger, 'type', None) == 'input.read':
            continue  # Для текущего прочтения точный формат уже предъявлен SocialMemory.
        recent = [ref for ref in profile['history'] if ref in visible and ref in rows]
        if recent:
            rule = {'kind': 'social_update_option', 'person': person, 'source': recent[-1],
                    'rule': 'Можно переосмыслить этот уже прочитанный разговор и своё отношение. '
                            'Вывод и изменение необязательны. Близость, доверие и готовность независимы.',
                    'format': {'kind': 'memory_write', 'cites': [recent[-1]], 'body': {'draft': {
                        'kind': 'semantic', 'origin': 'own_hypothesis', 'content': {
                            'text': 'своя интерпретация до 512 знаков', 'social': {'person': person}}}}},
                    'optional_fields': 'social: хотя бы одно из closeness, trust_as_source, readiness от0до1; '
                                       'для readiness нужен topic, без readiness topic не нужен.'}
            detail = json.dumps(rule, ensure_ascii=False)
            if len(detail) <= memory.text_limit and memory._safe_item(rule):
                result.append({'ref': trigger.event_id, 'origin': 'model_knowledge', 'layer': 'state', 'text': detail})
    return result
