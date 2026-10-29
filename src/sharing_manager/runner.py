"""Manage workspace and app sharing permissions, links, and visiting agents.

Services run from /home/user/workspace (the repo root). Conventions:

- Persistent state lives under ``DATA_DIR`` (``data/.apps/sharing-manager/``, or
  ``SHARING_MANAGER_DATA_DIR``), so a throwaway instance can run against a copy.
- Policy and registry inputs are read from ``INPUT_DIR`` (``data``, or
  ``SHARING_MANAGER_INPUT_DIR``). Every instance manages only the machine it runs on.
- Listen port: ``PORT`` (8082, or ``SHARING_MANAGER_PORT``), bound to loopback.

This is a synchronous Flask app served by the threaded Werkzeug server. The app owns
its own browser origin (the forwarder routes ``http://sharing-manager.<workspace-host>/``
straight to this port), so it serves at ``/`` and root-absolute URLs work unmodified.
"""

import base64
import hashlib
import html
import json
import logging
import os
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path
from typing import Literal, TypeVar

from flask import Flask, Response, abort, request, send_file
from pydantic import BaseModel, ConfigDict, Field, JsonValue, ValidationError
from share_gateway.grants import GrantsError
from werkzeug.exceptions import HTTPException
from werkzeug.serving import run_simple

from sharing_manager.store import GrantEditError, SharingStore, revision

DATA_DIR = Path(os.environ.get("SHARING_MANAGER_DATA_DIR", "data/.apps/sharing-manager"))
PORT = int(os.environ.get("SHARING_MANAGER_PORT", "8082"))
# Which machine this copy manages, shown in the page header: SHARING_MANAGER_MACHINE_NAME, or the
# one-line file ``machine-name`` in the app's data folder (which keeps the supervisord program identical everywhere).
MACHINE_NAME = os.environ.get("SHARING_MANAGER_MACHINE_NAME", "")

# The browser-side modules the workspace shell builds and every app serves from its own
# origin: the app contract (how a page talks to the shell framing it) and the element
# context menu. A module import is a fetch without cookies, which the forwarder refuses
# across origins, so they are served here rather than from the shell. Relative to the
# repo root the service runs from, like DATA_DIR.
SHELL_STATIC_MODULES_DIR = Path("system/apps/system_interface/imbue/system_interface/static/_static")
SHELL_STATIC_MODULE_NAMES = ("app_contract.js", "context_menu.js")

# The script every page serves: it connects the page to the shell, reports where the page
# is so the shell can reopen this app's window at the same place, and installs the element
# context menu. A page visited outside the shell runs it harmlessly.
SHELL_PAGE_SCRIPT_BODY = """
  import { connectToShell } from "/_static/app_contract.js";
  import { installElementContextMenu } from "/_static/context_menu.js";
  let handshake = null;
  const connection = connectToShell({
    onHandshake: (received) => {
      handshake = received;
      connection.location(location.pathname + location.search, document.title);
    },
  });
  installElementContextMenu({ connection, handshake: () => handshake });
"""
SHELL_PAGE_SCRIPT = f'<script type="module">{SHELL_PAGE_SCRIPT_BODY}</script>'


def inline_script_source(body: str) -> str:
    """The Content-Security-Policy source admitting one inline <script> whose body is exactly ``body``."""
    return "'sha256-" + base64.b64encode(hashlib.sha256(body.encode()).digest()).decode() + "'"


# Every HTML page is served under this policy (the app is reachable from the internet once
# the workspace is shared): scripts only from this origin plus the inline block above by
# hash, fetches, sockets and images only from this origin, inline styles, no plugins. A
# block is posted to ``/_csp-report``, which logs it on stderr (the supervisor log).
CONTENT_SECURITY_POLICY = (
    "default-src 'self'; "
    f"script-src 'self' 'report-sample' {inline_script_source(SHELL_PAGE_SCRIPT_BODY)}; "
    "style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; frame-src http: https:; "
    "object-src 'none'; base-uri 'self'; form-action 'self'; "
    "report-uri /_csp-report"
)
BROWSER_EXTENSION_SCHEMES = ("chrome-extension:", "moz-extension:", "safari-web-extension:")
STATIC_FILES = frozenset({"style.css", "sharing.js", "qrcode.js"})


class Frozen(BaseModel):
    """Base of every request and response body."""

    model_config = ConfigDict(frozen=True, extra="forbid")


class HealthResponse(Frozen):
    status: str = Field(description="Always 'ok'")


