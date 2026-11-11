"""Security probe that runs INSIDE a task container. Prints PASS/FAIL lines.
Covers parts of E1-E10 plus gateway checks that need no real API key."""
import json
import os
import socket
import urllib.error
import urllib.request

R = []
ERRS = []


def check(name, ok):
    R.append(ok)
    print(("PASS " if ok else "FAIL ") + name)


def http(method, url, body=None, headers=None, proxy=None, timeout=8):
    h = {"content-type": "application/json"}
    h.update(headers or {})
    req = urllib.request.Request(url, json.dumps(body).encode() if body is not None else None, h, method=method)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({"https": proxy, "http": proxy} if proxy else {}))
    try:
        return opener.open(req, timeout=timeout).status
    except urllib.error.HTTPError as e:
        return e.code
    except Exception as e:
        ERRS.append("%s %s -> %r" % (method, url, e))
        return None


GW = "http://gateway:8080"
tok = os.environ.get("ANTHROPIC_AUTH_TOKEN", "")
envtxt = json.dumps(dict(os.environ))
check("G3 no real Anthropic key in container env", "sk-ant-" not in envtxt)
check("G3b only a scoped task token (prefix tk) is present", tok.startswith("tk_"))
check("E5a no admin secret reachable from container", not os.path.exists("/etc/ai-company"))
check("E1 no docker.sock or gateway socket inside", not os.path.exists("/var/run/docker.sock")
      and not os.path.exists("/srv/ai-gateway"))
check("E3 other projects not visible", not os.path.exists("/srv/projects"))
check("E5b admin usage without secret -> 401", http("GET", GW + "/admin/summary") == 401)
check("E5c admin token mint without secret -> 401", http("POST", GW + "/admin/token", {"project": "x", "task": "y"}) == 401)
check("E5d task token cannot mint tokens -> 401", http("POST", GW + "/admin/token", {"project": "x", "task": "y"},
                                                   {"authorization": "Bearer " + tok}) == 401)
check("E5e tampered token -> 401", http("POST", GW + "/v1/messages", {"model": "claude-haiku-5-5", "max_tokens": 5},
                                    {"authorization": "Bearer " + tok[:-2] + "xx"}) == 401)
check("E5f forbidden path with own token -> 403", http("POST", GW + "/v1/files", {}, {"authorization": "Bearer " + tok}) == 403)
check("E5g forbidden model with own token -> 403", http("POST", GW + "/v1/messages", {"model": "gpt-x", "max_tokens": 5},
                                                    {"authorization": "Bearer " + tok}) == 403)
try:
    socket.create_connection(("1.1.1.1", 443), timeout=4)
    direct = True
except Exception:
    direct = False
check("E6a no direct internet", not direct)
check("E6b non-allow-listed domain denied by proxy", http("GET", "https://example.org", proxy=os.environ["HTTPS_PROXY"]) != 200)
check("E6c allow-listed domain works through proxy", http("GET", "https://github.com", proxy=os.environ["HTTPS_PROXY"], timeout=15) == 200)
try:
    socket.create_connection(("156.67.25.115", 22), timeout=4)
    host = True
except Exception:
    host = False
check("E7 host SSH unreachable", not host)
try:
    open("/work/.probe", "w").write("x")
    os.unlink("/work/.probe")
    own = True
except Exception:
    own = False
check("E3b own project dir writable", own)
try:
    open("/usr/x", "w")
    ro = False
except Exception:
    ro = True
check("E2 root filesystem read-only", ro)
for e in ERRS:
    print("DEBUG " + e)
print("LEAKTEST token=" + tok)  # the worker log must show [REDACTED] (E8)
print("PROBE %d/%d passed" % (sum(R), len(R)))
