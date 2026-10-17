"""Compare one synchronous browser snapshot against a Git revision, locally.

Run from the repository root:
  PYTHONPATH=backend .venv/bin/python backend/benchmarks/browser_snapshot.py --baseline a01ac7d
No supplier connections are made; fixtures use page.set_content and no assets.
"""
from __future__ import annotations

import argparse
import ast
import asyncio
import json
import statistics
import subprocess

from crawler.adapters.browser import SNAPSHOT_JS


def baseline_script(revision):
    source = subprocess.check_output(
        ["git", "show", f"{revision}:backend/crawler/adapters/browser.py"], text=True
    )
    for node in ast.parse(source).body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "SNAPSHOT_JS" for target in node.targets
        ):
            return ast.literal_eval(node.value)
    raise ValueError("Baseline revision has no SNAPSHOT_JS")


def fixture(groups, choices):
    parts = ['<!doctype html><h1>Local Product</h1><div id="order-form">']
    for group in range(groups):
        parts.append(f'<fieldset><legend>Finish {group}</legend>')
        for choice in range(choices):
            checked = " checked" if choice == 0 else ""
            parts.append(f'<label><input type="radio" name="finish{group}" value="{choice}"{checked}>Option {choice}</label>')
        parts.append('</fieldset>')
    parts.extend([
        '<label>Quantity<select name="quantity"><option value="100">100</option><option value="250">250</option></select></label>',
        '<label>Width<input name="width" type="number" value="24" min="1" max="120"></label>',
        '</div><div id="subtotal">$127.45</div>',
    ])
    return ''.join(parts)


async def run(args):
    from playwright.async_api import async_playwright

    baseline = baseline_script(args.baseline)
    results = []
    async with async_playwright() as runtime:
        browser = await runtime.chromium.launch(headless=True)
        try:
            page = await browser.new_page()
            for name, groups, choices in [('small', 6, 4), ('large', 80, 8)]:
                await page.set_content(fixture(groups, choices))
                before = await page.evaluate(baseline, {"priceSelector": "#subtotal"})
                after = await page.evaluate(SNAPSHOT_JS, {"priceSelector": "#subtotal"})
                if before != after:
                    raise AssertionError(f"Snapshot changed on {name} fixture")
                samples = {"baseline": [], "optimized": []}
                # Alternate ordering so warmup and runtime drift affect both.
                for trial in range(args.samples + 4):
                    order = [("baseline", baseline), ("optimized", SNAPSHOT_JS)]
                    if trial % 2:
                        order.reverse()
                    for label, script in order:
                        elapsed = await page.evaluate(
                            """({script,iterations}) => {
                                const snapshot=eval('('+script+')');
                                const start=performance.now();
                                for(let i=0;i<iterations;i++)snapshot({priceSelector:'#subtotal'});
                                return (performance.now()-start)/iterations;
                            }""", {"script": script, "iterations": args.iterations}
                        )
                        if trial >= 4:
                            samples[label].append(elapsed)
                medians = {label: statistics.median(values) for label, values in samples.items()}
                results.append({
                    "fixture": name, "controls": len(before["controls"]),
                    "radio_choices": groups * choices, "snapshots_identical": True,
                    "baseline_median_ms": round(medians["baseline"], 3),
                    "optimized_median_ms": round(medians["optimized"], 3),
                    "speedup": round(medians["baseline"] / medians["optimized"], 3),
                })
        finally:
            await browser.close()
    print(json.dumps({"baseline": args.baseline, "samples": args.samples,
                      "iterations_per_sample": args.iterations, "results": results}, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", default="a01ac7d")
    parser.add_argument("--samples", type=int, default=15)
    parser.add_argument("--iterations", type=int, default=20)
    asyncio.run(run(parser.parse_args()))
