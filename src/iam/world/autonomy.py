"""Продолжение собственной жизни от сохранённого выбора, без опроса модели в простое."""
import hashlib

from iam.clock import life_moment
from iam.cycle import Runner
from iam.state import EventSpec

OWNER = 'autonomy:v1:'
START_MS = 15_000
STEP_MS = 60_000
REST_MS = 900_000
OUTCOMES = ('action.confirmed', 'action.failed', 'action.unknown_outcome',
            'message.out_sent', 'message.out_unknown')


class AutonomousRunner(Runner):
    def plan(self):
        events = self.ctl.events_after(0)
        starts = [e for e in events if e.type == 'session.started'
                  and e.payload['session_id'] == self.ctl.session_id]
        if not starts:
            return None
        anchor, delay = starts[-1], START_MS
        accepted = []
        for event in events:
            if event.seq <= anchor.seq or event.type != 'proposal.accepted':
                continue
            request = self.ctl.find_event(key='req:' + event.payload['request_id']).payload['request']
            if request['purpose'] == 'choose_action':
                accepted.append(event)
        if accepted:
            rid = accepted[-1].payload['request_id']
            chosen = [e for e in accepted if e.payload['request_id'] == rid]
            ids = {e.payload['proposal_id'] for e in chosen}
            response = self.ctl.find_event(key='resp:' + rid).payload['response']
            proposals = [p for p in response['proposals'] if p['proposal_id'] in ids]
            actions = {p['proposal_id'] for p in proposals if p['kind'] in ('action', 'message')}
            if actions:
                outcomes = [e for e in events if e.type in OUTCOMES and e.payload.get('proposal_id') in actions]
                if {e.payload['proposal_id'] for e in outcomes} != actions:
                    # После аварии до attempted старое действие не исполняем. Явное
                    # возобновление допускает один новый выбор, а не бесконечный повтор.
                    resumed = [e for e in events if e.type == 'session.resumed' and e.seq > chosen[-1].seq]
                    pending = [a for a in self.ctl.state['world_time']['world']['actions'].values()
                               if a['proposal_id'] in actions]
                    if not resumed or any(a['status'] != 'accepted' for a in pending):
                        return None
                    return self._moment(resumed[-1], START_MS, 'unfinished:')
                anchor = outcomes[-1]
            else:
                anchor = chosen[-1]
            resting = any(p['kind'] == 'no_action' or p['kind'] == 'attend' and p['body']['focus'] == 'rest'
                          for p in proposals)
            delay = REST_MS if resting else STEP_MS
        return self._moment(anchor, delay)

    def _moment(self, anchor, delay, reason=''):
        ident = ':'.join((OWNER, self.ctl.persona_id, self.ctl.session_id, anchor.event_id))
        mid = 'mom_' + hashlib.sha256(ident.encode()).hexdigest()[:24]
        return life_moment(mid, anchor.life_time_ms + delay, OWNER + reason + anchor.event_id)

    def reconcile(self):
        if self.ctl.session_state != 'running':
            return
        plan = self.plan()
        target = plan['moment_id'] if plan else None
        with self.ctl._atomic():
            if plan and plan['owner_ref'].startswith(OWNER + 'unfinished:'):
                resume_id = plan['owner_ref'].split(':')[-1]
                self.ctl.record_event(EventSpec('system.error', 'err:autonomy_unfinished:' + resume_id,
                    {'fault':'unstarted_action', 'detail':'До остановки было принято, но не начато действие; автоматического повтора нет.'},
                    causes=(resume_id,)))
            for mid, moment in self.ctl.state['world_time']['moments'].items():
                if moment['owner_ref'].startswith(OWNER) and mid != target:
                    self.ctl.cancel_moment(mid)
            if (plan and target not in self.ctl.state['world_time']['moments']
                    and self.ctl.find_event(key='due:' + target + ':1') is None):
                self.ctl.schedule_moment(plan)

    def is_current(self, event):
        if event.type != 'time.moment_due' or not event.payload.get('owner_ref', '').startswith(OWNER):
            return True
        plan = self.plan()
        return plan is not None and event.payload['moment_id'] == plan['moment_id']

    def run_pending(self):
        self.reconcile()
        reports = super().run_pending()
        self.reconcile()
        return reports
