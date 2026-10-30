"""Запись транспортных данных Telegram через ядро, без изменения состояния личности.

S09 расширяет единственную точку записи: приёмник/отправитель не получают доступ к SQL.
Объект использует контроллер и его единственного Store-писателя. Он не обходит apply для
изменения состояния: здесь только inbox/outbox и служебные метки протокола доставки.
"""
import json

from iam.state.types import EventSpec
from iam.storage import InboxItem


class TelegramMailbox:
    def __init__(self, controller):
        self.ctl, self.store = controller, controller.store

    def atomic(self):
        return self.store.transaction()

    def get(self, key, default=None):
        row = self.store.con.execute("SELECT value FROM meta WHERE key = ?", ("telegram:" + key,)).fetchone()
        return default if row is None else json.loads(row[0])

    def put(self, key, value):
        self.store._writable()
        key, value = "telegram:" + key, json.dumps(value, ensure_ascii=False, sort_keys=True)
        if self.store.con.execute("SELECT 1 FROM meta WHERE key = ?", (key,)).fetchone():
            self.store.con.execute("UPDATE meta SET value = ? WHERE key = ?", (value, key))
        else:
            self.store.con.execute("INSERT INTO meta (key, value) VALUES (?, ?)", (key, value))

    def bind(self, bot_id):
        with self.atomic():
            old = self.get("bot_id")
            if old is not None and old != bot_id:
                raise ValueError("эта база уже связана с другим Telegram-ботом")
            self.put("bot_id", bot_id)

    def incoming(self, uid, *, input_id=None, sender=None, text=None):
        if input_id is None:
            result = self.ctl.record_event(EventSpec("system.rejected_sender", f"rjs:telegram:{uid}",
                {"update_id": uid, "reason": "unsupported_or_unauthorized"}), actor_ref="telegram_receiver")
        else:
            result = self.ctl.record_event(EventSpec("input.arrived", f"rx:telegram:{uid}", {
                "input_id": input_id, "channel": "telegram", "sender": str(sender), "text": text}),
                actor_ref="telegram_receiver")
        if result.event is None:
            raise RuntimeError("Telegram: запись входящего отклонена; offset сохранён не будет")
        if input_id is not None:
            self.store.add_inbox(InboxItem(input_id, "telegram", str(uid), result.event.seq,
                                          result.event.occurred_at, str(sender), {"text": text}))
        return int(input_id is not None and result.status == "recorded")

    def notices(self):
        read = {e.payload.get("input_id") for e in self.store.events() if e.type == "input.read"}
        rows = self.store.con.execute(
            "SELECT input_id, sender, received_at, channel FROM inbox WHERE channel = 'telegram' ORDER BY arrived_seq")
        return [dict(r) for r in rows if r["input_id"] not in read]

    def exists(self, action_id):
        return self.store.con.execute("SELECT 1 FROM outbox WHERE action_id = ?", (action_id,)).fetchone() is not None

    def reserve(self, action_id, recipient, text, requested_seq):
        self.store.add_outbox(action_id, "telegram", str(recipient), {"text": text}, requested_seq)

    def complete(self, action_id, status, external_id=None):
        self.store.set_outbox_status(action_id, status, None, external_id)

    def recover(self):
        with self.atomic():
            for row in self.store.con.execute("SELECT action_id FROM outbox WHERE channel = 'telegram' AND status = 'requested'"):
                self.complete(row[0], "unknown")
