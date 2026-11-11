#!/usr/bin/env python3
"""Task worker (Phase 0). Run as the unprivileged user `aiworker`.

For one task it: asks the gateway for a scoped token, creates a private internal
network, starts a key-less socat sidecar that reaches the gateway socket, runs ONE
hardened container for the project, saves a redacted log, then cleans everything up.
The worker never sees the real API key (it is readable only by `aigateway`).
"""
import argparse
import base64
import http.client
import json
import os
import re
import socket
import subprocess
import sys

ADMIN_FILE = "/etc/ai-company/admin.token"
GW_SOCK = "/srv/ai-gateway/gw.sock"
PROJECTS_FILE = "/etc/ai-company/projects.json"
PROJECTS_ROOT = "/srv/projects"
LOG_DIR = os.path.expanduser("~/logs")
PROXY = "egress-proxy"
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,30}$")
SECRET_PATTERNS = [r"sk-ant-[A-Za-z0-9_\-]+", r"tk_[A-Za-z0-9_\-]+", r"ghp_[A-Za-z0-9]+",
                   r"github_pat_[A-Za-z0-9_]+", r"(?i)bearer\s+[A-Za-z0-9._\-]+"]


def redact(text, extra=()):
    for s in extra:
        if s:
            text = text.replace(s, "[REDACTED]")
    for p in SECRET_PATTERNS:
        text = re.sub(p, "[REDACTED]", text)
    return text


class UConn(http.client.HTTPConnection):
    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.connect(GW_SOCK)


def gw(method, path, body=None):
    admin = open(ADMIN_FILE).read().strip()
    c = UConn("localhost")
    c.request(method, path, json.dumps(body) if body is not None else None,
              {"content-type": "application/json", "x-admin-secret": admin})
    r = c.getresponse()
    data = r.read().decode()
    if r.status != 200:
        raise RuntimeError("gateway %s %s -> %s %s" % (method, path, r.status, data[:200]))
    return json.loads(data)


def docker(*args, check=True, timeout=120, capture=True):
    env = dict(os.environ)
    env.setdefault("XDG_RUNTIME_DIR", "/run/user/%d" % os.getuid())
    env.setdefault("DOCKER_HOST", "unix://%s/docker.sock" % env["XDG_RUNTIME_DIR"])
    r = subprocess.run(["docker", *args], env=env, text=True, timeout=timeout,
                       stdout=subprocess.PIPE if capture else None, stderr=subprocess.STDOUT if capture else None)
    if check and r.returncode != 0:
        raise RuntimeError("docker %s failed: %s" % (" ".join(args[:2]), (r.stdout or "")[:300]))
    return r


