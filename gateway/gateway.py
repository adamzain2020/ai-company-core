#!/usr/bin/env python3
"""Claude Gateway (Phase 0).

Holds the real Anthropic API key. Task containers only ever hold a short-lived,
scoped task token. Every call is checked (token, path, model, cost cap), forwarded
with the real key injected, and its usage is written to a SQLite ledger.

Only the Python standard library is used. Request/response bodies and headers are
never logged.
"""
import hashlib
import hmac
import http.client
import json
import os
import secrets
import socketserver
import sqlite3
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlparse

# USD per million tokens (official pricing page, Nov 2026).
# Keys are model-family prefixes: a model id such as "claude-sonnet-5-5-2026xxxx" matches.
PRICES = {
    "claude-sonnet-5": dict(inp=2.0, out=10.0, cw=2.5, cr=0.10),
    "claude-opus-5": dict(inp=4.0, out=20.0, cw=5.0, cr=0.20),
    "claude-haiku-5": dict(inp=0.10, out=0.50, cw=0.125, cr=0.01),
}
# Haiku prompts above 100K tokens are billed at 0.50 in / 2.50 out (5x).
# ASSUMPTION: cache rates scale by the same factor. Verify at reconciliation (G4).
HAIKU_BIG_PROMPT = 100_000
HAIKU_BIG_FACTOR = 5.0

PROXY_PATHS = ("/v1/messages", "/v1/messages/count_tokens")


def load_conf():
    e = os.environ.get
    return dict(
        socket=e("GW_SOCKET", "/srv/ai-gateway/gw.sock"),
        db=e("GW_DB", "/var/lib/ai-gateway/ledger.db"),
        key_file=e("GW_KEY_FILE", "/etc/ai-company/anthropic.key"),
        admin_file=e("GW_ADMIN_FILE", "/etc/ai-company/admin.token"),
        upstream=e("GW_UPSTREAM", "https://api.anthropic.com"),
        max_task_cap=float(e("GW_MAX_TASK_CAP", "5.0")),
        daily_cap=float(e("GW_DAILY_CAP", "10.0")),
        max_ttl=int(e("GW_MAX_TTL", "7200")),
    )


def family(model):
    for fam in PRICES:
        if isinstance(model, str) and model.startswith(fam):
            return fam
    return None


def cost_usd(fam, usage):
    p = dict(PRICES[fam])
    inp = usage.get("input_tokens", 0) or 0
    out = usage.get("output_tokens", 0) or 0
    cr = usage.get("cache_read_input_tokens", 0) or 0
    cw_total = usage.get("cache_creation_input_tokens", 0) or 0
    if fam == "claude-haiku-5" and (inp + cr + cw_total) > HAIKU_BIG_PROMPT:
        p = {k: v * HAIKU_BIG_FACTOR for k, v in p.items()}
    cc = usage.get("cache_creation")
    if isinstance(cc, dict) and ("ephemeral_5m_input_tokens" in cc or "ephemeral_1h_input_tokens" in cc):
        w5 = cc.get("ephemeral_5m_input_tokens", 0) or 0
        w1 = cc.get("ephemeral_1h_input_tokens", 0) or 0
        cw_cost = w5 * p["cw"] + w1 * p["inp"] * 2.0  # 1h cache write = 2x input
    else:
        cw_cost = cw_total * p["cw"]
    return (inp * p["inp"] + out * p["out"] + cr * p["cr"] + cw_cost) / 1e6


def worst_case_usd(fam, body_len, max_tokens):
    """Pre-flight upper bound: ~1 token per 3 bytes of body at the plain input rate, plus max output."""
    p = PRICES[fam]
    if fam == "claude-haiku-5":
        p = {k: v * HAIKU_BIG_FACTOR for k, v in p.items()}
    return ((body_len / 3.0) * p["inp"] + (max_tokens or 0) * p["out"]) / 1e6


