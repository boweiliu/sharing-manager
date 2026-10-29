"""Sharing controls exercised against copied inputs, never the live policy."""

import json

import pytest
from share_gateway.grants import load_grants, upgrade_invites_in_file

from conftest import OWNER, POLICY
from sharing_manager import runner


def change(client, scope="files", kind="emails", value="person@example.org", operation="add", revision=None):
    current = client.get("/api/sharing", headers=OWNER).json
    return client.post("/api/grants", headers=OWNER, json={"scope": scope, "kind": kind, "value": value, "operation": operation, "revision": revision or current["revision"]})


def test_add_and_remove_change_actual_gateway_admission_and_preserve_other_scopes(isolated):
    client, store = isolated
    assert not load_grants(store.grants_path).allows("person", "person@example.org", "files", {"files", "notes"})
    added = change(client, value="  Person@Example.ORG  ")
    assert added.status_code == 200
    grants = load_grants(store.grants_path)
    assert grants.allows("person", "person@example.org", "files", {"files", "notes"})
    assert not grants.allows("person", "person@example.org", "notes", {"files", "notes"})
    assert not grants.allows("person", "person@example.org", None, {"files", "notes"})
    assert client.get("/api/sharing", headers=OWNER).json["services"]["files"]["emails"] == ["person@example.org"]
    removed = change(client, value="person@example.org", operation="remove")
    assert removed.status_code == 200
    assert not load_grants(store.grants_path).allows("person", "person@example.org", "files", {"files", "notes"})
    assert load_grants(store.grants_path).services["notes"].emails == {"existing@example.org"}
    assert load_grants(store.grants_path).workspace.email_domains == {"imbue.com"}
    assert len(list((store.data_dir / "history").glob("*.toml"))) == 2
    assert (store.data_dir / "source/.secrets/share_grants.toml").read_text() == store.raw_grants()


def test_workspace_access_is_inherited_and_links_use_registered_labels(isolated):
    client, store = isolated
    state = client.get("/api/sharing", headers=OWNER).json
    assert {t["name"]: t["url"] for t in state["targets"]} == {
        "workspace": "https://system_interface-test.example.test/",
        "files": "https://files-test.example.test/",
        "notes": "https://notes-test.example.test/",
        "unlabelled": None,
    }
    assert load_grants(store.grants_path).allows("coworker", "coworker@imbue.com", "files", {"files"})
    assert change(client, scope="workspace", value="team@example.org").status_code == 200
    assert load_grants(store.grants_path).allows("team", "team@example.org", None, {"files"})


def test_the_state_names_the_machine_it_manages(isolated, monkeypatch):
    client, store = isolated
    assert client.get("/api/sharing", headers=OWNER).json["machine"] == ""
    (store.data_dir).mkdir(parents=True, exist_ok=True)
    (store.data_dir / "machine-name").write_text("talker-086\n")
    assert client.get("/api/sharing", headers=OWNER).json["machine"] == "talker-086"
    monkeypatch.setattr(runner, "MACHINE_NAME", "worker-086")
    assert client.get("/api/sharing", headers=OWNER).json["machine"] == "worker-086"


def test_stale_screen_cannot_undo_gateway_email_upgrade(isolated):
    client, store = isolated
    assert change(client).status_code == 200
    old = client.get("/api/sharing", headers=OWNER).json["revision"]
    assert upgrade_invites_in_file(store.grants_path, "person@example.org", "durable-id")
    response = change(client, operation="remove", revision=old)
    assert response.status_code == 409
    assert load_grants(store.grants_path).services["files"].users == {"durable-id"}
    assert change(client, kind="users", value="durable-id", operation="remove").status_code == 200
    assert not load_grants(store.grants_path).allows("durable-id", "person@example.org", "files", {"files"})


@pytest.mark.parametrize("identity", [{"owner": False, "user_id": "visitor"}, {"owner": False, "agent_id": "visiting"}, {}, [], "malformed"])
def test_visitors_cannot_read_or_write_permissions(isolated, identity):
    client, store = isolated
    before = store.raw_grants()
    headers = {**OWNER, "X-Imbue-Identity": json.dumps(identity)}
    for path in ["/", "/api/sharing", "/raw", "/api/agents"]:
        assert client.get(path, headers=headers).status_code == 403
    assert client.post("/api/grants", headers=headers, json={}).status_code == 403
    assert client.post("/api/agents/invites", headers=headers, json={}).status_code == 403
    assert client.delete("/api/agents/other", headers=headers).status_code == 403
    assert store.raw_grants() == before


