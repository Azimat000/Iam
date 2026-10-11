"""Тесты tools/scenarios.py: формат сценария, полнота каталога, связь с реестром требований.

Проверяются сломанные образцы (каждая поломка даёт своё сообщение), временный корень репозитория
через IAM_ROOT и реальный каталог scenarios/.
"""
import copy
import json
import os
import subprocess
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
TOOL = REPO / "tools" / "scenarios.py"

sys.path.insert(0, str(REPO / "tools"))
import scenarios as sc  # noqa: E402

TYPES = {"input.arrived", "input.noticed", "input.read", "proposal.accepted", "proposal.rejected",
         "memory.retrieved", "memory.recorded", "system.error", "attention.chosen", "time.moment_due"}

GOOD_TOML = """
format = 1
id = "demo"
group = "required"
title = "Демо"
summary = "Проверочный сценарий."
requirements = ["R-001"]
plan_row = 1
plan_title = "Демо"
plan_expected = "Ничего плохого"
failure = "Случилось плохое."

[[background]]
id = "fact"
kind = "memory"
origin = "observation"
text = "Она видела дом."

[[step]]
id = "start"
kind = "session"
action = "start"

[[step]]
id = "msg"
kind = "input"
sender = "operator"
text = "Привет"

[[step]]
id = "choose"
kind = "decide"
situation = "Что делать."

[[assert]]
id = "a_event"
kind = "event"
type = "input.arrived"
min = 1
where = { sender = "operator" }
after = "start"
explain = "Сообщение сохранено."

[[assert]]
id = "a_state"
kind = "state"
path = "intentions.x.status"
op = "eq"
value = "active"
explain = "Статус."

[[assert]]
id = "a_state2"
kind = "state"
path = "memory.fact.availability"
op = "unchanged"
since = "start"
explain = "Не менялось."

[[assert]]
id = "a_text"
kind = "text"
marker = "ФРАЗА"
scope = "model_requests"
presence = "absent"
explain = "Нет в запросе."

[[assert]]
id = "a_absent"
kind = "absent"
type = "system.error"
explain = "Ошибок нет."

[[assert]]
id = "a_order"
kind = "order"
first = "input.arrived"
second = "input.noticed"
explain = "Порядок."

[[assert]]
id = "a_chain"
kind = "chain"
effect = "proposal.accepted"
caused_by = "memory.retrieved"
explain = "Цепочка."
"""

EXP_EXTRA = """
[[run]]
id = "one"

[[run]]
id = "two"
switches = { memory = "off" }

[[assert]]
id = "a_cmp"
kind = "compare"
class = "causal"
runs = ["one", "two"]
subject = "journal:proposal.accepted"
relation = "differs"
explain = "Различаются."
"""

REQS = [
    {"id": "R-001", "kind": "functional", "check": "Сценарий scenarios/required/demo.toml: проверка."},
    {"id": "R-002", "kind": "constraint", "check": "Просто текст проверки."},
]


def good() -> dict:
    return tomllib.loads(GOOD_TOML)


def errors(d: dict, stem="demo", group="required", reqs=("R-001", "R-002")) -> list[str]:
    return sc.validate_scenario(d, stem, group, set(reqs), TYPES)


def exp_scenario() -> dict:
    """Минимальный допустимый сценарий группы experiments: два прогона, оба класса утверждений."""
    d = tomllib.loads(GOOD_TOML + EXP_EXTRA)
    d["group"] = "experiments"
    for k in ("plan_row", "plan_title", "plan_expected"):
        del d[k]
    for a in d["assert"]:
        a["class"] = "technical" if a["id"] != "a_cmp" else "causal"
    return d


def dump_value(v):
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    if isinstance(v, list):
        return "[" + ", ".join(dump_value(x) for x in v) + "]"
    if isinstance(v, dict):
        return "{ " + ", ".join(f"{k} = {dump_value(x)}" for k, x in v.items()) + " }"
    return json.dumps(v, ensure_ascii=False)


def dump_toml(d: dict) -> str:
    out = []
    for k, v in d.items():
        if not (isinstance(v, list) and v and isinstance(v[0], dict)):
            out.append(f"{k} = {dump_value(v)}")
    for k, v in d.items():
        if isinstance(v, list) and v and isinstance(v[0], dict):
            for t in v:
                out.append(f"\n[[{k}]]")
                out += [f"{kk} = {dump_value(vv)}" for kk, vv in t.items()]
    return "\n".join(out) + "\n"


