"""Аварии и замок (S04, R-198): kill -9 посреди транзакции не оставляет частичных записей;
второй управляющий процесс не получает доступ к личности."""
import collections
import json
import os
import random
import signal
import subprocess
import sys
import time
import unittest

from iam.storage import AlreadyLocked, Store, WriterLock

from .helpers import HERE, PID, TmpDirCase, child_env

CHILD = HERE / "child.py"
MID_KILLS = 52     # проверка среза требует ≥ 50 убийств посреди транзакции
RANDOM_KILLS = 20  # плюс убийства в случайный момент (в том числе во время фиксации)


def spawn(*args):
    return subprocess.Popen([sys.executable, str(CHILD), *map(str, args)], stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, env=child_env())


def wait_line(proc, expected: str) -> str:
    line = proc.stdout.readline().strip()
    if line.split(" ")[0] != expected:
        proc.kill()
        raise AssertionError(f"подпроцесс: ожидалось {expected!r}, получено {line!r}; {proc.stderr.read()}")
    return line


def kill9(proc):
    proc.send_signal(signal.SIGKILL)
    proc.wait(timeout=10)
    proc.stdout.close()
    proc.stderr.close()


class CrashTest(TmpDirCase):
    def check_consistent(self, store: Store) -> int:
        """Каждый переход либо записан целиком (6 событий + запись памяти + снимок), либо его нет."""
        self.assertEqual(store.integrity_problems(), [])
        by_trn = collections.Counter(e.payload["transition_id"] for e in store.events())
        self.assertTrue(all(n == 6 for n in by_trn.values()), f"частичный переход: {by_trn}")
        snaps = store.con.execute("SELECT state_version, state FROM state_snapshots ORDER BY state_version").fetchall()
        self.assertEqual([r[0] for r in snaps], list(range(1, len(snaps) + 1)), "версии без пропусков")
        self.assertEqual({json.loads(r[1])["transition_id"] for r in snaps}, set(by_trn))
        mems = store.con.execute("SELECT COUNT(*) FROM memory_records").fetchone()[0]
        self.assertEqual(mems, len(by_trn))
        versions = {r[0] for r in snaps}
        self.assertTrue(all(e.state_version in versions for e in store.events()), "у каждого события есть снимок")
        return len(by_trn)

    def test_kill9_mid_transaction_leaves_no_partial_writes(self):
        Store.create(self.db, PID).close()
        rnd = random.Random(20261011)
        plan = ["mid"] * MID_KILLS + ["random"] * RANDOM_KILLS
        rnd.shuffle(plan)
        mid_kills = dirty_wal = 0
        total = 0
        for mode in plan:
            if mode == "mid":
                # Ровно посреди транзакции: подпроцесс сообщает MID и ждёт.
                proc = spawn("crash", self.db, rnd.randint(1, 3))
                wait_line(proc, "STARTED")
                wal = int(wait_line(proc, "MID").split()[1])
                mid_kills += 1
                dirty_wal += wal > 0
            else:
                proc = spawn("crash", self.db, 0)
                wait_line(proc, "STARTED")
                time.sleep(rnd.uniform(0.0, 0.05))
            kill9(proc)
            with Store.open(self.db) as s:
                n = self.check_consistent(s)
            self.assertGreaterEqual(n, total, "зафиксированное не теряется")
            total = n
        self.assertEqual(mid_kills, MID_KILLS)
        self.assertEqual(dirty_wal, MID_KILLS, "в каждом случае MID незафиксированные страницы уже лежали в WAL")
        self.assertGreater(total, 0, "хотя бы часть переходов успела зафиксироваться")

    def test_exception_mid_transaction_rolls_back(self):
        from .helpers import ev
        s = self.new_store()
        s.append_event(ev(1))
        with self.assertRaises(RuntimeError):
            with s.transaction():
                s.append_event(ev(2))
                s.append_event(ev(3))
                raise RuntimeError("сбой посреди перехода")
        self.assertEqual(s.last_seq(), 1)
        self.assertIsNone(s.find_event(key="rx:tg:2"))
        self.assertEqual(s.append_event(ev(2))[0].seq, 2, "после отката seq продолжается без пропуска")


class LockTest(TmpDirCase):
    def test_second_process_is_refused_until_first_dies(self):
        Store.create(self.db, PID).close()
        holder = spawn("hold", self.db)
        try:
            wait_line(holder, "READY")
            with self.assertRaises(AlreadyLocked) as cm:
                Store.open(self.db)
            self.assertIn(str(holder.pid), str(cm.exception))
            # Чтение без замка разрешено (панель, отчёты).
            with Store.open(self.db, readonly=True) as ro:
                self.assertEqual(ro.persona_id, PID)
        finally:
            kill9(holder)
        # Замок снимается ядром ОС даже после kill -9 — «зависших» замков нет.
        with Store.open(self.db) as s:
            self.assertEqual(s.persona_id, PID)

    def test_same_process_second_writer_refused(self):
        s = self.new_store()
        with self.assertRaises(AlreadyLocked):
            Store.open(self.db)
        s.close()
        Store.open(self.db).close()

    def test_lock_object(self):
        lock = WriterLock(self.db)
        self.assertFalse(lock.held)
        with lock:
            self.assertTrue(lock.held)
            self.assertIs(lock.acquire(), lock)  # повторный захват тем же владельцем — без ошибки
            self.assertEqual(lock.path.read_text(), str(os.getpid()))
            with self.assertRaises(AlreadyLocked):
                WriterLock(self.db).acquire()
        self.assertFalse(lock.held)
        lock.release()  # повторное освобождение — без ошибки
        self.assertEqual(lock.path.read_text(), "")

    def test_symlink_to_same_base_shares_lock(self):
        s = self.new_store()
        link = self.tmp / "alias.db"
        link.symlink_to(self.db)
        with self.assertRaises(AlreadyLocked):
            Store.open(link)
        s.close()
        with Store.open(link) as s2:
            self.assertEqual(s2.persona_id, PID)

    def test_create_while_locked_is_refused(self):
        with WriterLock(self.db):
            with self.assertRaises(AlreadyLocked):
                Store.create(self.db, PID)
        self.assertFalse(self.db.exists())


if __name__ == "__main__":
    unittest.main()
