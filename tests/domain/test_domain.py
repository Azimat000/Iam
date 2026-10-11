"""Домен S04: идентификаторы, каталог событий, типы, сериализация, проверка по схемам."""
import json
import re
import unittest
from pathlib import Path

from iam.domain import (CATALOG, EVENT_TYPES, Event, Link, MemoryRecord, Op, Source, Transition, ValidationError,
                        canonical_json, check_event_kind, ids, jsonschema, new_id, prefix_of, validate,
                        validate_envelope)
from iam.domain.types import MEMORY_KINDS, ORIGINS, STATE_GROUPS

REPO = Path(__file__).resolve().parents[2]
SPEC = REPO / "docs" / "spec"
SAMPLES = SPEC / "schemas" / "samples"


def sample(name: str) -> dict:
    return json.loads((SAMPLES / name).read_text(encoding="utf-8"))


def samples(prefix: str, kind: str) -> list[Path]:
    return sorted(SAMPLES.glob(f"{prefix}.*.{kind}.json"))


class IdsTest(unittest.TestCase):
    def test_new_id_format_and_uniqueness(self):
        a, b = new_id("evt"), new_id("evt")
        self.assertRegex(a, r"^evt_[A-Za-z0-9]{20}$")
        self.assertNotEqual(a, b)
        self.assertEqual(prefix_of(new_id("prop", 8)), "prop")
        self.assertEqual(len(new_id("mem", 40)), 44)

    def test_new_id_rejects_bad_input(self):
        with self.assertRaises(ValueError):
            new_id("zzz")
        for n in (7, 41):
            with self.assertRaises(ValueError):
                new_id("evt", n)

    def test_prefix_of_rejects(self):
        for bad in ("evt_short", "xyz_ABCDEFGH12", "EVT_ABCDEFGH", 42, "evt-ABCDEFGH"):
            with self.assertRaises(ValueError, msg=bad):
                prefix_of(bad)

    def test_persona_ids(self):
        self.assertTrue(ids.is_persona_id("per_Aurora0001"))
        self.assertFalse(ids.is_persona_id("evt_Aurora0001"))
        self.assertFalse(ids.is_persona_id(None))
        self.assertFalse(ids.is_experimental("per_Aurora0001"))
        self.assertTrue(ids.is_experimental("exp_Aurora0001"))
        with self.assertRaises(ValueError):
            ids.is_experimental("per_x")

    def test_prefixes_cover_spec(self):
        text = (SPEC / "events.md").read_text(encoding="utf-8")
        section = text.split("## 1. Идентификаторы")[1].split("## 2.")[0]
        spec_prefixes = set(re.findall(r"`([a-z]{2,4})_…`", section)) | {"per", "exp"}
        self.assertEqual(spec_prefixes, set(ids.PREFIXES))


class CatalogTest(unittest.TestCase):
    def test_catalog_matches_events_md_table(self):
        text = (SPEC / "events.md").read_text(encoding="utf-8")
        body = text.split("<!-- каталог:начало -->")[1].split("<!-- каталог:конец -->")[0]
        rows = {}
        for line in body.strip().splitlines()[2:]:
            cells = [c.strip() for c in line.strip("|").split("|")]
            rows[cells[0].strip("`")] = (cells[2] == "да", cells[3].strip("`"))
        self.assertEqual(rows, {t: (k.changes_state, k.key_template) for t, k in CATALOG.items()})

    def test_catalog_matches_schema_enum(self):
        schema = json.loads((SPEC / "schemas" / "event.schema.json").read_text(encoding="utf-8"))
        self.assertEqual(list(EVENT_TYPES), schema["properties"]["type"]["enum"])

    def test_key_templates(self):
        k = CATALOG["session.paused"]
        self.assertTrue(k.key_re.match("ses:ses_ABCDEFGH:pause:2"))
        self.assertFalse(k.key_re.match("ses:ses_ABCDEFGH:resume:2"))
        self.assertFalse(k.key_re.match("ses::pause:2"))
        out = CATALOG["message.out_requested"]
        self.assertTrue(out.key_re.match("out:act_ABCDEFGH"))
        self.assertFalse(out.key_re.match("out:act_ABCDEFGH:ok"), "ключ out_sent не выдаёт себя за out_requested")

    def test_check_event_kind(self):
        self.assertEqual(check_event_kind("input.arrived", False, "rx:tg:1"), [])
        self.assertIn("неизвестный", check_event_kind("tick", False, "x:1")[0])
        errs = check_event_kind("input.arrived", True, "rd:1")
        self.assertEqual(len(errs), 2)


