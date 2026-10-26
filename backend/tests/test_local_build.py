"""Update-path regressions use temporary checkouts and fake installers, never a worker."""
import ast
import importlib.util
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest

REPO = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("hermes_build_info_test", REPO / "backend/crawler/build_info.py")
build = importlib.util.module_from_spec(spec)
spec.loader.exec_module(build)


@pytest.fixture
def checkout(tmp_path):
    for folder in ("scripts", "backend/crawler", "frontend/src", "frontend/public", ".venv/bin", "bin"):
        (tmp_path / folder).mkdir(parents=True, exist_ok=True)
    shutil.copy2(REPO / "scripts/start-local.sh", tmp_path / "scripts/start-local.sh")
    shutil.copy2(REPO / "backend/crawler/build_info.py", tmp_path / "backend/crawler/build_info.py")
    shutil.copy2(REPO / "backend/crawler/managed_update.py", tmp_path / "backend/crawler/managed_update.py")
    files = {
        "backend/crawler/app.py": "raise AssertionError('build must not import app or Store')\n",
        "backend/crawler/store.py": "raise AssertionError('build must not initialize Store')\n",
        "backend/requirements.lock": "locked-python-dependency==1\n",
        "backend/pyproject.toml": "[project]\nname='fixture'\nversion='1'\n",
        "frontend/package.json": '{"name":"fixture","scripts":{"build":"fixture"}}',
        "frontend/package-lock.json": '{"name":"fixture","lockfileVersion":3}',
        "frontend/index.html": '<html><div id="root"></div></html>',
        "frontend/vite.config.ts": "// fixture config\n",
        "frontend/tsconfig.json": '{}',
        "frontend/src/App.tsx": "export const controls = 'old dashboard';\n",
    }
    for name, contents in files.items():
        (tmp_path / name).write_text(contents)
    python = f'''#!{sys.executable}
import json, os, subprocess, sys
from pathlib import Path
if len(sys.argv)>2 and sys.argv[1].endswith('managed_update.py') and sys.argv[2]=='supervise':
    with open(os.environ['BUILD_TEST_LOG'], 'a') as f: f.write(json.dumps(['python','supervise',*sys.argv[3:]])+'\\n')
    raw_data_dir=os.environ.get('ATLAS_DATA_DIR')
    from dotenv import load_dotenv
    load_dotenv(Path.cwd()/'.env', override=False)
    Path(os.environ['BUILD_TEST_LOG']).with_name('worker-environment.json').write_text(json.dumps({{'raw_data_dir':raw_data_dir,'data_dir':os.environ.get('ATLAS_DATA_DIR'),'browser_concurrency':os.environ.get('ATLAS_BROWSER_PRODUCT_CONCURRENCY')}}))
    sys.exit(0)
if sys.argv[1:2] == ['-m']:
    with open(os.environ['BUILD_TEST_LOG'], 'a') as f: f.write(json.dumps(['python', *sys.argv[1:]])+'\\n')
    if os.environ.get('FAIL_PIP') == '1' and sys.argv[2:3] == ['pip']: sys.exit(9)
    sys.exit(0)
os.execv(sys.executable, [sys.executable, *sys.argv[1:]])
'''
    npm = f'''#!{sys.executable}
import json, os, subprocess, sys
from pathlib import Path
with open(os.environ['BUILD_TEST_LOG'], 'a') as f: f.write(json.dumps(['npm', *sys.argv[1:]])+'\\n')
if sys.argv[1:] == ['ci']:
    Path('node_modules').mkdir(exist_ok=True)
elif sys.argv[1:] == ['run', 'build']:
    sys.exit(subprocess.call([sys.executable, '../backend/crawler/build_info.py', 'build-frontend']))
elif sys.argv[1:3] == ['run', 'build:assets']:
    if os.environ.get('FAIL_BUILD') == '1': sys.exit(8)
    args=sys.argv[3:]
    output=Path(args[args.index('--outDir')+1]) if '--outDir' in args else Path('dist')
    (output/'assets').mkdir(parents=True, exist_ok=True)
    (output/'index.html').write_text('<html><script src="assets/main.js"></script></html>')
    (output/'assets/main.js').write_text(os.environ['HERMES_UI_BUILD_SHA'])
else:
    sys.exit(7)
'''
    for path, text in ((tmp_path / ".venv/bin/python", python), (tmp_path / "bin/npm", npm)):
        path.write_text(text)
        path.chmod(0o755)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    return SimpleNamespace(root=tmp_path, env={**os.environ, "PATH": str(tmp_path / "bin") + os.pathsep + os.environ["PATH"],
        "BUILD_TEST_LOG": str(tmp_path / "commands.jsonl"), "ATLAS_PORT": str(port)})


