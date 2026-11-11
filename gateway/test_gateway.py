"""Local test of the gateway against a MOCK upstream (no real Anthropic call).
Proves gateway logic only: key injection, ledger, caps, scoped tokens, admin protection."""
import http.client
import json
import os
import socket
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

tmp = tempfile.mkdtemp()
REAL_KEY = "sk-ant-REAL-SECRET-DO-NOT-LEAK"
ADMIN = "admin-secret-for-test"
open(tmp + "/key", "w").write(REAL_KEY)
open(tmp + "/admin", "w").write(ADMIN)
os.environ.update(GW_SOCKET=tmp + "/gw.sock", GW_DB=tmp + "/l.db", GW_KEY_FILE=tmp + "/key",
                  GW_ADMIN_FILE=tmp + "/admin", GW_UPSTREAM="http://127.0.0.1:9101",
                  GW_MAX_TASK_CAP="5", GW_DAILY_CAP="1000")
sys.path.insert(0, os.path.dirname(__file__))
import gateway as G  # noqa: E402

SEEN = []


class Mock(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["content-length"])))
        SEEN.append((self.headers.get("x-api-key"), self.headers.get("authorization")))
        usage = {"input_tokens": 1000, "output_tokens": 500, "cache_creation_input_tokens": 2000,
                 "cache_read_input_tokens": 10000}
        if body.get("stream"):
            self.send_response(200)
            self.send_header("content-type", "text/event-stream")
            self.end_headers()
            ms = {"type": "message_start", "message": {"usage": {**usage, "output_tokens": 1}}}
            md = {"type": "message_delta", "usage": {"output_tokens": 500}}
            for e, d in (("message_start", ms), ("message_delta", md)):
                self.wfile.write(("event: %s\ndata: %s\n\n" % (e, json.dumps(d))).encode())
        else:
            b = json.dumps({"id": "x", "content": [{"type": "text", "text": "ok"}], "usage": usage}).encode()
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)


ThreadingHTTPServer.allow_reuse_address = True
m = ThreadingHTTPServer(("127.0.0.1", 9101), Mock)
threading.Thread(target=m.serve_forever, daemon=True).start()
srv = G.make_server(G.load_conf())
threading.Thread(target=srv.serve_forever, daemon=True).start()


class UConn(http.client.HTTPConnection):
    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.connect(tmp + "/gw.sock")


def req(method, path, body=None, headers=None):
    c = UConn("localhost")
    h = {"content-type": "application/json"}
    h.update(headers or {})
    c.request(method, path, json.dumps(body) if body is not None else None, h)
    r = c.getresponse()
    return r.status, r.read().decode()


def msg(tok, model="claude-sonnet-5-5", stream=False, maxt=100, path="/v1/messages", style="bearer"):
    h = {"authorization": "Bearer " + tok} if style == "bearer" else {"x-api-key": tok}
    return req("POST", path, {"model": model, "max_tokens": maxt, "stream": stream,
                              "messages": [{"role": "user", "content": "hi"}]}, h)


def issue(project, task, cap, role="executor", ttl=600):
    s, b = req("POST", "/admin/token", {"project": project, "task": task, "cap_usd": cap, "role": role, "ttl_s": ttl},
               {"x-admin-secret": ADMIN})
    assert s == 200, b
    return json.loads(b)["token"]


R = []


def check(name, ok):
    R.append(ok)
    print(("PASS " if ok else "FAIL ") + name)


expected = (1000 * 2.0 + 500 * 10.0 + 2000 * 2.5 + 10000 * 0.10) / 1e6
t1 = issue("alpha", "t1", 0.035)
s, body = msg(t1)
check("A1 call with task token succeeds", s == 200)
check("A2 upstream got the REAL key, not the task token", SEEN[-1][0] == REAL_KEY and t1 not in str(SEEN[-1]))
check("A3 real key never in client response", REAL_KEY not in body)
s, body = req("GET", "/admin/usage?task=t1", headers={"x-admin-secret": ADMIN})
row = json.loads(body)["rows"][0]
check("B1 ledger cost == formula ($%.6f)" % expected, abs(row["usd"] - expected) < 1e-9 and row["project"] == "alpha")
s, _ = msg(t1, stream=True)
row = json.loads(req("GET", "/admin/usage?task=t1", headers={"x-admin-secret": ADMIN})[1])["rows"][0]
check("B2 SSE usage parsed", s == 200 and row["calls"] == 2 and abs(row["usd"] - 2 * expected) < 1e-9)
s, _ = msg(t1, style="x-api-key")
check("B3 x-api-key style accepted", s == 200)
s, body = msg(t1)
check("C1 cap blocks next call with 429 (spent %.4f / cap 0.035)" % (3 * expected), s == 429 and "budget_exceeded" in body)
t2 = issue("beta", "t2", 5)
s, body = msg(t2, maxt=10_000_000)
check("C2 pre-flight worst case (huge max_tokens) -> 429", s == 429)
check("D1 bad token -> 401", msg("tk_nope")[0] == 401)
t3 = issue("alpha", "t3", 5, ttl=1)
time.sleep(1.2)
check("D2 expired token -> 401", msg(t3)[0] == 401)
check("E1 other path -> 403", req("POST", "/v1/files", {}, {"authorization": "Bearer " + t2})[0] == 403)
check("E2 non-allowed model -> 403", msg(t2, model="gpt-x")[0] == 403)
check("E3 versioned model id accepted (family prefix)", msg(t2, model="claude-haiku-5-5-20261101")[0] == 200)
check("F1 admin/token without secret -> 401", req("POST", "/admin/token", {"project": "x", "task": "y"})[0] == 401)
check("F2 task token cannot use admin API", req("POST", "/admin/token", {"project": "x", "task": "y"},
                                                {"authorization": "Bearer " + t2})[0] == 401)
check("F3 usage without secret -> 401", req("GET", "/admin/usage")[0] == 401)
check("F4 cap above gateway max is clamped to 5", json.loads(req("POST", "/admin/token",
      {"project": "p", "task": "q", "cap_usd": 999}, {"x-admin-secret": ADMIN})[1])["cap_usd"] == 5.0)
req("POST", "/admin/revoke", {"task": "t2"}, {"x-admin-secret": ADMIN})
check("G1 revoked token -> 401", msg(t2)[0] == 401)
rows = json.loads(req("GET", "/admin/summary", headers={"x-admin-secret": ADMIN})[1])["rows"]
check("G2 ledger separates projects", {r["project"] for r in rows} >= {"alpha", "beta"})
print("RESULT: %d/%d passed" % (sum(R), len(R)))
sys.exit(0 if all(R) else 1)
