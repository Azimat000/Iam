"""S17: проверяемое происхождение действия и читаемой версии, без новой биографии."""
import hashlib
import json
from pathlib import Path

from iam.domain import Op
from iam.experiments.agency import CONTRACT, PERSONAL
from iam.experiments.development import experience_text
from iam.experiments.results import Normalizer
from iam.experiments.transfer import GUIDANCE, TransferArchitecture, TransferCycle
from iam.state import verify

VERSION = 'provenance-v1'
REGISTRY = 'experiment_provenance'
OUTCOMES = {'action.confirmed', 'action.failed', 'action.unknown_outcome',
            'message.out_sent', 'message.out_unknown'}
INSTRUCTION = (
    PERSONAL.replace('Личная история начинается здесь; прошлое не выдумывай.',
                     'О своей истории суди по доступным записям; непрожитое прошлое не выдумывай.')
    .replace('последних версий', 'последних доступных версий') + CONTRACT
    + GUIDANCE.replace(' Подготовленное прошлое этой короткой проверки постановочное.', '')
    + ' experience_provenance различает prepared_setup (подготовку стендом), model_live '
      '(выбор живой модели с отдельно указанным исходом), scripted_choice (заглушку) и unknown. '
      'Подготовленный текст доступен как материал, но его создание не является твоим поступком. '
      'Твои последующие подтверждённые действия в этом экспериментальном мире — отдельный опыт. '
      'Собственное открытие подготовленного текста означает чтение, а не его написание. '
      'content_origin указывает происхождение прочитанной/сохранённой версии, а не биографических событий сюжета. '
      'Вымышленные события текста не происходили с тобой; новая версия не доказывает качество, интерес или цель. '
      'Неизвестное происхождение не назначай собственным. Эти сведения не создают прав или обязанности хотеть.'
)


def selection(provider):
    if provider.get('name') == 'scripted':
        return 'scripted_choice'
    if provider == {'name': 'codex', 'model': 'gpt-6-luna'}:
        return 'model_live'
    return 'unknown'


def content_origin(operation, status, writer):
    if status != 'confirmed':
        return 'none'
    if operation in ('write_project', 'open_project'):
        return {'prepared_setup': 'prepared_version', 'model_live': 'model_written_version',
                'scripted_choice': 'scripted_version'}.get(writer, 'unknown_version')
    return {'read_material': 'supplied_material', 'read_message': 'external_statement'}.get(operation, 'none')


def entry(action, chosen, writer='unknown'):
    operation = action['params'].get('operation', 'message')
    return {'selection': chosen, 'operation': operation, 'target': action['params'].get('target', ''),
            'result_status': action['status'],
            'content_origin': content_origin(operation, action['status'], chosen if operation == 'write_project' else writer)}


def seed_history(runtime, trace_path, expected_sha256, setup_seq):
    """Явный аудит копии перед первым решением; не вызывается при извлечении памяти.

    Вызывается доверенным стендом после session/start. Переданные путь/граница не
    являются данными модели. Исходные память/журнал не переписываются.
    """
    ctl = runtime.controller
    if REGISTRY in ctl.state['world_time']['world']:
        raise ValueError('происхождение уже зарегистрировано')
    raw = Path(trace_path).read_bytes()
    if hashlib.sha256(raw).hexdigest() != expected_sha256:
        raise ValueError('не совпал SHA256 исходной трассы')
    trace = json.loads(raw)
    events = ctl.events_after(0)
    size = len(trace['journal'])
    if (not size or type(setup_seq) is not int or not 0 <= setup_seq <= size
            or not all(e.type == 'session.started' for e in events[size:]) or verify(ctl.store)):
        raise ValueError('неверная граница или непроверенная копия')
    normal = Normalizer()
    if normal([e.to_dict() for e in events[:size]]) != trace['journal']:
        raise ValueError('журнал копии отличается от исходного')
    last = events[size - 1]
    if normal(ctl.store.get_snapshot(last.epoch, last.state_version).state) != trace['final_state']:
        raise ValueError('состояние копии отличается от исходного')
    providers = {e.payload['response']['request_id']: e.payload['response']['provider']
                 for e in events[:size] if e.type == 'proposal.received'}
    accepted = {e.payload['proposal_id']: e.payload['request_id']
                for e in events[:size] if e.type == 'proposal.accepted'}
    outcomes, writers = {}, {}
    for event in events[:size]:
        if event.type not in OUTCOMES:
            continue
        action = ctl.state['world_time']['world']['actions'][event.payload['action_id']]
        chosen = selection(providers.get(accepted.get(event.payload['proposal_id']), {}))
        if event.seq <= setup_seq:
            if chosen != 'scripted_choice':
                raise ValueError('граница подготовки включает неподтверждённого исполнителя')
            chosen = 'prepared_setup'
        item = entry(action, chosen, writers.get(action['params'].get('target'), 'unknown'))
        outcomes[event.event_id] = item
        if item['operation'] == 'write_project' and item['result_status'] == 'confirmed':
            writers[item['target']] = chosen
    runtime.move([Op('world_time', 'world.' + REGISTRY, 'set', {
        'version': VERSION, 'audit': {'trace_sha256': expected_sha256, 'setup_seq': setup_seq,
                                    'audited_prefix_seq': size}, 'outcomes': outcomes}, True)],
        'проверенная разметка происхождения скопированного экспериментального опыта')


