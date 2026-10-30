"""Приём без модели: inbox + событие + offset фиксируются одной транзакцией."""
from __future__ import annotations

import datetime as dt

from iam.cycle.runner import input_id_for
from iam.state.telegram import TelegramMailbox

from . import mailbox
from .client import TelegramError


class Receiver:
    def __init__(self, controller, client, allowed_user_ids):
        self.ctl, self.store, self.client = controller, controller.store, client
        self.allowed = mailbox.allowed_ids(allowed_user_ids)
        self.box = TelegramMailbox(controller)
        self.box.bind(client.bot_id)

    @property
    def offset(self):
        return self.box.get("offset", 0)

    @property
    def last_poll_at(self):
        return self.box.get("last_poll_at")

    def poll_once(self):
        updates = self.client.call("getUpdates", {"offset": self.offset, "timeout": self.client.timeout,
                                                 "limit": 100, "allowed_updates": ["message"]})
        if not isinstance(updates, list) or any(
            not isinstance(u, dict) or type(u.get("update_id")) is not int or u["update_id"] < 0
            for u in updates
        ):
            raise TelegramError()
        count = 0
        for update in sorted(updates, key=lambda u: u["update_id"]):
            uid = update["update_id"]
            if uid < self.offset:
                continue
            with self.box.atomic():
                count += self._save(update)
                self.box.put("offset", uid + 1)
        with self.box.atomic():
            self.box.put("last_poll_at", dt.datetime.now(dt.UTC).isoformat())
        return count

    def _save(self, update):
        uid, message = update["update_id"], update.get("message")
        message = message if isinstance(message, dict) else {}
        sender, chat = message.get("from"), message.get("chat")
        sender = sender if isinstance(sender, dict) else {}
        chat = chat if isinstance(chat, dict) else {}
        sid, cid, text = sender.get("id"), chat.get("id"), message.get("text")
        accepted = (type(sid) is int and sid in self.allowed and type(cid) is int and cid == sid
                    and chat.get("type") == "private" and sender.get("is_bot") is False
                    and isinstance(text, str) and bool(text))
        return self.box.incoming(uid, **({"input_id": input_id_for("telegram", str(uid)),
                                           "sender": sid, "text": text} if accepted else {}))

    def notices(self, level="N1"):
        """Метаданные для S10/S20; текст не выдаётся до input.read."""
        if level not in ("N1", "N2"):
            raise ValueError("уровень уведомления: N1 или N2")
        pending = self.box.notices()
        return {"count": len(pending)} if level == "N1" else {"count": len(pending), "items": pending}

    def read_content(self, input_id):
        if not any(e.type == "input.read" and e.payload.get("input_id") == input_id
                   for e in self.store.events()):
            raise PermissionError("сообщение ещё не прочитано личностью")
        return self.store.get_inbox(input_id).content