class ScenarioErrorsTest(unittest.TestCase):
    def assertHas(self, errs, needle):
        self.assertTrue(any(needle in e for e in errs), f"нет «{needle}» в {errs}")

    def test_good_is_valid(self):
        self.assertEqual(errors(good()), [])

    def test_experiments_good_is_valid(self):
        self.assertEqual(errors(exp_scenario(), group="experiments"), [])

    def mutate(self, fn, group="required", base=None):
        d = copy.deepcopy(base or good())
        fn(d)
        return errors(d, group=group)

    def test_top_level_problems(self):
        cases = [
            (lambda d: d.update(format=2), "format должно быть 1"),
            (lambda d: d.update(id="other"), "не совпадает с именем файла"),
            (lambda d: d.update(group="motivation"), "не совпадает с каталогом"),
            (lambda d: d.update(failure=""), "признак провала обязателен"),
            (lambda d: d.update(title=" "), "поле title"),
            (lambda d: d.update(bogus=1), "неизвестные поля"),
            (lambda d: d.update(requirements=[]), "непустой список"),
            (lambda d: d.update(requirements=["R-001", "R-001"]), "ID повторяются"),
            (lambda d: d.update(requirements=["R-999"]), "R-999 не существует"),
            (lambda d: d.pop("plan_row"), "нужен plan_row"),
            (lambda d: d.update(plan_row=0), "нужен plan_row"),
            (lambda d: d.pop("plan_title"), "нужно поле plan_title"),
        ]
        for fn, needle in cases:
            with self.subTest(needle):
                self.assertHas(self.mutate(fn), needle)

    def test_plan_fields_only_for_required(self):
        d = exp_scenario()
        d["plan_row"] = 3
        self.assertHas(errors(d, group="experiments"), "только для группы required")

    def test_background_problems(self):
        d = good()
        d["background"] = [{"kind": "wrong", "text": ""}, {"kind": "memory", "text": "x", "origin": "dream"},
                           {"kind": "memory", "text": "x", "run": "zz"}]
        errs = errors(d)
        self.assertHas(errs, "kind должно быть из")
        self.assertHas(errs, "text пустой")
        self.assertHas(errs, "origin «dream»")
        self.assertHas(errs, "run «zz» не объявлен")
        d["background"] = "oops"
        self.assertHas(errors(d), "background должно быть списком")

    def test_run_problems(self):
        d = exp_scenario()
        d["run"] = [{"id": "one"}, {"id": "one", "switches": {"memory": "maybe"}}, {"switches": {"x": "on"}}]
        errs = errors(d, group="experiments")
        self.assertHas(errs, "id повторяется")
        self.assertHas(errs, "switches — таблица")
        self.assertHas(errs, "нужен id")
        d["run"] = [{"id": "solo"}]
        self.assertHas(errors(d, group="experiments"), "одиночный [[run]]")
        d["run"] = "oops"
        self.assertHas(errors(d, group="experiments"), "run должно быть списком")

    def test_multiple_runs_need_compare(self):
        d = exp_scenario()
        d["assert"] = [a for a in d["assert"] if a["kind"] != "compare"]
        errs = errors(d, group="experiments")
        self.assertHas(errs, "нет утверждения kind=compare")
        self.assertHas(errs, "class=causal")

    def test_step_problems(self):
        cases = [
            (lambda d: d["step"][0].update(id="bad id"), "нужен id из латинских"),
            (lambda d: d["step"][1].update(id="start"), "id шага повторяется"),
            (lambda d: d["step"][0].update(kind="teleport"), "kind «teleport»"),
            (lambda d: d["step"][0].update(action="dance"), "session.action"),
            (lambda d: d["step"][0].update(reason="mood"), "reason «mood»"),
            (lambda d: d["step"][0].update(extra=1), "неизвестные поля"),
            (lambda d: d["step"][1].pop("text"), "нужно поле text"),
            (lambda d: d["step"][1].update(sender="friend"), "sender должно быть"),
            (lambda d: d["step"][1].update(text=" "), "text пустой"),
            (lambda d: d["step"][2].update(situation=" "), "situation пустая"),
            (lambda d: d["step"][1].update(run="zz"), "run «zz» не объявлен"),
            (lambda d: d["step"].append({"id": "w0", "kind": "wait"}), "ровно одно из"),
            (lambda d: d["step"].append({"id": "w2", "kind": "wait", "life_ms": 1, "calendar_ms": 1}), "ровно одно из"),
            (lambda d: d["step"].append({"id": "w3", "kind": "wait", "life_ms": -5}), "положительным целым"),
            (lambda d: d["step"].append({"id": "m1", "kind": "model", "proposal": "x", "delivery": "once"}), "proposal должно"),
            (lambda d: d["step"].append({"id": "m2", "kind": "model", "proposal": "other", "delivery": "never"}), "delivery должно"),
            (lambda d: d["step"].append({"id": "w1", "kind": "world", "outcome": "ok", "action": ""}), "outcome должно"),
            (lambda d: d["step"].append({"id": "w4", "kind": "world", "outcome": "failed", "action": " "}), "action пустое"),
            (lambda d: d["step"].append({"id": "o1", "kind": "operator", "intervention": "fly", "reason": "r"}), "intervention должно"),
            (lambda d: d["step"].append({"id": "o2", "kind": "operator", "intervention": "edit_config", "reason": " "}), "обязательна непустая причина"),
            (lambda d: d["step"].append({"id": "f1", "kind": "fault", "fault": "meteor"}), "fault должно"),
            (lambda d: d["step"].append({"id": "b1", "kind": "backup", "action": "burn"}), "backup.action"),
            (lambda d: d["step"].append({"id": "p1", "kind": "provider", "action": "swap", "to": ""}), "provider требует"),
            (lambda d: d.update(step=[]), "нет ни одного шага"),
        ]
        for fn, needle in cases:
            with self.subTest(needle):
                self.assertHas(self.mutate(fn), needle)

    def test_valid_rare_step_kinds(self):
        d = good()
        d["step"] += [
            {"id": "w1", "kind": "wait", "life_ms": 5},
            {"id": "w2", "kind": "wait", "calendar_ms": 5},
            {"id": "m1", "kind": "model", "proposal": "message", "delivery": "stale", "content": "x"},
            {"id": "wd", "kind": "world", "outcome": "no_answer", "action": "отправить"},
            {"id": "op", "kind": "operator", "intervention": "grant_capability", "reason": "тест"},
            {"id": "ft", "kind": "fault", "fault": "disk_full"},
            {"id": "bk", "kind": "backup", "action": "restore"},
            {"id": "pv", "kind": "provider", "action": "swap", "to": "B"},
        ]
        self.assertEqual(errors(d), [])

    def test_key_decision_limit_in_experiments(self):
        d = exp_scenario()
        for i in range(5):
            d["step"].append({"id": f"dd{i}", "kind": "decide", "situation": "ещё"})
        self.assertHas(errors(d, group="experiments"), "допустимо не более 4")

    def test_key_decisions_counted_per_run_and_shared(self):
        d = exp_scenario()
        d["step"] = [{"id": "s", "kind": "session", "action": "start"}]
        for i in range(3):
            d["step"].append({"id": f"c{i}", "kind": "decide", "situation": "общее"})
        for i in range(2):
            d["step"].append({"id": f"o{i}", "kind": "decide", "situation": "своё", "run": "one"})
        errs = errors(d, group="experiments")
        self.assertHas(errs, "прогон one: 5 ключевых решений")
        self.assertFalse(any("прогон two" in e and "ключевых решений (decide)," in e for e in errs))

    def test_experiments_need_decide(self):
        d = exp_scenario()
        d["step"] = [s for s in d["step"] if s["kind"] != "decide"]
        self.assertHas(errors(d, group="experiments"), "нет ни одного ключевого решения")

    def test_assert_problems(self):
        cases = [
            (lambda d: d["assert"][0].update(id="x y"), "нужен id из латинских"),
            (lambda d: d["assert"][1].update(id="a_event"), "id повторяется"),
            (lambda d: d["assert"][0].update(explain=""), "нет explain"),
            (lambda d: d["assert"][0].update(**{"class": "vibes"}), "class должно быть"),
            (lambda d: d["assert"][0].update(run="zz"), "run «zz» не объявлен"),
            (lambda d: d["assert"][0].update(kind="feel"), "kind «feel»"),
            (lambda d: d["assert"][0].update(bogus=1), "неизвестные поля"),
            (lambda d: d["assert"][0].pop("type"), "нужно поле type"),
            (lambda d: d["assert"][0].update(after="zz"), "after ссылается"),
            (lambda d: d["assert"][0].update(type="made.up"), "не из каталога"),
            (lambda d: d["assert"][0].update(count=1), "count нельзя сочетать"),
            (lambda d: d["assert"][0].update(min=-1), "неотрицательным целым"),
            (lambda d: d["assert"][0].update(where={}), "where должно быть непустой"),
            (lambda d: d["assert"][1].update(path="elsewhere.x"), "path должен начинаться"),
            (lambda d: d["assert"][1].update(op="vibes"), "op должно быть из"),
            (lambda d: d["assert"][1].pop("value"), "нужно поле value"),
            (lambda d: d["assert"][2].pop("since"), "нужен since"),
            (lambda d: d["assert"][2].update(value=1), "value не используется"),
            (lambda d: d["assert"][3].update(marker=" "), "marker пустой"),
            (lambda d: d["assert"][3].update(scope="heaven"), "scope должно быть"),
            (lambda d: d["assert"][3].update(presence="maybe"), "presence должно быть"),
            (lambda d: d["assert"][5].update(first="made.up"), "не из каталога"),
            (lambda d: d["assert"].clear(), "нет ни одного утверждения"),
        ]
        for fn, needle in cases:
            with self.subTest(needle):
                self.assertHas(self.mutate(fn), needle)

    def test_references_must_match_run(self):
        d = exp_scenario()
        d["step"].append({"id": "only_one", "kind": "wait", "life_ms": 5, "run": "one"})
        d["assert"].append({"id": "x_any", "kind": "absent", "type": "system.error", "after": "only_one", "explain": "e"})
        d["assert"][-1]["class"] = "technical"
        self.assertHas(errors(d, group="experiments"), "а утверждение относится к всем прогонам")
        d["assert"][-1]["run"] = "two"
        self.assertHas(errors(d, group="experiments"), "прогона «one», а утверждение относится к прогону «two»")
        d["assert"][-1]["run"] = "one"
        self.assertEqual(errors(d, group="experiments"), [])

    def test_window_allowed_on_every_kind(self):
        d = good()
        for a in d["assert"]:
            a["after"] = "start"
            a["before"] = "choose"
        self.assertEqual(errors(d), [])

    def test_input_deliveries_model_purpose_per(self):
        d = good()
        d["step"][1]["deliveries"] = 2
        d["step"].append({"id": "mr", "kind": "model", "proposal": "state_change", "delivery": "once", "purpose": "reflect"})
        d["assert"][0]["per"] = "cycle"
        self.assertEqual(errors(d), [])
        d["step"][1]["deliveries"] = 0
        d["step"][-1]["purpose"] = "daydream"
        d["assert"][0]["per"] = "day"
        errs = errors(d)
        self.assertHas(errs, "deliveries должно быть")
        self.assertHas(errs, "purpose должно быть")
        self.assertHas(errs, "per может быть только")

    def test_state_present_and_capabilities_group(self):
        d = good()
        d["assert"].append({"id": "p", "kind": "state", "path": "capabilities.shell.status", "op": "present", "explain": "e"})
        self.assertEqual(errors(d), [])
        d["assert"][-1]["value"] = 1
        self.assertHas(errors(d), "op=present поле value не используется")

    def test_proposal_kinds_follow_envelopes(self):
        text = (REPO / "docs" / "spec" / "envelopes.md").read_text(encoding="utf-8")
        for kind in sc.PROPOSAL_KINDS:
            self.assertIn(f"| `{kind}` |", text, kind)
        for purpose in sc.PURPOSES:
            self.assertIn(f"`{purpose}`", text, purpose)

    def test_state_absent_ok_and_since_checked(self):
        d = good()
        d["assert"].append({"id": "gone", "kind": "state", "path": "beliefs.x", "op": "absent", "explain": "нет"})
        self.assertEqual(errors(d), [])
        d["assert"][-1]["since"] = "nowhere"
        self.assertHas(errors(d), "since ссылается на несуществующий шаг")

    def test_compare_problems(self):
        base = exp_scenario()
        for runs, subj, rel, needle in [
            (["one", "one"], "journal:proposal.accepted", "differs", "два разных объявленных"),
            (["one", "zz"], "journal:proposal.accepted", "differs", "два разных объявленных"),
            (["one"], "journal:proposal.accepted", "differs", "два разных объявленных"),
            (["one", "two"], "vibes", "differs", "subject — «journal"),
            (["one", "two"], "journal:proposal.accepted", "better", "relation должно"),
        ]:
            with self.subTest(needle):
                d = copy.deepcopy(base)
                cmp = next(a for a in d["assert"] if a["kind"] == "compare")
                cmp.update(runs=runs, subject=subj, relation=rel)
                self.assertHas(errors(d, group="experiments"), needle)

    def test_experiments_assert_classes(self):
        d = exp_scenario()
        del d["assert"][0]["class"]
        self.assertHas(errors(d, group="experiments"), "обязателен class")
        d = exp_scenario()
        for a in d["assert"]:
            a["class"] = "causal"
        self.assertHas(errors(d, group="experiments"), "class=technical")

    def test_metrics(self):
        d = good()
        d["metric"] = [{"id": "m", "definition": "считаем", "source": "journal"}]
        self.assertEqual(errors(d), [])
        d["metric"] = [{"id": "m", "definition": " ", "source": "gut"}, {"id": "m", "definition": "x", "source": "state"},
                       {"extra": 1}]
        errs = errors(d)
        self.assertHas(errs, "нет definition")
        self.assertHas(errs, "source должно быть")
        self.assertHas(errs, "нужен уникальный id")
        self.assertHas(errs, "неизвестные поля")
        d["metric"] = "oops"
        self.assertHas(errors(d), "metric должно быть списком")

    def test_motivation_needs_metric(self):
        d = exp_scenario()
        d["group"] = "motivation"
        self.assertHas(errors(d, group="motivation"), "нужна хотя бы одна метрика")


