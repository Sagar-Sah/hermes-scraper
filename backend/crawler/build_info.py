"""Local build identity and launcher checks. Standard library only; no application imports.

Fingerprints cover code/build inputs, never .env, data, browser profiles or Git metadata.
Running this file during a dashboard build must not initialize the database or worker.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, build_opener

SCHEMA_VERSION = 1
FEATURE_VERSION = "local-build-v1"
FEATURES = ["selective_collection_export", "saved_export_cleanup", "build_identity", "managed_updates", "product_timing", "bounded_modern_collection_chunks"]
ROOT = Path(__file__).resolve().parents[2]
_CACHE: dict = {}


def _inputs(root: Path, group: str) -> list[Path]:
    if group == "backend":
        paths = list((root / "backend/crawler").rglob("*.py"))
        paths += list((root / "backend/crawler/contracts").rglob("*.json"))
        paths += [root / "backend/requirements.lock", root / "backend/pyproject.toml"]
    elif group == "frontend":
        paths = [p for folder in ("src", "public") for p in (root / "frontend" / folder).rglob("*") if p.is_file()]
        paths += [root / "frontend" / name for name in ("package.json", "package-lock.json", "index.html", "vite.config.ts")]
        paths += list((root / "frontend").glob("tsconfig*.json"))
    elif group == "python_dependencies":
        paths = [root / "backend/requirements.lock", root / "backend/pyproject.toml"]
    elif group == "frontend_dependencies":
        paths = [root / "frontend/package.json", root / "frontend/package-lock.json"]
    else:
        raise ValueError("Unknown fingerprint group")
    result = sorted(set(p for p in paths if p.is_file()), key=lambda p: p.relative_to(root).as_posix())
    for path in result:
        if path.is_symlink() or not path.resolve().is_relative_to(root.resolve()):
            raise ValueError("Build input must be a contained regular file")
    if not result:
        raise ValueError("Build inputs are missing")
    return result


def _digest_paths(root: Path, paths: list[Path]) -> str:
    digest = hashlib.sha256()
    for path in paths:
        digest.update(path.relative_to(root).as_posix().encode() + b"\0")
        digest.update(hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


def source_digest(root: Path = ROOT, group: str = "backend") -> str:
    root = Path(root).resolve()
    paths = _inputs(root, group)
    signature = tuple((str(p), p.stat().st_size, p.stat().st_mtime_ns, p.stat().st_ctime_ns) for p in paths)
    key = (str(root), group)
    cached = _CACHE.get(key)
    if cached is None or cached[0] != signature:
        cached = (signature, _digest_paths(root, paths))
        _CACHE[key] = cached
    return cached[1]


def dependency_digest(root: Path, group: str) -> str:
    digest = source_digest(root, group)
    # A recreated environment or a changed interpreter also needs installation.
    if group == "python_dependencies":
        digest = hashlib.sha256((digest + sys.version + sys.prefix).encode()).hexdigest()
    return digest


def dependency_ready(root: Path, group: str) -> bool:
    stamp = root / ".local-build" / (group + ".json")
    try:
        return json.loads(stamp.read_text())["sha256"] == dependency_digest(root, group)
    except (OSError, ValueError, KeyError, TypeError):
        return False


def _write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, sort_keys=True, indent=2) + "\n")
    temporary.replace(path)


def stamp_dependencies(root: Path, group: str) -> None:
    _write_json(root / ".local-build" / (group + ".json"), {"sha256": dependency_digest(root, group)})


def _assets(root: Path, dist: Path | None = None) -> list[Path]:
    dist = dist or root / "frontend/dist"
    paths = sorted((p for p in dist.rglob("*") if p.is_file() and p.name != "build-info.json"), key=lambda p: p.relative_to(dist).as_posix())
    for path in paths:
        if path.is_symlink() or not path.resolve().is_relative_to(dist.resolve()):
            raise ValueError("Dashboard asset must be a contained regular file")
    return paths


def _asset_hashes(root: Path, dist: Path | None = None) -> dict:
    dist = dist or root / "frontend/dist"
    paths = _assets(root, dist)
    signature = tuple((str(p), p.stat().st_size, p.stat().st_mtime_ns, p.stat().st_ctime_ns) for p in paths)
    key = (str(dist), "asset_hashes")
    cached = _CACHE.get(key)
    if cached is None or cached[0] != signature:
        cached = (signature, {p.relative_to(dist).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest() for p in paths})
        _CACHE[key] = cached
    return cached[1]


def stamp_frontend(root: Path, expected_source: str, dist: Path | None = None) -> dict:
    if source_digest(root, "frontend") != expected_source:
        raise ValueError("Dashboard sources changed during build; rebuild before starting")
    dist = dist or root / "frontend/dist"
    if not (dist / "index.html").is_file():
        raise ValueError("Dashboard build did not produce index.html")
    manifest = {"schema_version": SCHEMA_VERSION, "source_sha256": expected_source,
                "built_at": int(time.time()), "assets": _asset_hashes(root, dist)}
    _write_json(dist / "build-info.json", manifest)
    return manifest


def frontend_state(root: Path = ROOT) -> dict:
    # Release containers intentionally ship only dist. Verify its manifest/assets;
    # do not pretend that unavailable source inputs have been compared.
    source_available = (root / "frontend/package.json").is_file()
    source = source_digest(root, "frontend") if source_available else None
    try:
        manifest = json.loads((root / "frontend/dist/build-info.json").read_text())
        assets = manifest["assets"]
        built = manifest.get("source_sha256")
        if (manifest["schema_version"] != SCHEMA_VERSION or not isinstance(assets, dict) or "index.html" not in assets
                or not isinstance(built, str) or re.fullmatch(r"[0-9a-f]{64}", built) is None
                or not all(isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) for value in assets.values())):
            raise ValueError("Invalid dashboard stamp")
        valid = _asset_hashes(root) == assets
        return {"source_sha256": source, "source_check_available": source_available, "built_source_sha256": built,
                "assets_valid": valid, "rebuild_required": not valid or (source_available and built != source)}
    except (OSError, ValueError, TypeError, KeyError):
        return {"source_sha256": source, "source_check_available": source_available,
                "built_source_sha256": None, "assets_valid": False, "rebuild_required": True}


def build_frontend(root: Path = ROOT, vite_args: list[str] | None = None) -> None:
    vite_args = list(vite_args or [])
    dist = root / "frontend/dist"
    for index, argument in enumerate(vite_args):
        value = None
        if argument == "--outDir":
            if index + 1 >= len(vite_args):
                raise ValueError("--outDir requires an output directory")
            value = vite_args[index + 1]
        elif argument.startswith("--outDir="):
            value = argument.split("=", 1)[1]
        if value is not None:
            dist = (root / "frontend" / value).resolve()
    source = source_digest(root, "frontend")
    # Remove the previous successful stamp first. A failed build is never current.
    (dist / "build-info.json").unlink(missing_ok=True)
    env = {**os.environ, "HERMES_UI_BUILD_SHA": source}
    command = ["npm", "run", "build:assets"] + (["--", *vite_args] if vite_args else [])
    subprocess.run(command, cwd=root / "frontend", env=env, check=True)
    stamp_frontend(root, source, dist)
    print(f"Hermes dashboard built: {source[:12]}")


class RuntimeBuild:
    def __init__(self, root: Path = ROOT):
        self.root = Path(root).resolve()
        self.started_at = int(time.time())
        self.backend_sha = source_digest(self.root, "backend")

    def snapshot(self) -> dict:
        current = source_digest(self.root, "backend")
        frontend = frontend_state(self.root)
        return {"schema_version": SCHEMA_VERSION, "feature_version": FEATURE_VERSION,
                "features": list(FEATURES), "started_at": self.started_at,
                "runtime_backend_sha256": self.backend_sha, "source_backend_sha256": current,
                "restart_required": current != self.backend_sha, "frontend": frontend,
                "checkout_id": hashlib.sha256(str(self.root).encode()).hexdigest()[:16]}

    def diagnostic(self) -> str:
        info = self.snapshot()
        ui = info["frontend"]
        return (f"Hermes runtime {self.backend_sha[:12]} ({FEATURE_VERSION}); "
                f"dashboard {(ui['built_source_sha256'] or 'unstamped')[:12]}; "
                f"restart_required={info['restart_required']} rebuild_required={ui['rebuild_required']}")


def check_running(root: Path, port: int) -> int:
    """Only probes loopback. Refuses a second worker and never stops an existing one."""
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1):
            pass
    except OSError:
        return 0
    try:
        with build_opener(ProxyHandler({})).open(f"http://127.0.0.1:{port}/api/v1/health", timeout=2) as response:
            info = json.loads(response.read(65536))
        build = info.get("build", {})
        expected = RuntimeBuild(root).snapshot()
        if (build.get("checkout_id") == expected["checkout_id"]
                and build.get("runtime_backend_sha256") == expected["source_backend_sha256"]
                and build.get("restart_required") is False
                and build.get("frontend", {}).get("built_source_sha256") == expected["frontend"]["source_sha256"]
                and build.get("frontend", {}).get("rebuild_required") is False):
            print(f"Hermes is already running with this build at http://127.0.0.1:{port}; no second worker started.")
            return 10
        print("Hermes is already running with an older or different build. Pause collection and stop that service, then rerun ./scripts/start-local.sh. No files or workers were changed.", file=sys.stderr)
        return 11
    except (OSError, HTTPError, URLError, ValueError, TypeError, AttributeError):
        print(f"Port {port} is occupied by an unrecognized service. Stop it or choose another ATLAS_PORT; no second worker started.", file=sys.stderr)
        return 12


def main() -> int:
    if sys.argv[1:2] == ["build-frontend"]:
        build_parser = argparse.ArgumentParser(description="Build and stamp the dashboard; additional arguments pass to Vite")
        build_parser.add_argument("--root", type=Path, default=ROOT)
        args, vite_args = build_parser.parse_known_args(sys.argv[2:])
        build_frontend(args.root.resolve(), vite_args)
        return 0
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["digest", "dependency-ready", "stamp-dependencies", "frontend-ready", "build-frontend", "check-running", "describe"])
    parser.add_argument("group", nargs="?", default="backend")
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--port", type=int, default=18877)
    args = parser.parse_args()
    root = args.root.resolve()
    if args.action == "digest":
        print(source_digest(root, args.group))
    elif args.action == "dependency-ready":
        return 0 if dependency_ready(root, args.group) else 1
    elif args.action == "stamp-dependencies":
        stamp_dependencies(root, args.group)
    elif args.action == "frontend-ready":
        return 1 if frontend_state(root)["rebuild_required"] else 0
    elif args.action == "build-frontend":
        build_frontend(root)
    elif args.action == "check-running":
        return check_running(root, args.port)
    elif args.action == "describe":
        print(RuntimeBuild(root).diagnostic())
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        print(f"Hermes build check failed: {error}", file=sys.stderr)
        raise SystemExit(1)
