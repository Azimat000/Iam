#!/usr/bin/env bash
# Проверка изоляции и входа исполнителя Claude Code на Mac (барьер G11, R-119). Запускает ВАШ docker.
#
#   docker build --platform linux/arm64 -t iam-claude:1 docker/
#   docker compose -f docker/compose.yaml up -d
#   IAM_CLAUDE_TOKEN_FILE=$HOME/.iam-secrets/claude.env bash scripts/claude/verify_isolation.sh
#
# Файл токена: одна строка CLAUDE_CODE_OAUTH_TOKEN=… (получить: `claude setup-token`), права 600, ВНЕ репозитория.
# Скрипт кладёт на хост контрольный файл и контрольные секреты и убеждается, что внутри контейнера их нет; проверяет
# флаги, лимиты, посредника и живой вход; пишет результаты и собирает docs/evidence/claude-isolation.md.
# Код возврата 0 — все обязательные проверки пройдены. Проверен только `bash -n` (в облаке нет Docker).
set -u
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
OUT="${IAM_VERIFY_OUT:-$(mktemp -d "${TMPDIR:-/tmp}/iam-verify.XXXXXX")}"
mkdir -p "$OUT"
TOKEN_FILE="${IAM_CLAUDE_TOKEN_FILE:-}"
RESULTS="$OUT/results.jsonl"
REPORT="${IAM_VERIFY_REPORT:-$ROOT/docs/evidence/claude-isolation.md}"
: > "$RESULTS"
export PYTHONPATH="$ROOT/src${PYTHONPATH:+:$PYTHONPATH}"

record() {  # record ID PASS|FAIL|SKIP "подробности"
  python3 - "$1" "$2" "$3" >> "$RESULTS" <<'PY'
import json, sys
print(json.dumps({"id": sys.argv[1], "status": sys.argv[2], "detail": sys.argv[3]}, ensure_ascii=False))
PY
  printf '%-18s %s  %s\n' "$1" "$2" "$3"
}

sb() {  # sb КОМАНДА… — выполнить в контейнере с теми же флагами изоляции, что у личности; SB_OUT и SB_RC — вывод и код docker
  local cmd
  if ! cmd="$(python3 "$ROOT/scripts/claude/emit_cmd.py" ${TOKEN_FILE:+--token-file "$TOKEN_FILE"} -- "$@")"; then
    SB_OUT="не удалось собрать команду"; SB_RC=125; return 0
  fi
  SB_OUT="$(eval "$cmd" 2>&1 < /dev/null)"; SB_RC=$?
}
# Результат проверки засчитывается только если контейнер вообще выполнил команду (SB_RC=0): упавший docker не даёт ни PASS, ни ложных «секрета нет».
ran() { [ "$SB_RC" -eq 0 ]; }

command -v docker >/dev/null 2>&1 || { echo "нет docker"; exit 2; }
docker info >/dev/null 2>&1 || { echo "демон docker не запущен"; exit 2; }
[ -n "$TOKEN_FILE" ] && [ -f "$TOKEN_FILE" ] || { echo "задайте IAM_CLAUDE_TOKEN_FILE (файл с CLAUDE_CODE_OAUTH_TOKEN=…)"; exit 2; }
[ "$(grep -c . "$TOKEN_FILE")" = "1" ] && grep -Eq '^CLAUDE_CODE_OAUTH_TOKEN=.+$' "$TOKEN_FILE" \
  || { echo "в файле токена должна быть ровно одна строка CLAUDE_CODE_OAUTH_TOKEN=… (другие переменные не допускаются)"; exit 2; }
