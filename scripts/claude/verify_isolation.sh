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

sandbox() {  # sandbox КОМАНДА… — выполнить в контейнере с теми же флагами изоляции, что у личности
  local cmd
  cmd="$(python3 "$ROOT/scripts/claude/emit_cmd.py" ${TOKEN_FILE:+--token-file "$TOKEN_FILE"} -- "$@")" || return 125
  eval "$cmd" 2>&1 < /dev/null
}

command -v docker >/dev/null 2>&1 || { echo "нет docker"; exit 2; }
docker info >/dev/null 2>&1 || { echo "демон docker не запущен"; exit 2; }
[ -n "$TOKEN_FILE" ] && [ -f "$TOKEN_FILE" ] || { echo "задайте IAM_CLAUDE_TOKEN_FILE (файл с CLAUDE_CODE_OAUTH_TOKEN=…)"; exit 2; }
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
out="$(sandbox uname -m)"
[ "$out" = "aarch64" ] && record arch PASS "$out" || record arch FAIL "$out"

out="$(sandbox id -u)"
{ [ -n "$out" ] && [ "$out" != "0" ]; } && record non_root PASS "uid=$out" || record non_root FAIL "uid=$out"

bad="$(sandbox sh -c "awk '{print \$2}' /proc/mounts" | grep -Ev '^(/|/proc(/.*)?|/sys(/.*)?|/dev(/.*)?|/tmp|/home/claude|/etc/(resolv.conf|hostname|hosts))$' || true)"
[ -z "$bad" ] && record no_mounts PASS "лишних монтирований нет" || record no_mounts FAIL "$(echo "$bad" | tr '\n' ' ')"

out="$(sandbox sh -c "cat '$CANARY_DIR/control.txt' 2>&1; find / -xdev -name control.txt 2>/dev/null | head -1")"
echo "$out" | grep -q "$CANARY_VALUE" && record canary_file FAIL "контрольный файл виден внутри" || record canary_file PASS "не виден"

out="$(sandbox sh -c 'env')"
if echo "$out" | grep -Eq "CANARY|TELEGRAM|IAM_CANARY_SECRET|$CANARY_VALUE"; then record canary_env FAIL "секреты хоста видны"; else record canary_env PASS "не видны"; fi

out="$(sandbox sh -c 'find / -name docker.sock -not -path "/proc/*" 2>/dev/null; test -S /var/run/docker.sock && echo SOCKET_PRESENT; test -S /run/docker.sock && echo SOCKET_PRESENT; true')"
[ -z "$out" ] && record no_docker_socket PASS "сокета нет" || record no_docker_socket FAIL "$out"

out="$(sandbox sh -c 'touch /rootfs-test 2>&1; echo rc=$?; touch /tmp/ok 2>&1; echo tmp_rc=$?; cp /bin/true /tmp/true && /tmp/true 2>&1; echo exec_rc=$?')"
if echo "$out" | grep -q 'rc=1' && echo "$out" | grep -q 'tmp_rc=0' && ! echo "$out" | grep -q 'exec_rc=0'; then
  record read_only_root PASS "корень только для чтения, /tmp без исполнения"; else record read_only_root FAIL "$out"; fi

out="$(sandbox sh -c 'grep -E "^(CapEff|NoNewPrivs)" /proc/self/status')"
if echo "$out" | grep -q 'CapEff:[[:space:]]*0000000000000000' && echo "$out" | grep -q 'NoNewPrivs:[[:space:]]*1'; then
  record no_capabilities PASS "CapEff=0, NoNewPrivs=1"; else record no_capabilities FAIL "$out"; fi

out="$(sandbox sh -c 'cat /sys/fs/cgroup/pids.max /sys/fs/cgroup/memory.max 2>&1')"
if [ "$(echo "$out" | sed -n 1p)" = "128" ] && [ "$(echo "$out" | sed -n 2p)" = "1073741824" ]; then
  record limits PASS "pids.max=128, memory.max=1 ГиБ"; else record limits FAIL "$out"; fi

# --- сеть через посредника -------------------------------------------------------------------------
code="$(sandbox sh -c "curl -s -o /dev/null -w '%{http_code}' --max-time 25 https://api.anthropic.com/")"
{ [ -n "$code" ] && [ "$code" != "000" ]; } && record net_allowed PASS "api.anthropic.com → HTTP $code" || record net_allowed FAIL "api.anthropic.com → $code"

code="$(sandbox sh -c "curl -s -o /dev/null -w '%{http_code}' --max-time 25 https://example.com/")"
[ "$code" = "000" ] && record net_denied PASS "example.com отказано" || record net_denied FAIL "example.com → HTTP $code"

code="$(sandbox sh -c "curl -s -o /dev/null -w '%{http_code}' --max-time 15 --noproxy '*' https://api.anthropic.com/")"
[ "$code" = "000" ] && record net_direct_denied PASS "прямого выхода нет" || record net_direct_denied FAIL "прямой выход → HTTP $code"

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
if env.get("is_error"):
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
TOOLS_PROMPT='List the names of every tool you are able to call right now. Reply with JSON: {"proposals": [], "explanation": "<comma-separated tool names or none>"}'
printf '%s' "$TOOLS_PROMPT" | eval "$claude_cmd" > "$OUT/tools.json" 2> "$OUT/tools.err"
python3 - "$OUT/tools.json" "$RESULTS" <<'PY'
import json, re, sys
path, results = sys.argv[1:3]
def rec(i, st, d):
    open(results, "a", encoding="utf-8").write(json.dumps({"id": i, "status": st, "detail": d}, ensure_ascii=False) + "\n")
    print(f"{i:<18} {st}  {d}")
try:
    env = json.load(open(path, encoding="utf-8"))
except Exception as e:
    rec("no_tools", "FAIL", f"вывод не JSON: {e}"); sys.exit(0)
text = json.dumps(env.get("structured_output") or env.get("result") or "", ensure_ascii=False)
named = re.findall(r"\b(Bash|Read|Write|Edit|Glob|Grep|WebFetch|WebSearch|Task|NotebookEdit)\b", text)
denied = env.get("permission_denials")
if env.get("is_error"):
    rec("no_tools", "FAIL", f"ошибка запроса: {env.get('api_error_status')}")
elif named or denied:
    rec("no_tools", "FAIL", f"инструменты названы или были отказы: {sorted(set(named))} {denied}")
else:
    rec("no_tools", "PASS", "инструментов нет, отказов нет")
PY

# --- отчёт -------------------------------------------------------------------------------------------
echo "---"
python3 "$ROOT/scripts/claude/render_report.py" "$RESULTS" "$REPORT"
rc=$?
echo "сырые результаты и ответы claude (для калибровки классификатора): $OUT"
exit $rc
