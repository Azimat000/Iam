# Свидетельство S06: копии, восстановление, экспорт, место

Снято в сессии S06 (2026-10-11T21:21Z), после правок по независимой проверке. Python 3.13.16, SQLite 3.45.1, coverage.py 7.10.6.

## `python3 -m unittest discover -s tests/backup -t tests -v`
test_backup_of_missing_database_fails_cleanly ... ok
test_backup_offline_records_event_and_rotates ... ok
test_backup_reports_corrupt_neighbour ... ok
test_backup_while_process_owns_the_database ... ok
test_backup_with_unopenable_database_still_reports_error ... ok
test_default_backup_dir_is_used ... ok
test_export_and_secret_env ... ok
test_export_reports_redactions ... ok
test_os_errors_are_reported_not_traced ... ok
test_refusals_have_their_own_messages ... ok
test_restore_then_verify_everything ... ok
test_restore_without_live_db_prints_no_gap ... ok
test_runs_as_module_and_console_entry ... ok
test_verify_failures_and_usage_errors ... ok
test_verify_plain_database_prints_notes ... ok
Обязательная проверка среза: канареечный секрет не попадает в экспорт. ... ok
test_environment_secrets_are_removed_automatically ... ok
test_existing_output_is_never_overwritten ... ok
test_export_is_a_consistent_snapshot_taken_without_stopping_the_writer ... ok
Страховка: даже если редактирование что-то пропустило, готовый пакет не создаётся. ... ok
test_no_space_and_unreadable_database ... ok
test_package_content_and_manifest ... ok
test_write_failure_leaves_no_partial_file ... ok
test_bad_inputs ... ok
test_corrupted_zip_member_is_found ... ok
test_edits_are_found_by_manifest ... ok
test_good_package_passes ... ok
test_row_count_gaps_and_planted_secret_are_found ... ok
test_lost_range_only_when_live_journal_was_longer ... ok
test_replay_detects_inconsistent_restore_event ... ok
test_requires_a_writable_store_and_a_reason ... ok
test_second_restore_makes_epoch_three_and_replay_follows ... ok
test_store_without_events_is_refused ... ok
test_backup_listing_still_ok_after_restore ... ok
Проверка на отдельной копии (после возможной миграции формата) — отдельный рубеж. ... ok
test_copy_failure_before_the_store_opens_leaves_nothing ... ok
test_corrupt_backup_is_refused_before_any_write ... ok
test_existing_database_is_never_overwritten ... ok
test_failed_check_after_marking_leaves_nothing ... ok
test_not_enough_space_is_refused_before_any_write ... ok
test_reason_is_required ... ok
test_restore_over_the_live_database_is_refused ... ok
Копия цела по sha256 (манифест подогнан), но журнал не сходится со снимками: отказ до записи. ... ok
test_unreadable_live_database_only_loses_the_gap_information ... ok
Запрос, выданный после копии, в восстановленном журнале не существует: ответ ничего не меняет. ... ok
test_database_without_events_cannot_be_restored ... ok
test_live_database_is_untouched ... ok
Ответ модели, выданный до отката, устаревает по эпохе, даже если номер версии совпал. ... ok
Были только входящие, переходов состояния нет: линия снимков начинается с (1, 0), затем (2, 0). ... ok
test_restored_copy_is_checked_before_and_after ... ok
test_scenario_restore_backup ... ok
test_env_is_read_from_os_environ_by_default ... ok
test_env_secrets_by_name_and_length ... ok
test_redact_text_and_structures ... ok
test_assignments_can_be_disabled_for_repository_scan ... ok
test_canary_forms_raw_json_escaped_and_base64 ... ok
test_each_pattern_is_found ... ok
test_mask_never_shows_the_secret ... ok
test_ordinary_text_is_not_flagged ... ok
test_overlapping_findings_merge ... ok
test_partially_overlapping_findings_extend_the_first ... ok
test_short_canary_is_refused ... ok
test_telegram_token_inside_a_bot_api_url_and_russian_words ... ok
R-219: сканер проходит по файлам репозитория (шаблоны высокой точности) — секретов нет. ... ok
test_copy_failure_of_any_kind_rejects_migration ... ok
test_copy_is_made_before_migration_and_describes_it ... ok
test_migration_without_a_fresh_copy_is_rejected ... ok
test_open_for_write_copies_only_when_migration_is_pending ... ok
test_pre_migration_rotation_is_separate_and_keeps_three ... ok
test_backup_event_is_recorded_by_the_controller_after_the_copy ... ok
test_corrupt_snapshot_is_detected_and_not_counted ... ok
test_default_backup_dir ... ok
test_disk_full_during_copy_is_passed_through_and_cleaned ... ok
test_failed_snapshot_leaves_directory_untouched ... ok
test_failure_after_naming_removes_the_half_made_snapshot ... ok
test_fresh_snapshot_survives_rotation_even_if_the_clock_went_back ... ok
test_keep_parameter_and_bad_arguments ... ok
test_no_space_means_no_snapshot_and_no_changes ... ok
test_other_damages_are_detected ... ok
test_paths_with_uri_special_characters ... ok
R-201: после четвёртого снимка остаются три последних. ... ok
Писатель держит замок и продолжает жить; копия — отдельный самодостаточный файл. ... ok
test_snapshot_is_verified_and_described_by_manifest ... ok
test_snapshot_of_empty_database_is_refused ... ok
test_stale_partials_are_swept ... ok
test_database_without_persona_is_reported ... ok
test_healthy_database_passes_even_with_a_live_writer ... ok
test_integrity_check_findings_are_reported ... ok
test_missing_file_and_garbage ... ok
test_newer_format_is_reported ... ok
test_older_format_is_a_note_not_an_error ... ok
test_page_level_corruption_is_found ... ok
test_store_that_cannot_be_opened_is_reported ... ok
База цела для SQLite, но снимок не совпадает с журналом (правка в обход ядра). ... ok
Настоящий SQLITE_FULL: лимит страниц SQLite (имитация заполненного диска). Большая запись откатывается ... ok
test_enough_space_runs_the_call_normally ... ok
test_foreign_errors_inside_stop_are_raised ... ok
test_low_space_before_write_stops_the_session_and_keeps_the_database ... ok
test_other_errors_are_not_swallowed ... ok
Места нет даже на запись остановки: откат, ничего не записано, исключение «нет места» проглочено. ... ok
test_stop_on_low_space_for_stopped_and_paused_sessions ... ok
test_default_reserve_and_validation ... ok
test_ensure_requires_reserve_plus_need ... ok
test_is_disk_full ... ok
test_nonexistent_path_checks_nearest_existing_parent ... ok
Ran 105 tests in 4.695s
OK

## `python3 tools/covcheck.py` (фрагмент: модули среза)
```
Ran 512 tests in 65.404s
OK
src/iam/backup/__init__.py                     100.0%   100.0%  ok [критичный]
src/iam/backup/errors.py                       100.0%   100.0%  ok [критичный]
src/iam/backup/export.py                       100.0%   100.0%  ok [критичный]
src/iam/backup/restore.py                      100.0%   100.0%  ok [критичный]
src/iam/backup/scan.py                         100.0%   100.0%  ok [критичный]
src/iam/backup/snapshots.py                    100.0%   100.0%  ok [критичный]
src/iam/backup/space.py                        100.0%   100.0%  ok [критичный]
src/iam/cli.py                                 100.0%   100.0%  ok
src/iam/state/replay.py                        100.0%   100.0%  ok [критичный]
src/iam/state/restore.py                       100.0%   100.0%  ok [критичный]
src/iam/storage/db.py                          100.0%   100.0%  ok [критичный]
Модулей: 36, ниже порога: 0.
```
