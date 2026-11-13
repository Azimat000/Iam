import json
from unittest.mock import patch

from iam.domain import new_id
from iam.social.development import conversation_experience
from iam.stubs import Reply, rest, say
from iam.state import verify
from social import test_development_repair as fixtures


class ConversationExperienceTest(fixtures.SocialDevelopmentTest):
    def test_direct_read_event_basis_returns_only_with_visible_available_contact(self):
        self.enable();source,trigger=self.incoming()
        note=self.note(source);note['cites']=[trigger.event_id]
        self.p.queue(Reply((note,)))
        self.assertEqual(self.cycle.run(trigger).outcome,'done')
        own=next(r for r in self.m.records() if r.kind=='procedural')
        self.assertEqual(own.sources,(trigger.event_id,))
        self.assertEqual([i['ref'] for i in conversation_experience(self.m,[{'ref':source.memory_id}])],[own.memory_id])
        self.assertEqual(conversation_experience(self.m,[{'ref':trigger.event_id}]),[])
        self.assertTrue(self.m.fade(source.memory_id).applied)
        self.assertEqual(conversation_experience(self.m,[{'ref':source.memory_id}]),[])

    def note(self, source, text='Когда мне рассказывают о неприятном дне, сначала выслушать, не раздавать советы.'):
        return {'proposal_id':new_id('prop'),'kind':'memory_write','rationale':'Собственный вывод',
                'cites':[source.memory_id],'body':{'draft':{'kind':'procedural','origin':'own_hypothesis','content':{'text':text}}}}

    def test_composed_note_persists_and_returns_after_reopen_without_scoring(self):
        self.enable()
        source,trigger=self.incoming()
        self.p.compose_message=self.p.propose
        self.p.queue(Reply((say('cap_Telegram01','creator','Черновик'),)),Reply((self.note(source),rest())))
        report=self.cycle.run(trigger)
        self.assertEqual(report.outcome,'done')
        note=next(r for r in self.m.records() if r.kind=='procedural')
        self.assertEqual(note.sources,(source.memory_id,))
        self.assertNotIn('skill_assessment',note.content)
        self.assertFalse(report.actions)
        self.c.pause_session('operator')
        self.c=self.reopen(self.c);self.enable();self.c.resume_session()
        self.run_reply(rest())
        self.assertTrue(any(i['ref']==note.memory_id and note.text in i['text'] for i in self.p.calls[-1]['context']))
        self.assertEqual(verify(self.c.store),[])

    def test_notes_are_bounded_and_obey_forgetting_revision_and_contact_visibility(self):
        self.enable(); source,trigger=self.incoming()
        for i in range(4):
            self.run_reply(self.note(source,f'Вывод из разговора номер {i}'))
        visible=[{'ref':source.memory_id}]
        notes=conversation_experience(self.m,visible)
        self.assertEqual(len(notes),3)
        self.assertEqual(conversation_experience(self.m,[]),[])
        self.assertEqual(conversation_experience(self.m,[{'ref':new_id('inp')}]),[])
        self.m.off=True
        self.assertEqual(conversation_experience(self.m,visible),[])
        self.m.off=False
        with patch.object(self.m,'_window',return_value=None):
            self.assertEqual(conversation_experience(self.m,visible),[])
        with patch.object(self.m,'_safe_item',return_value=False):
            self.assertEqual(conversation_experience(self.m,visible),[])
        note=next(r for r in self.m.records() if r.memory_id==notes[-1]['ref'])
        self.assertTrue(self.m.revise(note.memory_id,'Пересмотрено',sources=note.sources,reason='Новое понимание').applied)
        self.assertNotIn(note.memory_id,[i['ref'] for i in conversation_experience(self.m,visible)])
        self.assertTrue(self.m.fade(source.memory_id).applied)
        self.assertEqual(conversation_experience(self.m,visible),[])
