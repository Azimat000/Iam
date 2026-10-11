"""Экспорт без секретов (R-202, R-204, R-219): «канареечные» секреты в журнале, памяти, входящих и исходящих
не попадают в пакет; готовый пакет перепроверяется; найденное — отказ, а не молчаливая утечка."""
import json
import unittest
import zipfile

from iam.backup import (BackupCorrupt, NotEnoughSpace, REDACTED, SecretFound, SecretScanner, SpaceGuard,
                        export_persona, verify_export)
from iam.backup import export as export_mod
from iam.domain import Op
from iam.state import EventSpec
from iam.storage import InboxItem

from .helpers import BackupCase, PID, T0, memory, set_

CANARY = "КАНАРЕЙКА-" + "q9x7" + "-Z4m2-секрет"           # заведомо фальшивый секрет
CANARY_QUOTED = 'кав"ычка\\слэш-' + "7f3a9c01"
TG = "987654321" + ":" + "AAE" + "k" * 32                  # похож на токен Telegram


def all_text(path) -> str:
    """Сырые байты zip и распакованные файлы: ищем и там, и там."""
    with zipfile.ZipFile(path) as z:
        parts = [z.read(n).decode("utf-8") for n in z.namelist()]
    return path.read_bytes().decode("latin-1") + "\n" + "\n".join(parts)


class ExportTest(BackupCase):
    def seed(self):
        """Личность, в данных которой встречаются секреты на всех путях записи."""
        c = self.persona()
        c.record_event(EventSpec("input.arrived", "rx:tg:77", {"text": f"мой токен {CANARY} и ещё {TG}"}))
        c.apply(self.prop(c, Op("memory", "mem_BBBBBBBB", "add", memory(f"Она запомнила: {CANARY_QUOTED}", mid="mem_BBBBBBBB"), True)))
        c.store.add_inbox(InboxItem("inp_AAAAAAAA", "telegram", "ext-1", 3, "2026-10-11T10:00:00.000Z", "operator",
                                    {"text": f"пароль: {CANARY}"}))
        c.store.add_outbox("act_AAAAAAAA", "telegram", "operator", {"text": f"ключ {CANARY} не показывать"}, 3)
        return c

    def scanner(self):
        return SecretScanner([CANARY, CANARY_QUOTED], use_env=False)

    def test_canary_secrets_do_not_reach_the_package(self):
        """Обязательная проверка среза: канареечный секрет не попадает в экспорт."""
        self.seed()
        out = self.tmp / "exp" / "person.zip"
        res = export_persona(self.db, out, scanner=self.scanner(), now=T0)
        text = all_text(out)
        for secret in (CANARY, CANARY_QUOTED, TG, json.dumps(CANARY_QUOTED)[1:-1]):
            self.assertNotIn(secret, text)
        self.assertIn(REDACTED, text)
        self.assertGreaterEqual(res.redactions["known_secret"], 4)
        self.assertEqual(res.redactions["telegram_bot_token"], 1)
        self.assertEqual(verify_export(out, scanner=self.scanner()), [])
        self.assertEqual(res.sha256, __import__("hashlib").sha256(out.read_bytes()).hexdigest())

    def test_environment_secrets_are_removed_automatically(self):
        self.seed()
        out = self.tmp / "e.zip"
        sc = SecretScanner(use_env=True, environ={"IAM_TELEGRAM_TOKEN": CANARY})
        export_persona(self.db, out, scanner=sc)
        self.assertNotIn(CANARY, all_text(out))

    def test_package_content_and_manifest(self):
        c = self.seed()
        out = self.tmp / "p.zip"
        res = export_persona(self.db, out, scanner=self.scanner(), now=T0)
        with zipfile.ZipFile(out) as z:
            names = set(z.namelist())
            man = json.loads(z.read("manifest.json"))
            events = [json.loads(x) for x in z.read("events.jsonl").decode().splitlines()]
            memories = [json.loads(x) for x in z.read("memory_records.jsonl").decode().splitlines()]
        self.assertEqual(names, {"manifest.json", "meta.jsonl", "schema_version.jsonl", "events.jsonl",
                                 "state_snapshots.jsonl", "memory_records.jsonl", "inbox.jsonl", "outbox.jsonl",
                                 "clock.jsonl"})
        self.assertEqual((man["kind"], man["format"], man["persona_id"], man["last_seq"], man["exported_at"]),
                         ("iam-export", 1, PID, c.store.last_seq(), "2026-10-11T12:00:00Z"))
        self.assertEqual(man["counts"]["events"], len(events))
        self.assertEqual([e["seq"] for e in events], list(range(1, len(events) + 1)))
        self.assertEqual(res.counts, man["counts"])
        self.assertEqual({m["memory_id"] for m in memories}, {"mem_AAAAAAAA", "mem_BBBBBBBB"})
        self.assertIsInstance(events[0]["payload"], dict, "вложенный JSON — структурой, не строкой")
        self.assertTrue(man["secrets_checked"])

    def test_export_is_a_consistent_snapshot_taken_without_stopping_the_writer(self):
        c = self.seed()
        out = self.tmp / "p.zip"
        export_persona(self.db, out, scanner=self.scanner())
        self.live_more(c)       # писатель жив и пишет дальше
        with zipfile.ZipFile(out) as z:
            n = json.loads(z.read("manifest.json"))["last_seq"]
        self.assertLess(n, c.store.last_seq())

    def test_leak_is_refused_and_no_file_is_created(self):
        """Страховка: даже если редактирование что-то пропустило, готовый пакет не создаётся."""
        self.seed()
        out = self.tmp / "leak.zip"

        class Blind(SecretScanner):
            def redact_value(self, value, counts=None):      # «редактор» сломан: пропускает всё
                return value

        with self.assertRaises(SecretFound) as cm:
            export_persona(self.db, out, scanner=Blind([CANARY], use_env=False))
        self.assertFalse(out.exists())
        self.assertEqual(list(self.tmp.glob(".*partial")), [])
        self.assertNotIn(CANARY, str(cm.exception))
        self.assertTrue(cm.exception.findings)

    def test_existing_output_is_never_overwritten(self):
        self.seed()
        out = self.tmp / "p.zip"
        out.write_text("чужое", encoding="utf-8")
        with self.assertRaises(FileExistsError):
            export_persona(self.db, out, scanner=self.scanner())
        self.assertEqual(out.read_text(encoding="utf-8"), "чужое")

    def test_no_space_and_unreadable_database(self):
        self.seed()
        with self.assertRaises(NotEnoughSpace):
            export_persona(self.db, self.tmp / "x.zip", space=SpaceGuard(self.tmp, usage=lambda p: (1, 1, 1)))
        self.assertFalse((self.tmp / "x.zip").exists())
        bad = self.tmp / "bad.db"
        bad.write_bytes(b"not a database " * 100)
        with self.assertRaises(BackupCorrupt):
            export_persona(bad, self.tmp / "y.zip")
        self.assertFalse((self.tmp / "y.zip").exists())

    def test_write_failure_leaves_no_partial_file(self):
        self.seed()
        real = export_mod._fsync_file
        def boom(path):
            raise OSError("сбой записи")
        export_mod._fsync_file = boom
        try:
            with self.assertRaises(OSError):
                export_persona(self.db, self.tmp / "z.zip", scanner=self.scanner())
        finally:
            export_mod._fsync_file = real
        self.assertEqual([p.name for p in self.tmp.glob("*z.zip*")], [])


