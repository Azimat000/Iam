# Свидетельство S05: привратник состояния и время жизни

Снято командами ниже в сессии S05 (2026-10-11T20:51Z), коммит 1cbf16a плюс незакоммиченные правки среза. Python 3.13.16, SQLite 3.45.1, coverage.py 7.10.6.

## `cd tests && python3 -m unittest state.test_boundaries state.test_fuzz state.test_replay state.test_response -v`
код возврата: 0
```
test_kill9_leaves_consistent_state (state.test_boundaries.KillTest.test_kill9_leaves_consistent_state) ... ok
test_only_state_package_writes_to_the_store (state.test_boundaries.SingleWriterTest.test_only_state_package_writes_to_the_store)
R-105: изменить состояние и журнал можно только через привратник. Статическая проверка: ни один модуль ... ok
test_race_of_two_writes_on_one_base_is_impossible (state.test_boundaries.SingleWriterTest.test_race_of_two_writes_on_one_base_is_impossible) ... ok
test_scan_actually_detects_offenders (state.test_boundaries.SingleWriterTest.test_scan_actually_detects_offenders)
Сама проверка не вакуумна: на искусственном нарушителе она срабатывает. ... ok
test_second_controller_in_same_process_is_refused (state.test_boundaries.SingleWriterTest.test_second_controller_in_same_process_is_refused) ... ok
test_second_process_cannot_open_for_writing (state.test_boundaries.SingleWriterTest.test_second_process_cannot_open_for_writing) ... ok
test_store_write_api_is_pinned (state.test_boundaries.SingleWriterTest.test_store_write_api_is_pinned) ... ok
test_random_sequences_keep_invariants (state.test_fuzz.FuzzTest.test_random_sequences_keep_invariants) ... ok
test_empty_journal_has_nothing_to_replay (state.test_replay.EmptyJournalTest.test_empty_journal_has_nothing_to_replay) ... ok
test_snapshot_without_transitions_is_a_problem (state.test_replay.EmptyJournalTest.test_snapshot_without_transitions_is_a_problem) ... ok
test_companion_with_other_version_is_detected (state.test_replay.ReplayTest.test_companion_with_other_version_is_detected) ... ok
test_decreasing_life_time_is_detected (state.test_replay.ReplayTest.test_decreasing_life_time_is_detected) ... ok
test_epoch_change_needs_restore_slice (state.test_replay.ReplayTest.test_epoch_change_needs_restore_slice) ... ok
test_forged_snapshot_is_detected (state.test_replay.ReplayTest.test_forged_snapshot_is_detected) ... ok
test_memory_tampering_is_detected (state.test_replay.ReplayTest.test_memory_tampering_is_detected) ... ok
test_missing_memory_record_is_detected (state.test_replay.ReplayTest.test_missing_memory_record_is_detected) ... ok
test_missing_snapshot_and_wrong_final_snapshot (state.test_replay.ReplayTest.test_missing_snapshot_and_wrong_final_snapshot) ... ok
test_replay_matches_snapshots_step_by_step (state.test_replay.ReplayTest.test_replay_matches_snapshots_step_by_step) ... ok
test_state_rebuilt_after_restart_equals_snapshot (state.test_replay.ReplayTest.test_state_rebuilt_after_restart_equals_snapshot) ... ok
test_version_jump_and_wrong_base_are_detected (state.test_replay.ReplayTest.test_version_jump_and_wrong_base_are_detected) ... ok
test_wrong_at_seq_is_detected (state.test_replay.ReplayTest.test_wrong_at_seq_is_detected) ... ok
test_accepted_proposals_become_one_transition (state.test_response.AcceptTest.test_accepted_proposals_become_one_transition) ... ok
test_citing_a_memory_record_is_allowed (state.test_response.AcceptTest.test_citing_a_memory_record_is_allowed) ... ok
test_confirmed_action_allows_done_through_citation (state.test_response.AcceptTest.test_confirmed_action_allows_done_through_citation) ... ok
test_same_answer_twice_is_applied_once (state.test_response.AcceptTest.test_same_answer_twice_is_applied_once)
Сценарий «Повтор результата или старый ответ»: дубликат ответа не применяется второй раз. ... ok
test_same_request_with_other_content_is_conflict (state.test_response.AcceptTest.test_same_request_with_other_content_is_conflict) ... ok
test_single_proposal_keeps_its_id (state.test_response.AcceptTest.test_single_proposal_keeps_its_id) ... ok
test_all_rejected_means_no_transition (state.test_response.RejectTest.test_all_rejected_means_no_transition) ... ok
test_bad_format_and_missing_echo (state.test_response.RejectTest.test_bad_format_and_missing_echo) ... ok
test_duplicate_proposal_ids_in_one_answer_are_refused (state.test_response.RejectTest.test_duplicate_proposal_ids_in_one_answer_are_refused) ... ok
test_each_proposal_is_checked_separately (state.test_response.RejectTest.test_each_proposal_is_checked_separately) ... ok
test_echo_must_repeat_the_request_not_just_the_current_state (state.test_response.RejectTest.test_echo_must_repeat_the_request_not_just_the_current_state)
Ответ, эхо которого не совпадает с версией запроса, — ошибка провайдера, даже если эхо совпало ... ok
test_empty_proposal_list (state.test_response.RejectTest.test_empty_proposal_list) ... ok
test_garbage_that_is_not_json_is_still_refused_and_logged (state.test_response.RejectTest.test_garbage_that_is_not_json_is_still_refused_and_logged) ... ok
test_reused_proposal_id_cannot_be_accepted_twice (state.test_response.RejectTest.test_reused_proposal_id_cannot_be_accepted_twice) ... ok
test_status_not_ok_is_ignored (state.test_response.RejectTest.test_status_not_ok_is_ignored) ... ok
test_tool_use_makes_answer_unusable (state.test_response.RejectTest.test_tool_use_makes_answer_unusable)
R-117: ответ, в котором обнаружен вызов инструмента, ничего не меняет. ... ok
test_unknown_request (state.test_response.RejectTest.test_unknown_request) ... ok
test_bad_requests_are_refused (state.test_response.RequestTest.test_bad_requests_are_refused) ... ok
test_no_request_in_pause (state.test_response.RequestTest.test_no_request_in_pause) ... ok
test_request_is_recorded_before_sending (state.test_response.RequestTest.test_request_is_recorded_before_sending) ... ok
test_answer_after_restart_cannot_apply (state.test_response.StaleTest.test_answer_after_restart_cannot_apply) ... ok
test_deadline (state.test_response.StaleTest.test_deadline) ... ok
test_epoch_alone_makes_answer_stale (state.test_response.StaleTest.test_epoch_alone_makes_answer_stale)
Версия состояния та же, но эпоха другая (после отката из копии): ответ устарел. ... ok
test_non_state_events_do_not_expire_the_answer (state.test_response.StaleTest.test_non_state_events_do_not_expire_the_answer) ... ok
test_old_version_answer_is_stale_and_not_applied (state.test_response.StaleTest.test_old_version_answer_is_stale_and_not_applied)
Ответ, основанный на прежней версии состояния, не применяется (R-108). ... ok
test_other_epoch_is_stale_too (state.test_response.StaleTest.test_other_epoch_is_stale_too) ... ok
test_session_changes_make_answer_stale (state.test_response.StaleTest.test_session_changes_make_answer_stale) ... ok
test_state_changing_event_expires_the_answer (state.test_response.StaleTest.test_state_changing_event_expires_the_answer) ... ok
Ran 49 tests in 25.515s
OK
```

