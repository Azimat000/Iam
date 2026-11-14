import copy
import json
import unittest

from iam.providers.codex.presentation import present_context, restore_context


def block(payload, *, ref="mem_Visible01", layer="state", origin="observation"):
    return {"ref": ref, "origin": origin, "layer": layer,
            "text": json.dumps(payload, ensure_ascii=False) if isinstance(payload, dict) else payload}


def turn(text, seq, speaker="interlocutor", **extra):
    return {"kind": "dialogue_turn", "version": 1, "speaker": speaker, "person": "creator",
            "seq": seq, "text": text, "outcome": "read" if speaker == "interlocutor" else "confirmed",
            "excerpt": {"start": 0, "end": len(text), "total": len(text), "more": False}, **extra}


class PresentationTest(unittest.TestCase):
    def assert_roundtrip(self, context):
        before = copy.deepcopy(context)
        view = present_context(context)
        self.assertEqual(context, before)
        self.assertEqual(restore_context(view), context)
        return view

    def test_current_and_recent_words_precede_state_with_exact_disagreement(self):
        disagreement = ("Я всё ещё немного за «Лишний четверг»: в нём есть странность, которая мне нравится. "
                        "Но понимаю, почему хочется его вычеркнуть; «Запасной вторник» звучит теплее и уютнее. "
                        "И синяя обложка ему, кажется, подойдёт.")
        question = "В чём из того, что я сегодня говорил, ты со мной скорее не согласна?"
        context = [block({"library": {}, "projects": {}, "intentions": {}, "world_results": []}),
                   block(turn(question, 3403)), block(turn(disagreement, 3350, "self"), ref="mem_Disagree1"),
                   block({"kind": "current_interaction", "read_now": {"text": question}, "unread_targets": []})]
        view = self.assert_roundtrip(context)
        self.assertEqual(next(iter(view)), "conversation")
        self.assertEqual(view["conversation"]["current"][0]["item"]["text"]["read_now"]["text"], question)
        recent = [entry["item"]["text"] for entry in view["conversation"]["recent_turns"]]
        self.assertEqual([entry["seq"] for entry in recent], [3350, 3403])
        self.assertEqual(len(recent[0]["text"]), 209)
        self.assertEqual((recent[0]["speaker"], recent[0]["text"]), ("self", disagreement))

    def test_correction_authorship_and_failed_unknown_delivery_are_preserved(self):
        context = [block(turn("Зелёный. «Лишний четверг».", 1), ref="mem_First000"),
                   block(turn("Синий. «Запасной вторник».", 2), ref="mem_Second00"),
                   block(turn("Согласна", 3, "self", outcome="failed")),
                   block(turn("Может быть", 4, "self", outcome="unknown"))]
        view = self.assert_roundtrip(context)
        turns = [entry["item"]["text"] for entry in view["conversation"]["recent_turns"]]
        self.assertEqual([row["outcome"] for row in turns], ["read", "read", "failed", "unknown"])
        self.assertEqual(turns[1]["speaker"], "interlocutor")
        self.assertEqual(turns[1]["text"], "Синий. «Запасной вторник».")

    def test_criteria_rights_feedback_and_extra_envelope_fields_survive(self):
        goal = {"text": "Написать сцену", "criterion": "Жест, а не объяснение", "status": "active"}
        catalog = {"library": {}, "projects": {}, "intentions": {"scene": goal}, "world_results": [],
                   "capabilities": [{"id": "cap_Write", "status": "forbidden"}],
                   "rights": {"network": False}, "creator_requests": {"one": {"status": "denied"}}}
        context = [dict(block(catalog), extension={"text": "future metadata"}),
                   block({"kind": "proposal_feedback", "reason": "unknown_cite", "executed": False}),
                   block("Правило: запись работы не отправляет её собеседнику.")]
        view = self.assert_roundtrip(context)
        self.assertEqual(len(view["state_and_choices"]), 3)
        self.assertEqual(view["state_and_choices"][0]["item"]["text"], catalog)

    def test_strings_are_literal_including_nested_json_ids_and_quotes(self):
        literal = '{"mem_Visible01": "c1", "text": "\\n кавычки \\\" и slash \\"}'
        payload = turn(literal, 1)
        payload["sources"] = ["mem_NotVisible"]
        payload["quote"] = literal
        context = [block(payload)]
        view = self.assert_roundtrip(context)
        decoded = view["conversation"]["recent_turns"][0]["item"]["text"]
        self.assertEqual(decoded["text"], literal)
        self.assertEqual(decoded["quote"], literal)
        # Вложенная ссылка остаётся данными; новым цитируемым блоком она не стала.
        self.assertEqual([item["ref"] for item in restore_context(view)], ["mem_Visible01"])

    def test_unread_notice_does_not_acquire_words_from_other_calls(self):
        present_context([block(turn("Секрет предыдущего запроса", 1))])
        context = [block({"notice_level": "pending", "count": 1, "input_id": "inp_Unread001"},
                         ref="inp_Unread001", layer="notice")]
        view = self.assert_roundtrip(context)
        self.assertNotIn("Секрет", json.dumps(view, ensure_ascii=False))
        self.assertEqual(len(view["conversation"]["unread"]), 1)
        self.assertEqual(present_context(context), view)

    def test_windows_are_never_joined_even_same_ref_version(self):
        windows = [dict(turn("начала", 10), excerpt={"start": 0, "end": 6, "total": 30, "more": True}),
                   dict(turn("хвост", 10), excerpt={"start": 25, "end": 30, "total": 30, "more": False}),
                   dict(turn("иная версия", 10), version=2)]
        view = self.assert_roundtrip([block(row) for row in windows])
        shown = [row["item"]["text"] for row in view["conversation"]["recent_turns"]]
        self.assertEqual(shown, windows)
        self.assertNotIn("началахвост", json.dumps(view, ensure_ascii=False))

    def test_full_read_window_and_long_tail_survive(self):
        text = "а" * 960 + "Какое одно приветствие ты выберешь?"
        memory = {"version": 1, "sources": ["evt_Read0000"], "text": text,
                  "speaker": "interlocutor", "statement_scope": "utterance_only", "person": "creator",
                  "seq": 20, "text_window": {"start": 0, "end": len(text), "total": len(text), "more": False}}
        frame = {"kind": "current_interaction", "read_now": {"text": text[:100],
                 "content_ref": "mem_FullRead0", "content_window": memory["text_window"]}, "unread_targets": []}
        view = self.assert_roundtrip([block(memory, ref="mem_FullRead0", layer="personal"), block(frame)])
        shown = view["conversation"]["recalled_utterances"][0]["item"]["text"]
        self.assertEqual(shown["text"], text)
        self.assertEqual(shown["text_window"]["end"], len(text))

    def test_outgoing_receipt_prefix_never_becomes_spoken_words(self):
        memory = {"version": 1, "sources": ["evt_Sent0000"], "text": "Подтверждён успех: Мои слова: привет",
                  "speaker": "self", "statement_scope": "utterance_only", "seq": 2,
                  "text_window": {"start": 0, "end": 40, "total": 40, "more": False}}
        context = [block(memory, layer="personal"), block(turn("привет", 2, "self"))]
        view = self.assert_roundtrip(context)
        self.assertEqual(view["conversation"]["recent_turns"][0]["item"]["text"]["text"], "привет")
        self.assertEqual(view["conversation"]["recalled_utterances"][0]["item"]["text"]["text"], memory["text"])

    def test_known_experience_state_and_evidence_formats(self):
        context = [block({"emotion": {"reaction": 0.1}, "sources": [], "text": "Интерпретация"}, layer="interpretation"),
                   block({"version": 1, "sources": [], "text_window": {}, "text": "Работа"}, layer="personal"),
                   block({"kind": "evidence_review", "recollection_sources": [{"quote": "Работа"}]}),
                   block({"choices": [], "current_focus": "dialogue"}),
                   block({"person": "creator", "history": [], "closeness": None, "trust_as_source": None}),
                   block({"kind": "character", "traits": []})]
        view = self.assert_roundtrip(context)
        self.assertEqual(len(view["experience"]), 2)
        self.assertEqual(len(view["conversation"]["evidence"]), 1)
        self.assertEqual(len(view["state_and_choices"]), 3)

    def test_unknown_invalid_and_noncanonical_json_are_literal(self):
        context = [block('{"kind":"dialogue_turn"}'), block({"kind": ["bad"]}),
                   block("[1,2]"), block("{broken"), block({"kind": "future_format", "text": "retain"}),
                   block({"version": 1}, layer="personal"), block({"custom": True}, layer="future")]
        view = self.assert_roundtrip(context)
        self.assertEqual(len(view["other_context"]), len(context))
        self.assertTrue(all(isinstance(row["item"]["text"], str) for row in view["other_context"]))

    def test_missing_sequence_preserves_input_order_without_inventing_chronology(self):
        context = [block(turn("one", None)), block(turn("two", "yesterday")), block(turn("three", True))]
        view = self.assert_roundtrip(context)
        self.assertEqual([row["index"] for row in view["conversation"]["recent_turns"]], [0, 1, 2])

    def test_reader_roles_and_selected_quotes_roundtrip(self):
        context = [block({'kind': 'conversation_source', 'conversation_role': role, 'seq': n,
                          'context_index': n, 'parent_context_index': 40-n, 'text': 'Слова'})
                   for n, role in enumerate(('current', 'recent', 'recalled', None))]
        context.append(block({'kind': 'conversation_quote', 'quote': 'Моя позиция', 'speaker': 'self'}))
        view = self.assert_roundtrip(context)['conversation']
        self.assertEqual([len(view[k]) for k in ('current', 'recent_turns', 'recalled_utterances', 'evidence')], [1,1,1,2])
        self.assertEqual(view['recent_turns'][0]['index'], view['recent_turns'][0]['item']['text']['context_index'])

    def test_no_mutable_alias_and_empty_context(self):
        self.assert_roundtrip([])
        context = [block(turn("Original", 1))]
        view = present_context(context)
        view["conversation"]["recent_turns"][0]["item"]["text"]["text"] = "Changed"
        self.assertIn("Original", context[0]["text"])
        restored = restore_context(present_context(context))
        restored[0]["text"] = "Changed again"
        self.assertIn("Original", context[0]["text"])

    def test_compact_serialization_reduces_repeated_json_escaping(self):
        context = [block(turn('Он сказал: "привет". ' * 30, i)) for i in range(6)]
        old = json.dumps(context, ensure_ascii=False, indent=1)
        new = json.dumps(self.assert_roundtrip(context), ensure_ascii=False, separators=(",", ":"))
        self.assertLess(len(new), len(old))


if __name__ == "__main__":
    unittest.main()
