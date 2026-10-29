"""Shared fixtures: the app run against copied inputs, never the live policy."""

import json
import socket
import threading

import pytest
from werkzeug.serving import make_server

from sharing_manager import runner
from sharing_manager.store import SharingStore

OWNER = {"X-Imbue-Identity": json.dumps({"owner": True, "user_id": "owner-id"}), "X-Sharing-Action": "1"}
POLICY = '[workspace]\nusers = []\nemails = []\nemail_domains = ["imbue.com"]\n\n[services.notes]\nemails = ["existing@example.org"]\n'
REGISTRY = (
    '[[apps]]\nname="files"\ndisplay_name="Files"\nlabel="files-test"\n\n'
    '[[apps]]\nname="notes"\nlabel="notes-test"\n\n'
    '[[apps]]\nname="unlabelled"\ndisplay_name="Unlabelled"\n\n'
    '[[apps]]\nname="owner-exec"\ninternal=true\nlabel="owner-test"\n\n'
    '[[apps]]\nname="system_interface"\ninternal=true\nlabel="system_interface-test"\n'
)


def write_inputs(root, state="up", domain="example.test"):
    grants = root / ".secrets/share_grants.toml"
    grants.parent.mkdir(parents=True)
    grants.write_text(POLICY)
    registry = root / ".state/apps.toml"
    registry.parent.mkdir(parents=True)
    registry.write_text(REGISTRY)
    status = root / ".state/share_gateway/status.json"
    status.parent.mkdir(parents=True)
    status.write_text(json.dumps({"state": state, "workspace_domain": domain}))


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    write_inputs(tmp_path / "inputs")
    store = SharingStore(tmp_path / "inputs", tmp_path / "data")
    monkeypatch.setattr(runner, "store", store)
    monkeypatch.setattr(runner, "IS_SANDBOX", True)
    return runner.app.test_client(), store


@pytest.fixture
def serve(tmp_path, monkeypatch):
    """Start the app on a free port over copied inputs; call with the sharing state ("up" or "off") and the workspace domain."""
    servers = []

    def start(state="up", domain="example.test"):
        write_inputs(tmp_path / "inputs", state, domain)
        monkeypatch.setattr(runner, "store", SharingStore(tmp_path / "inputs", tmp_path / "data"))
        monkeypatch.setattr(runner, "IS_SANDBOX", True)
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        server = make_server("127.0.0.1", port, runner.app, threaded=True)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        servers.append(server)
        return f"http://127.0.0.1:{port}"

    yield start
    for server in servers:
        server.shutdown()
