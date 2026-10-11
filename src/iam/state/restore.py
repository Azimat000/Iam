"""Отметка восстановления копии: новая эпоха, вмешательство оператора и `system.restored` (D-013).

Вызывается только на **восстановленной копии** (`iam.backup.restore`), никогда на живой базе. Под замком
писателя одной транзакцией записывает: событие `operator.intervention` (вид `restore_backup`) — оно открывает
эпоху `E+1` на той же версии состояния; событие `system.restored`, которое ссылается на него в `causes`;
снимок `(E+1, версия)` с тем же состоянием. После этого `StateController` на этой базе продолжает жить в новой
эпохе, а ответы моделей, выданные в эпохе `E`, устаревают по обычной проверке версии `(эпоха, версия)`.
Журнал событий после точки копии в восстановленную базу не попадает: нумерация `seq` продолжается от копии,
разрыв отмечен полями `lost_seq_from`/`lost_seq_to` (если известны).
"""
from __future__ import annotations

import datetime as dt

from iam.domain import Event, new_id
from iam.storage import Snapshot, Store

from . import rules
from .paths import empty_state


def _now() -> str:
    return dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def mark_restored(store: Store, *, snapshot_id: str, reason: str, checked_on_copy: bool, backup: dict,
                  lost_seq_to: int | None = None, now: str | None = None) -> tuple[Event, Event]:
    """Открыть новую эпоху на восстановленной копии. Возвращает (вмешательство, system.restored).

    `backup` — сведения о копии из манифеста (кладутся в событие как есть: sha256, created_at, last_seq…).
    `lost_seq_to` — последний `seq` живой базы на момент отката (если известен): события
    `last_seq+1 … lost_seq_to` отброшены восстановлением."""
    if store.readonly:
        raise ValueError("отметить восстановление можно только на базе, открытой для записи")
    if not reason or not reason.strip():
        raise ValueError("восстановление без причины не записывается")
    at = now or _now()
    snap = store.latest_snapshot()
    last = store.last_seq()
    if last == 0:
        raise ValueError("в базе нет ни одного события: восстанавливать нечего")
    life = store.get_event(last).life_time_ms
    life = max(life, (store.clock() or (0,))[0])
    baseline = None
    if snap is None:   # переходов не было: исходная точка (эпоха 1, версия 0), чтобы линия снимков началась
        baseline = Snapshot(1, 0, last, life, at, empty_state())
        snap = baseline
    from_epoch, version, state = snap.epoch, snap.state_version, snap.state
    to_epoch = from_epoch + 1
    operator_id = new_id("opr")
    lost_from = last + 1 if lost_seq_to is not None and lost_seq_to > last else None
    base = dict(persona_id=store.persona_id, occurred_at=at, life_time_ms=life, epoch=to_epoch,
                state_version=version, changes_state=True)
    intervention = Event(
        event_id=new_id("evt"), type="operator.intervention", source=rules.event_source("operator", "iam_cli"),
        idempotency_key=f"opr:{operator_id}",
        payload={"intervention_id": operator_id, "kind": "restore_backup", "reason": reason.strip(),
                 "snapshot_id": snapshot_id, "target": "backup"}, **base)
    restored_payload = {
        "snapshot_id": snapshot_id, "from_epoch": from_epoch, "to_epoch": to_epoch, "restored_to_seq": last,
        "checked_on_copy": checked_on_copy, "reason": reason.strip(), "backup": dict(backup)}
    if lost_from is not None:
        restored_payload.update(lost_seq_from=lost_from, lost_seq_to=lost_seq_to)
    restored = Event(
        event_id=new_id("evt"), type="system.restored", source=rules.event_source("core", "backup_restore"),
        idempotency_key=f"rst:{snapshot_id}", payload=restored_payload,
        causes=(intervention.event_id,), **base)
    with store.transaction():
        if baseline is not None:
            store.put_snapshot(baseline)
        intervention, _ = store.append_event(intervention)
        restored, _ = store.append_event(restored)
        store.put_snapshot(Snapshot(to_epoch, version, restored.seq, life, at, state))
    return intervention, restored