## `python3 tools/covcheck.py` (полный прогон тестов под измерителем)
код возврата: 0
```
----------------------------------------------------------------------
Ran 407 tests in 61.070s
OK
модуль                                         строки    ветви  итог
src/iam/__init__.py                            100.0%   100.0%  ok
src/iam/clock/__init__.py                      100.0%   100.0%  ok [критичный]
src/iam/clock/lifeclock.py                     100.0%   100.0%  ok [критичный]
src/iam/clock/moments.py                       100.0%   100.0%  ok [критичный]
src/iam/domain/__init__.py                     100.0%   100.0%  ok
src/iam/domain/catalog.py                      100.0%   100.0%  ok
src/iam/domain/ids.py                          100.0%   100.0%  ok
src/iam/domain/jsonschema.py                   100.0%   100.0%  ok
src/iam/domain/types.py                        100.0%   100.0%  ok
src/iam/state/__init__.py                      100.0%   100.0%  ok [критичный]
src/iam/state/controller.py                    100.0%   100.0%  ok [критичный]
src/iam/state/errors.py                        100.0%   100.0%  ok [критичный]
src/iam/state/paths.py                         100.0%   100.0%  ok [критичный]
src/iam/state/replay.py                        100.0%   100.0%  ok [критичный]
src/iam/state/rules.py                         100.0%   100.0%  ok [критичный]
src/iam/state/types.py                         100.0%   100.0%  ok [критичный]
src/iam/storage/__init__.py                    100.0%   100.0%  ok [критичный]
src/iam/storage/db.py                          100.0%   100.0%  ok [критичный]
src/iam/storage/errors.py                      100.0%   100.0%  ok [критичный]
src/iam/storage/lock.py                        100.0%   100.0%  ok [критичный]
src/iam/storage/migrations.py                  100.0%   100.0%  ok [критичный]
src/iam/storage/runtime.py                     100.0%   100.0%  ok [критичный]
src/iam/storage/store.py                       100.0%   100.0%  ok [критичный]
tools/covcheck.py                              100.0%   100.0%  ok
tools/memory.py                                 90.8%    84.4%  ok
tools/reqs.py                                   95.6%    90.6%  ok
tools/scenarios.py                             100.0%    99.4%  ok
Модулей: 27, ниже порога: 0.
```

## `python3 tools/memory.py check` и `python3 tools/reqs.py validate`
```
память проекта согласована
OK: 223 требований; все непустые строки PLAN.md (344) покрыты требованиями или пропусками
```
