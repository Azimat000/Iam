"""Отправка с постоянным учётом попытки ДО сети. Неопределённое автоматически не повторяем."""
from __future__ import annotations

import hashlib

from iam.interfaces import ActionOutcome, Delivery
from iam.state import EventSpec
from iam.state.telegram import TelegramMailbox

from . import mailbox
from .client import TelegramError

STATUSES = {"requested": "pending", "unknown": "uncertain", "sent": "sent", "failed": "failed"}


class Sender:
    def __init__(self, controller, client, allowed_user_ids, *, capability_id="cap_Telegram01", max_resend=1):
        if type(max_resend) is not int or not 0 <= max_resend <= 10:
            raise ValueError("max_resend: целое от 0 до 10")
        self.ctl, self.store, self.client = controller, controller.store, client
        self.allowed = mailbox.allowed_ids(allowed_user_ids)
        self.capability_id, self.max_resend = capability_id, max_resend
        self.box = TelegramMailbox(controller)
        self.box.bind(client.bot_id)
        self.box.recover()

    def status(self, action_id):
        return STATUSES[self.store.get_outbox(action_id).status]

    def send(self, action_id, recipient, text, *, requested_seq, retry_of=None, operator_approved=False):
        if type(recipient) is not int or recipient not in self.allowed:
            raise ValueError("получатель отсутствует в числовом белом списке")
        if not isinstance(text, str) or not 1 <= len(text) <= 4096:
            raise ValueError("сообщение: от 1 до 4096 символов")
        with self.box.atomic():
            if self.box.exists(action_id):
                old = self.store.get_outbox(action_id)
                if (old.recipient, old.content) != (str(recipient), {"text": text}):
                    raise ValueError("action_id уже занят другим сообщением")
                if old.status == "requested":
                    # Перезапуск между записью попытки и записью ответа: исход неизвестен.
                    self.box.complete(action_id, "unknown")
                return self.status(action_id)
            event = self.store.get_event(requested_seq)
            if self.ctl.session_state != "running" or event.type not in ("message.out_requested", "creator.request"):
                raise ValueError("нет принятого ядром запроса отправки")
            if event.type == "message.out_requested":
                action = self.ctl.state["world_time"].get("world", {}).get("actions", {}).get(action_id, {})
                if (event.payload.get("action_id") != action_id or action.get("to") != str(recipient)
                    or action.get("text") != text or action.get("capability_id") != self.capability_id
                    or action.get("status") != "attempted"):
                    raise ValueError("отправка не совпадает с принятым действием")
            elif (event.payload.get("text") != text or action_id != "act_" + hashlib.sha256(
                    event.payload.get("crq_id", "").encode()).hexdigest()[:16]):
                raise ValueError("отправка не совпадает с просьбой ядра")
            if retry_of is not None:
                original = self.store.get_outbox(retry_of)
                used = self.box.get("resend:" + retry_of, 0)
                if (not operator_approved or original.status != "unknown" or used >= self.max_resend
                    or self.box.get("retry_of:" + retry_of) is not None
                    or (original.recipient, original.content) != (str(recipient), {"text": text})):
                    raise ValueError("повтор не разрешён или исчерпан предел")
                self.box.put("resend:" + retry_of, used + 1)
                self.box.put("retry_of:" + action_id, retry_of)
            self.box.reserve(action_id, recipient, text, requested_seq)
        # requested — уже зафиксированная единственная попытка. Любой обрыв даёт unknown при повторном вызове.
        status, external_id = "unknown", None
        try:
            result = self.client.call("sendMessage", {"chat_id": recipient, "text": text})
            if (isinstance(result, dict) and type(result.get("message_id")) is int
                and result["message_id"] > 0 and isinstance(result.get("chat"), dict)
                and result["chat"].get("id") == recipient and result["chat"].get("type") == "private"):
                status, external_id = "sent", str(result["message_id"])
        except TelegramError as error:
            # Только явный отказ 4xx, кроме таймаута/редиректа/неизвестного 5xx — подтверждённая неудача.
            if 400 <= error.code < 500 and error.code != 408:
                status = "failed"
        self.box.complete(action_id, status, external_id)
        return self.status(action_id)

    def execute(self, request):
        if request.kind != "message" or request.capability_id != self.capability_id:
            return ActionOutcome("failed", "Telegram: недоступная возможность")
        if not isinstance(request.to, str) or not request.to.isascii() or not request.to.isdecimal():
            return ActionOutcome("failed", "Telegram: нужен числовой получатель")
        event = self.ctl.find_event(key="out:" + request.action_id)
        if event is None:
            return ActionOutcome("failed", "Telegram: нет запроса ядра")
        try:
            status = self.send(request.action_id, int(request.to), request.text, requested_seq=event.seq)
        except ValueError:
            return ActionOutcome("failed", "Telegram: отправка отклонена")
        return ActionOutcome({"sent": "confirmed", "failed": "failed", "uncertain": "unknown"}[status],
                             "Telegram: " + status)


class CreatorBridge:
    """Совместим с Runner: приёмник уже записал inbox и событие; повторного ingest не нужно."""
    def __init__(self, receiver, sender, recipient):
        self.receiver, self.sender, self.recipient = receiver, sender, recipient

    def poll_replies(self):
        try:
            self.receiver.poll_once()
        except TelegramError as error:
            self.receiver.ctl.record_event(EventSpec("system.error", f"err:telegram_poll:{self.receiver.offset}_{error.code}",
                {"fault": "telegram_poll", "code": error.code}), actor_ref="telegram_receiver")
        return []

    def submit(self, request):
        event = self.sender.ctl.find_event(key="crq:" + request.request_key)
        if event is None:
            return Delivery(False, "Telegram: нет просьбы ядра")
        aid = "act_" + hashlib.sha256(request.crq_id.encode()).hexdigest()[:16]
        try:
            status = self.sender.send(aid, self.recipient, request.text, requested_seq=event.seq)
        except ValueError:
            return Delivery(False, "Telegram: просьба отклонена")
        return Delivery(status == "sent", "Telegram: " + status)