class SchemaCopiesTest(unittest.TestCase):
    def test_package_schemas_identical_to_spec(self):
        spec = {p.name: p.read_bytes() for p in (SPEC / "schemas").glob("*.schema.json")}
        pkg = {p.name: p.read_bytes() for p in jsonschema.SCHEMA_DIR.glob("*.schema.json")}
        self.assertEqual(spec, pkg, "схемы в src/iam/domain/schemas расходятся с docs/spec/schemas")
        self.assertEqual(set(jsonschema.SCHEMA_NAMES), {n.split(".")[0] for n in pkg})

    def test_unknown_schema_name(self):
        with self.assertRaises(KeyError):
            jsonschema.errors("nope", {})

    def test_validate_raises_with_errors(self):
        with self.assertRaises(ValidationError) as cm:
            validate("transition", {})
        self.assertEqual(cm.exception.name, "transition")
        self.assertTrue(cm.exception.errors)
        validate("transition", sample("transition.belief_set.valid.json"))

    def test_unknown_keyword_is_error(self):
        self.assertIn("неподдерживаемые", jsonschema.check({"format": "x"}, 1, SPEC)[0])


class EventTest(unittest.TestCase):
    def test_roundtrip_valid_samples(self):
        for p in samples("event", "valid"):
            d = json.loads(p.read_text(encoding="utf-8"))
            ev = Event.from_dict(d)
            self.assertEqual(ev.to_dict(), d, p.name)
            self.assertIsInstance(ev.source, Source)
            self.assertEqual(ev.errors(), [])

    def test_invalid_samples_rejected(self):
        for p in samples("event", "invalid"):
            with self.assertRaises(ValidationError, msg=p.name):
                Event.from_dict(json.loads(p.read_text(encoding="utf-8")))

    def test_catalog_rule_enforced_beyond_schema(self):
        d = sample("event.input_arrived.valid.json")
        d["changes_state"] = True  # схема допускает, каталог — нет
        with self.assertRaises(ValidationError):
            Event.from_dict(d)
        d = sample("event.input_arrived.valid.json")
        d["idempotency_key"] = "rd:inp_1"
        with self.assertRaises(ValidationError):
            Event.from_dict(d)

    def test_with_seq_and_defaults(self):
        ev = Event(event_id="evt_ABCDEFGH", persona_id="per_ABCDEFGH", type="input.arrived",
                   occurred_at="2026-10-11T18:00:00Z", life_time_ms=0, source=Source("receiver", "tg"),
                   idempotency_key="rx:tg:1", epoch=1, state_version=0, changes_state=False)
        self.assertIsNone(ev.seq)
        self.assertEqual(ev.with_seq(5).seq, 5)
        self.assertEqual(ev.to_dict()["causes"], [])
        self.assertIs(ev.validate(), ev)


class MemoryRecordTest(unittest.TestCase):
    def test_roundtrip_valid_samples(self):
        for p in samples("memory_record", "valid"):
            d = json.loads(p.read_text(encoding="utf-8"))
            rec = MemoryRecord.from_dict(d)
            self.assertEqual(rec.to_dict(), d, p.name)
            self.assertEqual(rec.text, d["content"]["text"])
            self.assertEqual(rec.errors(), [])
            self.assertIs(rec.validate(), rec)

    def test_invalid_samples_rejected(self):
        for p in samples("memory_record", "invalid"):
            with self.assertRaises(ValidationError, msg=p.name):
                MemoryRecord.from_dict(json.loads(p.read_text(encoding="utf-8")))

    def test_links_and_optional_fields(self):
        rec = MemoryRecord(memory_id="mem_ABCDEFGH", persona_id="per_ABCDEFGH", layer="archive", kind="episodic",
                           origin="observation", content={"text": "т"}, source=Source("core", "x"),
                           created_at="2026-10-11T18:00:00Z", created_life_ms=0, sources=("evt_ABCDEFGH",),
                           links=(Link("cause", "evt_ABCDEFGH"),))
        d = rec.to_dict()
        self.assertEqual(d["links"], [{"type": "cause", "target": "evt_ABCDEFGH"}])
        self.assertNotIn("availability", d)
        self.assertEqual(MemoryRecord.from_dict(d), rec)