ASSETS_DIR = Path(__file__).parent / "static"
INPUT_DIR = Path(os.environ.get("SHARING_MANAGER_INPUT_DIR", "data"))
IS_SANDBOX = INPUT_DIR.resolve() != Path("data").resolve()
store = SharingStore(INPUT_DIR, DATA_DIR)


class GrantScope(Frozen):
    model_config = ConfigDict(frozen=True, extra="ignore")
    users: tuple[str, ...] = Field(default=(), description="Account identifiers admitted to this scope")
    emails: tuple[str, ...] = Field(default=(), description="Individual email addresses admitted to this scope")
    email_domains: tuple[str, ...] = Field(default=(), description="Email domains admitted to this scope")


class ShareTarget(Frozen):
    name: str = Field(description="The app's registered name, or workspace")
    title: str = Field(description="The name shown in the sharing navigation")
    icon: str = Field(default="", description="The registered SVG icon, rendered only as an image")
    system: bool = Field(default=False, description="Whether the app belongs in System apps")
    url: str | None = Field(default=None, description="The sharing URL, absent until its label and domain are known")


class SharingState(Frozen):
    state: str = Field(description="The sharing connection state")
    machine: str = Field(description="The machine this copy manages, empty when unnamed")
    targets: tuple[ShareTarget, ...] = Field(description="The workspace and grantable registered apps")
    workspace: GrantScope = Field(description="Permissions applying to every app")
    services: dict[str, GrantScope] = Field(description="Additional permissions per app")
    revision: str = Field(description="The exact policy revision, required by changes to prevent stale writes")
    raw: str = Field(description="The underlying unprocessed TOML policy")
    source: str = Field(description="The source policy's path")
    sandbox: bool = Field(description="Whether this instance uses isolated copies")


class GrantChange(Frozen):
    scope: str = Field(min_length=1, max_length=200, description="workspace or the app's registered name")
    kind: Literal["emails", "email_domains", "users"] = Field(description="The list to change; users may only be removed")
    value: str = Field(min_length=1, max_length=320, description="The email address, domain, or account identifier")
    operation: Literal["add", "remove"] = Field(description="Whether to grant or remove access")
    revision: str = Field(min_length=64, max_length=64, description="The revision currently shown by the client")


class AgentsResponse(Frozen):
    agents: tuple[dict[str, JsonValue], ...] = Field(description="Visiting agents, with scopes and expiry information")
    available: bool = Field(default=True, description="False when this workspace has no visiting-agent support")


class InviteRequest(Frozen):
    scope: str = Field(min_length=1, max_length=200, description="workspace or one registered app")
    name_hint: str = Field(default="", max_length=100, description="Optional suggested name for the visiting agent")
    expires_in_days: float | None = Field(default=None, gt=0, le=3650, description="Optional grant lifetime in days")


class InviteResponse(Frozen):
    instructions: str = Field(description="One-time instructions shown only to the workspace owner")
    code: str = Field(description="The one-time invitation code")
    expires_at: str = Field(description="When the unused code expires")


class RevokeResponse(Frozen):
    revoked: str = Field(description="The agent whose access was revoked")


Body = TypeVar("Body", bound=Frozen)


def error_response(message: str, status: int) -> Response:
    return Response(json.dumps({"error": message}), status=status, mimetype="application/json")


def model_response(model: Frozen) -> Response:
    return Response(model.model_dump_json(), mimetype="application/json")


def parse_request_body(model: type[Body]) -> Body:
    payload = request.get_json(silent=True)
    if payload is None:
        abort(400, "Send the change as JSON.")
    try:
        return model.model_validate(payload)
    except ValidationError:
        abort(400, "The change was not valid. Refresh and try again.")


def machine_name() -> str:
    named = store.data_dir / "machine-name"
    return MACHINE_NAME or (named.read_text().strip() if named.is_file() else "")


def sharing_state() -> SharingState:
    raw = store.raw_grants()
    policy = tomllib.loads(raw)
    status = store.sharing_status()
    domain = status.get("workspace_domain", "")
    apps = store.registered_apps()
    targets = []
    shell = next((a for a in apps if a.get("name") == "system_interface"), {})
    targets.append(ShareTarget(name="workspace", title="Whole workspace", url=shared_url(shell, domain)))
    for entry in apps:
        if entry.get("internal") or entry.get("name") == "sharing-manager":
            continue
        targets.append(ShareTarget(name=entry["name"], title=entry.get("display_name", entry["name"]), icon=entry.get("icon", ""), system=entry.get("system", False), url=shared_url(entry, domain)))
    store.preserve_sources()
    return SharingState(state=status.get("state", "off"), machine=machine_name(), targets=tuple(targets), workspace=GrantScope.model_validate(policy.get("workspace", {})), services={name: GrantScope.model_validate(value) for name, value in policy.get("services", {}).items()}, revision=revision(raw), raw=raw, source=str(store.grants_path), sandbox=IS_SANDBOX)


