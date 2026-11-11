#!/usr/bin/env bash
# Phase 0 server tests that need NO real API key. Run as aiworker:
#   sudo machinectl shell aiworker@   then:   bash /opt/ai-company/tests/phase0.sh
set -u
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
export DOCKER_HOST="${DOCKER_HOST:-unix://$XDG_RUNTIME_DIR/docker.sock}"
W="python3 /opt/ai-company/worker/worker.py"
P=0; F=0
ok(){ echo "PASS $1"; P=$((P+1)); }
bad(){ echo "FAIL $1"; F=$((F+1)); }

echo "== build task image"
docker build -q -t ai-task:latest /opt/ai-company/image || { echo "image build failed"; exit 1; }

echo "== gateway health (via socket, as the worker user)"
curl -s --unix-socket /run/ai-gateway/gw.sock http://localhost/health | grep -q '"ok": true' && ok "gateway healthy" || bad "gateway health"

echo "== run the probe inside a hardened task container"
OUT=$($W run --project alpha --task p0a --cap 0.5 --cmd "python /opt/task/probe.py" --emit-token /tmp/p0a.tok 2>&1)
echo "$OUT" | grep -E '^(PASS|FAIL|PROBE|LEAKTEST)'
echo "$OUT" | grep -q '^FAIL' && bad "probe has failures" || ok "probe: no failures"

echo "== E8: secrets must not appear in saved logs"
if grep -qE 'tk_|sk-ant-' "$HOME/logs/p0a.log"; then bad "E8 token found in log"; else ok "E8 log is redacted"; fi
grep -q 'REDACTED' "$HOME/logs/p0a.log" && ok "E8 redaction marker present" || bad "E8 no redaction marker"

echo "== E8: env file removed after the task"
ls "$XDG_RUNTIME_DIR/tasks/" 2>/dev/null | grep -q p0a.env && bad "E8 env file left behind" || ok "E8 env file removed"

echo "== E5: token of a finished task is revoked"
CODE=$(curl -s -o /dev/null -w '%{http_code}' --unix-socket /run/ai-gateway/gw.sock \
  -H "Authorization: Bearer $(cat /tmp/p0a.tok)" -H 'content-type: application/json' \
  -d '{"model":"claude-haiku-5-5","max_tokens":5,"messages":[{"role":"user","content":"hi"}]}' http://localhost/v1/messages)
[ "$CODE" = "401" ] && ok "E5 revoked token -> 401" || bad "E5 revoked token returned $CODE"
rm -f /tmp/p0a.tok

echo "== E3: project beta task cannot see project alpha data"
OUT2=$($W run --project beta --task p0b --cap 0.5 --cmd "ls /work; cat /work/data.txt 2>&1; echo ---; ls /srv 2>&1" 2>&1)
echo "$OUT2" | grep -q "ALPHA-FAKE-DATA" && bad "E3 beta read alpha data" || ok "E3 beta cannot read alpha data"

echo "== cleanup check (no leftovers)"
LEFT=$(docker ps -a --format '{{.Names}}' | grep -E '^(task-|gw-)' || true)
[ -z "$LEFT" ] && ok "no leftover task containers" || bad "leftover containers: $LEFT"
NETS=$(docker network ls --format '{{.Name}}' | grep -E '^task-net-' || true)
[ -z "$NETS" ] && ok "no leftover task networks" || bad "leftover networks: $NETS"

echo "RESULT: $P passed, $F failed"
[ "$F" = 0 ]
