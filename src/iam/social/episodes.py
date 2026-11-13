"""Ограниченные фрагменты доступной переписки вокруг найденных реплик."""
import json

from iam.memory.search import relevance
from iam.memory.window import text_window
from .context import adjacent, authorship, boundaries


def episode_context(memory, trigger, records, context):
    if memory.off or getattr(trigger, 'type', None) != 'input.read' or not records:
        return []
    turns = memory._turns()
    visible = {row.memory_id for row in records} | {item['ref'] for item in context}
    candidates = [turn for turn in turns if turn[2] == 'incoming'
                  and turn[1].memory_id in visible and trigger.event_id not in turn[1].sources]
    scores = relevance(memory.retrieval_query(trigger), [turn[1] for turn in candidates])
    ranked = sorted((turn for turn in candidates if turn[1].memory_id in scores),
                    key=lambda turn: (-scores[turn[1].memory_id], turn[0]))[:2]
    if not ranked:
        return []
    seeds = ranked
    links, edges = adjacent(memory, turns), boundaries(memory)
    positions = {turn[1].memory_id: i for i, turn in enumerate(turns)}
    selected = {}
    for seed in seeds:
        ref = seed[1].memory_id
        chain = [seed]
        answer = links.get(ref)
        if answer is not None:
            chain.append(answer)
        for _ in range(2):
            tail = chain[-1]
            index = positions[tail[1].memory_id] + 1
            if index >= len(turns):
                break
            following = turns[index]
            arrival = following[6]
            # Нельзя перескочить пропуск или принять позднее чтение за продолжение.
            if not (following[2] == 'incoming' and following[3] == seed[3]
                    and arrival is not None and tail[0] < arrival < following[0]
                    and not any(tail[0] < seq < following[0] and seq != arrival for seq in edges)):
                break
            chain.append(following)
            reply = links.get(following[1].memory_id)
            if reply is not None:
                chain.append(reply)
        for turn in chain:
            if trigger.event_id not in turn[1].sources and turn[4] is not None:
                selected.setdefault(turn[1].memory_id, (turn, ref))
    result, budget = [], 10000
    for turn, seed in selected.values():
        row = turn[1]
        metadata = {**authorship(turn), 'kind': 'conversation_episode', 'version': row.version,
                    'sources': list(row.sources), 'recall_role': 'conversation_episode',
                    'episode_seed': seed, 'relation': 'temporal_neighbor_not_verified_reply',
                    'content_available': True}
        packed = text_window(metadata, turn[4], min(2000, memory.text_limit, budget))
        if packed is None:
            break
        packet = json.loads(packed)
        # Как в dialogue(): это обзор слов, не страница канонического row.text.
        packet['excerpt'] = packet.pop('text_window')
        packed = json.dumps(packet, ensure_ascii=False)
        item = {'ref': row.memory_id, 'origin': row.origin, 'layer': 'state', 'text': packed}
        if memory._safe_item(item):
            result.append(item)
            budget -= len(packed)
    return sorted(result, key=lambda item: json.loads(item['text'])['seq'])
