"""Черты из состояния S24 действительно участвуют в эмоциональной оценке S21."""
import unittest

from iam.domain import new_id
from iam.emotion import appraise
from iam.stubs import Reply
from metacognition.test_reflection import reflect
from state.helpers import set_
from world.helpers import LivingCase


class CharacterIntegrationTest(LivingCase):
    def appraisal(self, row):
        proposal = {"proposal_id": new_id("prop"), "kind": "memory_write",
                    "rationale": "Оценить прочитанный опыт", "cites": [row.memory_id],
                    "body": {"draft": {"kind": "emotional_associative", "origin": "own_hypothesis",
                             "content": {"text": "Этот опыт вызвал реакцию.",
                                         "appraisal": {"valence": -0.8, "arousal": 0.5,
                                                       "relevance": 0, "regulation": 0}}}}}
        self.assertEqual(self.run_reply(proposal).outcome, "done")
        return self.m._source(self.c.state["affect"]["emotion"]["memory_id"]).content["emotion"]

    def test_reflected_trait_changes_next_appraisal_in_canonical_state(self):
        self.assertTrue(self.c.apply(self.prop(self.c, set_("traits", "traits", [
            {"name": "sensitivity", "value": 0.2, "source": "initial"}]))).applied)
        first = self.read()
        self.assertEqual(self.appraisal(first)["trace"]["sensitivity"], 0.2)
        self.time.advance(31_000)
        second = self.write("Первый самостоятельный текст", project="one")
        self.time.advance(31_000)
        third = self.write("Другой самостоятельный текст", project="two")
        body = {"part": "now", "level": "stable_change", "text": "Сдвиг после разного опыта",
                "trait": "sensitivity", "delta": 0.04}
        report = self.run_reply(reflect(), replies=(Reply((reflect(
            body, [r.memory_id for r in (first, second, third)]),)),))
        self.assertEqual(report.outcome, "done")
        self.assertEqual(self.c.state["traits"]["traits"][0]["value"], 0.24)
        self.assertEqual(self.appraisal(self.read())["trace"]["sensitivity"], 0.24)


class CharacterShapeTest(unittest.TestCase):
    def test_canonical_traits_take_precedence_and_legacy_state_still_works(self):
        spec = {"valence": -0.8, "arousal": 0.5, "relevance": 0, "regulation": 0}
        def reaction(traits):
            return appraise(spec, {"traits": traits, "affect": {}, "intentions": {}}, None, 0, 0)
        legacy = {"sensitivity": {"value": 0.8}, "irritability": {"value": 0.3}}
        canonical = {"traits": [{"name": name, **value} for name, value in legacy.items()]}
        self.assertEqual(reaction(legacy), reaction(canonical))
        self.assertEqual(reaction({**canonical, "sensitivity": {"value": 0}}), reaction(canonical))
        default = reaction({})
        for entries in ([], [None], [{"name": "other", "value": 1}],
                        [{"name": "sensitivity"}],
                        [{"name": "sensitivity", "value": 0.1}, {"name": "sensitivity", "value": 0.9}]):
            self.assertEqual(reaction({"traits": entries}), default)
        self.assertEqual(reaction({"sensitivity": None, "irritability": None}), default)