class VerifyExportTest(BackupCase):
    def make(self):
        self.persona()
        out = self.tmp / "p.zip"
        export_persona(self.db, out, scanner=SecretScanner(use_env=False))
        return out

    def rewrite(self, src, **changes):
        dst = self.tmp / "tampered.zip"
        with zipfile.ZipFile(src) as zin, zipfile.ZipFile(dst, "w") as zout:
            for name in zin.namelist():
                data = changes.pop(name, zin.read(name))
                if data is not None:
                    zout.writestr(name, data)
        return dst

    def test_good_package_passes(self):
        self.assertEqual(verify_export(self.make(), scanner=SecretScanner(use_env=False)), [])

    def test_bad_inputs(self):
        self.assertIn("не открывается", verify_export(self.tmp / "нет.zip")[0])
        junk = self.tmp / "junk.zip"
        junk.write_bytes(b"PK not really")
        self.assertIn("не открывается", verify_export(junk)[0])
        out = self.make()
        self.assertIn("нет manifest.json", verify_export(self.rewrite(out, **{"manifest.json": None}))[0])
        self.assertIn("не пакет экспорта", verify_export(self.rewrite(out, **{"manifest.json": b'{"kind":"x"}'}))[0])

    def test_corrupted_zip_member_is_found(self):
        out = self.make()
        raw = bytearray(out.read_bytes())
        with zipfile.ZipFile(out) as z:
            info = z.getinfo("events.jsonl")
        raw[info.header_offset + 30 + len("events.jsonl") + 5] ^= 0xFF
        out.write_bytes(raw)
        self.assertIn("повреждён", verify_export(out)[0])

    def test_edits_are_found_by_manifest(self):
        out = self.make()
        with zipfile.ZipFile(out) as z:
            events = z.read("events.jsonl")
        probs = verify_export(self.rewrite(out, **{"events.jsonl": events.replace(b"persona.created", b"persona.xxxxxxx")}))
        self.assertTrue(any("events.jsonl: не совпадает" in p for p in probs), probs)
        probs = verify_export(self.rewrite(out, **{"inbox.jsonl": None}))
        self.assertTrue(any("нет файла inbox.jsonl" in p for p in probs), probs)
        for bad in (b"{not json", b'{"kind":"iam-export","format":1}', b"[]"):
            self.assertEqual(verify_export(self.rewrite(out, **{"manifest.json": bad}))[0].split(" ")[0] in ("manifest.json", "это"), True)
        probs = verify_export(self.rewrite(out, **{"events.jsonl": None}))
        self.assertTrue(any("нет файла events.jsonl" in p for p in probs), probs)

    def test_row_count_gaps_and_planted_secret_are_found(self):
        out = self.make()
        with zipfile.ZipFile(out) as z:
            man = json.loads(z.read("manifest.json"))
            lines = z.read("events.jsonl").decode().splitlines()
        # пропуск в журнале: убрали строку 2 и честно пересчитали манифест, кроме seq
        import hashlib
        cut = ("\n".join([lines[0]] + lines[2:]) + "\n").encode()
        man["files"]["events.jsonl"] = {"sha256": hashlib.sha256(cut).hexdigest(), "bytes": len(cut)}
        probs = verify_export(self.rewrite(out, **{"events.jsonl": cut, "manifest.json": json.dumps(man).encode()}))
        self.assertTrue(any("events: строк не" in p for p in probs), probs)
        self.assertTrue(any("пропуски" in p for p in probs), probs)
        tg = "123456789" + ":" + "AAH" + "x" * 32
        probs = verify_export(self.rewrite(out, **{"inbox.jsonl": f'{{"text":"{tg}"}}\n'.encode()}),
                              scanner=SecretScanner(use_env=False))
        self.assertTrue(any(p.startswith("секрет:") for p in probs), probs)
