# Свидетельство S07: интерфейсы ядра и цикл на заглушках

Снято командами ниже в сессии S07 (2026-10-11T21:56Z). Python 3.13.16, SQLite 3.45.1, coverage.py 7.10.6.

## `cd tests && python3 -m unittest cycle.test_cycle cycle.test_safety cycle.test_runner cycle.test_interfaces cycle.test_kinds cycle.test_guards cycle.test_fuzz -v`
код возврата: 0
```
test_all_stages_in_order_and_in_the_journal (cycle.test_cycle.FullPassTest.test_all_stages_in_order_and_in_the_journal) ... ok
test_confirmed_action_becomes_a_memory_with_the_confirmation_as_source (cycle.test_cycle.FullPassTest.test_confirmed_action_becomes_a_memory_with_the_confirmation_as_source) ... ok
test_state_equals_replay_after_a_cycle (cycle.test_cycle.FullPassTest.test_state_equals_replay_after_a_cycle) ... ok
test_confirmed_message_is_out_sent_by_the_world (cycle.test_safety.ActionFailureTest.test_confirmed_message_is_out_sent_by_the_world) ... ok
test_crash_inside_the_world_is_unknown_outcome_not_success (cycle.test_safety.ActionFailureTest.test_crash_inside_the_world_is_unknown_outcome_not_success) ... ok
test_garbage_from_the_world_is_unknown (cycle.test_safety.ActionFailureTest.test_garbage_from_the_world_is_unknown) ... ok
test_lost_ack_of_a_message_is_out_unknown_and_not_resent (cycle.test_safety.ActionFailureTest.test_lost_ack_of_a_message_is_out_unknown_and_not_resent) ... ok
test_nothing_to_remember_skips_memory_update (cycle.test_safety.ActionFailureTest.test_nothing_to_remember_skips_memory_update) ... ok
test_process_death_mid_action_then_restart_settles_as_unknown (cycle.test_safety.ActionFailureTest.test_process_death_mid_action_then_restart_settles_as_unknown) ... ok
test_settle_unfinished_does_nothing_in_pause (cycle.test_safety.ActionFailureTest.test_settle_unfinished_does_nothing_in_pause) ... ok
test_world_failure_is_recorded_as_failed_and_not_remembered_as_done (cycle.test_safety.ActionFailureTest.test_world_failure_is_recorded_as_failed_and_not_remembered_as_done) ... ok
test_attend_and_no_action_set_attention (cycle.test_safety.AttentionKindsTest.test_attend_and_no_action_set_attention) ... ok
test_conflicting_proposals_in_one_answer (cycle.test_safety.AttentionKindsTest.test_conflicting_proposals_in_one_answer) ... ok
test_bad_request_key_is_rejected (cycle.test_safety.CreatorRequestTest.test_bad_request_key_is_rejected) ... ok
test_bridge_failure_is_an_error_not_a_crash (cycle.test_safety.CreatorRequestTest.test_bridge_failure_is_an_error_not_a_crash) ... ok
test_request_is_recorded_before_delivery_and_delivered_once (cycle.test_safety.CreatorRequestTest.test_request_is_recorded_before_delivery_and_delivered_once) ... ok
test_same_unresolved_request_is_not_sent_again (cycle.test_safety.CreatorRequestTest.test_same_unresolved_request_is_not_sent_again) ... ok
test_undelivered_request_is_not_reported_as_delivered (cycle.test_safety.CreatorRequestTest.test_undelivered_request_is_not_reported_as_delivered) ... ok
Не вышло с недоступным — в следующем ответе личность может обратиться к оператору (но не более одной). ... ok
Возможность появилась в реестре уже после запроса: в allowed_actions её не было (C-6). ... ok
test_claim_without_capability_is_rejected_and_wish_is_kept (cycle.test_safety.NoToolTest.test_claim_without_capability_is_rejected_and_wish_is_kept) ... ok
test_kind_outside_allowed_proposals_is_rejected (cycle.test_safety.NoToolTest.test_kind_outside_allowed_proposals_is_rejected) ... ok
test_limit_can_be_raised (cycle.test_safety.NoToolTest.test_limit_can_be_raised) ... ok
test_two_actions_in_one_answer_exceed_the_limit (cycle.test_safety.NoToolTest.test_two_actions_in_one_answer_exceed_the_limit) ... ok
test_unavailable_capability_is_not_offered_and_not_accepted (cycle.test_safety.NoToolTest.test_unavailable_capability_is_not_offered_and_not_accepted) ... ok
test_exhausted_script_is_a_provider_error (cycle.test_safety.ProviderFailureTest.test_exhausted_script_is_a_provider_error) ... ok
test_one_repair_attempt_for_a_malformed_answer (cycle.test_safety.ProviderFailureTest.test_one_repair_attempt_for_a_malformed_answer) ... ok
test_provider_error_is_logged_and_nothing_changes (cycle.test_safety.ProviderFailureTest.test_provider_error_is_logged_and_nothing_changes) ... ok
test_refusal_is_not_a_decision (cycle.test_safety.ProviderFailureTest.test_refusal_is_not_a_decision) ... ok
test_second_malformed_answer_ends_the_cycle (cycle.test_safety.ProviderFailureTest.test_second_malformed_answer_ends_the_cycle) ... ok
test_stale_after_repair_attempt_keeps_retry_link (cycle.test_safety.ProviderFailureTest.test_stale_after_repair_attempt_keeps_retry_link) ... ok
test_response_after_pause_is_not_applied (cycle.test_safety.StaleAnswerTest.test_response_after_pause_is_not_applied) ... ok
test_runner_returns_to_the_trigger_in_the_next_pass (cycle.test_safety.StaleAnswerTest.test_runner_returns_to_the_trigger_in_the_next_pass) ... ok
test_stale_answer_is_not_applied_and_a_fresh_one_is (cycle.test_safety.StaleAnswerTest.test_stale_answer_is_not_applied_and_a_fresh_one_is) ... ok
test_stale_without_budget_ends_without_decision_and_keeps_the_trigger (cycle.test_safety.StaleAnswerTest.test_stale_without_budget_ends_without_decision_and_keeps_the_trigger) ... ok
test_claim_of_execution_in_body_is_rejected (cycle.test_safety.ToolUseTest.test_claim_of_execution_in_body_is_rejected) ... ok
test_claimed_result_on_a_state_change_is_flagged_too (cycle.test_safety.ToolUseTest.test_claimed_result_on_a_state_change_is_flagged_too) ... ok
Три ответа подряд (accepted → attempted → confirmed) со ссылками на «нужные» события исход не дают. ... ok
Ни ответ модели, ни её state_change не пишут исход действия: статус меняет только автомат. ... ok
В state_change значения операций — данные состояния: ключ «result» внутри значения не признак. ... ok
test_tool_calls_inside_action_params_reject_the_whole_response (cycle.test_safety.ToolUseTest.test_tool_calls_inside_action_params_reject_the_whole_response) ... ok
test_tool_use_flag_from_provider_rejects_everything (cycle.test_safety.ToolUseTest.test_tool_use_flag_from_provider_rejects_everything) ... ok
test_unknown_body_field_rejects_only_that_proposal (cycle.test_safety.ToolUseTest.test_unknown_body_field_rejects_only_that_proposal) ... ok
test_archive_layer_is_not_offered_to_the_model (cycle.test_runner.IngestTest.test_archive_layer_is_not_offered_to_the_model) ... ok
test_messages_arrive_in_pause_without_calling_the_model (cycle.test_runner.IngestTest.test_messages_arrive_in_pause_without_calling_the_model) ... ok
test_repeated_delivery_creates_one_event_and_one_cycle (cycle.test_runner.IngestTest.test_repeated_delivery_creates_one_event_and_one_cycle) ... ok
test_unread_message_content_never_reaches_the_model (cycle.test_runner.IngestTest.test_unread_message_content_never_reaches_the_model) ... ok
test_activity_completed_trigger_uses_generic_context (cycle.test_runner.LimitTest.test_activity_completed_trigger_uses_generic_context) ... ok
test_decision_limit_per_pass_and_the_rest_waits (cycle.test_runner.LimitTest.test_decision_limit_per_pass_and_the_rest_waits) ... ok
test_unlisted_events_are_not_triggers (cycle.test_runner.LimitTest.test_unlisted_events_are_not_triggers) ... ok
test_cycle_is_idempotent_for_one_trigger (cycle.test_runner.NoEventsTest.test_cycle_is_idempotent_for_one_trigger) ... ok
test_no_cycle_while_paused_and_it_waits_for_resume (cycle.test_runner.NoEventsTest.test_no_cycle_while_paused_and_it_waits_for_resume) ... ok
Сценарий «Отсутствие новых сообщений»: выбор занятия вызван плановым моментом, не опросом. ... ok
test_without_events_the_provider_is_never_called (cycle.test_runner.NoEventsTest.test_without_events_the_provider_is_never_called) ... ok
test_endless_failures_give_up_after_max_rounds (cycle.test_runner.RobustnessTest.test_endless_failures_give_up_after_max_rounds) ... ok
test_exception_in_one_cycle_does_not_stop_the_others_and_is_not_retried (cycle.test_runner.RobustnessTest.test_exception_in_one_cycle_does_not_stop_the_others_and_is_not_retried) ... ok
test_provider_error_keeps_the_trigger_for_a_later_round (cycle.test_runner.RobustnessTest.test_provider_error_keeps_the_trigger_for_a_later_round) ... ok
test_skipped_old_triggers_do_not_use_up_the_decision_limit (cycle.test_runner.RobustnessTest.test_skipped_old_triggers_do_not_use_up_the_decision_limit) ... ok
test_foreign_objects_do_not_fit (cycle.test_interfaces.ConformanceTest.test_foreign_objects_do_not_fit) ... ok
test_store_memory_reads_only_personal_and_interpretation (cycle.test_interfaces.ConformanceTest.test_store_memory_reads_only_personal_and_interpretation) ... ok
test_stub_creator_bridge_roundtrip (cycle.test_interfaces.ConformanceTest.test_stub_creator_bridge_roundtrip) ... ok
test_stub_memory_search_and_descriptions (cycle.test_interfaces.ConformanceTest.test_stub_memory_search_and_descriptions) ... ok
test_stub_world_follows_its_script_then_default (cycle.test_interfaces.ConformanceTest.test_stub_world_follows_its_script_then_default) ... ok
test_stubs_and_the_real_controller_fit_the_sockets (cycle.test_interfaces.ConformanceTest.test_stubs_and_the_real_controller_fit_the_sockets) ... ok
test_types_validate_themselves (cycle.test_interfaces.ConformanceTest.test_types_validate_themselves) ... ok
Цикл не импортирует заглушки, хранилище и часы: связь только через интерфейсы и привратника. ... ok
test_cycle_runs_on_other_implementations_of_every_socket (cycle.test_interfaces.SwapTest.test_cycle_runs_on_other_implementations_of_every_socket) ... ok
test_import_scan_is_not_vacuous (cycle.test_interfaces.SwapTest.test_import_scan_is_not_vacuous) ... ok
test_interfaces_do_not_import_stubs_or_the_cycle (cycle.test_interfaces.SwapTest.test_interfaces_do_not_import_stubs_or_the_cycle) ... ok
test_attempt_needs_an_accepted_proposal_as_its_cause (cycle.test_kinds.ActionMachineTest.test_attempt_needs_an_accepted_proposal_as_its_cause) ... ok
test_cannot_skip_the_attempt (cycle.test_kinds.ActionMachineTest.test_cannot_skip_the_attempt) ... ok
test_creation_only_as_accepted (cycle.test_kinds.ActionMachineTest.test_creation_only_as_accepted) ... ok
test_message_sent_belongs_to_the_world_and_unknown_to_core_or_world (cycle.test_kinds.ActionMachineTest.test_message_sent_belongs_to_the_world_and_unknown_to_core_or_world) ... ok
test_model_state_change_cannot_touch_action_records (cycle.test_kinds.ActionMachineTest.test_model_state_change_cannot_touch_action_records) ... ok
test_only_the_world_confirms (cycle.test_kinds.ActionMachineTest.test_only_the_world_confirms) ... ok
Даже с верными основаниями статус не получить переходом, который несёт общее событие. ... ok
Исход без ссылки на попытку (событие-основание — не попытка) не принимается. ... ok
test_handler_decides_ops_and_sees_the_context (cycle.test_kinds.KindHandlerTest.test_handler_decides_ops_and_sees_the_context) ... ok
Разборщик не обходит защиту: поля сеанса и времени остаются недоступны. ... ok
test_handler_violation_rejects_that_proposal_only (cycle.test_kinds.KindHandlerTest.test_handler_violation_rejects_that_proposal_only) ... ok
test_journal_reading_helpers (cycle.test_kinds.KindHandlerTest.test_journal_reading_helpers) ... ok
test_kind_must_be_allowed_by_the_request_even_for_state_change (cycle.test_kinds.KindHandlerTest.test_kind_must_be_allowed_by_the_request_even_for_state_change) ... ok
test_kind_without_handler_is_not_supported (cycle.test_kinds.KindHandlerTest.test_kind_without_handler_is_not_supported) ... ok
test_state_change_cannot_be_overridden (cycle.test_kinds.KindHandlerTest.test_state_change_cannot_be_overridden) ... ok
test_extra_field_in_every_executable_kind_is_rejected (cycle.test_guards.BodyFieldsTest.test_extra_field_in_every_executable_kind_is_rejected) ... ok
test_validator_helpers (cycle.test_guards.BodyFieldsTest.test_validator_helpers) ... ok
test_attention_refused_ends_the_cycle_before_the_model (cycle.test_guards.GatekeeperRefusesTest.test_attention_refused_ends_the_cycle_before_the_model) ... ok
test_refused_attempt_means_the_world_is_never_asked (cycle.test_guards.GatekeeperRefusesTest.test_refused_attempt_means_the_world_is_never_asked) ... ok
test_refused_creator_request_is_not_delivered (cycle.test_guards.GatekeeperRefusesTest.test_refused_creator_request_is_not_delivered) ... ok
test_refused_memory_write_is_reported (cycle.test_guards.GatekeeperRefusesTest.test_refused_memory_write_is_reported) ... ok
test_refused_outcome_leaves_the_attempt_visible_and_unremembered (cycle.test_guards.GatekeeperRefusesTest.test_refused_outcome_leaves_the_attempt_visible_and_unremembered) ... ok
test_request_refused_means_no_call_to_the_model (cycle.test_guards.GatekeeperRefusesTest.test_request_refused_means_no_call_to_the_model) ... ok
test_request_stuck_before_sending_may_be_proposed_again (cycle.test_guards.GatekeeperRefusesTest.test_request_stuck_before_sending_may_be_proposed_again) ... ok
test_unsettled_attempt_stays_visible_when_even_unknown_is_refused (cycle.test_guards.GatekeeperRefusesTest.test_unsettled_attempt_stays_visible_when_even_unknown_is_refused) ... ok
test_reply_may_be_a_function_or_a_ready_dict (cycle.test_guards.ScriptedProviderTest.test_reply_may_be_a_function_or_a_ready_dict) ... ok
test_random_runs_keep_the_honesty_invariants (cycle.test_fuzz.CycleFuzzTest.test_random_runs_keep_the_honesty_invariants) ... ok
test_no_bare_store_open_outside_storage_and_backup (cycle.test_fuzz.MigrationGuardTest.test_no_bare_store_open_outside_storage_and_backup) ... ok
Ran 97 tests in 5.981s
OK
```