def launch(checkout, **extra):
    return subprocess.run(["bash", "scripts/start-local.sh"], cwd=checkout.root,
        env={**checkout.env, **extra}, text=True, capture_output=True, timeout=20)


def commands(checkout):
    path = checkout.root / "commands.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def reset_commands(checkout):
    (checkout.root / "commands.jsonl").write_text("")


def test_first_launch_stamps_dependencies_and_build_then_starts(checkout):
    result = launch(checkout)
    assert result.returncode == 0, result.stderr
    calls = commands(checkout)
    assert ["npm", "ci"] in calls
    assert ["npm", "run", "build"] in calls
    assert sum(call[:2] == ["python", "supervise"] for call in calls) == 1
    assert build.frontend_state(checkout.root)["rebuild_required"] is False
    assert not (checkout.root / "data").exists()
    assert "Hermes runtime" in result.stdout


def test_unchanged_launch_reuses_verified_build_and_installs_nothing(checkout):
    assert launch(checkout).returncode == 0
    reset_commands(checkout)
    assert launch(checkout).returncode == 0
    assert all(call[:2] == ["python", "supervise"] for call in commands(checkout))


def test_git_updated_frontend_source_forces_rebuild_without_dependency_install(checkout):
    assert launch(checkout).returncode == 0
    old = build.frontend_state(checkout.root)["built_source_sha256"]
    reset_commands(checkout)
    (checkout.root / "frontend/src/App.tsx").write_text("export const controls = 'new export controls';\n")
    result = launch(checkout)
    assert result.returncode == 0, result.stderr
    assert ["npm", "run", "build"] in commands(checkout)
    assert ["npm", "ci"] not in commands(checkout)
    assert not any(call[:3] == ["python", "-m", "pip"] for call in commands(checkout))
    assert build.frontend_state(checkout.root)["built_source_sha256"] != old


@pytest.mark.parametrize("path", ["frontend/package-lock.json", "frontend/package.json"])
def test_changed_frontend_dependencies_install_and_rebuild(checkout, path):
    assert launch(checkout).returncode == 0
    reset_commands(checkout)
    target = checkout.root / path
    target.write_text(target.read_text() + "\n")
    assert launch(checkout).returncode == 0
    assert ["npm", "ci"] in commands(checkout)
    assert ["npm", "run", "build"] in commands(checkout)


def test_changed_python_requirements_install_before_launch(checkout):
    assert launch(checkout).returncode == 0
    reset_commands(checkout)
    (checkout.root / "backend/requirements.lock").write_text("changed-lock==2\n")
    assert launch(checkout).returncode == 0
    calls = commands(checkout)
    assert ["python", "-m", "pip", "install", "-r", "backend/requirements.lock"] in calls
    assert ["python", "-m", "playwright", "install", "chromium"] in calls
    assert ["npm", "run", "build"] not in calls


def test_failed_rebuild_cannot_launch_stale_dashboard(checkout):
    assert launch(checkout).returncode == 0
    reset_commands(checkout)
    (checkout.root / "frontend/src/App.tsx").write_text("new controls\n")
    result = launch(checkout, FAIL_BUILD="1")
    assert result.returncode != 0
    assert not any(call[:2] == ["python", "supervise"] for call in commands(checkout))
    assert build.frontend_state(checkout.root)["rebuild_required"] is True
    assert not (checkout.root / "frontend/dist/build-info.json").exists()


