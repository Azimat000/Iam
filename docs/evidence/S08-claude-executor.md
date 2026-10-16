# Свидетельство S08: изолированный исполнитель Claude Code

Снято командами ниже в сессии S08 (2026-10-16T20:30Z). Python 3.13.16, coverage.py 7.10.6, Claude Code CLI 2.1.301 (Claude Code).

**Важно.** Всё ниже — проверки на подделках (FakeClaude, подделка docker). Настоящая изоляция, вход по подписке и форма успешного ответа проверяются на Mac по барьеру G11 (`docs/evidence/claude-isolation.md`); пока он не закрыт, требования изоляции — `implemented`, не `verified`.

## Реальный образец, снятый в среде разработки
Ошибка авторизации (токен-пустышка), `claude -p --output-format json --tools ""`: объект с `is_error: true`, `api_error_status: 401`, `terminal_reason: api_error`, код возврата процесса 0. Он положен в `iam.providers.fake.auth_error` и разобран классификатором.

## `cd tests && python3 -m unittest providers.test_command providers.test_classify providers.test_provider providers.test_budget providers.test_run providers.test_scripts -v`
код возврата: 0
```
test_container_name_cannot_smuggle_flags (providers.test_command.ConfigGuardTest.test_container_name_cannot_smuggle_flags) ... ok
Клиент docker (без демона) разбирает наши флаги: опечатки в именах флагов ловятся без запуска контейнера. ... ok
test_home_tmpfs_belongs_to_the_unprivileged_user (providers.test_command.ConfigGuardTest.test_home_tmpfs_belongs_to_the_unprivileged_user) ... ok
test_model_name_is_checked (providers.test_command.ConfigGuardTest.test_model_name_is_checked) ... ok
test_stream_mode_is_for_the_check_script_only (providers.test_command.ConfigGuardTest.test_stream_mode_is_for_the_check_script_only) ... ok
test_the_image_is_never_pulled_implicitly (providers.test_command.ConfigGuardTest.test_the_image_is_never_pulled_implicitly) ... ok
test_unsafe_configurations_cannot_be_built (providers.test_command.ConfigGuardTest.test_unsafe_configurations_cannot_be_built) ... ok
test_claude_runs_tool_less_and_stateless (providers.test_command.IsolationFlagsTest.test_claude_runs_tool_less_and_stateless) ... ok
test_environment_is_an_allowlist (providers.test_command.IsolationFlagsTest.test_environment_is_an_allowlist) ... ok
test_every_isolation_flag_is_present (providers.test_command.IsolationFlagsTest.test_every_isolation_flag_is_present) ... ok
test_forbidden_things_are_absent (providers.test_command.IsolationFlagsTest.test_forbidden_things_are_absent) ... ok
test_names_are_unique_and_kill_command_targets_the_container (providers.test_command.IsolationFlagsTest.test_names_are_unique_and_kill_command_targets_the_container) ... ok
test_token_goes_by_file_not_by_value (providers.test_command.IsolationFlagsTest.test_token_goes_by_file_not_by_value) ... ok
Текст ошибки не должен сойти за ответ модели, даже если флаг is_error не выставлен. ... ok
test_envelope_has_metadata_but_never_the_result_text (providers.test_classify.ClassifierTable.test_envelope_has_metadata_but_never_the_result_text) ... ok
Реальная ошибка входа вышла с кодом 0, а упавший процесс с мусором — с кодом ≠ 0: смотрим на содержимое. ... ok
test_real_auth_error_sample_is_the_one_captured_in_dev (providers.test_classify.ClassifierTable.test_real_auth_error_sample_is_the_one_captured_in_dev) ... ok
test_table (providers.test_classify.ClassifierTable.test_table) ... ok
test_tool_use_keeps_parsed_data_for_the_journal (providers.test_classify.ClassifierTable.test_tool_use_keeps_parsed_data_for_the_journal) ... ok
Ход оборван (`error_max_turns`) или конверт чужого вида: даже со структурированным выводом это не ответ. ... ok
test_each_failure_class_has_its_own_exception (providers.test_provider.ErrorClassesTest.test_each_failure_class_has_its_own_exception) ... ok
test_tool_attempt_makes_the_result_unusable_but_is_reported_not_raised (providers.test_provider.ErrorClassesTest.test_tool_attempt_makes_the_result_unusable_but_is_reported_not_raised) ... ok
test_queue_and_exhaustion (providers.test_provider.FakeClaudeTest.test_queue_and_exhaustion) ... ok
test_one_repair_attempt_then_success (providers.test_provider.FormatRetryTest.test_one_repair_attempt_then_success) ... ok
test_retries_can_be_switched_off (providers.test_provider.FormatRetryTest.test_retries_can_be_switched_off) ... ok
test_schema_violation_counts_as_bad_format (providers.test_provider.FormatRetryTest.test_schema_violation_counts_as_bad_format) ... ok
test_second_bad_attempt_ends_with_format_error (providers.test_provider.FormatRetryTest.test_second_bad_attempt_ends_with_format_error) ... ok
test_json_schema_flag_can_be_switched_off (providers.test_provider.RoutingTest.test_json_schema_flag_can_be_switched_off) ... ok
test_simple_tasks_go_to_the_cheap_model_and_only_listed_ones_to_the_strong (providers.test_provider.RoutingTest.test_simple_tasks_go_to_the_cheap_model_and_only_listed_ones_to_the_strong) ... ok
test_the_list_of_hard_operations_is_configuration (providers.test_provider.RoutingTest.test_the_list_of_hard_operations_is_configuration) ... ok
test_context_goes_through_stdin_only (providers.test_provider.StreamsAndMinimisationTest.test_context_goes_through_stdin_only) ... ok
test_nothing_unnecessary_reaches_the_model (providers.test_provider.StreamsAndMinimisationTest.test_nothing_unnecessary_reaches_the_model) ... ok
Провайдер берёт только `context` запроса: лишние поля запроса (например, текст непрочитанного) не уходят. ... ok
test_json_in_text_and_fenced_json_are_accepted (providers.test_provider.ValidAnswerTest.test_json_in_text_and_fenced_json_are_accepted) ... ok
test_missing_cites_default_to_an_empty_list (providers.test_provider.ValidAnswerTest.test_missing_cites_default_to_an_empty_list) ... ok
test_missing_explanation_is_empty_text (providers.test_provider.ValidAnswerTest.test_missing_explanation_is_empty_text) ... ok
test_model_cannot_choose_proposal_ids_or_forge_the_envelope (providers.test_provider.ValidAnswerTest.test_model_cannot_choose_proposal_ids_or_forge_the_envelope) ... ok
test_secrets_in_executor_messages_do_not_reach_the_exception (providers.test_provider.ValidAnswerTest.test_secrets_in_executor_messages_do_not_reach_the_exception) ... ok
test_valid_answer_becomes_a_schema_valid_response_with_our_ids (providers.test_provider.ValidAnswerTest.test_valid_answer_becomes_a_schema_valid_response_with_our_ids) ... ok
test_wait_is_limited_by_the_request_deadline (providers.test_provider.ValidAnswerTest.test_wait_is_limited_by_the_request_deadline) ... ok
test_auth_error_pauses_with_auth_lost_and_saves_state (providers.test_budget.AuthAndLimitScenarioTest.test_auth_error_pauses_with_auth_lost_and_saves_state) ... ok
test_resume_allows_work_again (providers.test_budget.AuthAndLimitScenarioTest.test_resume_allows_work_again) ... ok
test_runs_are_told_apart_by_the_journal (providers.test_budget.AuthAndLimitScenarioTest.test_runs_are_told_apart_by_the_journal) ... ok
test_subscription_limit_is_recorded_and_pauses_as_auth_lost (providers.test_budget.AuthAndLimitScenarioTest.test_subscription_limit_is_recorded_and_pauses_as_auth_lost) ... ok
test_calls_threshold_alone (providers.test_budget.BudgetCounters.test_calls_threshold_alone) ... ok
test_defaults_are_30_minutes_and_10_calls (providers.test_budget.BudgetCounters.test_defaults_are_30_minutes_and_10_calls) ... ok
test_new_session_starts_a_new_budget (providers.test_budget.BudgetCounters.test_new_session_starts_a_new_budget) ... ok
test_time_threshold_alone (providers.test_budget.BudgetCounters.test_time_threshold_alone) ... ok
test_whichever_comes_first (providers.test_budget.BudgetCounters.test_whichever_comes_first) ... ok
test_counters_are_restored_from_the_journal_after_restart (providers.test_budget.BudgetRecoveryTest.test_counters_are_restored_from_the_journal_after_restart) ... ok
test_pause_does_not_eat_the_thirty_minutes (providers.test_budget.BudgetRecoveryTest.test_pause_does_not_eat_the_thirty_minutes) ... ok
test_restoring_ignores_other_sessions_and_empty_history (providers.test_budget.BudgetRecoveryTest.test_restoring_ignores_other_sessions_and_empty_history) ... ok
test_budgeted_runner_raises_a_typed_error (providers.test_budget.RepeatedFailureTest.test_budgeted_runner_raises_a_typed_error) ... ok
test_errors_pass_through_unchanged (providers.test_budget.RepeatedFailureTest.test_errors_pass_through_unchanged) ... ok
test_failure_count_starts_over_after_a_pause (providers.test_budget.RepeatedFailureTest.test_failure_count_starts_over_after_a_pause) ... ok
test_long_messages_are_truncated_in_the_journal (providers.test_budget.RepeatedFailureTest.test_long_messages_are_truncated_in_the_journal) ... ok
test_pause_happens_even_if_the_limit_record_is_refused (providers.test_budget.RepeatedFailureTest.test_pause_happens_even_if_the_limit_record_is_refused) ... ok
test_provider_is_not_called_outside_a_running_session (providers.test_budget.RepeatedFailureTest.test_provider_is_not_called_outside_a_running_session) ... ok
test_second_failure_in_a_row_pauses (providers.test_budget.RepeatedFailureTest.test_second_failure_in_a_row_pauses) ... ok
test_success_resets_the_failure_count (providers.test_budget.RepeatedFailureTest.test_success_resets_the_failure_count) ... ok
test_threshold_is_configurable (providers.test_budget.RepeatedFailureTest.test_threshold_is_configurable) ... ok
test_budget_applies_per_session (providers.test_budget.SessionBudgetPauseTest.test_budget_applies_per_session) ... ok
test_eleventh_call_is_refused_and_pauses_as_session_limit (providers.test_budget.SessionBudgetPauseTest.test_eleventh_call_is_refused_and_pauses_as_session_limit) ... ok
test_format_retry_costs_a_call (providers.test_budget.SessionBudgetPauseTest.test_format_retry_costs_a_call) ... ok
test_thirty_minutes_stop_the_session_before_the_call_limit (providers.test_budget.SessionBudgetPauseTest.test_thirty_minutes_stop_the_session_before_the_call_limit) ... ok
test_full_pass_with_an_action (providers.test_run.CycleOnClaudeCodeTest.test_full_pass_with_an_action) ... ok
test_garbage_then_valid_answer_in_the_same_request (providers.test_run.CycleOnClaudeCodeTest.test_garbage_then_valid_answer_in_the_same_request) ... ok
R-117: попытка инструмента делает результат непригодным — ничего не применено и не выполнено. ... ok
R-118: изоляция и проверка результата — два независимых слоя. Валидатор отклоняет «выполнено» в теле предложения, ... ok
test_answer_of_the_old_provider_after_the_swap_is_stale (providers.test_run.ModelSwapTest.test_answer_of_the_old_provider_after_the_swap_is_stale) ... ok
test_swap_keeps_state_and_memory (providers.test_run.ModelSwapTest.test_swap_keeps_state_and_memory) ... ok
test_context_in_stdin_and_result_from_stdout_only (providers.test_run.SubprocessRunnerTest.test_context_in_stdin_and_result_from_stdout_only) ... ok
test_failing_cleanup_still_reports_the_timeout (providers.test_run.SubprocessRunnerTest.test_failing_cleanup_still_reports_the_timeout) ... ok
test_missing_program_is_code_127_not_an_exception (providers.test_run.SubprocessRunnerTest.test_missing_program_is_code_127_not_an_exception) ... ok
test_nonzero_exit_keeps_stderr (providers.test_run.SubprocessRunnerTest.test_nonzero_exit_keeps_stderr) ... ok
test_provider_with_the_real_runner_reports_a_timeout (providers.test_run.SubprocessRunnerTest.test_provider_with_the_real_runner_reports_a_timeout) ... ok
test_timeout_kills_and_runs_cleanup (providers.test_run.SubprocessRunnerTest.test_timeout_kills_and_runs_cleanup) ... ok
test_undecodable_output_does_not_crash_the_runner (providers.test_run.SubprocessRunnerTest.test_undecodable_output_does_not_crash_the_runner) ... ok
test_compose_keeps_the_sandbox_network_internal (providers.test_scripts.ArtifactsTest.test_compose_keeps_the_sandbox_network_internal) ... ok
test_default_config_matches_the_compose_names (providers.test_scripts.ArtifactsTest.test_default_config_matches_the_compose_names) ... ok
test_dockerfile (providers.test_scripts.ArtifactsTest.test_dockerfile) ... ok
test_proxy_allowlist_is_minimal_and_ends_with_deny_all (providers.test_scripts.ArtifactsTest.test_proxy_allowlist_is_minimal_and_ends_with_deny_all) ... ok
test_all_required_passing_is_a_pass (providers.test_scripts.ReportTest.test_all_required_passing_is_a_pass) ... ok
test_extra_failure_also_fails_but_extra_pass_is_listed (providers.test_scripts.ReportTest.test_extra_failure_also_fails_but_extra_pass_is_listed) ... ok
test_main_writes_the_report_and_returns_the_verdict (providers.test_scripts.ReportTest.test_main_writes_the_report_and_returns_the_verdict) ... ok
test_missing_failed_and_skipped_required_checks_fail_the_report (providers.test_scripts.ReportTest.test_missing_failed_and_skipped_required_checks_fail_the_report) ... ok
test_reading_results (providers.test_scripts.ReportTest.test_reading_results) ... ok
test_secrets_never_reach_the_report_and_cells_are_escaped (providers.test_scripts.ReportTest.test_secrets_never_reach_the_report_and_cells_are_escaped) ... ok
Контейнер не запускается вовсе: ни одна проверка устройства не должна показать PASS (ложное «секретов нет»). ... ok
test_every_hole_is_caught_by_its_own_check (providers.test_scripts.ScriptFlowTest.test_every_hole_is_caught_by_its_own_check) ... ok
test_ideal_container_passes_everything (providers.test_scripts.ScriptFlowTest.test_ideal_container_passes_everything) ... ok
test_missing_init_event_is_a_failure_not_a_pass (providers.test_scripts.ScriptFlowTest.test_missing_init_event_is_a_failure_not_a_pass) ... ok
test_no_docker_or_dead_daemon_is_a_clear_error (providers.test_scripts.ScriptFlowTest.test_no_docker_or_dead_daemon_is_a_clear_error) ... ok
test_refuses_to_run_with_unsafe_inputs (providers.test_scripts.ScriptFlowTest.test_refuses_to_run_with_unsafe_inputs) ... ok
test_token_file_must_hold_exactly_the_token_line (providers.test_scripts.ScriptFlowTest.test_token_file_must_hold_exactly_the_token_line) ... ok
test_token_never_appears_in_outputs_or_report (providers.test_scripts.ScriptFlowTest.test_token_never_appears_in_outputs_or_report) ... ok
test_emit_cmd_prints_the_same_isolation_flags (providers.test_scripts.StaticTest.test_emit_cmd_prints_the_same_isolation_flags) ... ok
test_every_required_check_is_produced_by_the_script (providers.test_scripts.StaticTest.test_every_required_check_is_produced_by_the_script) ... ok
test_script_has_the_safety_checks_it_promises (providers.test_scripts.StaticTest.test_script_has_the_safety_checks_it_promises) ... ok
test_shell_syntax (providers.test_scripts.StaticTest.test_shell_syntax) ... ok
Ran 100 tests in 38.459s
OK
```

