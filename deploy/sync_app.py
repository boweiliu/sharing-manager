"""Pull published main safely and apply it. Run from the workspace root.

The app runs from the workspace's own Python environment (it uses the share gateway's grants code), so applying
is: fast-forward the clone, refresh the supervisord program, restart, check health.

Credentials are inherited from the workspace environment, never stored here.
Local edits and divergent history are left alone. Never copies app data.
"""
import fcntl
import json
import os
import time
from pathlib import Path
import shutil
import subprocess
import sys
import urllib.request


ROOT = Path.cwd()
APP = ROOT / ".external_worktrees/sharing-manager"
STATE = ROOT / "data/.state/sharing-manager-deploy"
LOG = Path("/var/log/supervisor/sharing-manager-sync.log")
LOG_MAX_BYTES = 1024 * 1024


def command(args, *, env=None):
    result = subprocess.run(args, cwd=ROOT, env=env, capture_output=True, text=True, timeout=180)
    if result.returncode:
        # Never include the credential-bearing environment or a full command.
        raise RuntimeError(f"{args[0]} failed: {result.stderr[-1500:]}")
    return result.stdout.strip()


def git(*args, env=None):
    return command(["git", "-C", str(APP), *args], env=env)


def trim_log(path=LOG, max_bytes=LOG_MAX_BYTES):
    """Keep recent output bounded; cron's append descriptor remains valid."""
    if not path.exists() or path.stat().st_size <= max_bytes:
        return
    with path.open("r+b") as stream:
        stream.seek(-max_bytes // 2, 2)
        recent = stream.read().split(b"\n", 1)
        tail = recent[1] if len(recent) == 2 else recent[0]
        stream.seek(0)
        stream.write(tail)
        stream.truncate()


def synchronize(fetch=True):
    STATE.mkdir(parents=True, exist_ok=True)
    with (STATE / "lock").open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        trim_log(LOG)
        if git("status", "--porcelain"):
            raise RuntimeError("Sharing has local edits; automatic update left them untouched.")
        if git("branch", "--show-current") != "main":
            raise RuntimeError("Sharing is being developed on another branch; update skipped.")
        before = git("rev-parse", "HEAD")
        target = before
        if fetch:
            env = os.environ.copy()
            headers = ["X-Latchkey-Gateway-Password: " + env["LATCHKEY_GATEWAY_PASSWORD"]]
            if env.get("LATCHKEY_GATEWAY_PERMISSIONS_OVERRIDE"):
                headers.append("X-Latchkey-Gateway-Permissions-Override: " + env["LATCHKEY_GATEWAY_PERMISSIONS_OVERRIDE"])
            env["GIT_CONFIG_COUNT"] = str(len(headers))
            for index, header in enumerate(headers):
                env[f"GIT_CONFIG_KEY_{index}"] = "http.extraHeader"
                env[f"GIT_CONFIG_VALUE_{index}"] = header
            remote = env["LATCHKEY_GATEWAY"].rstrip("/") + "/gateway/https://github.com/boweiliu/sharing-manager.git"
            git("fetch", "--quiet", remote, "main", env=env)
            target = git("rev-parse", "FETCH_HEAD")
            git("merge-base", "--is-ancestor", before, target)
        applied = (STATE / "applied").read_text().strip() if (STATE / "applied").exists() else ""
        if applied == target and before == target:
            print(f"Sharing already current: {target[:12]}")
            return
        if before != target:
            git("merge", "--ff-only", target)
        shutil.copyfile(APP / "deploy/sharing-manager.conf", ROOT / "system/supervisord.conf.d/sharing-manager.conf")
        command(["supervisorctl", "reread"])
        command(["supervisorctl", "update"])
        command(["supervisorctl", "restart", "sharing-manager"])
        healthy = False
        for _ in range(20):
            try:
                with urllib.request.urlopen("http://127.0.0.1:8082/health", timeout=5) as response:
                    healthy = json.load(response) == {"status": "ok"}
            except OSError:
                healthy = False
            if healthy:
                break
            time.sleep(1)
        if not healthy:
            raise RuntimeError("Sharing did not pass its health check.")
        command(["uv", "run", "--no-sync", "python", str(APP / "deploy/install_schedule.py")])
        (STATE / "applied").write_text(target + "\n")
        print(f"Sharing applied and healthy: {target[:12]}")


if __name__ == "__main__":
    try:
        synchronize(fetch="--install-local" not in sys.argv)
    except (RuntimeError, OSError, KeyError, ValueError, subprocess.TimeoutExpired) as error:
        print(str(error), file=sys.stderr)
        sys.exit(1)