## `python3 -m unittest discover -s tests` (все тесты проекта) и `python3 tools/covcheck.py`
код возврата covcheck: 0
```
Ran 609 tests in 76.339s
OK
src/iam/cycle/__init__.py                      100.0%   100.0%  ok [критичный]
src/iam/cycle/attention.py                     100.0%   100.0%  ok [критичный]
src/iam/cycle/cycle.py                         100.0%   100.0%  ok [критичный]
src/iam/cycle/handlers.py                      100.0%   100.0%  ok [критичный]
src/iam/cycle/runner.py                        100.0%   100.0%  ok [критичный]
src/iam/cycle/validator.py                     100.0%   100.0%  ok [критичный]
src/iam/interfaces/__init__.py                 100.0%   100.0%  ok
src/iam/interfaces/protocols.py                100.0%   100.0%  ok
src/iam/interfaces/types.py                    100.0%   100.0%  ok
src/iam/state/__init__.py                      100.0%   100.0%  ok [критичный]
src/iam/state/controller.py                    100.0%   100.0%  ok [критичный]
src/iam/state/errors.py                        100.0%   100.0%  ok [критичный]
src/iam/state/paths.py                         100.0%   100.0%  ok [критичный]
src/iam/state/replay.py                        100.0%   100.0%  ok [критичный]
src/iam/state/restore.py                       100.0%   100.0%  ok [критичный]
src/iam/state/rules.py                         100.0%   100.0%  ok [критичный]
src/iam/state/types.py                         100.0%   100.0%  ok [критичный]
src/iam/stubs/__init__.py                      100.0%   100.0%  ok
src/iam/stubs/capabilities.py                  100.0%   100.0%  ok
src/iam/stubs/creator.py                       100.0%   100.0%  ok
src/iam/stubs/memory.py                        100.0%   100.0%  ok
src/iam/stubs/provider.py                      100.0%   100.0%  ok
src/iam/stubs/world.py                         100.0%   100.0%  ok
Модулей: 51, ниже порога: 0.
```

## `python3 tools/memory.py check`, `python3 tools/reqs.py validate`
```
память проекта согласована
OK: 223 требований; все непустые строки PLAN.md (344) покрыты требованиями или пропусками
```
