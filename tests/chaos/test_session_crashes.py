"""100 настоящих SIGKILL на границах сеанса, резерва и незавершённой транзакции."""
import os
import select
import shutil
import signal
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from iam.domain import new_id
from iam.session import SessionConflict, SessionRunner
from iam.state import StateController, verify
from iam.storage import Store


class SessionCrashTest(unittest.TestCase):
    def test_100_kills_keep_replay_snapshots_and_reserved_calls(self):
        with tempfile.TemporaryDirectory(prefix="iam-chaos-") as temp:
            path=Path(temp)/"p.db"
            with Store.create(path,new_id("exp")) as store:
                StateController(store).create_persona()
            for i in range(100):
                phase=("started","mid","reserved")[i%3]
                proc=subprocess.Popen([sys.executable,str(Path(__file__).with_name("child_session.py")),str(path),phase],
                    stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
                try:
                    ready,_,_=select.select([proc.stdout],[],[],15)
                    self.assertTrue(ready,"Дочерний процесс не вышел на фазу аварии")
                    line=proc.stdout.readline().strip()
                    self.assertEqual(line,"READY")
                    proc.send_signal(signal.SIGKILL)
                    proc.wait(timeout=5)
                    self.assertEqual(proc.returncode,-signal.SIGKILL)
                finally:
                    if proc.poll() is None:proc.kill();proc.wait()
                    proc.stdin.close();proc.stdout.close();proc.stderr.close()
                with Store.open(path) as store:
                    ctl=StateController(store)
                    self.assertEqual(ctl.session_state,"paused")
                    self.assertEqual(ctl.state["world_time"]["pause_reason"],"crash")
                    self.assertEqual(store.integrity_problems(),[],f"перезапуск {i}")
                    self.assertEqual(verify(store),[],f"перезапуск {i}")
                    self.assertNotIn("chaos",ctl.state["beliefs"])
                    run=SessionRunner(ctl)
                    self.assertEqual(run.usage()["calls"],int(phase=="reserved"))
                    if phase=="reserved":
                        with self.assertRaises(SessionConflict):run.command("resume","лимит после аварии")
