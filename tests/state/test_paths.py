"""Операции перехода над состоянием: set / add / remove / transition и конечные автоматы."""
import unittest

from iam.domain import Op
from iam.state import MACHINES, OpError, apply_op, apply_ops, empty_state
from iam.state.paths import check_json, overlaps, split_path, touched


def op(group, path, kind, *value):
    return Op(group, path, kind, value[0] if value else None, bool(value))


class ApplyOpsTest(unittest.TestCase):
    def run_ops(self, state, *ops):
        return apply_ops(state, ops, MACHINES)

    def test_set_creates_parents_and_does_not_touch_original(self):
        s = empty_state()
        new, ch = self.run_ops(s, op("beliefs", "tea.confidence", "set", 0.4))
        self.assertEqual(new["beliefs"], {"tea": {"confidence": 0.4}})
        self.assertEqual(s["beliefs"], {}, "исходное состояние не меняется")
        self.assertEqual(ch, [{"group": "beliefs", "path": "tea.confidence", "op": "set", "before": None, "after": 0.4}])

    def test_set_overwrites_and_copies_value(self):
        value = {"a": [1]}
        new, ch = self.run_ops(empty_state(), op("traits", "x", "set", 1), op("traits", "x", "set", value))
        value["a"].append(2)
        self.assertEqual(new["traits"]["x"], {"a": [1]}, "значение скопировано, а не разделено")
        self.assertEqual(ch[1]["before"], 1)

    def test_add_to_list(self):
        new, ch = self.run_ops(empty_state(), op("affect", "reactions", "add", "радость"),
                               op("affect", "reactions", "add", "тревога"))
        self.assertEqual(new["affect"]["reactions"], ["радость", "тревога"])
        self.assertEqual(ch[1]["before"], ["радость"])
        with self.assertRaises(OpError) as e:
            self.run_ops(new, op("affect", "reactions.x", "add", 1))
        self.assertEqual(e.exception.code, "path_blocked")
        new["affect"]["mood"] = {"valence": 0}
        with self.assertRaises(OpError) as e:
            self.run_ops(new, op("affect", "mood", "add", 1))
        self.assertEqual(e.exception.code, "not_a_list")

    def test_remove_key_and_list_value(self):
        s, _ = self.run_ops(empty_state(), op("beliefs", "a.b", "set", 1), op("beliefs", "xs", "add", 1),
                            op("beliefs", "xs", "add", 2), op("beliefs", "xs", "add", 1))
        new, ch = self.run_ops(s, op("beliefs", "a.b", "remove"), op("beliefs", "xs", "remove", 1))
        self.assertEqual(new["beliefs"], {"a": {}, "xs": [2, 1]}, "из списка убирается первое равное значение")
        self.assertEqual((ch[0]["before"], ch[0]["after"]), (1, None))
        for bad, code in ((op("beliefs", "nope", "remove"), "path_missing"),
                          (op("beliefs", "no.pe", "remove"), "path_missing"),
                          (op("beliefs", "xs", "remove", 99), "value_missing"),
                          (op("beliefs", "a", "remove", 1), "value_missing")):
            with self.assertRaises(OpError) as e:
                self.run_ops(s, bad)
            self.assertEqual(e.exception.code, code)
        with self.assertRaises(OpError):
            self.run_ops(s, op("beliefs", "xs", "remove", float("nan")))

    def test_transition_uses_state_machine(self):
        s, _ = self.run_ops(empty_state(), op("intentions", "int_X.status", "set", "active"))
        new, ch = self.run_ops(s, op("intentions", "int_X.status", "transition", "deferred"))
        self.assertEqual(new["intentions"]["int_X"]["status"], "deferred")
        self.assertEqual((ch[0]["before"], ch[0]["after"]), ("active", "deferred"))
        # active → waiting_opportunity → active допустимо, deferred → done — нет
        self.run_ops(s, op("intentions", "int_X.status", "transition", "waiting_opportunity"),
                     op("intentions", "int_X.status", "transition", "active"))
        for value in ("done", "abandoned", "nonsense", ["list"]):
            if value in ("done", "abandoned"):
                # из active они допустимы — проверяем недопустимость из deferred
                with self.assertRaises(OpError) as e:
                    self.run_ops(new, op("intentions", "int_X.status", "transition", value))
            else:
                with self.assertRaises(OpError) as e:
                    self.run_ops(s, op("intentions", "int_X.status", "transition", value))
            self.assertEqual(e.exception.code, "bad_transition")
        done, _ = self.run_ops(s, op("intentions", "int_X.status", "transition", "done"))
        with self.assertRaises(OpError) as e:       # done — конечное состояние
            self.run_ops(done, op("intentions", "int_X.status", "transition", "active"))
        self.assertEqual(e.exception.code, "bad_transition")

    def test_transition_without_machine_or_target(self):
        s, _ = self.run_ops(empty_state(), op("beliefs", "mood", "set", "ok"))
        with self.assertRaises(OpError) as e:
            self.run_ops(s, op("beliefs", "mood", "transition", "bad"))
        self.assertEqual(e.exception.code, "no_machine")
        with self.assertRaises(OpError) as e:
            self.run_ops(s, op("beliefs", "absent", "transition", "x"))
        self.assertEqual(e.exception.code, "path_missing")

    def test_group_and_value_errors(self):
        for bad, code in ((op("memory", "x", "set", 1), "bad_group"), (op("nogroup", "x", "set", 1), "bad_group"),
                          (op("traits", "", "set", 1), "bad_path"), (op("traits", "a..b", "set", 1), "bad_path"),
                          (op("traits", "x", "set"), "bad_op"), (op("traits", "x", "add"), "bad_op"),
                          (op("traits", "x", "transition"), "bad_op"),
                          (op("traits", "x", "set", float("inf")), "bad_value"),
                          (op("traits", "x", "set", {1: 2}), "bad_value"),
                          (op("traits", "x", "set", (1, 2)), "bad_value"),
                          (op("traits", "x", "bogus", 1), "bad_op")):
            with self.assertRaises(OpError, msg=str(bad)) as e:
                apply_ops(empty_state(), [bad], MACHINES)
            self.assertEqual(e.exception.code, code, str(bad))

    def test_failure_leaves_original_untouched(self):
        s = empty_state()
        with self.assertRaises(OpError):
            apply_ops(s, [op("traits", "a", "set", 1), op("traits", "b", "remove")], MACHINES)
        self.assertEqual(s["traits"], {})

    def test_json_values(self):
        check_json({"a": [1, 2.5, "s", None, True, {"b": []}]})
        for bad in (object(), {"a": {1}}, [float("nan")], {"a": float("-inf")}):
            with self.assertRaises(OpError):
                check_json(bad)

    def test_path_helpers(self):
        self.assertEqual(split_path("a.b"), ["a", "b"])
        self.assertEqual(touched(op("beliefs", "a.b", "set", 1)), ("beliefs", "a", "b"))
        self.assertTrue(overlaps(("b", "a"), ("b", "a", "x")))
        self.assertTrue(overlaps(("b", "a", "x"), ("b", "a")))
        self.assertFalse(overlaps(("b", "a"), ("b", "c")))
        self.assertFalse(overlaps(("b", "a"), ("t", "a")))

    def test_apply_op_single_returns_change(self):
        s = empty_state()
        ch = apply_op(s, op("traits", "k", "set", 1))
        self.assertEqual(s["traits"], {"k": 1})
        self.assertEqual(ch["after"], 1)


if __name__ == "__main__":
    unittest.main()