class CatalogTest(unittest.TestCase):
    PLAN = ("# План\n\n### Обязательные сценарии\n\n| Проверка | Ожидаемый результат |\n|---|---|\n"
            "| Демо | Ничего плохого |\n\n### Дальше\n")

    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.root = Path(self.td.name)
        (self.root / "docs" / "spec").mkdir(parents=True)
        (self.root / "docs" / "PLAN.md").write_text(self.PLAN, encoding="utf-8")
        rows = "\n".join(f"| `{t}` | ядро | да | `k:{i}` |" for i, t in enumerate(sorted(TYPES)))
        (self.root / "docs" / "spec" / "events.md").write_text(
            f"# События\n\n<!-- каталог:начало -->\n| Тип | Кто | Состояние | Ключ |\n|---|---|---|---|\n{rows}\n<!-- каталог:конец -->\n",
            encoding="utf-8")
        self.groups = mock.patch.dict(sc.GROUPS, {"required": 1, "experiments": 1, "motivation": 0}, clear=True)
        self.groups.start()
        self.write_reqs(REQS)
        self.put("required", good())
        self.put("experiments", self.exp())

    def tearDown(self):
        self.groups.stop()
        self.td.cleanup()

    def exp(self):
        d = exp_scenario()
        d["id"] = "demo_exp"
        d["requirements"] = ["R-002"]
        return d

    def write_reqs(self, reqs):
        lines = []
        for r in reqs:
            lines += ["[[req]]"] + [f"{k} = {dump_value(v)}" for k, v in r.items()] + [""]
        (self.root / "requirements.toml").write_text("\n".join(lines), encoding="utf-8")

    def put(self, group, d, name=None):
        p = self.root / "scenarios" / group
        p.mkdir(parents=True, exist_ok=True)
        (p / f"{name or d['id']}.toml").write_text(dump_toml(d), encoding="utf-8")

    def reqs(self):
        return tomllib.loads((self.root / "requirements.toml").read_text(encoding="utf-8"))["req"]

    def run_tool(self, *args):
        env = dict(os.environ, IAM_ROOT=str(self.root))
        p = subprocess.run([sys.executable, str(TOOL), *args], capture_output=True, text=True, env=env)
        return p.returncode, p.stdout + p.stderr

    def assertHas(self, errs, needle):
        self.assertTrue(any(needle in e for e in errs), f"нет «{needle}» в {errs}")

    def test_valid_catalog(self):
        # R-002 имеет check без ссылок, поэтому сценарий demo_exp добавим только после правки check
        self.write_reqs([REQS[0], {**REQS[1], "check": "Просто текст. scenarios/experiments/demo_exp.toml"}])
        self.assertEqual(sc.validate_catalog(self.root, self.reqs()), [])

    def test_missing_link_in_check(self):
        errs = sc.validate_catalog(self.root, self.reqs())
        self.assertHas(errs, "R-002: сценарий scenarios/experiments/demo_exp.toml перечисляет требование, но его check не ссылается")

    def test_check_points_to_missing_scenario(self):
        self.write_reqs([REQS[0], {**REQS[1], "check": "см. scenarios/required/ghost.toml"}])
        errs = sc.validate_catalog(self.root, self.reqs())
        self.assertHas(errs, "несуществующий сценарий scenarios/required/ghost.toml")

    def test_check_points_to_scenario_not_listing_it(self):
        self.write_reqs([REQS[0], {**REQS[1], "check": "см. scenarios/required/demo.toml"}])
        errs = sc.validate_catalog(self.root, self.reqs())
        self.assertHas(errs, "R-002: check ссылается на scenarios/required/demo.toml, но сценарий не перечисляет")

    def test_functional_without_scenario_or_test(self):
        self.write_reqs(REQS + [{"id": "R-003", "kind": "functional", "check": "Посмотреть глазами."}])
        errs = sc.validate_catalog(self.root, self.reqs())
        self.assertHas(errs, "R-003: функциональное требование без сценария")

    def test_group_counts_and_unknown_group(self):
        (self.root / "scenarios" / "required" / "demo.toml").unlink()
        errs = sc.validate_catalog(self.root, self.reqs())
        self.assertHas(errs, "группа required: сценариев 0, нужно 1")
        self.assertHas(errs, "присутствует в 0 сценариях")
        (self.root / "scenarios" / "weird").mkdir()
        self.assertHas(sc.validate_catalog(self.root, self.reqs()), "неизвестная группа")

    def test_broken_toml_and_missing_dir(self):
        (self.root / "scenarios" / "required" / "bad.toml").write_text("x = [", encoding="utf-8")
        self.assertHas(sc.validate_catalog(self.root, self.reqs()), "не читается")
        import shutil
        shutil.rmtree(self.root / "scenarios")
        self.assertHas(sc.validate_catalog(self.root, self.reqs()), "нет каталога scenarios/")

    def test_duplicate_id_and_file_name(self):
        d = good()
        self.put("required", d, name="copy")
        errs = sc.validate_catalog(self.root, self.reqs())
        self.assertHas(errs, "не совпадает с именем файла")
        self.assertHas(errs, "уже есть в")

    def test_plan_row_mismatches(self):
        d = good()
        d["plan_title"] = "Не то"
        d["plan_expected"] = "Тоже не то"
        self.put("required", d)
        errs = sc.validate_catalog(self.root, self.reqs())
        self.assertHas(errs, "plan_title не совпадает")
        self.assertHas(errs, "plan_expected не совпадает")

    def test_plan_row_twice_and_out_of_range(self):
        d = good()
        d["id"] = "demo2"
        self.put("required", d)
        self.assertHas(sc.validate_catalog(self.root, self.reqs()), "присутствует в 2 сценариях")
        (self.root / "scenarios" / "required" / "demo2.toml").unlink()
        d = good()
        d["plan_row"] = 5
        self.put("required", d)
        self.assertHas(sc.validate_catalog(self.root, self.reqs()), "вне диапазона 1–1")

    def test_plan_table_row_count(self):
        with mock.patch.dict(sc.GROUPS, {"required": 2}):
            errs = sc.validate_catalog(self.root, self.reqs())
        self.assertHas(errs, "в таблице 1 строк, ожидалось 2")

    def test_missing_plan_and_events(self):
        (self.root / "docs" / "PLAN.md").unlink()
        (self.root / "docs" / "spec" / "events.md").unlink()
        errs = sc.validate_catalog(self.root, self.reqs())
        self.assertHas(errs, "нет docs/PLAN.md")
        self.assertHas(errs, "нет docs/spec/events.md")

    def test_plan_without_heading_or_table(self):
        (self.root / "docs" / "PLAN.md").write_text("# План\n", encoding="utf-8")
        rows, errs = sc.plan_table(self.root)
        self.assertEqual(rows, [])
        self.assertHas(errs, "нет раздела")
        (self.root / "docs" / "PLAN.md").write_text("### Обязательные сценарии\n\nтекст\n### Другое\n", encoding="utf-8")
        self.assertHas(sc.plan_table(self.root)[1], "таблица «Обязательные сценарии» пуста")

    def test_events_without_markers_or_types(self):
        p = self.root / "docs" / "spec" / "events.md"
        p.write_text("# События\n", encoding="utf-8")
        self.assertHas(sc.event_catalog(self.root)[1], "нет маркеров")
        p.write_text("<!-- каталог:начало -->\nничего\n<!-- каталог:конец -->\n", encoding="utf-8")
        self.assertHas(sc.event_catalog(self.root)[1], "каталог типов событий пуст")

    # --- командная строка

    def test_cli_validate_ok_and_fail(self):
        code, out = self.run_tool("validate")
        self.assertEqual(code, 1, out)
        self.assertIn("НЕ ПРОХОДИТ", out)
        self.write_reqs([REQS[0], {**REQS[1], "check": "Просто текст. scenarios/experiments/demo_exp.toml"}])
        import contextlib
        import io
        buf = io.StringIO()
        with mock.patch.object(sc, "ROOT", self.root), contextlib.redirect_stdout(buf):
            code = sc.cmd_validate(None)
        self.assertEqual(code, 0, buf.getvalue())
        self.assertIn("OK: сценариев 2", buf.getvalue())

    def test_cli_list(self):
        code, out = self.run_tool("list", "--group", "required")
        self.assertEqual(code, 0)
        self.assertIn("required/demo: Демо [R-001]", out)
        self.assertNotIn("demo_exp", out)
        code, out = self.run_tool("list")
        self.assertIn("experiments/demo_exp", out)

    def test_cli_list_warns_on_unknown_group(self):
        (self.root / "scenarios" / "weird").mkdir()
        code, out = self.run_tool("list")
        self.assertIn("ВНИМАНИЕ", out)

    def test_cli_coverage(self):
        code, out = self.run_tool("coverage")
        self.assertEqual(code, 0, out)
        self.assertIn("закрыто сценарием каталога: 1", out)
        self.assertIn("R-001: demo", out)
        self.write_reqs(REQS + [{"id": "R-003", "text": "голое", "kind": "functional", "check": "Посмотреть."}])
        code, out = self.run_tool("coverage")
        self.assertEqual(code, 1)
        self.assertIn("R-003", out)

    def call(self, *args):
        """Тот же вызов командной строки, но в этом же процессе (для измерения охвата)."""
        import contextlib
        import io
        buf = io.StringIO()
        with mock.patch.object(sc, "ROOT", self.root), contextlib.redirect_stdout(buf):
            code = sc.main(list(args))
        return code, buf.getvalue()

    def test_inprocess_commands(self):
        code, out = self.call("validate")
        self.assertEqual(code, 1)
        self.assertIn("НЕ ПРОХОДИТ", out)
        code, out = self.call("list", "--group", "required")
        self.assertEqual(code, 0)
        self.assertIn("required/demo", out)
        (self.root / "scenarios" / "weird").mkdir()
        code, out = self.call("list")
        self.assertIn("ВНИМАНИЕ", out)
        self.assertIn("experiments/demo_exp", out)
        (self.root / "scenarios" / "weird").rmdir()
        code, out = self.call("coverage")
        self.assertEqual(code, 0, out)
        self.assertIn("закрыто сценарием каталога: 1", out)
        self.write_reqs(REQS + [{"id": "R-003", "text": "голое", "kind": "functional", "check": "Посмотреть."}])
        code, out = self.call("coverage")
        self.assertEqual(code, 1)
        self.assertIn("R-003", out)
        (self.root / "scenarios" / "weird").mkdir()
        code, out = self.call("coverage")
        self.assertIn("ВНИМАНИЕ", out)

    def test_inprocess_missing_requirements_file(self):
        (self.root / "requirements.toml").unlink()
        with self.assertRaises(SystemExit) as cm:
            self.call("validate")
        self.assertIn("нет requirements.toml", str(cm.exception))

    def test_cli_missing_requirements_file(self):
        (self.root / "requirements.toml").unlink()
        code, out = self.run_tool("validate")
        self.assertNotEqual(code, 0)
        self.assertIn("нет requirements.toml", out)