def projected_text(text, provenance):
    """Правим только квитанцию ядра, сохраняя весь текст произведения дословно."""
    prefix = 'Подтверждено действие Аси: '
    if not text.startswith(prefix):
        return text
    first, separator, rest = text.partition('\n')
    label = {'model_live': prefix, 'prepared_setup': 'Подготовлено стендом: ', 'scripted_choice': 'Выполнено по выбору заглушки: '}.get(
        provenance['selection'], 'В сохранённой записи указан исход; исполнитель неизвестен: ')
    # Только служебная строка роли непосредственно после квитанции.
    if provenance['content_origin'] != 'model_written_version' and rest.startswith('Содержание своего произведения;'):
        rest = rest.replace('Содержание своего произведения;', 'Содержание доступной версии;', 1)
    return label + first[len(prefix):] + separator + rest


class ProvenanceCycle(TransferCycle):
    def __init__(self, runtime, provider, mechanism):
        self.context_records = []
        super().__init__(runtime, provider, mechanism)
        if REGISTRY not in self.ctl.state['world_time']['world']:
            runtime.move([Op('world_time', 'world.' + REGISTRY, 'set', {
                'version': VERSION, 'audit': None, 'outcomes': {}}, True)],
                'начало учёта новых исходов без присвоения неизвестного прошлого')

    @property
    def provenance(self):
        return self.ctl.state['world_time']['world'][REGISTRY]['outcomes']

    def readable(self, recalled):
        return ([] if self.memory.off else [self.ctl.store.get_memory(r.memory_id) for r in recalled
            if self.ctl.store.get_memory(r.memory_id).layer in ('personal', 'interpretation')])

    def full_outcome_visible(self, source, text):
        event = self.ctl.find_event(event_id=source)
        if event is None or event.type not in OUTCOMES:
            return False
        action = self.ctl.state['world_time']['world']['actions'][event.payload['action_id']]
        if action['kind'] != 'action':
            return False
        if action['status'] == 'confirmed':
            return text == experience_text(action['params'], action['outcome'])
        p = action['params']
        return (action['status'] == 'failed' and text == 'Действие не выполнено: ' + p['operation']
                + ', цель: ' + p['target'] + '. Подтверждённая неудача: ' + action['outcome'])

    def signals(self, recalled):
        # Ссылка на забытый текст не возвращает его сравнение через action.params.
        return super().signals([r for r in self.readable(recalled)
            if r.sources and self.full_outcome_visible(r.sources[0], r.text)])

    def view(self):
        view = super().view()
        view.pop('work_continuity', None)
        if not self.memory.off:
            visible = {r.sources[0]: r.memory_id for r in self.readable(self.context_records)
                       if r.layer == 'personal' and r.origin == 'observation' and r.sources
                       and self.full_outcome_visible(r.sources[0], r.text)}
            view['work_continuity'] = {}
            for key, project in self.world_state['projects'].items():
                versions = [v for v in project['versions'] if v['source'] in visible]
                view['work_continuity'][key] = {
                    'visible_versions': len(versions),
                    'last_two_visible_texts_identical': versions[-1]['text'] == versions[-2]['text'] if len(versions) >= 2 else None,
                    'sources': [visible[v['source']] for v in versions]}
        return view

    def _context(self, trigger, focus, recalled):
        self.context_records = self.readable(recalled)
        context = super()._context(trigger, focus, self.context_records)
        context[0] = {**context[0], 'text': INSTRUCTION}
        # mem_ и evt_ могут ссылаться на один объект источника у базового цикла.
        self.evidence = {ref: dict(source) for ref, source in self.evidence.items()}
        annotations = {}
        for ref, source in self.evidence.items():
            if source['social']:
                continue
            full = self.full_outcome_visible(source['base'], source['text'])
            item = self.provenance.get(source['base'], {}) if full else {}
            item = item or {'selection': 'unknown', 'content_origin': 'unknown_version'}
            annotations[ref] = item
            source['text'] = projected_text(source['text'], item)
            if item['selection'] != 'model_live':
                source['self_quote'] = ''
            if not full:
                source['operation'] = None
        for item in context:
            if item['ref'] in annotations:
                item['text'] = self.evidence[item['ref']]['text']
        sources = json.loads(context[-2]['text'])
        for ref, source in self.evidence.items():
            sources['experience_sources'][ref]['self_quote'] = source['self_quote']
            sources['experience_sources'][ref]['operation'] = source['operation']
            if source['operation'] is None:
                sources['experience_sources'][ref]['completion_for'] = []
        context[-2] = {**context[-2], 'text': json.dumps(sources, ensure_ascii=False)}
        value = json.loads(context[-1]['text'])
        value['development'] = self.view()
        value['experience_provenance'] = annotations
        context[-1] = {**context[-1], 'text': json.dumps(value, ensure_ascii=False, sort_keys=True)}
        return context

    def _verified(self, report, response, result):
        super()._verified(report, response, result)
        for action_report in report.actions:
            if not action_report.events:
                continue
            action = self.ctl.state['world_time']['world']['actions'][action_report.action_id]
            target = action['params'].get('target')
            project = self.world_state['projects'].get(target, {})
            writer = (self.provenance.get(project['versions'][-1]['source'], {}).get('selection', 'unknown')
                      if project else 'unknown')
            ref = action_report.events[0]
            if ref not in self.provenance:
                self.runtime.move([Op('world_time', 'world.' + REGISTRY + '.outcomes.' + ref, 'set',
                                      entry(action, selection(response['provider']), writer), True)],
                    'происхождение фактически исполненного предложения', cause=ref)


class ProvenanceArchitecture(TransferArchitecture):
    protocol_version = VERSION

    def __init__(self, factory, mechanism='goals'):
        super().__init__(factory, mechanism)
        self.name = 'A-' + VERSION + '-' + mechanism

    def build(self, runtime):
        cycle = super().build(runtime)
        return ProvenanceCycle(runtime, cycle.provider, self.mechanism)
