"""Dashboard job listings stay lightweight while detail retains quote evidence."""
import json

from crawler.b2sign_harvest import B2SignHarvestManager


def test_list_reads_report_only_and_detail_reads_results(tmp_path):
    manager = B2SignHarvestManager.__new__(B2SignHarvestManager)
    manager.root = tmp_path
    manager._tasks = {}
    job = tmp_path / ("a" * 32)
    (job / "results").mkdir(parents=True)
    (job / "report.json").write_text(json.dumps({
        "id": job.name, "status": "running", "warnings": [], "completed_count": 1,
    }))
    (job / "results" / "quote.json").write_text(json.dumps({"price": "12.34"}))

    summary = manager.list()[0]
    assert summary["status"] == "attention"
    assert summary["completed_count"] == 1
    assert "results" not in summary
    assert "harvest_interrupted" in summary["warnings"]

    detail = manager.get(job.name)
    assert detail["results"] == [{"price": "12.34"}]
