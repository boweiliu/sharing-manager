"""Read the live sharing policy and make locked, scoped edits without stale overwrites."""

import hashlib
import json
import re
import tomllib
from pathlib import Path
from typing import Literal
from uuid import uuid4

import tomlkit
from share_gateway.grants import locked_grants_file, parse_grants, write_grants_atomic

PUBLIC_DOMAINS = frozenset("gmail.com googlemail.com yahoo.com yahoo.ca yahoo.co.id yahoo.co.in yahoo.co.jp yahoo.co.nz yahoo.co.uk yahoo.com.ar yahoo.com.au yahoo.com.br yahoo.com.hk yahoo.com.mx yahoo.com.ph yahoo.com.sg yahoo.com.tw yahoo.com.vn yahoo.de yahoo.dk yahoo.es yahoo.fr yahoo.gr yahoo.ie yahoo.it yahoo.nl yahoo.no yahoo.pl yahoo.se hotmail.com outlook.com live.com msn.com aol.com icloud.com me.com mac.com proton.me protonmail.com pm.me gmx.com gmx.net mail.com yandex.com zoho.com qq.com 163.com 126.com naver.com hey.com fastmail.com tuta.io".split())
DOMAIN_PATTERN = re.compile(r"^[^\W_](?:[^\W_]|-)*(?<!-)(?:\.[^\W_](?:[^\W_]|-)*(?<!-))*\.[^\W\d_]{2,}$")


class GrantEditError(ValueError):
    """An invalid or conflicting sharing edit."""


def write_private(path: Path, text: str) -> None:
    """Replace a file atomically, creating its folder; the gateway's writer only creates the file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    write_grants_atomic(path, text)


def revision(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def normalized_value(kind: str, value: str) -> str:
    value = value.strip()
    if kind == "users":
        if not value or len(value) > 300:
            raise GrantEditError("Choose a valid account to remove.")
        return value
    value = value.lower()
    if kind == "email_domains":
        value = value.removeprefix("@")
        if not DOMAIN_PATTERN.fullmatch(value):
            raise GrantEditError("Enter an email domain, such as example.com.")
        if value in PUBLIC_DOMAINS:
            raise GrantEditError("Use an individual email address for public email providers.")
    elif kind == "emails":
        local, separator, domain = value.partition("@")
        if not separator or not local or re.search(r"\s|@", local) or not DOMAIN_PATTERN.fullmatch(domain):
            raise GrantEditError("Enter a valid email address, such as name@example.com.")
    return value


class SharingStore:
    def __init__(self, input_dir: Path, data_dir: Path) -> None:
        self.input_dir = input_dir
        self.data_dir = data_dir
        self.grants_path = input_dir / ".secrets/share_grants.toml"

    def raw_grants(self) -> str:
        text = self.grants_path.read_text(encoding="utf-8")
        parse_grants(text)
        return text

    def registered_apps(self) -> list[dict]:
        return tomllib.loads((self.input_dir / ".state/apps.toml").read_text()).get("apps", [])

    def sharing_status(self) -> dict:
        path = self.input_dir / ".state/share_gateway/status.json"
        if not path.exists():
            return {"state": "off", "workspace_domain": ""}
        return json.loads(path.read_text())

    def preserve_sources(self) -> None:
        """Keep the unprocessed inputs, with their source paths, for inspection and isolated previews."""
        dest = self.data_dir / "source"
        for relative in [".secrets/share_grants.toml", ".state/apps.toml", ".state/share_gateway/status.json"]:
            source = self.input_dir / relative
            target = dest / relative
            if source.exists() and source.resolve() != target.resolve():
                write_private(target, source.read_text())
        write_private(dest / "sources.json", json.dumps({"inputs": str(self.input_dir.resolve()), "grants": str(self.grants_path.resolve())}, indent=2))

    def edit(self, scope: str, kind: Literal["users", "emails", "email_domains"], value: str, operation: str, expected_revision: str) -> None:
        """Mutate one grant under the gateway's lock; reject stale screens instead of undoing concurrent edits."""
        if scope == "sharing-manager":
            raise GrantEditError("The sharing manager is available only to the workspace owner.")
        if scope != "workspace":
            if not any(a.get("name") == scope and not a.get("internal", False) for a in self.registered_apps()):
                raise GrantEditError("This app is not available for sharing.")
        if operation == "add" and kind == "users":
            raise GrantEditError("Add people by email address.")
        with locked_grants_file(self.grants_path):
            before = self.raw_grants()
            if revision(before) != expected_revision:
                raise GrantEditError("Permissions changed elsewhere. Refresh and try again.")
            normalized = normalized_value(kind, value) if operation == "add" else value
            document = tomlkit.parse(before)
            if scope == "workspace":
                if "workspace" not in document:
                    document["workspace"] = tomlkit.table()
                table = document["workspace"]
            else:
                if "services" not in document:
                    document["services"] = tomlkit.table()
                if scope not in document["services"]:
                    document["services"][scope] = tomlkit.table()
                table = document["services"][scope]
            values = list(table.get(kind, []))
            if operation == "add":
                if normalized in values:
                    raise GrantEditError("This permission already exists.")
                values.append(normalized)
            else:
                if normalized not in values:
                    raise GrantEditError("That permission changed elsewhere. Refresh and try again.")
                values.remove(normalized)
            table[kind] = values
            after = tomlkit.dumps(document)
            parse_grants(after)
            history = self.data_dir / "history" / (uuid4().hex + ".toml")
            write_private(history, before)
            write_grants_atomic(self.grants_path, after)