class Ledger:
    def __init__(self, path):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.lock = threading.Lock()
        with self.lock:
            self.db.executescript(
                """
                CREATE TABLE IF NOT EXISTS tokens(
                  hash TEXT PRIMARY KEY, project TEXT, task TEXT, role TEXT,
                  cap REAL, spent REAL DEFAULT 0, exp REAL, created REAL);
                CREATE TABLE IF NOT EXISTS calls(
                  ts REAL, hash TEXT, project TEXT, task TEXT, role TEXT, model TEXT,
                  input INTEGER, output INTEGER, cache_write INTEGER, cache_read INTEGER,
                  usd REAL, status INTEGER);
                """
            )
            self.db.commit()

    def issue(self, project, task, role, cap, ttl):
        tok = "tk_" + secrets.token_urlsafe(32)
        h = hashlib.sha256(tok.encode()).hexdigest()
        now = time.time()
        with self.lock:
            self.db.execute("INSERT INTO tokens VALUES(?,?,?,?,?,0,?,?)", (h, project, task, role, cap, now + ttl, now))
            self.db.commit()
        return tok, now + ttl

    def get(self, tok):
        h = hashlib.sha256(tok.encode()).hexdigest()
        with self.lock:
            r = self.db.execute("SELECT hash,project,task,role,cap,spent,exp FROM tokens WHERE hash=?", (h,)).fetchone()
        if not r:
            return None
        return dict(hash=r[0], project=r[1], task=r[2], role=r[3], cap=r[4], spent=r[5], exp=r[6])

    def revoke(self, task):
        with self.lock:
            n = self.db.execute("UPDATE tokens SET exp=0 WHERE task=?", (task,)).rowcount
            self.db.commit()
        return n

    def today_spent(self):
        start = time.time() - (time.time() % 86400)
        with self.lock:
            r = self.db.execute("SELECT COALESCE(SUM(usd),0) FROM calls WHERE ts>=?", (start,)).fetchone()
        return r[0]

    def record(self, t, model, usage, usd, status):
        with self.lock:
            self.db.execute(
                "INSERT INTO calls VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (time.time(), t["hash"], t["project"], t["task"], t["role"], model,
                 usage.get("input_tokens", 0) or 0, usage.get("output_tokens", 0) or 0,
                 usage.get("cache_creation_input_tokens", 0) or 0,
                 usage.get("cache_read_input_tokens", 0) or 0, usd, status))
            self.db.execute("UPDATE tokens SET spent=spent+? WHERE hash=?", (usd, t["hash"]))
            self.db.commit()

    def usage(self, task=None):
        q = ("SELECT project,task,role,model,COUNT(*),SUM(input),SUM(output),SUM(cache_write),SUM(cache_read),"
             "SUM(usd),SUM(status>=400) FROM calls ")
        a = ()
        if task:
            q += "WHERE task=? "
            a = (task,)
        q += "GROUP BY project,task,role,model"
        with self.lock:
            rows = self.db.execute(q, a).fetchall()
        keys = ["project", "task", "role", "model", "calls", "input", "output", "cache_write", "cache_read", "usd", "errors"]
        return [dict(zip(keys, r)) for r in rows]


class Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True