class TransitionTest(unittest.TestCase):
    def test_roundtrip(self):
        d = sample("transition.belief_set.valid.json")
        tr = Transition.from_dict(d)
        self.assertEqual(tr.to_dict(), d)
        self.assertIs(tr.validate(), tr)

    def test_invalid(self):
        with self.assertRaises(ValidationError):
            Transition.from_dict(sample("transition.no_cause.invalid.json"))

    def test_op_value_presence_is_preserved(self):
        self.assertNotIn("value", Op("beliefs", "x", "remove").to_dict())
        self.assertEqual(Op.from_dict({"group": "beliefs", "path": "x", "op": "set", "value": None}).to_dict(),
                         {"group": "beliefs", "path": "x", "op": "set", "value": None})
        tr = Transition("trn_ABCDEFGH", "per_ABCDEFGH", 1, 0, (Op("traits", "a", "remove"),), "причина",
                        ("evt_ABCDEFGH",))
        self.assertNotIn("proposal_id", tr.to_dict())


class EnvelopeTest(unittest.TestCase):
    def test_valid_and_invalid(self):
        for name in ("request", "response", "proposal"):
            for p in samples(name, "valid"):
                d = json.loads(p.read_text(encoding="utf-8"))
                self.assertIs(validate_envelope(name, d), d)
            for p in samples(name, "invalid"):
                with self.assertRaises(ValidationError, msg=p.name):
                    validate_envelope(name, json.loads(p.read_text(encoding="utf-8")))
        with self.assertRaises(KeyError):
            validate_envelope("event", {})


class ConstantsTest(unittest.TestCase):
    def test_enums_match_schemas(self):
        mem = json.loads((SPEC / "schemas" / "memory_record.schema.json").read_text(encoding="utf-8"))
        self.assertEqual(list(MEMORY_KINDS), mem["properties"]["kind"]["enum"])
        self.assertEqual(list(ORIGINS), mem["properties"]["origin"]["enum"])
        tr = json.loads((SPEC / "schemas" / "transition.schema.json").read_text(encoding="utf-8"))
        self.assertEqual(list(STATE_GROUPS) + ["memory"], tr["properties"]["ops"]["items"]["properties"]["group"]["enum"])

    def test_canonical_json(self):
        self.assertEqual(canonical_json({"б": 1, "а": [1, 2]}), '{"а":[1,2],"б":1}')


class ValidatorKeywordsTest(unittest.TestCase):
    """Каждое поддержанное ключевое слово действительно проверяет (а не пропускает молча)."""

    def chk(self, schema, value):
        return jsonschema.check(schema, value, SPEC / "schemas")

    def test_keywords(self):
        cases = [
            ({"const": 1}, 1, 2),
            ({"type": "string", "maxLength": 2}, "аб", "абв"),
            ({"type": "integer", "minimum": 0, "maximum": 3}, 3, 4),
            ({"type": "integer", "minimum": 0}, 0, -1),
            ({"type": "number"}, 1.5, True),
            ({"type": "array", "uniqueItems": True}, [1, 2], [1, 1]),
            ({"type": "array", "minItems": 1, "items": {"type": "integer"}}, [1], ["x"]),
            ({"type": "object", "properties": {"a": {"type": "null"}}}, {"a": None}, {"a": 1}),
            ({"allOf": [{"type": "integer"}, {"minimum": 5}]}, 5, 4),
            ({"not": {"const": "x"}}, "y", "x"),
            ({"if": {"const": 1}, "then": {"type": "boolean"}}, 2, 1),
        ]
        for schema, good, bad in cases:
            self.assertEqual(self.chk(schema, good), [], (schema, good))
            self.assertNotEqual(self.chk(schema, bad), [], (schema, bad))
        self.assertEqual(self.chk({"if": {"const": 1}}, 1), [], "if без then ничего не требует")

    def test_ref_to_sibling_file(self):
        good = sample("response.ok.valid.json")
        self.assertEqual(self.chk({"$ref": "response.schema.json"}, good), [])
        self.assertNotEqual(self.chk({"$ref": "proposal.schema.json"}, {}), [])

    def test_validate_methods_raise(self):
        bad_ev = Event(event_id="bad", persona_id="per_ABCDEFGH", type="input.arrived", occurred_at=T0,
                       life_time_ms=0, source=Source("receiver", "tg"), idempotency_key="rx:tg:1", epoch=1,
                       state_version=0, changes_state=False)
        with self.assertRaises(ValidationError):
            bad_ev.validate()
        self.assertTrue(bad_ev.errors())
        tr = Transition("trn_ABCDEFGH", "per_ABCDEFGH", 1, 0, (), "причина", ("evt_ABCDEFGH",))
        with self.assertRaises(ValidationError):
            tr.validate()


T0 = "2026-10-11T18:00:00Z"


if __name__ == "__main__":
    unittest.main()
