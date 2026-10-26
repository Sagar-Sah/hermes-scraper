"""Build identity decisions run in Node; no browser or application service."""
import json
from pathlib import Path
import shutil
import subprocess

import pytest

FRONTEND = Path(__file__).resolve().parents[1]


def test_loaded_dashboard_and_backend_update_diagnostics():
    if not shutil.which("node") or not (FRONTEND / "node_modules/typescript").is_dir():
        pytest.skip("Install dashboard dependencies to run TypeScript checks")
    code = r"""
const fs = require('node:fs');
const vm = require('node:vm');
const {createRequire} = require('node:module');
const path = require('node:path');
const frontend = process.argv[1];
const localRequire = createRequire(path.join(frontend, 'package.json'));
const ts = localRequire('typescript');
const source = fs.readFileSync(path.join(frontend, 'src/BuildStatus.tsx'), 'utf8');
const js = ts.transpileModule(source, {compilerOptions: {
  module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX
}}).outputText;
const scope = {exports: {}, require: localRequire, __HERMES_UI_BUILD__: 'a'.repeat(64)};
vm.runInNewContext(js, scope);
const {buildWarning, loadedDashboardBuild} = scope.exports;
const current = {feature_version:'local-build-v1', features:['selective_collection_export'],
  runtime_backend_sha256:'b'.repeat(64), source_backend_sha256:'b'.repeat(64), restart_required:false,
  frontend:{built_source_sha256:'a'.repeat(64), rebuild_required:false, assets_valid:true}};
process.stdout.write(JSON.stringify({
  loaded:loadedDashboardBuild,
  current:buildWarning(current),
  backend:buildWarning({...current,restart_required:true}),
  assets:buildWarning({...current,frontend:{...current.frontend,rebuild_required:true}}),
  olderTab:buildWarning(current,'c'.repeat(64)),
  olderApi:buildWarning(undefined),
  dev:buildWarning(current,'development')
}));
"""
    result = subprocess.run(["node", "-e", code, str(FRONTEND)], text=True, capture_output=True, check=True)
    actual = json.loads(result.stdout)
    assert actual["loaded"] == "a" * 64
    assert actual["current"] is None
    assert "Open Update Hermes to install and restart safely" in actual["backend"]
    assert "dashboard build is outdated" in actual["assets"]
    assert "Reload this page" in actual["olderTab"]
    assert "backend does not report its build" in actual["olderApi"]
    assert actual["dev"] is None