def test_failed_dependency_install_is_not_stamped(checkout):
    result = launch(checkout, FAIL_PIP="1")
    assert result.returncode != 0
    assert not (checkout.root / ".local-build/python_dependencies.json").exists()
    assert not any(call[:2] == ["python", "supervise"] for call in commands(checkout))


def test_tampered_or_missing_assets_force_rebuild(checkout):
    assert launch(checkout).returncode == 0
    reset_commands(checkout)
    (checkout.root / "frontend/dist/assets/main.js").write_text("older UI")
    assert build.frontend_state(checkout.root)["rebuild_required"] is True
    assert launch(checkout).returncode == 0
    assert ["npm", "run", "build"] in commands(checkout)


def test_runtime_reports_restart_after_source_update_and_clears_on_restart(checkout):
    assert launch(checkout).returncode == 0
    runtime = build.RuntimeBuild(checkout.root)
    old = runtime.snapshot()["runtime_backend_sha256"]
    (checkout.root / "backend/crawler/app.py").write_text("new backend source\n")
    changed = runtime.snapshot()
    assert changed["restart_required"] is True
    assert changed["runtime_backend_sha256"] == old
    assert changed["source_backend_sha256"] != old
    assert build.RuntimeBuild(checkout.root).snapshot()["restart_required"] is False


def test_private_data_and_tests_do_not_change_runtime_build(checkout):
    original = build.source_digest(checkout.root)
    for name in (".env", "data/account.json", "backend/tests/test_new.py"):
        path = checkout.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("synthetic-private-value")
    assert build.source_digest(checkout.root) == original


def test_dist_only_release_verifies_assets_without_claiming_source_comparison(checkout):
    assert launch(checkout).returncode == 0
    shutil.rmtree(checkout.root / "frontend/src")
    (checkout.root / "frontend/package.json").unlink()
    state = build.frontend_state(checkout.root)
    assert state["source_check_available"] is False
    assert state["source_sha256"] is None
    assert state["assets_valid"] is True
    assert state["rebuild_required"] is False


def test_invalid_manifest_never_crashes_health_or_looks_current(checkout):
    assert launch(checkout).returncode == 0
    manifest = checkout.root / "frontend/dist/build-info.json"
    value = json.loads(manifest.read_text())
    value["source_sha256"] = {"malformed": True}
    manifest.write_text(json.dumps(value))
    runtime = build.RuntimeBuild(checkout.root)
    assert runtime.snapshot()["frontend"]["rebuild_required"] is True
    assert "dashboard unstamped" in runtime.diagnostic()


def test_dotenv_argument_and_explicit_browser_setting_preserved(checkout):
    (checkout.root / ".env").write_text("ATLAS_BROWSER_PRODUCT_CONCURRENCY=99\n")
    result = launch(checkout, ATLAS_BROWSER_HEADLESS="true")
    assert result.returncode == 0, result.stderr
    call = next(call for call in commands(checkout) if call[:2] == ["python", "supervise"])
    assert json.loads((checkout.root / "worker-environment.json").read_text())["browser_concurrency"] == "99"


def test_existing_dotenv_data_directory_is_not_overridden_by_launcher(checkout):
    checkout.env.pop("ATLAS_DATA_DIR", None)
    existing = str(checkout.root / "existing-campaign-data")
    (checkout.root / ".env").write_text(f"ATLAS_DATA_DIR={existing}\n")
    result = launch(checkout)
    assert result.returncode == 0, result.stderr
    captured = json.loads((checkout.root / "worker-environment.json").read_text())
    assert captured["raw_data_dir"] is None and captured["data_dir"] == existing
    assert not (checkout.root / "data").exists()


def test_explicit_shell_data_directory_preserves_precedence(checkout):
    (checkout.root / ".env").write_text("ATLAS_DATA_DIR=other-fixture-data\n")
    explicit = str(checkout.root / "explicit-campaign-data")
    result = launch(checkout, ATLAS_DATA_DIR=explicit)
    assert result.returncode == 0, result.stderr
    captured = json.loads((checkout.root / "worker-environment.json").read_text())
    assert captured["raw_data_dir"] == explicit and captured["data_dir"] == explicit