def shared_url(entry: dict, domain: str) -> str | None:
    label = entry.get("label", "")
    # Older workspaces label the shell "system_interface-<id>", so a label may hold an underscore; a domain may not.
    if not domain or not label or any(c not in "abcdefghijklmnopqrstuvwxyz0123456789-." for c in domain) or any(c not in "abcdefghijklmnopqrstuvwxyz0123456789-_." for c in label):
        return None
    return f"https://{label}.{domain}/"


def agent_access_command() -> Path | None:
    """The visiting-agent command of this workspace's environment, absent on older workspaces."""
    beside_python = Path(sys.executable).with_name("agent-access")
    if beside_python.exists():
        return beside_python
    found = shutil.which("agent-access")
    return Path(found) if found else None


def run_agent_command(arguments: list[str]) -> dict[str, JsonValue]:
    if IS_SANDBOX:
        if arguments[0] == "list":
            return {"agents": []}
        abort(409, "Visiting agents cannot be changed in an isolated copy.")
    command = agent_access_command()
    if command is None:
        abort(501, "Visiting agents are not available on this workspace.")
    result = subprocess.run([str(command), *arguments, "--json"], capture_output=True, text=True, timeout=30, check=False)
    if result.returncode:
        try:
            message = json.loads(result.stderr).get("error", "The visiting-agent operation failed.")
        except json.JSONDecodeError:
            message = "The visiting-agent operation failed."
        abort(502, message)
    return json.loads(result.stdout)


def owner_actor() -> str:
    identity = json.loads(request.headers.get("X-Imbue-Identity", "{}"))
    user_id = identity.get("user_id")
    if not isinstance(user_id, str) or not user_id:
        abort(400, "Open this app through your signed-in workspace to invite or revoke agents.")
    return "user:" + user_id


app = Flask("sharing_manager", static_folder=None)


@app.before_request
def owner_only() -> Response | None:
    """Only the owner may read sharing settings or change anyone's access."""
    if request.path == "/health":
        return None
    try:
        identity = json.loads(request.headers.get("X-Imbue-Identity", '{"owner":true}'))
    except json.JSONDecodeError:
        return error_response("Owner access is required.", 403)
    if not isinstance(identity, dict) or identity.get("owner") is not True:
        return error_response("Only the workspace owner can manage sharing.", 403)
    if request.method in {"POST", "PUT", "PATCH", "DELETE"} and request.path != "/_csp-report":
        if request.headers.get("X-Sharing-Action") != "1":
            return error_response("Use the sharing app to make this change.", 403)
        if request.headers.get("Sec-Fetch-Site", "same-origin") not in {"same-origin", "none"}:
            return error_response("Use the sharing app to make this change.", 403)
    return None


@app.errorhandler(GrantsError)
@app.errorhandler(FileNotFoundError)
@app.errorhandler(tomllib.TOMLDecodeError)
def unreadable_policy(error: Exception) -> Response:
    """An unreadable policy is unknown; never show an empty editable policy."""
    logging.getLogger(__name__).warning("Sharing inputs could not be loaded: %s", error)
    return error_response("Sharing settings could not be read. Refresh or ask your agent to check the sharing configuration.", 503)


@app.errorhandler(HTTPException)
def http_error(error: HTTPException) -> Response:
    return error_response(error.description or error.name, error.code or 500)


@app.route("/static/<filename>")
def static_asset(filename: str) -> Response:
    """The sharing page stylesheet, script and QR code library."""
    if filename not in STATIC_FILES:
        abort(404)
    return send_file((ASSETS_DIR / filename).absolute())


@app.route("/api/sharing")
def get_sharing() -> Response:
    """Read current workspace and app permissions and their sharing links."""
    return model_response(sharing_state())


@app.route("/api/grants", methods=["POST"])
def change_grant() -> Response:
    """Add or remove one permission immediately, preserving concurrent edits and other scopes."""
    change = parse_request_body(GrantChange)
    try:
        store.edit(change.scope, change.kind, change.value, change.operation, change.revision)
    except GrantEditError as exc:
        return error_response(str(exc), 409 if "elsewhere" in str(exc) else 400)
    return model_response(sharing_state())


