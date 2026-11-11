#!/usr/bin/env bash
# Installs the gateway service and worker files. Run once with: sudo bash install/install.sh
# Does NOT need or create any Anthropic key (see install/set-key.sh).
set -euo pipefail
[ "$(id -u)" = 0 ] || { echo "run with sudo"; exit 1; }
id aiworker >/dev/null 2>&1 || { echo "user aiworker is missing"; exit 1; }
REPO="$(cd "$(dirname "$0")/.." && pwd)"

id aigateway >/dev/null 2>&1 || adduser --system --group --no-create-home --shell /usr/sbin/nologin aigateway

install -d -m 755 /etc/ai-company /opt/ai-company
# Socket lives in /srv (NOT /run or /etc): rootless Docker copies those at start and would not see it.
install -d -o aigateway -g aiworker -m 750 /srv/ai-gateway
if [ ! -f /etc/ai-company/admin.token ]; then
  (umask 077; openssl rand -hex 32 > /etc/ai-company/admin.token)
fi
chown aigateway:aiworker /etc/ai-company/admin.token
chmod 640 /etc/ai-company/admin.token

if [ ! -f /etc/ai-company/projects.json ]; then
  printf '{"alpha": {"uid": 20001}, "beta": {"uid": 20002}}\n' > /etc/ai-company/projects.json
fi
chmod 644 /etc/ai-company/projects.json

rm -rf /opt/ai-company/gateway /opt/ai-company/worker /opt/ai-company/image /opt/ai-company/tests
cp -r "$REPO/gateway" "$REPO/worker" "$REPO/image" "$REPO/tests" /opt/ai-company/
chown -R root:root /opt/ai-company
chmod -R go-w /opt/ai-company
chmod -R a+rX /opt/ai-company

cat > /etc/systemd/system/ai-gateway.service <<'UNIT'
[Unit]
Description=Claude Gateway (Phase 0)
After=network-online.target

[Service]
User=aigateway
Group=aiworker
ExecStart=/usr/bin/python3 /opt/ai-company/gateway/gateway.py
StateDirectory=ai-gateway
StateDirectoryMode=0700
Environment=GW_SOCKET=/srv/ai-gateway/gw.sock
Environment=GW_MAX_TASK_CAP=2.0
Environment=GW_DAILY_CAP=5.0
Restart=on-failure
NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome=yes
PrivateTmp=yes
ReadWritePaths=/srv/ai-gateway
ProtectKernelTunables=yes
ProtectControlGroups=yes

[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable ai-gateway.service
systemctl restart ai-gateway.service
sleep 1
systemctl is-active ai-gateway.service
echo "installed. Gateway is running WITHOUT an API key (it answers 503 to model calls)."