class RealCatalogTest(unittest.TestCase):
    """Проверки реального каталога репозитория."""

    @classmethod
    def setUpClass(cls):
        cls.reqs = tomllib.loads((REPO / "requirements.toml").read_text(encoding="utf-8"))["req"]
        cls.catalog, cls.load_errs = sc.load_catalog(REPO)

    def test_catalog_valid(self):
        self.assertEqual(self.load_errs, [])
        self.assertEqual(sc.validate_catalog(REPO, self.reqs), [])

    def test_counts(self):
        for group, n in sc.GROUPS.items():
            self.assertEqual(sum(1 for r in self.catalog if r.split("/")[1] == group), n, group)

    def test_every_plan_row_exactly_once(self):
        rows, errs = sc.plan_table(REPO)
        self.assertEqual(errs, [])
        self.assertEqual(len(rows), 19)
        for n, (title, expected) in enumerate(rows, 1):
            hits = [r for r, d in self.catalog.items() if d.get("group") == "required" and d.get("plan_row") == n]
            self.assertEqual(len(hits), 1, title)
            self.assertEqual(self.catalog[hits[0]]["plan_title"], title)
            self.assertEqual(self.catalog[hits[0]]["plan_expected"], expected)

    def test_required_file_names_from_registry(self):
        """Имена файлов, обещанные требованиями R-169…R-187, существуют."""
        for r in self.reqs:
            if 169 <= int(r["id"][2:]) <= 187:
                paths = [p for p in sc.SCEN_PATH_RE.findall(r["check"]) if p[0] == "required"]
                self.assertEqual(len(paths), 1, r["id"])
                self.assertIn(f"scenarios/{paths[0][0]}/{paths[0][1]}.toml", self.catalog)

    def test_every_functional_requirement_has_scenario_or_test(self):
        listed = {rid for d in self.catalog.values() for rid in d["requirements"]}
        for r in self.reqs:
            if r["kind"] == "functional":
                self.assertTrue(r["id"] in listed or sc.TEST_MARK.search(r["check"]), r["id"])

    def test_absence_assertions_are_not_vacuous(self):
        """Утверждение «строки нет» осмысленно, только если строка действительно подаётся на вход
        (в шаге, ответе модели или предыстории): иначе оно истинно при любой системе."""
        for rel, d in self.catalog.items():
            fed = " ".join([s.get("text", "") + " " + s.get("content", "") for s in d.get("step", [])]
                           + [b.get("text", "") for b in d.get("background", [])])
            for a in d["assert"]:
                if a["kind"] == "text" and a["presence"] == "absent":
                    self.assertIn(a["marker"], fed, f"{rel}: {a['id']}: маркер нигде не подаётся на вход")

    def test_every_assertion_explained_and_closed_vocabulary(self):
        for rel, d in self.catalog.items():
            for a in d["assert"]:
                self.assertIn(a["kind"], sc.ASSERT_KINDS, rel)
                self.assertTrue(a["explain"].strip(), rel)
                self.assertGreaterEqual(len(a["explain"]), 15, f"{rel}: {a['id']}: слишком короткий explain")

    def test_comparison_scenarios_have_two_runs_each(self):
        for rel, d in self.catalog.items():
            runs = d.get("run", [])
            self.assertNotEqual(len(runs), 1, rel)
            if len(runs) > 1:
                self.assertTrue(any(a["kind"] == "compare" for a in d["assert"]), rel)

    def test_motivation_covers_six_phenomena_with_metrics(self):
        mot = {r: d for r, d in self.catalog.items() if d["group"] == "motivation"}
        self.assertEqual(len(mot), 6)
        for rel, d in mot.items():
            self.assertTrue(d.get("metric"), rel)
            self.assertIn("R-132", d["requirements"], rel)

    def test_experiments_cover_plan_topics(self):
        names = {d["id"] for d in self.catalog.values() if d["group"] == "experiments"}
        self.assertEqual(names, {"exp_diff_experience", "exp_correction", "exp_repeat_criticism", "exp_unread",
                                 "exp_no_messages", "exp_new_interest", "exp_no_tool", "exp_refusal"})

    def test_cli_on_real_repo(self):
        for args in (["validate"], ["coverage"], ["list"]):
            p = subprocess.run([sys.executable, str(TOOL), *args], capture_output=True, text=True, cwd=REPO)
            self.assertEqual(p.returncode, 0, p.stdout + p.stderr)


if __name__ == "__main__":
    unittest.main()
