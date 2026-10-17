"""A helper-only stability fix must invalidate durable browser branch markers."""

import ast
from pathlib import Path
import shutil

from crawler import browser_progress


def test_helper_only_behavior_change_invalidates_scope_but_comments_do_not(tmp_path, monkeypatch):
    source_root = Path(browser_progress.__file__).parent
    # Mirror the files consumed by the fingerprint into a temporary source tree.
    # Include the helper independently: omitting it from adapter_semantics must
    # make the behavior-change assertion below fail.
    names = set(browser_progress.adapter_semantics()) | {"browser_stability.py"}
    for name in names:
        target = tmp_path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source_root / name, target)
    monkeypatch.setattr(browser_progress, "__file__", str(tmp_path / "browser_progress.py"))

    spec = {"url": "https://fixture.invalid/product", "selection_filters": {}}
    def after_restart():
        # Production reads these files once per process. Simulate restarting
        # after an update rather than treating edited files as hot reloaded.
        browser_progress.adapter_semantics.cache_clear()
        return browser_progress.extraction_scope(spec)

    try:
        before = after_restart()
        helper = tmp_path / "browser_stability.py"
        source = helper.read_text()
        helper.write_text("# Documentation-only change\n" + source)
        assert after_restart() == before

        syntax = ast.parse(source)
        stability_class = next(node for node in syntax.body
                               if isinstance(node, ast.ClassDef) and node.name == "SettledSnapshot")
        constructor = next(node for node in stability_class.body
                           if isinstance(node, ast.FunctionDef) and node.name == "__init__")
        # Change actual stability behavior while leaving browser.py and the spec
        # byte-for-byte unchanged, like a later helper-only timing correction.
        constructor.args.defaults = [ast.Constant(value=0.25)]
        helper.write_text(ast.unparse(ast.fix_missing_locations(syntax)) + "\n")
        assert after_restart() != before
    finally:
        browser_progress.adapter_semantics.cache_clear()
