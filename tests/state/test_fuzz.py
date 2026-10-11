"""Фазз-тест (проверка среза): случайные последовательности предложений — дубли, устаревшие, параллельные,
конфликтные ключи, ответы модели, паузы — и инварианты: версия монотонна и растёт ровно на применённый
переход, ничего не применяется дважды, состояние совпадает с воспроизведением журнала и со снимком."""
import random
import unittest

from iam.domain import Transition, new_id
from iam.state import EventSpec, Proposal, verify

from .helpers import PID, StateCase, set_
from .test_response import ResponseCase, op, proposal

GROUPS = ("traits", "beliefs", "affect", "preferences")


class FuzzTest(ResponseCase):
    steps = 300
    seeds = range(25)

    def setUp(self):
        StateCase.setUp(self)   # каждая серия создаёт собственную личность и свои часы

    def fresh(self, rnd, c, base=None):
        g, k, v = rnd.choice(GROUPS), f"k{rnd.randrange(6)}", rnd.randrange(1000)
        return self.prop(c, set_(g, k, v), base=base), (g, k, v)

    def run_series(self, seed):
        rnd = random.Random(seed)
        self.time = type(self.time)()
        self.db = self.tmp / f"fuzz{seed}.db"
        c = self.c = self.controller(running=True)
        expected: dict = {}
        applied: list[Proposal] = []
        old_bases: list[tuple[int, int]] = [(c.epoch, c.state_version)]
        answers: list[dict] = []
        outcomes = {"applied": 0, "duplicate": 0, "stale": 0, "conflict": 0, "rejected": 0}
        prev = c.state_version
        for step in range(self.steps):
            action = rnd.choice(["fresh", "fresh", "dup", "stale", "parallel", "conflict", "answer", "answer_dup",
                                 "answer_stale", "pause", "arrive", "time"])
            before = c.state
            if action == "fresh":
                p, (g, k, v) = self.fresh(rnd, c)
                res = c.apply(p)
                if c.session_state != "running":
                    self.assertEqual(res.reason, "session_forbids", step)
                if res.applied:
                    expected[(g, k)] = v
                    applied.append(p)
                    old_bases.append((c.epoch, c.state_version))
            elif action == "dup" and applied:
                res = c.apply(rnd.choice(applied))
                self.assertEqual(res.status, "duplicate", step)
            elif action == "stale":
                p, _ = self.fresh(rnd, c, base=rnd.choice(old_bases[:-1] or old_bases))
                res = c.apply(p)
                self.assertIn(res.status, ("stale", "applied", "rejected"))   # не stale — если база оказалась текущей
                if res.applied:
                    expected[(p.transition.ops[0].group, p.transition.ops[0].path)] = p.transition.ops[0].value
                    applied.append(p)
                    old_bases.append((c.epoch, c.state_version))
            elif action == "parallel" and c.session_state == "running":
                (a, (ga, ka, va)), (b, _) = self.fresh(rnd, c), self.fresh(rnd, c)
                ra = c.apply(a)
                before = c.state
                rb = c.apply(b)
                self.assertEqual((ra.status, rb.status), ("applied", "stale"), step)
                expected[(ga, ka)] = va
                applied.append(a)
                old_bases.append((c.epoch, c.state_version))
                res = rb
            elif action == "conflict" and applied:
                orig = rnd.choice(applied).transition
                evil = Proposal(Transition(orig.transition_id, PID, c.epoch, c.state_version,
                                           (set_("traits", "evil", step),), "подмена", orig.caused_by), "core")
                res = c.apply(evil)
                self.assertEqual(res.status, "conflict", step)
            elif action == "answer" and c.session_state == "running":
                g, k, v = rnd.choice(GROUPS), f"k{rnd.randrange(6)}", rnd.randrange(1000)
                req = self.ask()
                resp = self.answer(req, [proposal([op(g, k, "set", v)])])
                answers.append(resp)
                r = c.receive_response(resp)
                self.assertEqual(r.status, "applied", step)
                expected[(g, k)] = v
                old_bases.append((c.epoch, c.state_version))
                res = r.transition
            elif action == "answer_dup" and answers:
                r = c.receive_response(rnd.choice(answers))
                self.assertIn(r.status, ("duplicate",), step)
                res = None
            elif action == "answer_stale" and c.session_state == "running":
                req = self.ask()
                c.apply(self.prop(c, set_("traits", "bump", step)))
                expected[("traits", "bump")] = step
                old_bases.append((c.epoch, c.state_version))
                r = c.receive_response(self.answer(req, [proposal([op("traits", "late", "set", step)])]))
                self.assertEqual(r.status, "stale", step)
                res = None
            elif action == "pause":
                if c.session_state == "running":
                    self.assertTrue(c.pause_session().applied)
                else:
                    self.assertTrue(c.resume_session().applied)
                old_bases.append((c.epoch, c.state_version))
                res = None
            elif action == "arrive":
                key = f"rx:tg:{rnd.randrange(30)}"
                r = c.record_event(EventSpec("input.arrived", key, {"text": key}))
                self.assertIn(r.status, ("recorded", "duplicate"))
                res = None
            else:
                self.time.advance(rnd.randrange(1, 5000))
                res = None
            if res is not None:
                outcomes[res.status] += 1
            # инварианты после каждого шага
            self.assertGreaterEqual(c.state_version, prev, step)
            self.assertLessEqual(c.state_version - prev, 1 + (action == "answer_stale"), step)
            prev = c.state_version
            if res is not None and res.status != "applied":
                self.assertEqual(c.state, before, f"seed={seed} step={step} {action}: отказ изменил состояние")
        # итог серии
        carriers = [e for e in c.store.events() if e.changes_state and "transition" in e.payload]
        self.assertEqual(len(carriers), c.state_version, "ровно один носитель на версию")
        keys = [e.idempotency_key for e in c.store.events()]
        self.assertEqual(len(keys), len(set(keys)), "ключи идемпотентности уникальны")
        self.assertEqual(len({e.payload["transition"]["transition_id"] for e in carriers}), len(carriers),
                         "ни один переход не применён дважды")
        lives = [e.life_time_ms for e in c.store.events()]
        self.assertEqual(lives, sorted(lives), "время жизни не убывает вдоль seq")
        self.assertEqual(c.store.integrity_problems(), [])
        self.assertEqual(verify(c.store), [], f"seed={seed}: воспроизведение журнала расходится со снимками")
        for (g, k), v in expected.items():
            self.assertEqual(c.state[g][k], v, f"seed={seed}: {g}.{k}")
        self.assertNotIn("evil", c.state["traits"])
        self.assertNotIn("late", c.state["traits"])
        c.store.close()
        return outcomes

    def test_random_sequences_keep_invariants(self):
        total = {}
        for seed in self.seeds:
            for k, n in self.run_series(seed).items():
                total[k] = total.get(k, 0) + n
        # серия действительно исследовала все исходы, а не только «применено»
        for kind in ("applied", "duplicate", "stale", "conflict"):
            self.assertGreater(total[kind], 0, kind)


if __name__ == "__main__":
    unittest.main()
