"""Private, append-only custom supplier registration; no browser credentials.

Registration validates public HTTPS URLs without fetching their contents. Each
host owns one immutable JSON entry, atomically linked into place so concurrent
registrations cannot overwrite a supplier. Built-ins remain caller-owned data.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import re
import uuid
from urllib.parse import urlsplit, urlunsplit

from .transport import validate_url


def _public_https(value):
    if not isinstance(value, str) or not value or len(value) > 4096 or value != value.strip():
        raise ValueError("Enter a public HTTPS supplier URL")
    try:
        parts = urlsplit(value)
        if (parts.scheme != "https" or not parts.hostname or parts.username is not None
                or parts.password is not None or parts.port not in (None, 443)
                or parts.query or parts.fragment or any(ord(c) < 33 for c in value)
                or "\\" in value):
            raise ValueError
        host = parts.hostname.encode("idna").decode("ascii").lower()
        if host.endswith(".") or not re.fullmatch(r"[a-z0-9-]+(?:\.[a-z0-9-]+)+", host):
            raise ValueError
    except (ValueError, UnicodeError):
        raise ValueError("Use a public HTTPS URL without credentials, query parameters, fragments or custom ports") from None
    return urlunsplit(("https", host, parts.path or "/", "", "")), host.removeprefix("www.")


class SupplierRegistry:
    def __init__(self, data_root, defaults):
        self.root = Path(data_root) / "supplier-registry"
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.root.chmod(0o700)
        self.defaults = deepcopy(defaults)
        self._custom = {}
        self._load()

    def _load(self):
        custom = {}
        built_in_hosts = {urlsplit(item["origin"]).hostname.removeprefix("www.") for item in self.defaults.values()}
        for path in sorted(self.root.glob("custom_*.json")):
            if path.is_symlink():
                raise ValueError("Supplier registry contains an unsupported linked file")
            item = json.loads(path.read_text(encoding="utf-8"))
            website, host = _public_https(item.get("website_url"))
            login, login_host = _public_https(item.get("entry_url"))
            identifier = "custom_" + hashlib.sha256(host.encode()).hexdigest()
            if (item.get("schema") != 1 or item.get("id") != identifier or path.stem != identifier
                    or item.get("custom") is not True or host != login_host or host in built_in_hosts
                    or item.get("origin") != "https://" + host
                    or not isinstance(item.get("name"), str) or not item["name"].strip()
                    or website != item["website_url"] or login != item["entry_url"]):
                raise ValueError("Supplier registry entry is invalid")
            custom[identifier] = item
        self._custom = custom

    def items(self):
        self._load()
        defaults = {key: {**deepcopy(item), "id": key, "custom": False,
                          "website_url": item["origin"] + "/"} for key, item in self.defaults.items()}
        return {**defaults, **deepcopy(self._custom)}

    def get(self, key):
        item = self.items().get(key)
        if item is None:
            raise ValueError("unknown_supplier")
        return item

    def supplier_for(self, url):
        try:
            parts = urlsplit(url)
            if parts.scheme != "https" or parts.port not in (None, 443) or parts.username is not None or parts.password is not None:
                return None
            host = (parts.hostname or "").removeprefix("www.")
        except (TypeError, ValueError):
            return None
        return next((key for key, item in self.items().items()
                     if urlsplit(item["origin"]).hostname.removeprefix("www.") == host), None)

    def canonical_origin(self, origin):
        try:
            parts = urlsplit(origin)
            if parts.path not in ("", "/") or parts.query or parts.fragment:
                return origin
        except (TypeError, ValueError):
            return origin
        key = self.supplier_for(origin)
        return self.get(key)["origin"] if key else origin

    async def add(self, name, website_url, login_url=None):
        if not isinstance(name, str) or not name.strip() or len(name.strip()) > 120 or any(ord(c) < 32 for c in name):
            raise ValueError("Enter a supplier name between 1 and 120 characters")
        website, host = _public_https(website_url)
        login, login_host = _public_https(login_url if login_url is not None else website)
        if host != login_host:
            raise ValueError("The sign-in page must use the supplier's same domain, with or without www")
        if self.supplier_for(website) is not None:
            raise ValueError("A supplier for this website is already registered")
        # Validate DNS/public addresses; never fetch a page or open a session.
        await validate_url(website)
        if urlsplit(login).hostname != urlsplit(website).hostname:
            await validate_url(login)
        identifier = "custom_" + hashlib.sha256(host.encode()).hexdigest()
        item = {"schema": 1, "id": identifier, "name": name.strip(), "custom": True,
                "origin": "https://" + host, "website_url": website, "entry_url": login}
        target = self.root / (identifier + ".json")
        temporary = self.root / ("." + uuid.uuid4().hex + ".tmp")
        try:
            with temporary.open("x", encoding="utf-8") as stream:
                os.chmod(temporary, 0o600)
                json.dump(item, stream, ensure_ascii=False, allow_nan=False)
                stream.flush()
                os.fsync(stream.fileno())
            try:
                # Atomic create-without-replace, including across app processes.
                os.link(temporary, target)
            except FileExistsError:
                raise ValueError("A supplier for this website is already registered") from None
            descriptor = os.open(self.root, os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        finally:
            temporary.unlink(missing_ok=True)
        self._custom[identifier] = item
        return deepcopy(item)

    async def remove(self, identifier):
        self._load()
        if identifier not in self._custom:
            raise ValueError("unknown_supplier")
        target = self.root / (identifier + ".json")
        target.unlink(missing_ok=True)
        descriptor = os.open(self.root, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        self._custom.pop(identifier, None)