@app.route("/raw")
def raw_policy() -> Response:
    """The underlying sharing policy in its native TOML format."""
    raw = html.escape(store.raw_grants())
    source = html.escape(str(store.grants_path))
    return Response(f'<!doctype html><html><head><title>Sharing settings</title><link rel="stylesheet" href="/static/style.css"></head><body><main class="raw-page"><a href="/">Back to Sharing</a><h1>Underlying sharing settings</h1><p>{source}</p><pre>{raw}</pre></main>{SHELL_PAGE_SCRIPT}</body></html>', mimetype="text/html")


@app.route("/api/agents")
def visiting_agents() -> Response:
    """List visiting agents and the scopes they may use; empty and marked unavailable on a workspace without them."""
    if not IS_SANDBOX and agent_access_command() is None:
        return model_response(AgentsResponse(agents=(), available=False))
    return model_response(AgentsResponse.model_validate({"agents": run_agent_command(["list"])["agents"]}))


@app.route("/api/agents/invites", methods=["POST"])
def invite_agent() -> Response:
    """Create one short-lived invitation for a visiting agent, scoped to the selected target."""
    body = parse_request_body(InviteRequest)
    if body.scope != "workspace" and not any(a.get("name") == body.scope and not a.get("internal") for a in store.registered_apps()):
        return error_response("This app is not available for sharing.", 400)
    arguments = ["invite", "--granter", owner_actor(), "--scope", body.scope, "--name-hint", body.name_hint]
    if body.expires_in_days is not None:
        arguments.extend(["--expires-in-days", str(body.expires_in_days)])
    result = run_agent_command(arguments)
    return model_response(InviteResponse.model_validate({key: result[key] for key in ["instructions", "code", "expires_at"]}))


@app.route("/api/agents/<agent_id>", methods=["DELETE"])
def revoke_agent(agent_id: str) -> Response:
    """Revoke one visiting agent as the workspace owner."""
    result = run_agent_command(["revoke", agent_id, "--revoker", owner_actor(), "--owner"])
    return model_response(RevokeResponse(revoked=str(result["revoked"])))


@app.after_request
def add_content_security_policy(response: Response) -> Response:
    if response.mimetype == "text/html":
        response.headers.setdefault("Content-Security-Policy", CONTENT_SECURITY_POLICY)
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


@app.route("/")
def index() -> Response:
    """The app's page."""
    page = (ASSETS_DIR / "index.html").read_text()
    return Response(page.replace("</body>", SHELL_PAGE_SCRIPT + "</body>"), mimetype="text/html")


@app.route("/_csp-report", methods=["POST"])
def content_security_policy_report() -> Response:
    """Log a resource blocked by the page security policy."""
    # A block caused by a browser extension is the policy doing its job and is not logged.
    body = request.get_json(force=True, silent=True)
    report = body.get("csp-report") if isinstance(body, dict) else None
    if not isinstance(report, dict):
        return Response(status=204)
    blocked, page, source, directive, line, sample = (
        str(report.get(key, ""))[:500]
        for key in ("blocked-uri", "document-uri", "source-file", "effective-directive", "line-number", "script-sample")
    )
    if blocked.startswith(BROWSER_EXTENSION_SCHEMES) or source.startswith(BROWSER_EXTENSION_SCHEMES):
        return Response(status=204)
    where = f" at {source}:{line}" if source and line else ""
    starting = f" starting {sample!r}" if sample else ""
    remedy = (
        "add its inline_script_source to CONTENT_SECURITY_POLICY if the page needs it"
        if blocked == "inline"
        else "add its origin to CONTENT_SECURITY_POLICY if the page needs it"
    )
    logging.getLogger(__name__).error(
        f"ERROR Content-Security-Policy blocked {blocked!r} on {page!r} ({directive}){where}{starting}; {remedy}",
    )
    return Response(status=204)


@app.route("/_static/<basename>")
def shell_module(basename: str) -> Response:
    """One of the two browser-side modules the workspace shell builds and every app serves from its own origin."""
    # The two shell-built modules and nothing else, so this route can never read outside that directory.
    if basename not in SHELL_STATIC_MODULE_NAMES:
        abort(404)
    module_path = SHELL_STATIC_MODULES_DIR / basename
    if not module_path.is_file():
        abort(404)
    return send_file(module_path.absolute(), mimetype="text/javascript")


@app.route("/health")
def health() -> Response:
    """Whether the app is up."""
    return model_response(HealthResponse(status="ok"))


def main() -> None:
    run_simple("127.0.0.1", PORT, app, threaded=True, use_reloader=False, use_debugger=False)


if __name__ == "__main__":
    main()
