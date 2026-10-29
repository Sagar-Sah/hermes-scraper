"""Private, separate production-read and staging-write Companion connections.

The HTTP transport retains upstream staging protection. An optional Basic auth
credential serves that protection while the connector token uses its own header.
No credentials or remote response bodies are returned in status/errors.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
from urllib.parse import urlsplit

import httpx


DEFAULT_ORIGINS = {
    "production": {"https://printitprints.com", "https://www.printitprints.com"},
    "staging": {"https://staging3.tonyl36.sg-host.com"},
}


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def private_save(path: Path, value):
    from .product_companion_service import _atomic
    if path.is_symlink() or path.parent.is_symlink():
        raise ValueError("unsafe_companion_storage")
    _atomic(path, value)


class CompanionEnvironmentRegistry:
    def __init__(self, root, *, transport=None, raw_transport=None, approved_origins=None):
        self.root = Path(root) / "environments"
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.root.is_symlink():
            raise ValueError("unsafe_companion_storage")
        os.chmod(self.root, 0o700)
        self.approved_origins = approved_origins or DEFAULT_ORIGINS
        self.transport = transport or self._http
        # A custom JSON transport cannot be called "raw HTTP evidence". Tests
        # and alternate connectors supply an explicit raw transport as needed.
        self.raw_transport = raw_transport or (self._http_raw if transport is None else None)

    @staticmethod
    def _role(environment, role=None):
        if environment not in ("production", "staging"):
            raise ValueError("invalid_companion_environment")
        role = role or ("read" if environment == "production" else "write")
        if (environment, role) not in (("production", "read"), ("production", "release"), ("staging", "write")):
            raise ValueError("invalid_companion_environment_role")
        return role

    def _path(self, environment, role=None):
        return self.root / (environment + "-" + self._role(environment, role) + ".json")

    def _config(self, environment, role=None):
        path = self._path(environment, role)
        if path.is_symlink() or not path.is_file():
            raise ValueError("companion_environment_not_configured")
        return json.loads(path.read_text())

    def status(self, environment=None, *, role=None):
        if environment is None:
            return {key: self.status(env, role=r) for key, env, r in (
                ("production", "production", "read"), ("staging", "staging", "write"),
                ("production_release", "production", "release"))}
        try:
            cfg = self._config(environment, role)
        except ValueError:
            return {"configured": False, "connected": False}
        return {key: cfg[key] for key in ("environment", "role", "base_url", "connected", "capabilities", "reason") if key in cfg} | {
            "configured": True, "protected_preview_credentials_present": bool(cfg.get("protection_auth"))}

    async def configure(self, environment, base_url, token=None, protection_auth=None, *, role=None):
        role = self._role(environment, role)
        p = urlsplit(base_url)
        origin = f"{p.scheme}://{p.netloc}"
        if (p.scheme != "https" or p.username or p.password or p.port not in (None, 443)
                or p.path not in ("", "/") or p.query or p.fragment
                or origin not in self.approved_origins.get(environment, set())):
            raise ValueError("companion_origin_not_approved_for_environment")
        try:
            previous = self._config(environment, role)
        except ValueError:
            previous = None
        same_connection = (isinstance(previous, dict) and previous.get("environment") == environment
                           and previous.get("role") == role and previous.get("base_url") == origin)
        # Credentials are retained only within the exact already configured
        # origin and role. A blank token cannot grant access to another site or
        # turn an existing production read connection into a release grant.
        if token is None or token == "":
            if not same_connection:
                raise ValueError("saved_companion_connection_required")
            token = previous.get("token")
        if not isinstance(token, str) or not re.fullmatch(r"[A-Za-z0-9_-]{40,160}", token):
            raise ValueError("invalid_companion_connector_token")
        if protection_auth is not None:
            if (environment != "staging" or not isinstance(protection_auth, dict)
                    or set(protection_auth) != {"username", "password"}
                    or any(not isinstance(x, str) or not x or len(x) > 512 or any(ord(c) < 32 for c in x)
                           for x in protection_auth.values())):
                raise ValueError("invalid_staging_protection_credentials")
        cfg = {"environment": environment, "role": role, "base_url": origin,
               "token": token, "connected": False}
        if protection_auth:
            cfg["protection_auth"] = protection_auth
        elif same_connection and previous.get("protection_auth"):
            cfg["protection_auth"] = previous["protection_auth"]
        credentials_changed = (not same_connection or token != previous.get("token")
                               or cfg.get("protection_auth") != previous.get("protection_auth"))
        try:
            capabilities = await self.transport(cfg, "GET", "/capabilities", None)
            if (capabilities.get("environment") != environment or capabilities.get("role") != role
                    or capabilities.get("site_origin") != origin):
                raise ValueError("companion_environment_identity_mismatch")
            cfg.update(connected=True, capabilities={
                "schema": "hermes-companion-capabilities/v1",
                "connector_version": capabilities.get("connector_version") if re.fullmatch(r"[0-9]+(?:\.[0-9]+){1,3}", str(capabilities.get("connector_version", ""))) else "unknown",
                "inventory": capabilities.get("inventory") is True,
                "published_catalog": capabilities.get("published_catalog") is True,
                "read_families": [name for name in capabilities.get("read_families", []) if name in {"custom_csv", "pila", "pirc", "pigf", "dps", "wsdisplay", "woocommerce"}],
                "write_families": [name for name in capabilities.get("write_families", []) if name == "custom_csv"],
                "write_operations": [name for name in capabilities.get("write_operations", []) if name in {"set_cost", "add_row", "add_document"}],
            })
        except ValueError as exc:
            # Save private credentials for an explicitly configured connection,
            # but never replace an already working connection with a failed one.
            if self.status(environment, role=role).get("connected") and credentials_changed:
                raise
            if same_connection and isinstance(previous.get("capabilities"), dict):
                # Remember which lane was explicitly configured, while marking
                # it unverified. An expired catalog connector must not quietly
                # become a different public inventory transport after recheck.
                cfg["capabilities"] = previous["capabilities"]
            cfg["reason"] = str(exc) if re.fullmatch(r"[a-z0-9_]+", str(exc)) else "companion_handshake_failed"
        private_save(self._path(environment, role), cfg)
        return self.status(environment, role=role)

    @staticmethod
    async def _http(cfg, method, path, payload):
        status, _, raw, _ = await CompanionEnvironmentRegistry._http_raw(cfg, method, path, payload)
        if status not in (200, 201):
            raise ValueError("companion_connector_http_" + str(status))
        try:
            value = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise ValueError("companion_connector_schema_changed") from None
        if not isinstance(value, dict):
            raise ValueError("companion_connector_schema_changed")
        return value

    @staticmethod
    async def _http_raw(cfg, method, path, payload):
        """Return actual response bytes/URL without inventing public provenance."""
        auth = cfg.get("protection_auth")
        headers = {"X-Hermes-Companion-Token": cfg["token"], "Cache-Control": "no-cache"}
        if cfg.get("release_authorization"):
            headers.update({"X-Hermes-Release-Manifest": cfg["release_authorization"]["manifest_sha256"],
                            "X-Hermes-Release-Approval": cfg["release_authorization"]["approval_id"]})
        try:
            async with httpx.AsyncClient(timeout=45, follow_redirects=False,
                    auth=httpx.BasicAuth(auth["username"], auth["password"]) if auth else None) as client:
                response = await client.request(method,
                    cfg["base_url"] + "/wp-json/hermes-companion/v1" + path,
                    headers=headers, json=payload)
            return response.status_code, str(response.url), response.content, dict(response.headers)
        except httpx.HTTPError:
            raise ValueError("companion_connector_transport_failed") from None

    def published_catalog_connection(self, base_url):
        """Select only this site's verified production-read catalog capability.

        Connector0.2.0 and unconfigured sites retain the public catalog lane.
        An explicitly enabled but expired connector cannot silently fall back.
        """
        try:
            cfg = self._config("production", "read")
        except ValueError:
            path = self._path("production", "read")
            if path.exists() or path.is_symlink():
                raise ValueError("companion_catalog_connection_invalid") from None
            return None
        if not isinstance(cfg, dict) or not isinstance(cfg.get("capabilities") or {}, dict):
            raise ValueError("companion_catalog_connection_invalid")
        if (cfg.get("capabilities") or {}).get("published_catalog") is not True or cfg.get("base_url") != base_url:
            return None
        if (cfg.get("environment") != "production" or cfg.get("role") != "read"
                or cfg.get("base_url") not in self.approved_origins.get("production", set())):
            raise ValueError("companion_catalog_connection_identity_changed")
        if cfg.get("connected") is not True:
            raise ValueError("companion_catalog_connection_unverified")
        return cfg

    async def published_catalog_page(self, base_url, page):
        if type(page) is not int or not 1 <= page <= 1000:
            raise ValueError("invalid_companion_catalog_page")
        cfg = self.published_catalog_connection(base_url)
        if cfg is None:
            raise ValueError("companion_published_catalog_not_supported")
        if not callable(self.raw_transport):
            raise ValueError("companion_catalog_raw_transport_required")
        return await self.raw_transport(cfg, "GET", "/catalog?page=" + str(page), None)

    async def request(self, environment, method, path, payload=None, *, release_authorization=None):
        method = str(method).upper()
        if not isinstance(path, str) or not re.fullmatch(r"/[A-Za-z0-9/_?=&.-]+", path) or ".." in path:
            raise ValueError("invalid_companion_connector_path")
        role = "read" if environment == "production" else "write"
        if method not in ("GET", "HEAD"):
            if method != "POST" or not re.fullmatch(r"/(?:previews|apply/[a-f0-9]{32}|rollback/[a-f0-9]{32})", path):
                raise ValueError("unsupported_companion_write_endpoint")
            if environment == "production":
                if (not isinstance(release_authorization, dict)
                        or release_authorization.get("actor") != "human"
                        or not re.fullmatch(r"[a-f0-9]{64}", str(release_authorization.get("manifest_sha256", "")))
                        or not re.fullmatch(r"[a-f0-9]{32}", str(release_authorization.get("approval_id", "")))):
                    raise ValueError("manual_production_approval_required")
                role = "release"
        cfg = self._config(environment, role)
        if not cfg.get("connected"):
            raise ValueError("companion_environment_connection_unverified")
        if role == "release":
            cfg = {**cfg, "release_authorization": release_authorization}
        return await self.transport(cfg, method, path, payload)

    async def snapshot(self, environment, product_id):
        if not re.fullmatch(r"[1-9][0-9]{0,11}", str(product_id)):
            raise ValueError("invalid_companion_product_id")
        return await self.request(environment, "GET", "/product-snapshot/" + str(product_id))

    async def inventory(self, environment="production", cursor=None):
        if cursor is not None and (type(cursor) is not int or not 1 <= cursor <= 10**7):
            raise ValueError("invalid_companion_inventory_cursor")
        return await self.request(environment, "GET", "/inventory?page=" + str(cursor or 1))

    async def environment_snapshot(self, environment):
        return await self.request(environment, "GET", "/environment-snapshot")

    def _browser_options(self, environment):
        """Internal verifier-only options. Never expose through a route/status."""
        cfg = self._config(environment)
        auth = cfg.get("protection_auth")
        return {"http_credentials": dict(auth)} if auth else {}