## `python3 tools/covcheck.py` (все тесты проекта + пороги)
код возврата covcheck: 0
```
Ran 709 tests in 173.559s
OK
src/iam/cycle/__init__.py                      100.0%   100.0%  ok [критичный]
src/iam/cycle/attention.py                     100.0%   100.0%  ok [критичный]
src/iam/cycle/cycle.py                         100.0%   100.0%  ok [критичный]
src/iam/cycle/handlers.py                      100.0%   100.0%  ok [критичный]
src/iam/cycle/runner.py                        100.0%   100.0%  ok [критичный]
src/iam/cycle/validator.py                     100.0%   100.0%  ok [критичный]
src/iam/providers/__init__.py                  100.0%   100.0%  ok [критичный]
src/iam/providers/budget.py                    100.0%   100.0%  ok [критичный]
src/iam/providers/classify.py                  100.0%   100.0%  ok [критичный]
src/iam/providers/claude_code.py               100.0%   100.0%  ok [критичный]
src/iam/providers/command.py                   100.0%   100.0%  ok [критичный]
src/iam/providers/errors.py                    100.0%   100.0%  ok [критичный]
src/iam/providers/fake.py                      100.0%   100.0%  ok [критичный]
src/iam/providers/run.py                       100.0%   100.0%  ok [критичный]
src/iam/state/__init__.py                      100.0%   100.0%  ok [критичный]
src/iam/state/controller.py                    100.0%   100.0%  ok [критичный]
src/iam/state/errors.py                        100.0%   100.0%  ok [критичный]
src/iam/state/paths.py                         100.0%   100.0%  ok [критичный]
src/iam/state/replay.py                        100.0%   100.0%  ok [критичный]
src/iam/state/restore.py                       100.0%   100.0%  ok [критичный]
src/iam/state/rules.py                         100.0%   100.0%  ok [критичный]
src/iam/state/types.py                         100.0%   100.0%  ok [критичный]
Модулей: 59, ниже порога: 0.
```

## `bash -n scripts/claude/verify_isolation.sh`, реестр, память
```
bash -n: ok
OK: 223 требований; все непустые строки PLAN.md (344) покрыты требованиями или пропусками
память проекта согласована
```
