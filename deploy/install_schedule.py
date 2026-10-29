"""Install a durable, two-minute Sharing code-update job on this workspace."""
from pathlib import Path
import shutil

root = Path.cwd()
line = ("* * * * * root /home/user/workspace/system/libs/automations/with_agent_env.sh "
        "env MINDS_JOB_STATE_DIR=/home/user/workspace/data/.state/jobs "
        "/home/user/workspace/system/libs/automations/run_job.sh sharing-manager-sync --every 2m "
        "uv run --no-sync python .external_worktrees/sharing-manager/deploy/sync_app.py "
        ">> /var/log/supervisor/sharing-manager-sync.log 2>&1\n")
durable = root / "data/.state/cron.d/sharing-manager-sync"
durable.parent.mkdir(parents=True, exist_ok=True)
durable.write_text(line)
durable.chmod(0o644)
shutil.copyfile(durable, "/etc/cron.d/sharing-manager-sync")
Path("/etc/cron.d/sharing-manager-sync").chmod(0o644)
print("Sharing updates scheduled every two minutes.")