perm="$(stat -c '%a' "$TOKEN_FILE" 2>/dev/null || stat -f '%Lp' "$TOKEN_FILE" 2>/dev/null)"   # GNU, затем BSD/macOS
[ "$perm" = "600" ] || { echo "права файла токена должны быть 600, сейчас $perm"; exit 2; }
case "$TOKEN_FILE" in "$ROOT"/*) echo "файл токена не должен лежать в репозитории"; exit 2;; esac

# --- контрольные данные хоста: внутри контейнера их быть не должно ------------------------------------
CANARY_DIR="$OUT/canary"; mkdir -p "$CANARY_DIR"
CANARY_VALUE="iam-canary-$(date +%s)-$RANDOM"
printf '%s\n' "$CANARY_VALUE" > "$CANARY_DIR/control.txt"
export TELEGRAM_BOT_TOKEN="000000000:CANARY-NOT-A-REAL-TOKEN-$CANARY_VALUE"
export IAM_CANARY_SECRET="$CANARY_VALUE"

# --- устройство контейнера -------------------------------------------------------------------------
sb uname -m
ran && [ "$SB_OUT" = "aarch64" ] && record arch PASS "$SB_OUT" || record arch FAIL "rc=$SB_RC $SB_OUT"

sb id -u
ran && [[ "$SB_OUT" =~ ^[0-9]+$ ]] && [ "$SB_OUT" != "0" ] && record non_root PASS "uid=$SB_OUT" || record non_root FAIL "rc=$SB_RC $SB_OUT"

sb sh -c "awk '{print \$2}' /proc/mounts"
bad="$(echo "$SB_OUT" | grep -Ev '^(/|/proc(/.*)?|/sys(/.*)?|/dev(/.*)?|/tmp|/home/claude|/etc/(resolv.conf|hostname|hosts))$' || true)"
ran && [ -n "$SB_OUT" ] && [ -z "$bad" ] && record no_mounts PASS "лишних монтирований нет" || record no_mounts FAIL "rc=$SB_RC $(echo "$bad" | tr '\n' ' ')"

sb sh -c "cat '$CANARY_DIR/control.txt' 2>&1; find / -xdev -name control.txt 2>/dev/null | head -1; true"
ran && ! echo "$SB_OUT" | grep -q "$CANARY_VALUE" && record canary_file PASS "не виден" || record canary_file FAIL "rc=$SB_RC контрольный файл виден или контейнер не запустился"

sb sh -c 'env'
ran && [ -n "$SB_OUT" ] && ! echo "$SB_OUT" | grep -Eq "CANARY|TELEGRAM|IAM_CANARY_SECRET|$CANARY_VALUE" && record canary_env PASS "не видны" || record canary_env FAIL "rc=$SB_RC секреты хоста видны или контейнер не запустился"

sb sh -c 'echo started; find / -name docker.sock -not -path "/proc/*" 2>/dev/null; test -S /var/run/docker.sock && echo SOCKET_PRESENT; test -S /run/docker.sock && echo SOCKET_PRESENT; true'
ran && [ "$SB_OUT" = "started" ] && record no_docker_socket PASS "сокета нет" || record no_docker_socket FAIL "rc=$SB_RC $SB_OUT"

sb sh -c 'touch /rootfs-test 2>/dev/null; echo ro=$?; touch /tmp/ok 2>/dev/null; echo tmp=$?; cp /bin/true /tmp/true 2>/dev/null; /tmp/true >/dev/null 2>&1; echo exec=$?'
if ran && echo "$SB_OUT" | grep -qx 'ro=1' && echo "$SB_OUT" | grep -qx 'tmp=0' && ! echo "$SB_OUT" | grep -qx 'exec=0'; then
  record read_only_root PASS "корень только для чтения, /tmp без исполнения"; else record read_only_root FAIL "rc=$SB_RC $(echo "$SB_OUT" | tr '\n' ' ')"; fi

sb sh -c 'grep -E "^(CapEff|CapBnd|NoNewPrivs)" /proc/self/status'
if ran && echo "$SB_OUT" | grep -Eq 'CapEff:[[:space:]]*0+$' && echo "$SB_OUT" | grep -Eq 'CapBnd:[[:space:]]*0+$' && echo "$SB_OUT" | grep -Eq 'NoNewPrivs:[[:space:]]*1$'; then
  record no_capabilities PASS "CapEff=0, CapBnd=0, NoNewPrivs=1"; else record no_capabilities FAIL "rc=$SB_RC $(echo "$SB_OUT" | tr '\n' ' ')"; fi

sb sh -c 'cat /sys/fs/cgroup/pids.max /sys/fs/cgroup/memory.max 2>&1'
if ran && [ "$(echo "$SB_OUT" | sed -n 1p)" = "128" ] && [ "$(echo "$SB_OUT" | sed -n 2p)" = "1073741824" ]; then
  record limits PASS "pids.max=128, memory.max=1 ГиБ"; else record limits FAIL "rc=$SB_RC $SB_OUT"; fi

# --- сеть через посредника -------------------------------------------------------------------------
internal="$(docker network inspect -f '{{.Internal}}' iam-sandbox 2>&1)"
[ "$internal" = "true" ] && record sandbox_internal PASS "iam-sandbox: internal=true" || record sandbox_internal FAIL "iam-sandbox: $internal"

sb sh -c "curl -s -o /dev/null -w '%{http_code}' --max-time 25 https://api.anthropic.com/ || true"
ran && [[ "$SB_OUT" =~ ^[0-9]{3}$ ]] && [ "$SB_OUT" != "000" ] && record net_allowed PASS "api.anthropic.com → HTTP $SB_OUT" || record net_allowed FAIL "api.anthropic.com → rc=$SB_RC $SB_OUT"

sb sh -c "curl -s -o /dev/null -w '%{http_code}' --max-time 25 https://example.com/ || true"
ran && [ "$SB_OUT" = "000" ] && record net_denied PASS "example.com отказано" || record net_denied FAIL "example.com → rc=$SB_RC $SB_OUT"

sb sh -c "curl -s -o /dev/null -w '%{http_code}' --max-time 15 --noproxy '*' https://api.anthropic.com/ || true"
ran && [ "$SB_OUT" = "000" ] && record net_direct_denied PASS "прямого выхода нет" || record net_direct_denied FAIL "прямой выход → rc=$SB_RC $SB_OUT"

# --- живой вход и формат ответа ----------------------------------------------------------------------
claude_cmd="$(python3 "$ROOT/scripts/claude/emit_cmd.py" --token-file "$TOKEN_FILE" --claude)"
PROMPT='{"purpose": "choose_action", "context": [{"ref": "evt_AAAAAAAA", "origin": "observation", "layer": "state", "text": "Проверка связи."}], "allowed_proposals": ["no_action"], "allowed_actions": [], "max_output_tokens": 200}'
printf '%s' "$PROMPT" | eval "$claude_cmd" > "$OUT/live.json" 2> "$OUT/live.err"
python3 - "$OUT/live.json" "$RESULTS" <<'PY'
import json, sys
path, results = sys.argv[1:3]
def rec(i, st, d):
    open(results, "a", encoding="utf-8").write(json.dumps({"id": i, "status": st, "detail": d}, ensure_ascii=False) + "\n")
    print(f"{i:<18} {st}  {d}")
try:
    env = json.load(open(path, encoding="utf-8"))
except Exception as e:
    rec("auth", "FAIL", f"вывод не JSON: {e}"); rec("format", "FAIL", "нет вывода"); sys.exit(0)
meta = {k: env.get(k) for k in ("is_error", "api_error_status", "terminal_reason", "num_turns")}
if env.get("is_error") or env.get("api_error_status"):
    rec("auth", "FAIL", f"ошибка: {meta}"); rec("format", "SKIP", "нет ответа")
else:
    rec("auth", "PASS", f"вход по подписке работает: {meta}")
    data = env.get("structured_output")
    if not isinstance(data, dict):
        try:
            data = json.loads(env.get("result", ""))
        except Exception:
            data = None
    ok = isinstance(data, dict) and isinstance(data.get("proposals"), list)
    rec("format", "PASS" if ok else "FAIL", "proposals есть" if ok else "нет объекта с proposals")
PY
# Список инструментов берём не из слов модели, а из события init самого claude (stream-json): это то, что реально доступно.
stream_cmd="$(python3 "$ROOT/scripts/claude/emit_cmd.py" --token-file "$TOKEN_FILE" --claude --stream)"
printf '%s' 'Reply with the single word: ok' | eval "$stream_cmd" > "$OUT/init.jsonl" 2> "$OUT/init.err"
python3 - "$OUT/init.jsonl" "$RESULTS" <<'PY'
import json, sys
path, results = sys.argv[1:3]
def rec(i, st, d):
    open(results, "a", encoding="utf-8").write(json.dumps({"id": i, "status": st, "detail": d}, ensure_ascii=False) + "\n")
    print(f"{i:<18} {st}  {d}")
init = None
for line in open(path, encoding="utf-8", errors="replace"):
    try:
        ev = json.loads(line)
    except ValueError:
        continue
    if isinstance(ev, dict) and ev.get("type") == "system" and ev.get("subtype") == "init":
        init = ev
        break
if init is None:
    rec("no_tools", "FAIL", "событие init не найдено: список инструментов не получен")
elif init.get("tools"):
    rec("no_tools", "FAIL", f"у модели есть инструменты: {init['tools']}")
else:
    rec("no_tools", "PASS", "init: инструментов нет")
PY

# --- отчёт -------------------------------------------------------------------------------------------
echo "---"
python3 "$ROOT/scripts/claude/render_report.py" "$RESULTS" "$REPORT"
rc=$?
echo "сырые результаты и ответы claude (для калибровки классификатора): $OUT"
exit $rc
