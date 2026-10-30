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
