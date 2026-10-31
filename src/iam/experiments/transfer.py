"""S17: мотивационные сигналы для свободного A, без назначения действия.

Числовые формулы S15 — только экспериментальные приоритеты. Изменение текста
не измеряет качество, а запись файла не обучает интересу или навыку.
"""
import json

from iam.domain import MemoryRecord, Op, Source, new_id
from iam.experiments.agency import AgencyArchitecture, AgencyCycle
from iam.experiments.development import CAP_DEVELOPMENT, MATERIALS
from iam.experiments.motivation.policy import (
    GoalsMotivation, NeedsMotivation, NoveltyMotivation, initial_interest,
)
from iam.state import EventSpec

POLICIES = {'needs': NeedsMotivation, 'novelty': NoveltyMotivation, 'goals': GoalsMotivation}
VERSION = 'transfer-v1'
GUIDANCE = (
    ' Таблица motivation — экспериментальные числовые сигналы, а не команда или оценка качества. '
    'Можно выбрать любое доступное действие, собственный будущий замысел или бездействие. '
    'Высокое число не обязывает продолжать; дословно новый текст ещё не означает продвижение. '
    'Для goals учитывается только твой принятый замысел с доступным основанием. '
    'Обязанности создавать замысел нет. Подготовленное прошлое этой короткой проверки постановочное.'
)


class TransferCycle(AgencyCycle):
    def __init__(self, runtime, provider, mechanism):
        if mechanism not in POLICIES:
            raise ValueError('неизвестный механизм переноса')
        self.mechanism = POLICIES[mechanism]()
        super().__init__(runtime, provider)

    def signals(self, recalled):
        """Только явно извлечённый личный опыт; точечная проверка его источника."""
        if self.runtime.switches['motivation_off'] or self.memory.off:
            return {'enabled': False}
        history = {}
        seen = set()
        for record in recalled:
            if record.layer != 'personal' or record.origin != 'observation' or not record.sources:
                continue
            ref = record.sources[0]
            if ref in seen:
                continue
            event = self.ctl.find_event(event_id=ref)
            if event is None or event.type not in ('action.confirmed', 'action.failed'):
                continue
            action = self.ctl.state['world_time']['world']['actions'][event.payload['action_id']]
            if action['kind'] != 'action' or action['capability_id'] != CAP_DEVELOPMENT:
                continue
            p = action['params']
            if p['operation'] not in ('write_project', 'read_material'):
                continue
            seen.add(ref)
            key = ('project:' if p['operation'] == 'write_project' else 'material:') + p['target']
            history.setdefault(key, []).append((event.seq, record.memory_id, action, ref))
        view = self.view()
        visible_bases = {ref for rows in history.values() for row in rows for ref in (row[1], row[3])}
        goals = {key: item for key, item in self.ctl.state['intentions'].items()
                 if item['status'] == 'active' and set(item['sources']) & visible_bases}
        items = []
        for key, rows in sorted(history.items()):
            rows.sort()
            confirmed = [row for row in rows if row[2]['status'] == 'confirmed']
            texts = [row[2]['params'].get('text', row[2]['outcome']) for row in confirmed]
            failures = 0
            for row in reversed(rows):
                if row[2]['status'] == 'confirmed':
                    break
                failures += 1
            last = rows[-1][2]['params']
            project = self.world_state['projects'].get(last['target'], {}) if key.startswith('project:') else {}
            topic = (project.get('topic', last.get('topic', '')) if key.startswith('project:')
                     else MATERIALS[last['target']][0])
            strength = view['interests'].get(topic, {}).get('values', {}).get('strength', 0.0)
            identical = len(texts) > 1 and texts[-1] == texts[-2]
            interest = initial_interest(score=-min(1.0, 0.25 * failures), strength=max(0.0, strength),
                                        novelty=0.0 if identical else 1.0, skill=0.0,
                                        failures=failures, repeats=len(texts))
            matching = [name for name, goal in goals.items() if key == 'project:' + goal['project']]
            # need=0.6 — одинаковый постановочный фон, не измеренная потребность личности.
            score = self.mechanism.score(interest, 0.6, bool(matching))
            items.append({'activity': key, 'sources': [row[1] for row in rows],
                          'inputs': interest, 'salience': score, 'accepted_goals': matching,
                          'last_two_visible_texts_identical': identical if len(texts) > 1 else None})
        return {'enabled': True, 'mechanism': self.mechanism.name, 'items': items,
                'energy': view['energy'], 'rest_salience': 0.5 + 1.2 * (1 - view['energy']),
                'fixed_activity_need': 0.6, 'skill_measured': False,
                'meaningful_progress_measured': False, 'selects_action': False}

    def _context(self, trigger, focus, recalled):
        context = super()._context(trigger, focus, recalled)
        context[0] = {**context[0], 'text': context[0]['text'] + GUIDANCE}
        value = json.loads(context[-1]['text'])
        value['motivation'] = self.signals(recalled)
        context[-1] = {**context[-1], 'text': json.dumps(value, ensure_ascii=False, sort_keys=True)}
        return context

    def _verified(self, rep, response, result):
        super()._verified(rep, response, result)
        # О неудаче сохраняется факт исхода, а не фиктивное выполненное действие.
        if self.memory.off:
            return
        for action in rep.actions:
            if action.status != 'failed':
                continue
            event = self.ctl.find_event(event_id=action.events[0])
            stored = self.ctl.state['world_time']['world']['actions'][action.action_id]
            if stored['kind'] != 'action' or stored['capability_id'] != CAP_DEVELOPMENT:
                continue
            p = stored['params']
            mid, tid = new_id('mem'), new_id('trn')
            record = MemoryRecord(mid, self.ctl.persona_id, 'personal', 'episodic', 'observation',
                                  {'text': 'Действие не выполнено: ' + p['operation'] + ', цель: ' + p['target']
                                           + '. Подтверждённая неудача: ' + event.payload['summary']},
                                  Source('core', VERSION), event.occurred_at, event.life_time_ms,
                                  (event.event_id,))
            self.runtime.move([Op('memory', mid, 'add', record.to_dict(), True)],
                              'личный опыт подтверждённой неудачи', cause=event.event_id,
                              carrier=EventSpec('memory.recorded', 'mem:' + tid + ':0', {'memory_id': mid}))


class TransferArchitecture(AgencyArchitecture):
    protocol_version = VERSION

    def __init__(self, factory, mechanism='goals'):
        if mechanism not in POLICIES:
            raise ValueError('неизвестный механизм переноса')
        super().__init__(factory)
        self.mechanism = mechanism
        self.name = 'A-' + VERSION + '-' + mechanism

    def build(self, runtime):
        # Сохраняем прежний транспорт, возможности, мир и проверку повторов.
        agency = super().build(runtime)
        return TransferCycle(runtime, agency.provider, self.mechanism)
