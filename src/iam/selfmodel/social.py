"""Основания из пережитого общения; не подтверждения истинности чужих слов."""
import hashlib
import re
import unicodedata


def pairs(memory, rows, *, cited=None):
    """Только доступное прочтение и собственная непосредственная оценка этого опыта."""
    result = {}
    for note in sorted(rows.values(), key=lambda row: (row.created_life_ms, row.memory_id)):
        if (note.version != 1 or note.origin != 'own_hypothesis' or note.layer != 'interpretation'
                or note.content.get('interpretation', {}).get('kind') not in ('social', 'appraisal')
                or not note.sources or cited is not None and note.memory_id not in cited):
            continue
        source = rows.get(note.sources[0])
        if (source is None or source.version != 1 or source.kind != 'episodic'
                or source.origin != 'stated_by_other' or source.content.get('experience') != 'live'
                or len(source.sources) != 1):
            continue
        event = memory.ctl.find_event(event_id=source.sources[0])
        if event is None or event.type != 'input.read' or not event.payload.get('input_id'):
            continue
        normalized = ' '.join(re.findall(r'\w+', unicodedata.normalize('NFKC', source.text).casefold()))
        if not normalized:
            continue
        fingerprint = hashlib.sha256(('social:' + normalized).encode()).hexdigest()
        key = 'social:' + event.payload['input_id']
        # Повторная оценка того же прочтения не создаёт нового основания.
        result[key] = (source, note, fingerprint, event.life_time_ms)
    return result


def roots(memory, rows, refs):
    cited = set(refs)
    return {key: (fingerprint, at) for key, (source, note, fingerprint, at) in pairs(memory, rows, cited=cited).items()
            if source.memory_id in cited and note.memory_id in cited}


def candidates(model, state, minimum, span):
    """До трёх ещё не использованных пар; никакой черты или дельты за личность."""
    traits = [trait for trait in state['traits'].get('traits', [])
              if isinstance(trait, dict) and type(trait.get('value')) in (int, float)]
    if not traits:
        return []
    rows = model.records()
    available = pairs(model.memory, rows)
    previous = state['selfmodel'].get('reflections', {}).values()
    for trait in traits:
        entries = [entry for entry in previous if entry.get('trait') == trait.get('name')]
        used = {key for entry in entries for key in entry['bases']}
        fingerprints = {key for entry in entries for key in entry['fingerprints']}
        eligible = []
        for key, pair in sorted(available.items(), key=lambda item: (item[1][3], item[0])):
            if key in used or pair[2] in fingerprints:
                continue
            eligible.append(pair)
            fingerprints.add(pair[2])
        if len(eligible) >= minimum and eligible[-1][3] - eligible[0][3] >= span:
            chosen = [eligible[0], *eligible[-(minimum - 1):]]
            windows = []
            for source, note, _, _ in chosen:
                for row in (source, note):
                    packed = model.memory._window(row, limit=min(model.memory.text_limit, 1000))
                    item = {'ref': row.memory_id, 'origin': row.origin, 'layer': row.layer, 'text': packed}
                    if packed is None or not model.memory._safe_item(item):
                        return []
                    windows.append(item)
            return windows
    return []
