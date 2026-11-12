"""Измерение конкретного умения по исходным критериям, а не числу файлов."""
import json

from iam.domain import Op
from iam.state import KindDecision, Violation
from iam.world import identifier, text


class Learning:
    def __init__(self, motivation):
        self.owner, self.memory, self.ctl = motivation, motivation.memory, motivation.ctl

    def remember(self, assessment):
        result = assessment['result']
        sources = (result['memory_id'], *assessment['goal_sources'])
        evaluation = assessment['evaluation']
        verdict = evaluation['verdict'] if evaluation is not None else 'uncertain'
        record = self.memory.prepare(kind='semantic', origin='model_explanation',
                    text='Оценка исходного критерия: ' + verdict,
                    sources=sources, layer='interpretation', content={'skill_assessment': {
                        'skill_id':assessment['project_id'], 'operation':assessment['operation'],
                        'goal':assessment['goal_text'], 'criterion':assessment['criterion'],
                        'verdict':verdict, 'scope_match':assessment['scope_match'],
                        'evidence_kind':result['scope'], 'action_id':result['action_id'],
                        'quote':evaluation['support_quote'] if evaluation is not None else '',
                        'assessment_event':assessment['event_id'],
                        'fingerprint':assessment['result_fingerprint'],
                        'independent_observation':False}})
        return self.memory.write(record)

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
            identity=(value['action_id'],value['goal'],value['criterion'])
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
