#!/usr/bin/env bash
# E4: database isolation between tasks. NO API key needed. Run as aiworker:
#   sudo machinectl shell aiworker@   then:   bash /opt/ai-company/tests/e4.sh
# Idea: every task gets its OWN temporary database on its OWN private (internal) network.
# Task A must reach its own DB, and must NOT reach task B's DB (by name or by IP).
set -u
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
export DOCKER_HOST="${DOCKER_HOST:-unix://$XDG_RUNTIME_DIR/docker.sock}"
P=0; F=0
ok(){ echo "PASS $1"; P=$((P+1)); }
bad(){ echo "FAIL $1"; F=$((F+1)); }

NA=e4-net-a; NB=e4-net-b; DA=e4-db-a; DB=e4-db-b
cleanup(){
  docker rm -f -v "$DA" "$DB" >/dev/null 2>&1
  docker volume rm e4-vol-a e4-vol-b >/dev/null 2>&1
  docker network rm "$NA" "$NB" >/dev/null 2>&1
}
trap cleanup EXIT
cleanup

PW_A=$(openssl rand -hex 12); PW_B=$(openssl rand -hex 12)
echo "== create two private networks and two temporary databases"
docker network create --internal "$NA" >/dev/null && docker network create --internal "$NB" >/dev/null || { echo "network create failed"; exit 1; }
for x in a b; do
  N=e4-net-$x; D=e4-db-$x; if [ $x = a ]; then PW=$PW_A; else PW=$PW_B; fi
  docker run -d --name "$D" --network "$N" --network-alias db \
    --cap-drop=ALL --cap-add=SETUID --cap-add=SETGID --cap-add=CHOWN --cap-add=DAC_OVERRIDE \
    --security-opt no-new-privileges --memory 512m --pids-limit 200 -v e4-vol-$x:/var/lib/mysql \
    -e MARIADB_ROOT_PASSWORD="$PW" -e MARIADB_DATABASE=app mariadb:11 >/dev/null || { echo "db start failed ($x)"; exit 1; }
done

PROBE() { # network, python code
  docker run --rm --network "$1" --user 20001:20001 --cap-drop=ALL --read-only \
    --security-opt no-new-privileges --entrypoint python ai-task:latest -c "$2" 2>&1
}

echo "== wait for database A to accept connections (up to 60s)"
UP=no
for i in $(seq 1 30); do
  R=$(PROBE "$NA" 'import socket;s=socket.create_connection(("db",3306),3);print("OPEN")')
  if echo "$R" | grep -q OPEN; then UP=yes; break; fi
  sleep 2
done
[ "$UP" = yes ] && ok "E4a task A reaches ITS OWN database" || bad "E4a task A cannot reach its own database ($R)"

IPB=$(docker inspect -f '{{(index .NetworkSettings.Networks "'$NB'").IPAddress}}' "$DB")
echo "(database B address: $IPB)"
R=$(PROBE "$NA" 'import socket
try:
    socket.create_connection(("'$IPB'",3306),4);print("REACHED")
except Exception as e:
    print("BLOCKED",type(e).__name__)')
echo "$R" | grep -q BLOCKED && ok "E4b task A cannot reach database B by IP" || bad "E4b task A reached database B by IP ($R)"

R=$(PROBE "$NA" 'import socket
try:
    socket.getaddrinfo("'$DB'",3306);print("RESOLVED")
except Exception as e:
    print("NOT-RESOLVED")')
echo "$R" | grep -q NOT-RESOLVED && ok "E4c task A cannot even resolve database B's name" || bad "E4c name of database B is visible from task A ($R)"

R=$(ss -ltn 2>/dev/null | grep -E ':3306\b' || true)
[ -z "$R" ] && ok "E4d database is not published on the host" || bad "E4d port 3306 is open on host: $R"

echo "== cleanup removes databases and their data"
cleanup
LEFT=$(docker ps -a --format '{{.Names}}' | grep '^e4-' || true)
VOLS=$(docker volume ls -q | grep -c "^e4-vol" || true)
[ -z "$LEFT" ] && ok "E4e no leftover database containers" || bad "E4e leftovers: $LEFT"
[ "$VOLS" = 0 ] && ok "E4f no leftover data volumes" || bad "E4f $VOLS volume(s) left behind"

echo "RESULT: $P passed, $F failed"
[ "$F" = 0 ]
