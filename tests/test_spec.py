"""Тесты спецификации S02: схемы, образцы, полнота групп состояния и видов памяти, каталог событий."""
import json
import re
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SPEC = REPO / "docs" / "spec"
SCHEMAS = SPEC / "schemas"
sys.path.insert(0, str(Path(__file__).resolve().parent))
import schema_lite  # noqa: E402

STATE_GROUPS = ["traits", "affect", "beliefs", "relations", "intentions", "preferences", "selfmodel", "world_time"]
MEMORY_KINDS = ["sensory", "working", "episodic", "semantic", "autobiographical", "emotional_associative",
                "procedural", "social", "prospective"]
ORIGINS = ["observation", "model_knowledge", "stated_by_other", "own_hypothesis", "imagination", "simulation_event"]


def table_after(text: str, anchor: str) -> str:
    """Тело первой таблицы после якоря (строки после разделителя |---|…)."""
    part = text.split(anchor)[1]
    m = re.search(r"^\|[-|]+\|\n((?:\|.*\n)+)", part, re.M)
    return m.group(1)


def load(p: Path):
    return json.loads(p.read_text(encoding="utf-8"))


class SchemasTest(unittest.TestCase):
    def test_all_schemas_valid_json_with_id(self):
        files = sorted(SCHEMAS.glob("*.schema.json"))
        self.assertGreaterEqual(len(files), 6)
        for f in files:
            s = load(f)
            self.assertEqual(s["$id"], f.name)
            self.assertEqual(schema_lite.check({"type": "object"}, s, SCHEMAS), [])

    def test_samples(self):
        samples = sorted((SCHEMAS / "samples").glob("*.json"))
        self.assertGreaterEqual(len(samples), 15)
        counts = {"valid": 0, "invalid": 0}
        for f in samples:
            name, _, verdict, _ = f.name.rsplit(".", 3)
            self.assertIn(verdict, counts, f.name)
            schema = load(SCHEMAS / f"{name}.schema.json")
            errs = schema_lite.check(schema, load(f), SCHEMAS)
            counts[verdict] += 1
            if verdict == "valid":
                self.assertEqual(errs, [], f"{f.name}: {errs}")
            else:
                self.assertTrue(errs, f"{f.name}: образец должен быть отклонён")
        self.assertGreater(counts["invalid"], 5)

    def test_every_schema_has_valid_and_invalid_sample(self):
        for f in SCHEMAS.glob("*.schema.json"):
            base = f.name.split(".")[0]
            kinds = {s.name.rsplit(".", 3)[2] for s in (SCHEMAS / "samples").glob(f"{base}.*.json")}
            self.assertEqual(kinds, {"valid", "invalid"}, base)

    def test_validator_rejects_unknown_keyword(self):
        self.assertTrue(schema_lite.check({"oneOf": []}, 1, SCHEMAS))


class EventCatalogTest(unittest.TestCase):
    def catalog(self):
        text = (SPEC / "events.md").read_text(encoding="utf-8")
        block = text.split("<!-- каталог:начало -->")[1].split("<!-- каталог:конец -->")[0]
        return re.findall(r"^\| `([a-z_.]+)` \| ([^|]+) \| (да|нет) \| `([^`]+)` \|", block, re.M)

    def test_catalog_equals_schema_enum(self):
        rows = self.catalog()
        types = [r[0] for r in rows]
        self.assertEqual(len(types), len(set(types)), "типы в каталоге повторяются")
        self.assertEqual(set(types), set(load(SCHEMAS / "event.schema.json")["properties"]["type"]["enum"]))

    def test_perception_stages_are_distinct_events(self):
        types = {r[0] for r in self.catalog()}
        self.assertTrue({"input.arrived", "input.noticed", "input.read"} <= types)

    def test_arrival_does_not_change_state_but_confirmation_does(self):
        rows = {r[0]: r[2] for r in self.catalog()}
        self.assertEqual(rows["input.arrived"], "нет")
        self.assertEqual(rows["action.confirmed"], "да")
        self.assertEqual(rows["proposal.stale"], "нет")

    def test_idempotency_keys_present_and_unique_patterns(self):
        keys = [r[3] for r in self.catalog()]
        self.assertEqual(len(keys), len(set(keys)))

    def test_model_never_produces_events_directly(self):
        # Ни в каталоге, ни в источниках события нет «модели»: она только предлагает.
        self.assertNotIn("модель", " ".join(r[1] for r in self.catalog()))
        kinds = load(SCHEMAS / "event.schema.json")["properties"]["source"]["properties"]["kind"]["enum"]
        self.assertNotIn("model", kinds)

    def test_epoch_in_all_versioned_schemas(self):
        for name in ("event", "request", "response"):
            sch = load(SCHEMAS / f"{name}.schema.json")
            self.assertIn("epoch", sch["required"], name)
        self.assertIn("base_epoch", load(SCHEMAS / "transition.schema.json")["required"])

    def test_state_groups_match_transition_schema(self):
        enum = load(SCHEMAS / "transition.schema.json")["properties"]["ops"]["items"]["properties"]["group"]["enum"]
        self.assertEqual(enum, STATE_GROUPS + ["memory"])
        prop = load(SCHEMAS / "proposal.schema.json")["allOf"]
        sc = next(c for c in prop if c["if"]["properties"]["kind"]["const"] == "state_change")
        self.assertEqual(sc["then"]["properties"]["body"]["properties"]["ops"]["items"]["properties"]["group"]["enum"], enum)

    def test_pause_table_mentions_every_event_prefix(self):
        # каждое семейство событий из каталога названо в таблице допустимости в time.md
        time_md = (SPEC / "time.md").read_text(encoding="utf-8")
        for t, *_ in self.catalog():
            family = t.split(".")[0]
            self.assertTrue(f"`{family}." in time_md or f"`{t}" in time_md, f"{t}: семейство {family} не упомянуто в time.md")


