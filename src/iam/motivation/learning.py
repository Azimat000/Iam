"""Измерение конкретного умения по исходным критериям, а не числу файлов."""
import json

from iam.domain import Op
from iam.state import KindDecision, Violation
from iam.world import identifier, text


class Learning:
    instructions = (
        'Оценку practice предлагай только в обычном choose_action; в appraise не предлагай practice. '
        'Практику можно оценить, не завершая активное намерение: memory_write '
        'draft={kind:semantic,origin:own_hypothesis,content:{text:зачем оценить,'
        'practice:{intention_id,result_ref,skill_id:свой переносимый ключ умения}}}; '
        'cites: result_ref и event_id доступного результата из practice_options. '
        'Оценщик проверит неизменные исходные цель и критерий; отрицательный результат тоже сохраняется. '
        'Одна оценка за ответ; done имеет приоритет. Один результат по этому критерию и умению '
        'повторно не оценивается; assessed_skills показывает уже доступные оценки этой пары. '
        'skill_id описывает способ работы и может совпадать у разных проектов. '
        'Два разных положительно оценённых результата одного умения позволяют предложить procedure '
        'с тем же skill_id и собственным method; это гипотеза метода, а не доказанное общее мастерство.'
    )

    def __init__(self, motivation):
        self.owner, self.memory, self.ctl = motivation, motivation.memory, motivation.ctl

    def assessment_record(self, assessment):
        result = assessment['result']
        sources = (result['memory_id'], *assessment['goal_sources'])
        evaluation = assessment['evaluation']
        verdict = evaluation['verdict'] if evaluation is not None else 'uncertain'
        extra = ({'format': 2, 'basis': 'practice', 'project_id': assessment['project_id']}
                 if assessment.get('basis') == 'practice' else {})
        record = self.memory.prepare(kind='semantic', origin='model_explanation',
                    text='Оценка исходного критерия: ' + verdict,
                    sources=sources, layer='interpretation', content={'skill_assessment': {
                        **extra, 'skill_id':assessment.get('skill_id', assessment['project_id']),
                        'operation':assessment['operation'],
                        'goal':assessment['goal_text'], 'criterion':assessment['criterion'],
                        'verdict':verdict, 'scope_match':assessment['scope_match'],
                        'evidence_kind':result['scope'], 'action_id':result['action_id'],
                        'quote':evaluation['support_quote'] if evaluation is not None else '',
                        'assessment_event':assessment['event_id'],
                        'fingerprint':assessment['result_fingerprint'],
                        'independent_observation':False}})
        return record

    def remember(self, assessment):
        return self.memory.write(self.assessment_record(assessment))

    def practice_candidate(self, pr, request):
        """Проверка запроса практики переиспользует весь контракт результата done."""
        draft = pr['body']['draft']
        content = draft['content']
        spec = content['practice']
        if (self.memory.off or self.owner.off or request['purpose'] != 'choose_action'
                or pr['kind'] != 'memory_write' or set(draft) != {'kind', 'origin', 'content'}
                or draft['kind'] != 'semantic' or draft['origin'] != 'own_hypothesis'
                or set(content) != {'text', 'practice'} or not text(content['text'], 512)
                or not isinstance(spec, dict) or set(spec) != {'intention_id', 'result_ref', 'skill_id'}
                or not identifier(spec['skill_id'])):
            raise ValueError('Нужна отдельная оценка практики по активной цели')
        normalized = dict(pr, body={'intention_id': spec['intention_id'], 'result_ref': spec['result_ref']})
        goal, result = self.owner.intentions.result(normalized, request)
        visible = {item['ref'] for item in request['context']}
        if result['event_id'] not in visible:
            raise ValueError('Подтверждающее событие не предъявлено')
        return goal, result, spec['skill_id']

    @staticmethod
    def practice_key(goal, result, skill_id):
        from iam.intentions.completion import digest
        return 'assess:req_' + digest({'basis': 'practice', 'action_id': result['action_id'], 'goal': goal['text'],
                                      'criterion': goal['criterion'], 'skill_id': skill_id})[:32]

    def practice(self, pr, ctx):
        from iam.intentions.completion import digest
        try:
            goal, result, skill_id = self.practice_candidate(pr, ctx.request)
            key = digest({'request': ctx.request, 'proposal': pr, 'goal': goal})
            assessed = self.owner.intentions.reviews.get(key)
            if assessed is None:
                code = ('assessment_slot_used' if self.owner.intentions.reviews else 'practice_unassessed')
                return KindDecision(violation=Violation(code, 'Нужна отдельная оценка именно этого предложения'))
            reservation = self.ctl.find_event(key=self.practice_key(goal, result, skill_id))
            if (assessed.get('basis') != 'practice' or assessed.get('skill_id') != skill_id
                    or assessed['result'] != result or reservation is None
                    or reservation.payload.get('binding') != key):
                raise ValueError('Оценка не относится к этому результату')
            record = self.assessment_record(assessed)
            return KindDecision((Op('memory', record.memory_id, 'add', record.to_dict(), True),))
        except (KeyError, ValueError, TypeError):
            return KindDecision(violation=Violation('practice_invalid', 'Практика требует доступного результата и привязанной оценки'))

    def practice_options(self, context):
        """До четырёх доступных пар, без чтения скрытого результата и выбора умения за личность."""
        if self.memory.off or self.owner.off:
            return []
        results = []
        for item in context:
            try:
                value = json.loads(item['text'])
            except (ValueError, TypeError):
                continue
            if isinstance(value, dict):
                results.extend(value.get('world_results', []))
        options = []
        visible = {item['ref'] for item in context}
        assessed = [row for ref, row in self.assessments().items() if ref in visible]
        for iid, goal in self.owner.intentions.visible().items():
            if goal['status'] != 'active':
                continue
            for result in results:
                if not isinstance(result, dict) or result.get('target') != goal['project_id']:
                    continue
                try:
                    pr = {'body': {'intention_id': iid, 'result_ref': result['memory_id']},
                          'cites': [result['memory_id'], result['event_id']]}
                    _, checked = self.owner.intentions.result(pr, {'context': context})
                    if checked['event_id'] not in visible:
                        continue
                except (KeyError, TypeError, ValueError):
                    continue
                option = {'intention_id': iid, 'result_ref': checked['memory_id'], 'event_id': checked['event_id'],
                          'project_id': goal['project_id'], 'operation': goal['operation']}
                prior = {row.content['skill_assessment']['skill_id']: row.memory_id for row in assessed
                         if row.content['skill_assessment']['action_id'] == checked['action_id']
                         and row.content['skill_assessment']['goal'] == goal['text']
                         and row.content['skill_assessment']['criterion'] == goal['criterion']}
                if prior:
                    option['assessed_skills'] = prior
                if option not in options:
                    options.append(option)
        return options[-4:]

    def context(self):
        """Цитируемые оценки и приёмы, чьи структурные поля не входят в обычное окно памяти."""
        if self.memory.off or self.owner.off:
            return []
        groups = {}
        for row in sorted(self.assessments().values(), key=lambda row: (row.created_life_ms, row.memory_id)):
            groups.setdefault(row.content['skill_assessment']['skill_id'], []).append(row)
        recent_groups = sorted(groups.values(), key=lambda group: (group[-1].created_life_ms, group[-1].memory_id))
        selected = [row for group in recent_groups[-2:] for row in group[-2:]]
        rows = {row.memory_id: row for row in self.memory.records()}
        selected += [rows[value['source']] for value in list(self.procedures().values())[-2:]]
        context = []
        for row in selected:
            packet = {'kind': row.kind, 'text': row.text, 'sources': list(row.sources),
                      **{key: row.content[key] for key in ('skill_assessment', 'procedure') if key in row.content}}
            detail = json.dumps(packet, ensure_ascii=False)
            if len(detail) <= self.memory.text_limit and self.memory._safe_item(packet):
                context.append({'ref': row.memory_id, 'origin': row.origin,
                                'layer': 'interpretation', 'text': detail})
        return context

    def measurements(self):
        if self.memory.off or self.owner.off:
            return {}
        measurements = {}
        seen = set()
        for row in self.assessments().values():
            value = row.content['skill_assessment']
            identity=(value['action_id'],value['goal'],value['criterion'],value['skill_id'])
            if identity in seen:
                continue
            seen.add(identity)
            measurements.setdefault(value['skill_id'], []).append(dict(value, memory_id=row.memory_id))
        return {key:{'samples':samples, 'rubric':'исходные цель и критерий; отдельная модельная оценка',
                     'estimate': (sum(s['verdict']=='pass' and s['scope_match'] for s in samples if s['verdict']!='uncertain') /
                                  sum(s['verdict']!='uncertain' for s in samples))
                                  if any(s['verdict']!='uncertain' for s in samples) else None,
                     'generalized_skill_verified':False} for key,samples in measurements.items()}

    def assessments(self):
        """Пересмотр оценки не сохраняет старую цитату/вердикт под новым основанием."""
        rows={r.memory_id:r for r in self.memory.records()}
        result={}
        for row in rows.values():
            value=row.content.get('skill_assessment')
            if value is None or row.version!=1 or not row.sources:
                continue
            source=rows.get(row.sources[0])
            if (source is not None and source.version==1 and source.origin=='observation'
                    and source.content.get('outcome')=='confirmed' and source.content.get('action_id')==value['action_id']):
                result[row.memory_id]=row
        return result

    def proposal(self, pr, ctx):
        draft = pr['body']['draft']
        spec = draft['content']['procedure']
        try:
            rows = self.assessments() if not self.memory.off and not self.owner.off else {}
            visible = {item['ref'] for item in ctx.request['context']}
            sources = pr['cites']
            if (not isinstance(spec,dict) or set(spec) != {'skill_id','method'} or not identifier(spec['skill_id'])
                    or not text(spec['method'],500) or not text(draft['content'].get('text'),512)
                    or draft['kind']!='procedural' or draft['origin']!='own_hypothesis'
                    or len(sources)<2 or any(ref not in rows or ref not in visible for ref in sources)):
                raise ValueError('Нужны два доступных оценённых опыта и метод')
            assessments=[rows[ref].content['skill_assessment'] for ref in sources]
            if (any(a['skill_id']!=spec['skill_id'] or a['verdict']!='pass' or not a['scope_match'] for a in assessments)
                    or len({a['action_id'] for a in assessments})<2 or len({a['fingerprint'] for a in assessments})<2):
                raise ValueError('Нужны разные содержательные результаты')
            rec=self.memory.prepare(kind='procedural', origin='own_hypothesis', text=draft['content']['text'],
                                    sources=sources, layer='interpretation', content={'procedure':spec})
            pointer={'memory_id':rec.memory_id,'version':rec.version}
            rec.content['motivation_pointer']={'topic':spec['skill_id'], 'collection':'s22_procedures', **pointer}
            return KindDecision((Op('memory',rec.memory_id,'add',rec.to_dict(),True),
                                 Op('preferences','s22_procedures.'+spec['skill_id'],'set',pointer,True)))
        except (KeyError,ValueError,TypeError):
            return KindDecision(violation=Violation('procedure_invalid','Приём требует двух разных подтверждённых содержательных результатов'))

    def procedures(self):
        if self.memory.off or self.owner.off:
            return {}
        rows={r.memory_id:r for r in self.memory.records()}
        result={}
        for skill_id,pointer in self.ctl.state['preferences'].get('s22_procedures',{}).items():
            row=rows.get(pointer['memory_id'])
            if row is not None and row.version==pointer['version']:
                result[skill_id]={'method':row.content['procedure']['method'], 'source':row.memory_id,
                                  'sources':list(row.sources), 'kind':'learned_hypothesis'}
        return result
