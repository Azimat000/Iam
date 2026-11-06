"""Гипотетические варианты не являются желаниями: выбирает только последующий appraise."""
import copy
import json
import time

from iam.domain import new_id
from iam.interfaces import ProviderError
from iam.intentions.completion import valid_response
from iam.state import KindContext
from .prospective import ProspectiveCycle

GENERATOR = (
    'Сейчас только поиск возможностей, не выбор желания и не действие личности. '
    'Предложи от нуля до трёх разных возможных будущих результатов, связанных с новыми видимыми '
    'собственными основаниями. Используй полные intention/create как формат КАНДИДАТОВ, '
    'со своими text, criterion, project_id, operation, intention_id, rationale и cites. '
    'Это гипотетические варианты, они не будут приняты как цели или память. '
    'Возможность должна быть осуществима доступной операцией; запись плана не является измерением. '
    'Не выполняй ничего, не утверждай, что вариант уже выбран или результат достигнут. '
    'Можно вернуть пустой список. Каждый вариант должен цитировать хотя бы одно новое основание '
    'из предыдущего перечисления. Следующий отдельный выбор может отвергнуть все варианты или '
    'предложить свой; ни один вариант не требуется предпочитать.'
)


class OptionsCycle(ProspectiveCycle):
    expose_options = True
    max_options = 3
    max_option_chars = 1500
    option_buffer_chars = 5000

    def _prepare_appraisal(self, request, bases):
        started = time.monotonic()
        query = copy.deepcopy(request)
        rid = new_id('req')
        query.update(request_id=rid, idempotency_key='req:'+rid, allowed_proposals=['intention'])
        query['context'].append({'ref':request['context'][0]['ref'],'origin':'model_knowledge',
                                 'layer':'state','text':GENERATOR})
        audit = {'phase':'future_option_search','hypothetical':True,'selected':False,
                 'request':query,'response':None,'candidates':[],'rejected':[],'error':None}
        try:
            # Без CompletionProvider: read-only варианты не запускают done-evaluator.
            response = self.provider.inner.propose(query)
        except Exception as error:
            audit['error'] = type(error).__name__
            self.provider.record(rid, audit)
            raise
        audit['response'] = response
        same = ((request['epoch'],request['state_version'],request['session_id']) ==
                (self.ctl.epoch,self.ctl.state_version,self.ctl.session_id) and self.ctl.session_state=='running')
        if not valid_response(response, query):
            audit['error'] = 'options_response_invalid'
        elif not same:
            audit['error'] = 'options_state_changed'
        elif len(response['proposals']) > self.max_options:
            audit['error'] = 'too_many_options'
        elif self.provider.scanner.find(json.dumps(response,ensure_ascii=False)):
            audit['error'] = 'options_secret'
        if audit['error'] is None:
            sources = {row.memory_id for row in bases}
            used_ids = set()
            for proposal in response['proposals']:
                try:
                    if (proposal['kind']!='intention' or proposal['body'].get('op')!='create'
                            or not sources.intersection(proposal['cites'])):
                        raise ValueError('not_new_based_create')
                    context = KindContext(query,self.ctl.state,(),self.ctl.life_time_ms)
                    # Только предикат допустимости. Ops с wants/status выбрасываются, не применяются.
                    self.intentions.checked(proposal,context)
                    option = {key:copy.deepcopy(proposal[key]) for key in ('rationale','cites','body')}
                    if (len(json.dumps(option,ensure_ascii=False)) > self.max_option_chars
                            or option['body']['intention_id'] in used_ids):
                        raise ValueError('option_bound')
                    used_ids.add(option['body']['intention_id'])
                    audit['candidates'].append(option)
                except (KeyError,TypeError,ValueError):
                    audit['rejected'].append(proposal['proposal_id'])
        remaining = request['limits']['deadline_ms'] - int((time.monotonic()-started)*1000)
        if remaining <= 0:
            audit['error'] = 'options_deadline'
        saved = self.provider.record(rid,audit)
        if audit['error'] is not None:
            raise ProviderError(audit['error'])
        if saved.event is None:
            raise ProviderError('options_audit_unavailable')
        options = audit['candidates'] if self.expose_options else []
        packet = json.dumps({'kind':'hypothetical_future_options','is_commitment':False,'options':options},
                            ensure_ascii=False)
        if len(packet) > self.option_buffer_chars:
            raise ProviderError('option_buffer_overflow')
        # Одинаковая фиксированная длина: control не узнаёт число вариантов через размер пакета.
        packet = packet.ljust(self.option_buffer_chars)
        result = copy.deepcopy(request)
        result['context'].append({'ref':saved.event.event_id,'origin':'imagination','layer':'state','text':packet})
        result['issued_at'] = self.ctl.clock.calendar_now()
        result['life_time_ms'] = self.ctl.clock.now_ms()
        if ((request['epoch'],request['state_version'],request['session_id']) !=
                (self.ctl.epoch,self.ctl.state_version,self.ctl.session_id) or self.ctl.session_state!='running'):
            raise ProviderError('options_state_changed')
        remaining = request['limits']['deadline_ms'] - int((time.monotonic()-started)*1000)
        if remaining <= 0:
            raise ProviderError('options_deadline')
        result['limits']['deadline_ms'] = remaining
        return result
