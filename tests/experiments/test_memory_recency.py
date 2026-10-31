"""Регрессия: свободное время видит свежий доступный опыт, а не начало жизни."""
from dataclasses import replace
from types import SimpleNamespace
import unittest

from iam.domain import MemoryRecord, Source
from iam.experiments.memory import BenchMemory


class RecencyTests(unittest.TestCase):
    def test_empty_word_and_punctuation_search_respect_recent_visible_limit(self):
        items = [MemoryRecord('mem_Record'+str(i).zfill(10), 'per_TestPersona01', 'personal', 'episodic',
                 'observation', {'text': 'needle '+str(i)}, Source('core','test'), '2026-01-01T00:00:00Z', i, ())
                 for i in range(16)]
        items[-1] = replace(items[-1], layer='archive', content={'text':'ARCHIVE-PRIVATE'})
        items[-2] = replace(items[-2], content={'text':'SUPERSEDED-PRIVATE','superseded_by':'mem_NextRecord01'})
        byid={r.memory_id:r for r in items}
        ctl=SimpleNamespace(events_after=lambda _: [SimpleNamespace(payload={'changes':[
            {'group':'memory','path':r.memory_id} for r in items]})],
            store=SimpleNamespace(get_memory=byid.__getitem__))
        memory=BenchMemory(ctl);memory.conversational=True
        expected=[r.memory_id for r in items[2:14][::-1]]
        for query in ('','...','needle'):
            self.assertEqual([r.memory_id for r in memory.retrieve(query,limit=12)],expected)
        self.assertEqual(memory.retrieve('absent',limit=1)[0].memory_id,items[13].memory_id)
        memory.off=True;self.assertEqual(memory.retrieve(''),[])
