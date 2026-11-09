from dataclasses import replace
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
from tempfile import TemporaryDirectory
import tomllib
import unittest
from unittest.mock import patch

from iam.backup import check_backup, verify_database
from iam.clock import LifeClock, life_moment
from iam.domain import MemoryRecord, Op, Source, new_id
from iam.persona.context import character_context
from iam.persona.create import create
from iam.state import StateController
from iam.storage import AlreadyLocked, PersonaMismatch, StorageError, Store, WriterLock, default_path
from iam.stubs import Reply, ScriptedProvider, StubCapabilities, StubCreatorBridge, rest
from iam.world import CAPABILITIES, PersistentWorld
from iam.world.cycle import WorldMemory
from iam.world.options import OptionsCycle
from state.helpers import FakeTime

REPO = Path(__file__).resolve().parents[2]
CANDIDATES = REPO / "scenarios" / "personas" / "final"
DECISION = "D-999"


def load(path):
    with path.open("rb") as handle:
        return tomllib.load(handle)


class CreateTest(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.parent = Path(self.directory.name)
        self.root = self.parent / "permanent"
        self.profile = load(CANDIDATES / "curious.toml")

    def test_birth_backup_and_separate_restore(self):
        created = create(self.root, self.profile, decision=DECISION)
        self.assertTrue(created.persona_id.startswith("per_"))
        self.assertEqual(created.database, default_path(self.root.resolve(), created.persona_id))
        self.assertNotEqual(created.database.stat().st_ino, created.restored.stat().st_ino)
        backup = check_backup(created.backup)
        self.assertTrue(backup.ok, backup.problems)
        self.assertTrue(verify_database(created.restored).ok)

        with Store.open(created.database, readonly=True) as store:
            ctl = StateController(store)
            initial = ctl.state
            events = list(store.events())
            self.assertEqual(store.store_kind, "permanent")
            self.assertEqual([event.type for event in events], ["persona.created", "system.backup_created"])
            self.assertEqual(events[0].event_id, created.birth_event_id)
            self.assertEqual(events[0].life_time_ms, 0)
            self.assertEqual(ctl.session_state, "stopped")
            self.assertEqual(ctl.life_time_ms, 0)
            self.assertEqual(ctl.state_version, 1)
            self.assertEqual(ctl.epoch, 1)
            self.assertEqual(initial["traits"]["profile"], {
                "format": 1, "label": self.profile["label"], "decision": DECISION, "origin": "operator_provided",
                "inclinations": self.profile["inclinations"],
            })
            self.assertEqual({row["name"]: row["value"] for row in initial["traits"]["traits"]}, self.profile["traits"])
            self.assertEqual({row["source"] for row in initial["traits"]["traits"]}, {created.birth_event_id})
            self.assertEqual(initial["traits"]["values"], self.profile["values"])
            self.assertEqual(initial["traits"]["boundaries"], self.profile["boundaries"])
            self.assertEqual(initial["preferences"]["likes"], self.profile["inclinations"])
            world = initial["world_time"]["world"]
            self.assertEqual(world["space"]["objects"]["notebook"]["place_id"], "desk")
            self.assertEqual(len(world["library"]), 3)
            self.assertTrue(all(not row["read"] and row["origin"] == "operator_provided"
                                for row in world["library"].values()))
            self.assertEqual(world["projects"], {})
            for table in ("memory_records", "inbox", "outbox"):
                self.assertEqual(store.con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], 0)
            self.assertEqual(initial["intentions"], {})

        with Store.open(created.restored, readonly=True, expect_persona=created.persona_id) as store:
            ctl = StateController(store)
            self.assertEqual(ctl.state, initial)
            self.assertEqual(ctl.epoch, 2)
            self.assertEqual(ctl.life_time_ms, 0)
            events = list(store.events())
            self.assertEqual([event.type for event in events],
                             ["persona.created", "operator.intervention", "system.restored"])
            self.assertEqual(events[0].event_id, created.birth_event_id)
            self.assertEqual(events[-1].payload["snapshot_id"], created.snapshot_id)
            self.assertTrue(events[-1].payload["checked_on_copy"])

    def test_real_choice_cycle_sees_the_agreed_character(self):
        created = create(self.root, self.profile, decision=DECISION)
        time = FakeTime()
        with Store.open(created.database) as store:
            clock = LifeClock(0, monotonic=time.mono, calendar=time.calendar)
            ctl = StateController(store, clock=clock)
            time.advance(20_000)
            self.assertEqual(ctl.life_time_ms, 0)
            world = PersistentWorld(ctl)
            memory = WorldMemory(ctl)
            with patch.object(memory, "_blocked", return_value={self.profile["boundaries"][0]}):
                self.assertEqual(character_context(ctl, memory), [])
            remove_likes = Op("preferences", "likes", "remove")
            self.assertTrue(ctl.intervene("technical_fix", "Проверка изменения предпочтений", (remove_likes,)).applied)
            provider = ScriptedProvider([Reply((rest(),))])
            cycle = OptionsCycle(ctl, provider, memory, world, StubCapabilities(CAPABILITIES), StubCreatorBridge())
            self.assertTrue(ctl.start_session().applied)
            moment = life_moment(new_id("mom"), ctl.life_time_ms, "world")
            self.assertTrue(ctl.schedule_moment(moment).applied)
            trigger = ctl.fire_due_moments()[0].events[0]
            self.assertEqual(cycle.run(trigger).outcome, "done")
            self.assertNotIn("likes", ctl.state["preferences"])

            request, = provider.calls
            character, = [item for item in request["context"] if item["ref"] == created.birth_event_id]
            self.assertEqual(character["origin"], "model_knowledge")
            self.assertEqual(character["layer"], "state")
            self.assertEqual(request["purpose"], "choose_action")
            payload = json.loads(character["text"])
            self.assertEqual(payload["label"], self.profile["label"])
            self.assertEqual({row["name"]: row["value"] for row in payload["traits"]}, self.profile["traits"])
            self.assertEqual(payload["values"], self.profile["values"])
            self.assertEqual(payload["boundaries"], self.profile["boundaries"])
            self.assertEqual(payload["inclinations"], self.profile["inclinations"])
            self.assertNotIn(DECISION, character["text"])
            self.assertNotIn("В пустой комнате", json.dumps(request, ensure_ascii=False))
            self.assertEqual(ctl.state["intentions"], {})

    def test_existing_and_concurrent_creation_do_not_replace_database(self):
        with WriterLock(self.root.resolve().with_name(".permanent.creation")):
            with self.assertRaises(AlreadyLocked):
                create(self.root, self.profile, decision=DECISION)
        created = create(self.root, self.profile, decision=DECISION)
        before = created.database.read_bytes()
        with self.assertRaises(FileExistsError):
            create(self.root, load(CANDIDATES / "social.toml"), decision=DECISION)
        self.assertEqual(list(created.database.parent.glob("*.db")), [created.database])
        self.assertEqual(created.database.read_bytes(), before)

    def test_interrupted_restore_does_not_publish_and_allows_retry(self):
        with patch("iam.persona.create.restore_backup", side_effect=OSError("Проверочный обрыв")):
            with self.assertRaises(OSError):
                create(self.root, self.profile, decision=DECISION)
        self.assertFalse(self.root.exists())
        self.assertFalse(list(self.parent.glob(".permanent.creating-*")))
        self.assertTrue(create(self.root, self.profile, decision=DECISION).database.is_file())

    def test_sync_error_after_publish_keeps_valid_bundle(self):
        def sync(path):
            if path == self.root.resolve().parent:
                raise OSError("Сбой синхронизации")

        with patch("iam.persona.create._fsync_dir", side_effect=sync):
            with self.assertRaisesRegex(OSError, "Комплект опубликован"):
                create(self.root, self.profile, decision=DECISION)
        database, = (self.root / "personas").glob("*.db")
        self.assertTrue(verify_database(database).ok)
        with self.assertRaises(FileExistsError):
            create(self.root, self.profile, decision=DECISION)

    def test_rejected_birth_does_not_publish(self):
        with patch.object(StateController, "create_persona") as birth:
            birth.return_value.applied = False
            birth.return_value.reason = "bad_value"
            with self.assertRaisesRegex(StorageError, "Рождение отклонено"):
                create(self.root, self.profile, decision=DECISION)
        self.assertFalse(self.root.exists())

    def test_rejects_malformed_or_experimental_profiles(self):
        profiles = (
            load(REPO / "scenarios" / "personas" / "curious.toml"),
            {**self.profile, "format": True},
            {**self.profile, "label": " "},
            {**self.profile, "traits": {"curiosity": 0.5}},
            {**self.profile, "traits": {**self.profile["traits"], "curiosity": float("nan")}},
            {**self.profile, "boundaries": "Граница"},
            {**self.profile, "boundaries": []},
            {**self.profile, "values": [" "]},
            {**self.profile, "inclinations": ["exp_TestStory001"]},
            {**self.profile, "inclinations": ["Ссылка на exp/test.toml"]},
            {**self.profile, "inclinations": ["simulation_event"]},
            {**self.profile, "inclinations": ["token=example-secret-123"]},
            {**self.profile, "memory": []},
        )
        for profile in profiles:
            with self.subTest(profile=profile):
                with self.assertRaises(ValueError):
                    create(self.root, profile, decision=DECISION)
                self.assertFalse(self.root.exists())
        with self.assertRaisesRegex(ValueError, "номер решения"):
            create(self.root, self.profile, decision="нет")

    def test_experiment_location_and_foreign_memory_are_rejected(self):
        experimental_id = new_id("exp")
        experimental = default_path(self.parent, experimental_id)
        with Store.create(experimental, experimental_id) as store:
            StateController(store).create_persona()
        with self.assertRaisesRegex(ValueError, "вне экспериментов"):
            create(experimental.parent / "permanent", self.profile, decision=DECISION)
        alias = self.parent / "alias"
        alias.symlink_to(experimental.parent, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "вне экспериментов"):
            create(alias / "permanent", self.profile, decision=DECISION)
        before = experimental.read_bytes()
        created = create(self.root, self.profile, decision=DECISION)
        self.assertEqual(experimental.read_bytes(), before)
        record = MemoryRecord(memory_id=new_id("mem"), persona_id=experimental_id, layer="personal",
                              kind="episodic", origin="simulation_event", source=Source("core", "test"),
                              created_at="2026-11-09T00:00:00Z", created_life_ms=0,
                              sources=("evt_" + experimental_id.removeprefix("exp_"),),
                              content={"text": "Эксперимент"})
        with Store.open(created.database) as store:
            with self.assertRaises(PersonaMismatch):
                store.add_memory(record)
            with self.assertRaises(sqlite3.IntegrityError):
                store.add_memory(replace(record, persona_id=created.persona_id))
            self.assertEqual(store.con.execute("SELECT COUNT(*) FROM memory_records").fetchone()[0], 0)
        self.assertNotIn(experimental_id.encode(), created.database.read_bytes())

    def test_cli_creates_only_from_a_profile_and_reports_paths(self):
        cmd = [sys.executable, "-m", "iam.persona.create", "--root", str(self.root), "--decision", DECISION,
               "--profile"]
        env = {**os.environ, "PYTHONPATH": str(REPO / "src")}
        for path, code in ((CANDIDATES / "reflective.toml", 0), (CANDIDATES / "social.toml", 1)):
            run = subprocess.run([*cmd, str(path)], env=env, capture_output=True, text=True, check=False)
            self.assertEqual(run.returncode, code, run.stderr)
            if code == 0:
                report = json.loads(run.stdout)
                self.assertTrue(Path(report["database"]).is_file())
                self.assertTrue(Path(report["backup"]).is_file())
                self.assertTrue(Path(report["restored"]).is_file())
            else:
                self.assertIn("уже существует", run.stderr)
        run = subprocess.run([*cmd, str(CANDIDATES / "social.toml"), "--import", "experiment.db"],
                             env=env, capture_output=True, text=True, check=False)
        self.assertEqual(run.returncode, 2)
