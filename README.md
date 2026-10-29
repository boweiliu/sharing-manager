# Sharing

Owner-only controls for a workspace's and each app's sharing: who may open which app, the sharing link of each, and visiting agents. One copy runs on each machine and manages only that machine (its own grants file, app list and sharing status). A line in the page header names the machine; put its name in `data/.apps/sharing-manager/machine-name`.

Taken from the Sharing app in the fresh087 workspace (`system/apps/sharing_manager`, commit 4fe244168), whose layout and wording follow Studio's native Share tab. Differences from that copy:

- Link row: besides Copy there is **Open in browser** (a new tab) and **Show QR code** (a dialog with the link drawn as a QR code, for scanning with a phone). All three are off while sharing is off or an app has no link yet.
- Runs in the workspace's own Python environment instead of as a monorepo member: it needs `share_gateway.grants` (the lock and parser the gateway itself uses), `flask`, `pydantic` and `tomlkit`. The `app-schema` API document and `imbue-common` are not used, so there is no `/api/schema`.
- Visiting agents need the `agent-access` command, which older workspaces lack. Where it is missing the section is hidden and `/api/agents` answers `available: false`.
- The folders under the app's data are created before the gateway's atomic writer is used (older gateways only create the file).
- `qrcode.js` in `static/` is `qrcode-generator` 2.0.4 by Kazuhiko Arase (MIT, notice kept in the file); the page draws the SVG itself from its modules.

Adds and removals use the gateway's grants parser and lock, check the displayed revision, keep other scopes and unknown fields, and replace the policy atomically. Originals are kept under `data/.apps/sharing-manager/history/`; raw source snapshots and their paths under `source/`. The page links to the underlying TOML (`/raw`).

Environment: `SHARING_MANAGER_PORT` (8082, loopback), `SHARING_MANAGER_DATA_DIR`, `SHARING_MANAGER_INPUT_DIR` (default `data`; any other directory makes an isolated copy that blocks agent changes), `SHARING_MANAGER_MACHINE_NAME`.

## Running and updating

`deploy/sharing-manager.conf` is the supervisord program (copied to `system/supervisord.conf.d/`); it runs `python -m sharing_manager.runner` with `PYTHONPATH` pointing at this clone. `deploy/sync_app.py` fetches GitHub `main`, fast-forwards a clean clone, refreshes the program, restarts and checks `/health`; `deploy/install_schedule.py` runs it every two minutes from cron. Push to `main` and every machine picks it up.

## Tests

From the workspace root: `PYTHONPATH=.external_worktrees/sharing-manager/src uv run --no-sync pytest .external_worktrees/sharing-manager/tests -m '' --no-cov`. To also check that the QR code scans back to the link, run `uv run --no-sync --with zxing-cpp --with pillow python -m pytest ...` (those checks skip without the decoder).