def test_port_check_and_worker_use_the_same_explicit_shell_port(checkout):
    # Uvicorn's port is a CLI setting. A dotenv ATLAS_PORT is deliberately not
    # used by this launcher; README documents the explicit shell invocation.
    (checkout.root / ".env").write_text("ATLAS_PORT=1\n")
    assert launch(checkout).returncode == 0
    call = next(call for call in commands(checkout) if call[:2] == ["python", "supervise"])
    assert call[call.index("--port") + 1] == checkout.env["ATLAS_PORT"]


@pytest.fixture
def health_server():
    value = {}
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            assert self.path == "/api/v1/health"
            self.send_response(200)
            self.end_headers()
            self.wfile.write(json.dumps(value).encode())
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield value, server.server_port
    server.shutdown()
    thread.join()
    server.server_close()


def test_existing_current_backend_does_not_start_second_worker(checkout, health_server):
    assert launch(checkout).returncode == 0
    reset_commands(checkout)
    value, port = health_server
    value.update({"build": build.RuntimeBuild(checkout.root).snapshot()})
    result = launch(checkout, ATLAS_PORT=str(port))
    assert result.returncode == 0
    assert "already running with this build" in result.stdout
    assert commands(checkout) == []


@pytest.mark.parametrize("old_health", [False, True])
def test_existing_stale_backend_refuses_with_restart_instruction(checkout, health_server, old_health):
    assert launch(checkout).returncode == 0
    reset_commands(checkout)
    value, port = health_server
    value.update({"status": "ok"} if old_health else {"build": build.RuntimeBuild(checkout.root).snapshot()})
    (checkout.root / "backend/crawler/app.py").write_text("newly pulled backend\n")
    result = launch(checkout, ATLAS_PORT=str(port))
    assert result.returncode != 0
    assert "Pause collection and stop" in result.stderr
    assert commands(checkout) == []


def test_health_includes_runtime_identity_without_importing_application(checkout):
    tree = ast.parse((REPO / "backend/crawler/app.py").read_text())
    health = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "health")
    health.decorator_list = []
    namespace = {"store": SimpleNamespace(sqlite=True), "runtime_build": build.RuntimeBuild(checkout.root),
        "settings": SimpleNamespace(api_token=None, persist_browser_sessions=True, execution_capacity=lambda: {}),
        "browser_workload": SimpleNamespace(snapshot=lambda: {})}
    exec(compile(ast.Module(body=[health], type_ignores=[]), "health-only", "exec"), namespace)
    info = namespace["health"]()
    assert info["build"]["feature_version"] == "local-build-v1"
    assert "selective_collection_export" in info["build"]["features"]


def test_changed_sources_during_build_cannot_be_stamped(checkout):
    source = build.source_digest(checkout.root, "frontend")
    (checkout.root / "frontend/dist").mkdir()
    (checkout.root / "frontend/dist/index.html").write_text("index")
    (checkout.root / "frontend/src/App.tsx").write_text("changed during build")
    with pytest.raises(ValueError, match="changed during build"):
        build.stamp_frontend(checkout.root, source)


def test_external_vite_output_is_stamped_without_changing_default_assets(checkout, tmp_path):
    assert launch(checkout).returncode == 0
    manifest = checkout.root / "frontend/dist/build-info.json"
    original = manifest.read_bytes()
    output = tmp_path / "external-output"
    result = subprocess.run([sys.executable, str(checkout.root / "backend/crawler/build_info.py"),
        "build-frontend", "--outDir", str(output), "--emptyOutDir"], env=checkout.env,
        text=True, capture_output=True, timeout=20)
    assert result.returncode == 0, result.stderr
    assert (output / "index.html").is_file()
    assert json.loads((output / "build-info.json").read_text())["source_sha256"] == build.source_digest(checkout.root, "frontend")
    assert manifest.read_bytes() == original


def test_symlinked_source_or_asset_is_rejected(checkout):
    (checkout.root / "frontend/src/external.ts").symlink_to(checkout.root / "backend/requirements.lock")
    with pytest.raises(ValueError, match="regular file"):
        build.source_digest(checkout.root, "frontend")

