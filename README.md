# ai-company-core

Phase 0 core: Claude Gateway + task worker. Validation only; no production use.

- gateway/  Claude Gateway: holds the real API key, scoped task tokens, cost caps, SQLite ledger
- worker/   Task worker: per-task internal network, key-less sidecar, hardened container, redacted logs
- image/    Task image (Claude Agent SDK) and an in-container security probe
- install/  install.sh (service + files), set-key.sh (silent key entry, never echoed)
- tests/    phase0.sh: server-side tests that need no API key (E3, E5, E8, cleanup)

Local gateway test (mock upstream, no real API call): `python3 gateway/test_gateway.py`