class CompletenessTest(unittest.TestCase):
    def anchors(self, fname):
        text = (SPEC / fname).read_text(encoding="utf-8")
        return set(re.findall(r"\{#([\w-]+)\}", text))

    def test_eight_state_groups_in_spec(self):
        text = (SPEC / "state.md").read_text(encoding="utf-8")
        table = text.split("## 1. Обзор")[1].split("Общие правила")[0]
        keys = re.findall(r"^\| \d \| `(\w+)` \|", table, re.M)
        self.assertEqual(keys, STATE_GROUPS)
        anchors = self.anchors("state.md")
        for g in STATE_GROUPS:
            self.assertIn(g.replace("_", "-"), anchors, f"нет раздела для группы {g}")

    def test_nine_memory_kinds_in_spec_and_schema(self):
        text = (SPEC / "provenance.md").read_text(encoding="utf-8")
        table = table_after(text, "{#kinds}")
        self.assertEqual(re.findall(r"^\| `(\w+)` \|", table, re.M), MEMORY_KINDS)
        self.assertEqual(load(SCHEMAS / "memory_record.schema.json")["properties"]["kind"]["enum"], MEMORY_KINDS)

    def test_origin_types_in_spec_and_schema(self):
        enum = load(SCHEMAS / "memory_record.schema.json")["properties"]["origin"]["enum"]
        self.assertEqual(enum, ORIGINS + ["model_explanation"])
        text = (SPEC / "provenance.md").read_text(encoding="utf-8")
        for o in enum:
            self.assertIn(f"`{o}`", text)

    def test_three_layers(self):
        enum = load(SCHEMAS / "memory_record.schema.json")["properties"]["layer"]["enum"]
        self.assertEqual(enum, ["archive", "personal", "interpretation"])

    def test_proposal_kinds_in_spec_match_schema(self):
        text = (SPEC / "envelopes.md").read_text(encoding="utf-8")
        table = table_after(text, "{#proposal}")
        spec_kinds = re.findall(r"^\| `(\w+)` \|", table, re.M)
        self.assertEqual(spec_kinds, load(SCHEMAS / "proposal.schema.json")["properties"]["kind"]["enum"])

    def test_perception_stages_and_roles_documented(self):
        for needle, fname in [("input.arrived", "events.md"), ("N1", "events.md"), ("N2", "events.md"),
                              ("operator", "permissions.md"), ("interlocutor", "permissions.md"),
                              ("restore_from_archive", "permissions.md"), ("монотонн", "time.md"),
                              ("proposal.stale", "envelopes.md")]:
            self.assertIn(needle, (SPEC / fname).read_text(encoding="utf-8"), f"{needle} в {fname}")

    def test_every_section_lists_requirements(self):
        for f in ["events.md", "state.md", "time.md", "provenance.md", "permissions.md", "envelopes.md"]:
            text = (SPEC / f).read_text(encoding="utf-8")
            anchors = re.findall(r"\{#([\w-]+)\}\s*\n\s*\nТребования: (R-\d+(?:, R-\d+)*)\n", text)
            self.assertEqual(len(anchors), len(re.findall(r"\{#[\w-]+\}", text)), f"{f}: у каждого раздела должна быть строка «Требования:»")


if __name__ == "__main__":
    unittest.main()
