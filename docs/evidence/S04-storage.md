# Свидетельство S04: хранилище, домен, покрытие

Снято командами ниже в сессии S04 (2026-10-11T19:58Z) на чистом рабочем дереве, коммит c8987f7. Python 3.13.16, SQLite 3.45.1, coverage.py 7.10.6.

## `cd tests && python3 -m unittest storage.test_crash_lock storage.test_journal storage.test_migrations -v`
код возврата: 0
```
test_exception_mid_transaction_rolls_back (storage.test_crash_lock.CrashTest.test_exception_mid_transaction_rolls_back) ... ok
test_kill9_mid_transaction_leaves_no_partial_writes (storage.test_crash_lock.CrashTest.test_kill9_mid_transaction_leaves_no_partial_writes) ... ok
test_create_while_locked_is_refused (storage.test_crash_lock.LockTest.test_create_while_locked_is_refused) ... ok
test_lock_object (storage.test_crash_lock.LockTest.test_lock_object) ... ok
test_same_process_second_writer_refused (storage.test_crash_lock.LockTest.test_same_process_second_writer_refused) ... ok
test_second_process_is_refused_until_first_dies (storage.test_crash_lock.LockTest.test_second_process_is_refused_until_first_dies) ... ok
test_symlink_to_same_base_shares_lock (storage.test_crash_lock.LockTest.test_symlink_to_same_base_shares_lock) ... ok
test_archive_layer_immutable_personal_mutable (storage.test_journal.AppendOnlyTest.test_archive_layer_immutable_personal_mutable) ... ok
test_clock_monotonic (storage.test_journal.AppendOnlyTest.test_clock_monotonic) ... ok
test_inbox_immutable (storage.test_journal.AppendOnlyTest.test_inbox_immutable) ... ok
test_meta_identity_fixed (storage.test_journal.AppendOnlyTest.test_meta_identity_fixed) ... ok
test_outbox_status_machine (storage.test_journal.AppendOnlyTest.test_outbox_status_machine) ... ok
test_raw_connection_without_api_is_still_guarded_by_triggers (storage.test_journal.AppendOnlyTest.test_raw_connection_without_api_is_still_guarded_by_triggers) ... ok
test_replace_and_upsert_cannot_bypass (storage.test_journal.AppendOnlyTest.test_replace_and_upsert_cannot_bypass) ... ok
test_schema_changes_denied_on_core_connections (storage.test_journal.AppendOnlyTest.test_schema_changes_denied_on_core_connections) ... ok
test_search_index_written_only_by_triggers (storage.test_journal.AppendOnlyTest.test_search_index_written_only_by_triggers) ... ok
test_seq_gap_and_foreign_persona_rejected (storage.test_journal.AppendOnlyTest.test_seq_gap_and_foreign_persona_rejected) ... ok
test_simulation_never_in_permanent_store (storage.test_journal.AppendOnlyTest.test_simulation_never_in_permanent_store) ... ok
test_snapshots_form_one_line (storage.test_journal.AppendOnlyTest.test_snapshots_form_one_line)
R-024: версии внутри эпохи только растут; эпоха растёт на 1 (восстановление копии), не назад. ... ok
test_snapshots_immutable (storage.test_journal.AppendOnlyTest.test_snapshots_immutable) ... ok
test_update_and_delete_on_events_rejected (storage.test_journal.AppendOnlyTest.test_update_and_delete_on_events_rejected) ... ok
test_concurrent_migrator_does_not_apply_twice (storage.test_migrations.MigrateTest.test_concurrent_migrator_does_not_apply_twice) ... ok
test_empty_to_v1_and_rerun_is_noop (storage.test_migrations.MigrateTest.test_empty_to_v1_and_rerun_is_noop) ... ok
test_failed_migration_rolls_back_whole_step (storage.test_migrations.MigrateTest.test_failed_migration_rolls_back_whole_step) ... ok
test_filled_base_survives_rerun_and_next_migration (storage.test_migrations.MigrateTest.test_filled_base_survives_rerun_and_next_migration) ... ok
test_foreign_database_refused (storage.test_migrations.MigrateTest.test_foreign_database_refused) ... ok
test_hook_not_called_for_fresh_base (storage.test_migrations.MigrateTest.test_hook_not_called_for_fresh_base) ... ok
test_readonly_open_requires_current_format (storage.test_migrations.MigrateTest.test_readonly_open_requires_current_format) ... ok
test_target_limits_migrations (storage.test_migrations.MigrateTest.test_target_limits_migrations) ... ok
test_too_new_refused (storage.test_migrations.MigrateTest.test_too_new_refused) ... ok

----------------------------------------------------------------------
Ran 30 tests in 8.082s

OK
```

## `python3 -m unittest discover -s tests`
код возврата: 0
```
Ran 255 tests in 14.839s

OK
```

## `python3 tools/covcheck.py`
код возврата: 0
```
модуль                                         строки    ветви  итог
src/iam/__init__.py                            100.0%   100.0%  ok
src/iam/domain/__init__.py                     100.0%   100.0%  ok
src/iam/domain/catalog.py                      100.0%   100.0%  ok
src/iam/domain/ids.py                          100.0%   100.0%  ok
src/iam/domain/jsonschema.py                    98.8%    98.5%  ok
src/iam/domain/types.py                         99.3%    92.9%  ok
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
Модулей: 17, ниже порога: 0.
```

## `python3 tools/reqs.py validate`
код возврата: 0
```
OK: 223 требований; все непустые строки PLAN.md (344) покрыты требованиями или пропусками
```