def test_cross_site_and_missing_action_header_are_refused(isolated):
    client, store = isolated
    assert client.post("/api/grants", json={}).status_code == 403
    assert client.post("/api/grants", json={}, headers={**OWNER, "Sec-Fetch-Site": "cross-site"}).status_code == 403
    assert store.raw_grants() == POLICY


@pytest.mark.parametrize("kind,value", [("emails", "invalid"), ("emails", "a@@example.org"), ("email_domains", "gmail.com"), ("email_domains", "bad_domain.org")])
def test_invalid_grants_never_change_the_policy(isolated, kind, value):
    client, store = isolated
    assert change(client, kind=kind, value=value).status_code == 400
    assert store.raw_grants() == POLICY


def test_invalid_unknown_and_duplicate_grants_are_refused(isolated):
    client, store = isolated
    assert change(client, scope="owner-exec").status_code == 400
    assert change(client, scope="missing").status_code == 400
    assert change(client, scope="sharing-manager").status_code == 400
    assert change(client, kind="users", value="forged-id").status_code == 400
    assert change(client, kind="email_domains", value="  @EXAMPLE.NET  ").status_code == 200
    assert load_grants(store.grants_path).services["files"].email_domains == {"example.net"}
    assert change(client, kind="email_domains", value="example.net").status_code == 400


def test_a_request_that_is_not_a_valid_change_is_refused_with_a_message(isolated):
    client, store = isolated
    response = client.post("/api/grants", headers=OWNER, json={"scope": "files"})
    assert response.status_code == 400
    assert "error" in response.json
    assert client.post("/api/grants", headers=OWNER, data="not json").status_code == 400
    assert store.raw_grants() == POLICY


def test_malformed_or_missing_policy_is_unknown_instead_of_empty(isolated):
    client, store = isolated
    store.grants_path.write_text("[workspace\nemails = [")
    assert client.get("/api/sharing", headers=OWNER).status_code == 503
    store.grants_path.unlink()
    assert client.get("/api/sharing", headers=OWNER).status_code == 503


def test_the_page_script_stylesheet_and_qr_library_are_served_and_nothing_else(isolated):
    client, _ = isolated
    for name in ["style.css", "sharing.js", "qrcode.js"]:
        assert client.get(f"/static/{name}", headers=OWNER).status_code == 200
    assert b"qrcode" in client.get("/static/qrcode.js", headers=OWNER).data
    assert client.get("/static/runner.py", headers=OWNER).status_code == 404
    assert client.get("/static/../runner.py", headers=OWNER).status_code == 404


def test_invites_and_revokes_are_scoped_and_use_verified_owner_identity(isolated, monkeypatch):
    client, store = isolated
    calls = []

    def run(arguments):
        calls.append(arguments)
        if arguments[0] == "invite":
            return {"instructions": "one-time instructions", "code": "test-code", "expires_at": "2026-10-21T20:00:00Z"}
        return {"revoked": "agent-test"}

    monkeypatch.setattr(runner, "run_agent_command", run)
    assert client.post("/api/agents/invites", headers=OWNER, json={"scope": "files"}).status_code == 200
    assert calls[0] == ["invite", "--granter", "user:owner-id", "--scope", "files", "--name-hint", ""]
    assert client.delete("/api/agents/agent-test", headers=OWNER).status_code == 200
    assert calls[1] == ["revoke", "agent-test", "--revoker", "user:owner-id", "--owner"]
    assert client.post("/api/agents/invites", headers=OWNER, json={"scope": "owner-exec"}).status_code == 400


def test_isolated_instances_cannot_mutate_real_visiting_agents(isolated):
    client, store = isolated
    assert client.get("/api/agents", headers=OWNER).json == {"agents": [], "available": True}
    assert client.post("/api/agents/invites", headers=OWNER, json={"scope": "files"}).status_code == 409
    assert client.delete("/api/agents/agent-test", headers=OWNER).status_code == 409


def test_a_workspace_without_visiting_agents_says_so_instead_of_failing(isolated, monkeypatch):
    client, store = isolated
    monkeypatch.setattr(runner, "IS_SANDBOX", False)
    monkeypatch.setattr(runner, "agent_access_command", lambda: None)
    assert client.get("/api/agents", headers=OWNER).json == {"agents": [], "available": False}
    response = client.post("/api/agents/invites", headers=OWNER, json={"scope": "files"})
    assert response.status_code == 501
    assert "not available" in response.json["error"]


@pytest.mark.parametrize("label,domain", [("files test", "example.test"), ("files/../x", "example.test"), ("files-test", "example.test/evil"), ("files-test", "Example.test")])
def test_a_link_is_never_built_from_odd_characters(label, domain):
    assert runner.shared_url({"label": label}, domain) is None
