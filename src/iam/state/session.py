"""Служебный учёт запусков исполнителя: единственная точка записи остаётся в iam.state.

Резерв фиксируется ДО внешнего процесса, включая неудачные и оборванные попытки. Он не
меняет версию состояния личности: иначе резерв делал бы ожидаемый ответ устаревшим.
Настройки и команды оператора отдельно записываются через StateController.intervene.
"""
import json


class SessionLedger:
    def __init__(self, controller):
        self.ctl, self.store = controller, controller.store

    def read(self, session_id):
        row = self.store.con.execute("SELECT value FROM meta WHERE key = ?", ("session:" + session_id,)).fetchone()
        return json.loads(row[0]) if row else {"attempts": []}

    def seed_from_journal(self):
        """Совместимость с S08: ответы дают известный расход; оборванный запрос
        консервативно занимает две попытки (основную и исправление формата).
        Инициализация только при открытии сеанса, до появления новых запросов.
        """
        sid = self.ctl.session_id
        if sid is None:
            return
        with self.store.transaction():
            key = "session:" + sid
            if self.store.con.execute("SELECT 1 FROM meta WHERE key = ?", (key,)).fetchone():
                return
            events = self.ctl.events_after(0)
            requests = {e.payload["request"]["request_id"]: e for e in events if e.type == "proposal.requested"
                        and e.payload["request"]["session_id"] == sid}
            counts = {e.payload["response"]["request_id"]: e.payload["response"]["usage"]["calls"]
                      for e in events if e.type == "proposal.received"}
            attempts = [{"request_id": rid, "at": ev.occurred_at, "life_ms": ev.life_time_ms, "legacy": True}
                        for rid, ev in requests.items() for _ in range(max(1, counts.get(rid, 2)))]
            self.store.con.execute("INSERT INTO meta (key, value) VALUES (?, ?)", (key, json.dumps({"attempts": attempts})))

    def reserve(self, session_id, max_calls, request_id):
        with self.store.transaction():
            data = self.read(session_id)
            if len(data["attempts"]) >= max_calls:
                return False
            data["attempts"].append({"request_id": request_id, "at": self.ctl.clock.calendar_now(),
                                     "life_ms": self.ctl.life_time_ms})
            key, value = "session:" + session_id, json.dumps(data)
            if self.store.con.execute("SELECT 1 FROM meta WHERE key = ?", (key,)).fetchone():
                self.store.con.execute("UPDATE meta SET value = ? WHERE key = ?", (value, key))
            else:
                self.store.con.execute("INSERT INTO meta (key, value) VALUES (?, ?)", (key, value))
            return True


def panel_records(controller):
    """Ограниченные запросы чтения; прямое соединение остаётся внутри iam.state."""
    con = controller.store.con
    return {
        "history": [dict(r) for r in con.execute(
            "SELECT epoch, state_version, life_time_ms, created_at FROM state_snapshots ORDER BY epoch DESC, state_version DESC LIMIT 50")],
        "memories": [json.loads(r[0]) for r in con.execute(
            "SELECT record FROM memory_records WHERE layer IN ('personal','interpretation') ORDER BY rid DESC LIMIT 50")],
        "outbox": [dict(r) for r in con.execute(
            "SELECT action_id, status, external_id FROM outbox ORDER BY requested_seq DESC LIMIT 50")],
    }