def make_server(conf):
    ledger = Ledger(conf["db"])
    key = open(conf["key_file"]).read().strip() if os.path.exists(conf["key_file"]) else ""
    admin = open(conf["admin_file"]).read().strip()
    up = urlparse(conf["upstream"])

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):  # never log headers or bodies
            pass

        def reply(self, code, obj):
            b = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)

        def err(self, code, kind, msg):
            self.reply(code, {"type": "error", "error": {"type": kind, "message": msg}})

        def is_admin(self):
            got = self.headers.get("x-admin-secret", "")
            return hmac.compare_digest(got.encode(), admin.encode())

        def body(self):
            n = int(self.headers.get("content-length", 0) or 0)
            return self.rfile.read(n) if n else b""

        # ---- admin + proxy -------------------------------------------------
        def do_GET(self):
            u = urlparse(self.path)
            if u.path == "/health":
                return self.reply(200, {"ok": True})
            if u.path in ("/admin/usage", "/admin/summary"):
                if not self.is_admin():
                    return self.err(401, "authentication_error", "admin secret required")
                task = (parse_qs(u.query).get("task") or [None])[0]
                return self.reply(200, {"rows": ledger.usage(task), "today_usd": ledger.today_spent()})
            return self.err(403, "forbidden", "path not allowed")

        def do_POST(self):
            path = urlparse(self.path).path
            if path == "/admin/token":
                return self.admin_token()
            if path == "/admin/revoke":
                if not self.is_admin():
                    return self.err(401, "authentication_error", "admin secret required")
                task = json.loads(self.body() or b"{}").get("task", "")
                return self.reply(200, {"revoked": ledger.revoke(task)})
            if path in PROXY_PATHS:
                return self.proxy(path)
            return self.err(403, "forbidden", "path not allowed")

        def admin_token(self):
            if not self.is_admin():
                return self.err(401, "authentication_error", "admin secret required")
            d = json.loads(self.body() or b"{}")
            project, task = str(d.get("project", "")), str(d.get("task", ""))
            if not project or not task:
                return self.err(400, "invalid_request", "project and task required")
            cap = min(float(d.get("cap_usd", 1.0)), conf["max_task_cap"])
            ttl = min(int(d.get("ttl_s", 3600)), conf["max_ttl"])
            tok, exp = ledger.issue(project, task, str(d.get("role", "executor")), cap, ttl)
            self.reply(200, {"token": tok, "cap_usd": cap, "expires": exp})

        def proxy(self, path):
            auth = self.headers.get("authorization", "")
            tok = auth[7:] if auth.startswith("Bearer ") else self.headers.get("x-api-key", "")
            t = ledger.get(tok) if tok else None
            raw = self.body()
            if not t or t["exp"] < time.time():
                return self.err(401, "authentication_error", "bad, expired or revoked task token")
            try:
                req = json.loads(raw or b"{}")
            except ValueError:
                return self.err(400, "invalid_request", "invalid JSON")
            model = req.get("model")
            fam = family(model)
            if not fam:
                return self.err(403, "forbidden", "model not allowed")
            if not key:
                return self.err(503, "gateway_unconfigured", "API key not installed yet")
            if path == "/v1/messages":
                worst = worst_case_usd(fam, len(raw), req.get("max_tokens", 0))
                fresh = ledger.get(tok)
                if fresh["spent"] + worst > fresh["cap"]:
                    return self.err(429, "budget_exceeded",
                                    "task cap $%.2f would be exceeded (spent $%.4f)" % (fresh["cap"], fresh["spent"]))
                if ledger.today_spent() + worst > conf["daily_cap"]:
                    return self.err(429, "budget_exceeded", "daily cap reached")
            hdr = {"content-type": "application/json", "x-api-key": key, "accept-encoding": "identity",
                   "anthropic-version": self.headers.get("anthropic-version", "2023-06-01")}
            if self.headers.get("anthropic-beta"):
                hdr["anthropic-beta"] = self.headers["anthropic-beta"]
            try:
                cls = http.client.HTTPSConnection if up.scheme == "https" else http.client.HTTPConnection
                c = cls(up.hostname, up.port or (443 if up.scheme == "https" else 80), timeout=600)
                c.request("POST", path, raw, hdr)
                r = c.getresponse()
            except Exception:
                return self.err(502, "upstream_error", "upstream unreachable")
            ctype = r.getheader("content-type", "application/json")
            self.send_response(r.status)
            self.send_header("content-type", ctype)
            self.end_headers()
            buf = b""
            while True:
                chunk = r.read(4096)
                if not chunk:
                    break
                try:
                    self.wfile.write(chunk)
                    self.wfile.flush()
                except OSError:
                    pass
                buf += chunk
            usage = {}
            try:
                if "event-stream" in ctype:
                    for line in buf.decode("utf-8", "replace").splitlines():
                        if line.startswith("data:"):
                            try:
                                d = json.loads(line[5:])
                            except ValueError:
                                continue
                            if d.get("type") == "message_start":
                                usage.update(d.get("message", {}).get("usage", {}))
                            elif d.get("type") == "message_delta":
                                usage.update(d.get("usage", {}))
                else:
                    usage = json.loads(buf or b"{}").get("usage", {}) or {}
            except ValueError:
                usage = {}
            usd = cost_usd(fam, usage) if (r.status == 200 and path == "/v1/messages") else 0.0
            ledger.record(t, model, usage, usd, r.status)
            sys.stderr.write("call task=%s role=%s model=%s status=%s usd=%.6f\n" % (t["task"], t["role"], model, r.status, usd))

    sock = conf["socket"]
    os.makedirs(os.path.dirname(sock), exist_ok=True)
    if os.path.exists(sock):
        os.unlink(sock)
    old = os.umask(0o007)  # socket mode 660: owner + group only
    try:
        srv = Server(sock, H)
    finally:
        os.umask(old)
    srv.ledger = ledger
    return srv


if __name__ == "__main__":
    conf = load_conf()
    s = make_server(conf)
    sys.stderr.write("gateway listening on %s\n" % conf["socket"])
    s.serve_forever()