def run_task(a):
    for label, v in (("project", a.project), ("task", a.task)):
        if not NAME_RE.match(v):
            sys.exit("invalid %s name (use a-z, 0-9, dash)" % label)
    projects = json.load(open(PROJECTS_FILE))
    if a.project not in projects:
        sys.exit("unknown project: %s" % a.project)
    uid = int(projects[a.project]["uid"])
    pdir = os.path.join(PROJECTS_ROOT, a.project)
    if not os.path.isdir(pdir):
        sys.exit("project directory missing: %s" % pdir)

    net, sidecar, cname = "task-net-" + a.task, "gw-" + a.task, "task-" + a.task
    runtime = os.environ.get("XDG_RUNTIME_DIR", "/run/user/%d" % os.getuid())
    envdir = os.path.join(runtime, "tasks")
    os.makedirs(envdir, mode=0o700, exist_ok=True)
    envfile = os.path.join(envdir, a.task + ".env")
    os.makedirs(LOG_DIR, mode=0o700, exist_ok=True)
    tok = None
    exit_code = None
    try:
        tok = gw("POST", "/admin/token", {"project": a.project, "task": a.task, "role": a.role,
                                          "cap_usd": a.cap, "ttl_s": a.timeout + 300})["token"]
        if a.emit_token:  # tests only
            with open(a.emit_token, "w") as f:
                os.chmod(a.emit_token, 0o600)
                f.write(tok)
        docker("network", "create", "--internal", net)
        docker("network", "connect", net, PROXY)
        docker("run", "-d", "--name", sidecar, "--network", net, "--network-alias", "gateway",
               "--read-only", "--cap-drop=ALL", "--security-opt", "no-new-privileges",
               "-v", GW_SOCK + ":/gw.sock", "alpine/socat",
               "TCP-LISTEN:8080,fork,reuseaddr", "UNIX-CONNECT:/gw.sock")
        env = {
            "HOME": "/tmp",
            "ANTHROPIC_BASE_URL": "http://gateway:8080",
            "ANTHROPIC_AUTH_TOKEN": tok,
            "HTTP_PROXY": "http://%s:3128" % PROXY, "HTTPS_PROXY": "http://%s:3128" % PROXY,
            "NO_PROXY": "gateway,localhost,127.0.0.1",
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        }
        if a.prompt:
            env["TASK_PROMPT_B64"] = base64.b64encode(a.prompt.encode()).decode()
        fd = os.open(envfile, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            for k, v in env.items():
                f.write("%s=%s\n" % (k, v))
        cmd = ["run", "--name", cname, "--network", net, "--user", "%d:%d" % (uid, uid),
               "--cap-drop=ALL", "--security-opt", "no-new-privileges", "--read-only",
               "--tmpfs", "/tmp:rw,size=512m", "--memory", "3g", "--memory-swap", "3g",
               "--cpus", "1.5", "--pids-limit", "512",
               "-v", "%s:/work" % pdir, "-w", "/work", "--env-file", envfile, a.image]
        if a.cmd:
            cmd += ["sh", "-c", a.cmd]
        try:
            r = docker(*cmd, check=False, timeout=a.timeout)
            out, exit_code = r.stdout or "", r.returncode
        except subprocess.TimeoutExpired as e:
            docker("kill", cname, check=False)
            out, exit_code = ((e.stdout or b"").decode() if isinstance(e.stdout, bytes) else (e.stdout or "")) + "\n[TIMEOUT]", 124
        if a.debug:
            out += "\n[sidecar state] " + (docker("inspect", "-f", "{{.State.Status}} exit={{.State.ExitCode}}", sidecar, check=False).stdout or "")
            out += "[sidecar logs]\n" + (docker("logs", sidecar, check=False).stdout or "")
        text = redact(out, [tok])
        logpath = os.path.join(LOG_DIR, a.task + ".log")
        fd = os.open(logpath, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(text)
        print(text)
        print("--- exit=%s log=%s" % (exit_code, logpath))
    finally:
        try:
            os.unlink(envfile)
        except OSError:
            pass
        docker("rm", "-f", cname, sidecar, check=False)
        docker("network", "disconnect", net, PROXY, check=False)
        docker("network", "rm", net, check=False)
        try:
            gw("POST", "/admin/revoke", {"task": a.task})
            u = gw("GET", "/admin/usage?task=" + a.task)
            print("--- usage:", json.dumps(u["rows"]))
        except Exception as e:  # cleanup must not hide the real result
            print("--- cleanup note:", e)
    return exit_code or 0


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="sub", required=True)
    r = sub.add_parser("run")
    r.add_argument("--project", required=True)
    r.add_argument("--task", required=True)
    r.add_argument("--role", default="executor")
    r.add_argument("--cap", type=float, default=0.5, help="USD cap for this task")
    r.add_argument("--timeout", type=int, default=600)
    r.add_argument("--image", default="ai-task:latest")
    r.add_argument("--prompt", default="")
    r.add_argument("--cmd", default="", help="shell command instead of the default agent run")
    r.add_argument("--debug", action="store_true", help="append sidecar state and logs to the output")
    r.add_argument("--emit-token", default="", help="tests only: write the task token to this file")
    a = ap.parse_args()
    sys.exit(run_task(a))


if __name__ == "__main__":
    main()
