#!/usr/bin/env bash
# Stores the Anthropic API key readable ONLY by the aigateway user.
# The key is typed silently (never shown, never in shell history). Run: sudo bash install/set-key.sh
set -euo pipefail
[ "$(id -u)" = 0 ] || { echo "run with sudo"; exit 1; }
read -rsp "Paste the Anthropic API key (hidden), then press Enter: " KEY
echo
case "$KEY" in sk-ant-*) ;; *) echo "That does not look like an Anthropic key (sk-ant-...). Nothing saved."; exit 1;; esac
(umask 077; printf '%s' "$KEY" > /etc/ai-company/anthropic.key)
chown aigateway:aigateway /etc/ai-company/anthropic.key
chmod 600 /etc/ai-company/anthropic.key
unset KEY
systemctl restart ai-gateway.service
sleep 1
systemctl is-active ai-gateway.service
ls -l /etc/ai-company/anthropic.key
