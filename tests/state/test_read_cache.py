"""Декодирование не заменяет свежий SQL, фильтры доступа и изоляцию возвращённых объектов."""
from dataclasses import replace
import json
from unittest.mock import patch

from iam.domain import MemoryRecord, canonical_json, new_id
from iam.memory import PersistentMemory
from iam.state import StateController
from iam.storage import Store
from state.helpers import StateCase, memory


class MemoryReadCacheTest(StateCase):
    def setUp(self):
        super().setUp()
        self.c = self.controller(running=True)

    def put(self, text='Мысль', **fields):
        record = MemoryRecord.from_dict(memory(text, mid=new_id('mem'), origin='own_hypothesis', **fields))
        self.c.store.add_memory(record)
        return record

    def raw(self, record):
        return self.c.store.con.execute('SELECT record FROM memory_records WHERE memory_id=?',
                                       (record.memory_id,)).fetchone()[0]

    def test_same_records_decode_once_but_sql_runs_every_read(self):
        first = self.put()
        second = self.put('Интерпретация', layer='interpretation')
        statements = []
        self.c.store.con.set_trace_callback(statements.append)
        self.addCleanup(self.c.store.con.set_trace_callback, None)
        with patch.object(MemoryRecord, 'from_dict', wraps=MemoryRecord.from_dict) as decode:
            once = self.c.readable_memories()
            again = self.c.readable_memories()
        self.assertEqual(decode.call_count, 2)
        self.assertEqual({r.memory_id for r in once}, {first.memory_id, second.memory_id})
        self.assertEqual(once, again)
        self.assertTrue(all(isinstance(r, MemoryRecord) for r in again))
        self.assertEqual(sum(sql == "SELECT record FROM memory_records WHERE layer IN ('personal','interpretation')"
                             for sql in statements), 2)

    def test_exact_json_change_revalidates_even_with_same_version(self):
        record = self.put()
        self.c.readable_memories()
        old_raw = self.raw(record)
        changed = replace(record, content={'text': 'Изменённая мысль'}, availability=0.4)
        self.c.store.update_memory(changed)
        with patch.object(MemoryRecord, 'from_dict', wraps=MemoryRecord.from_dict) as decode:
            result = self.c.readable_memories()
            self.c.readable_memories()
        self.assertEqual(decode.call_count, 1)
        self.assertEqual(result, [changed])
        self.assertNotIn(old_raw, self.c._memory_decode_cache)
        self.assertEqual(len(self.c._memory_decode_cache), 1)
        # Даже другое кодирование того же содержимого является новым проверяемым JSON.
        recoded = json.dumps(changed.to_dict(), ensure_ascii=False, indent=1)
        self.c.store.con.execute('UPDATE memory_records SET record=? WHERE memory_id=?', (recoded, record.memory_id))
        with patch.object(MemoryRecord, 'from_dict', wraps=MemoryRecord.from_dict) as decode:
            self.assertEqual(self.c.readable_memories(), [changed])
        self.assertEqual(decode.call_count, 1)

    def test_nested_mutation_of_returned_record_does_not_change_cached_data(self):
        record = self.put(history=({'version': 1, 'at_seq': 1, 'change': 'Исходный текст',
                                   'previous_sources': ['evt_Original0001']},))
        record = replace(record, content={'text': record.text, 'nested': {'values': [1, 2]},
                                         'source_details': [{'kind': 'core', 'ref': 'original'}]})
        self.c.store.update_memory(record)
        first = self.c.readable_memories()[0]
        first.content['nested']['values'].append(3)
        first.content['source_details'][0]['ref'] = 'changed'
        first.history[0]['previous_sources'].append('evt_Changed0001')
        object.__setattr__(first.source, 'ref', 'changed by caller')
        second = self.c.readable_memories()[0]
        self.assertEqual(second, record)
        self.assertIsNot(first, second)
        self.assertIsNot(first.source, second.source)
        self.assertEqual(self.c.store.get_memory(record.memory_id), record)

    def test_add_delete_empty_and_archive_always_follow_current_table(self):
        first = self.put()
        archive = self.put('Архивный секрет', layer='archive')
        with patch.object(MemoryRecord, 'from_dict', wraps=MemoryRecord.from_dict) as decode:
            self.assertEqual(self.c.readable_memories(), [first])
        self.assertEqual(decode.call_count, 1)
        self.assertNotIn(self.raw(archive), self.c._memory_decode_cache)
        second = self.put('Новая мысль')
        self.assertEqual({r.memory_id for r in self.c.readable_memories()}, {first.memory_id, second.memory_id})
        self.c.store.con.execute('DELETE FROM memory_records WHERE memory_id=?', (first.memory_id,))
        self.assertEqual(self.c.readable_memories(), [second])
        self.assertEqual(len(self.c._memory_decode_cache), 1)
        self.c.store.con.execute('DELETE FROM memory_records WHERE memory_id=?', (second.memory_id,))
        self.assertEqual(self.c.readable_memories(), [])
        self.assertEqual(self.c._memory_decode_cache, {})

    def test_rollback_discards_uncommitted_cached_rows_and_revisions(self):
        original = self.put()
        self.c.readable_memories()
        with self.assertRaisesRegex(RuntimeError, 'rollback'):
            with self.c.store.transaction():
                changed = replace(original, content={'text': 'Незафиксированное'}, version=2)
                self.c.store.update_memory(changed)
                temporary = self.put('Незафиксированная новая запись')
                self.assertEqual({r.memory_id for r in self.c.readable_memories()},
                                 {original.memory_id, temporary.memory_id})
                raise RuntimeError('rollback')
        self.assertEqual(self.c.readable_memories(), [original])
        self.assertEqual(list(self.c._memory_decode_cache.values()), [original])

    def test_readonly_reader_follows_other_connection_and_sql_snapshot(self):
        original = self.put()
        store = Store.open(self.db, readonly=True)
        self.addCleanup(store.close)
        reader = StateController(store, clock=self.clock())
        self.assertEqual(reader.readable_memories(), [original])
        store.con.execute('BEGIN')
        self.assertEqual(reader.readable_memories(), [original])
        changed = replace(original, content={'text': 'Новая зафиксированная версия'}, version=2)
        self.c.store.update_memory(changed)
        self.assertEqual(reader.readable_memories(), [original])
        store.con.execute('COMMIT')
        self.assertEqual(reader.readable_memories(), [changed])
        self.c.store.con.execute('DELETE FROM memory_records WHERE memory_id=?', (original.memory_id,))
        self.assertEqual(reader.readable_memories(), [])
        self.assertEqual(reader._memory_decode_cache, {})

    def test_malformed_or_invalid_changed_record_never_falls_back_to_cached_copy(self):
        record = self.put()
        self.c.readable_memories()
        valid = self.raw(record)
        for broken in ('{broken', '{}', canonical_json({**record.to_dict(), 'version': 0})):
            self.c.store.con.execute('UPDATE memory_records SET record=? WHERE memory_id=?',
                                    (broken, record.memory_id))
            for _ in range(2):
                with self.assertRaises(ValueError):
                    self.c.readable_memories()
        self.c.store.con.execute('UPDATE memory_records SET record=? WHERE memory_id=?', (valid, record.memory_id))
        self.assertEqual(self.c.readable_memories(), [record])

    def test_real_revision_and_forgetting_still_change_available_memory(self):
        mem = PersistentMemory(self.c)
        row = mem.prepare(kind='semantic', origin='own_hypothesis', text='Старая мысль',
                          sources=(self.root(self.c),))
        self.assertTrue(mem.write(row).applied)
        self.assertEqual([r.text for r in mem.records()], ['Старая мысль'])
        self.assertTrue(mem.revise(row.memory_id, 'Пересмотренная мысль', sources=row.sources,
                                   reason='Новые основания').applied)
        self.assertEqual([r.text for r in mem.records()], ['Пересмотренная мысль'])
        self.assertTrue(mem.fade(row.memory_id, availability=0, detail=0).applied)
        self.assertEqual(mem.records(), [])
        current, = self.c.readable_memories()
        self.assertEqual((current.availability, current.detail), (0, 0))
        mem.off = True
        self.assertEqual(mem.retrieve('Пересмотренная'), [])
