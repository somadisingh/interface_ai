"""Run reports are generated from the (redacted) log for every run."""

from __future__ import annotations

from pathlib import Path

from cua.handoff.channel import ScriptedChannel
from cua.runtime import open_runtime
from cua.schema import load_capability
from tests.conftest import PASSWORD, USERNAME, LiveServer

ROOT = Path(__file__).parents[2]
READ = load_capability(ROOT / "tests/fixtures/read_savings_balance.yaml")
SECRETS = {"MOCKCORE_USERNAME": USERNAME, "MOCKCORE_PASSWORD": PASSWORD}


def _run(base: str, runs: Path, member_id: str, *, debug_trace: bool = False) -> Path:
    with open_runtime(
        base,
        human=ScriptedChannel(),
        root=ROOT,
        runs_dir=runs,
        secrets_source=SECRETS,
        debug_trace=debug_trace,
    ) as rt:
        result = rt.engine.run(READ, {"member_id": member_id})
        if result.status == "failed":
            rt.keep_trace = True
        return rt.log.run_dir


def test_success_report(mockcore_url: str, tmp_path: Path) -> None:
    run_dir = _run(mockcore_url, tmp_path, "12345")
    report = (run_dir / "run_report.md").read_text()
    assert "**success**" in report and "## Timeline" in report
    assert "Step completed" in report and '<img src="screenshots/' in report
    assert "1,520.33" not in report and "1520.33" not in report and PASSWORD not in report
    for src in report.split('src="')[1:]:
        assert (run_dir / src.split('"')[0]).exists()


def test_failure_report_has_detail_and_trace(
    live_mockcore: LiveServer, mockcore_url: str, tmp_path: Path
) -> None:
    live_mockcore.set_faults("error500=/members/1")
    run_dir = _run(mockcore_url, tmp_path, "12345")
    report = (run_dir / "run_report.md").read_text()
    assert "## Failure detail" in report and "`APP_ERROR`" in report
    # no raw trace by default: Playwright traces hold typed credentials and unmasked pages
    assert not list(run_dir.glob("*.zip"))


def test_raw_trace_is_opt_in_and_labelled(
    live_mockcore: LiveServer, mockcore_url: str, tmp_path: Path
) -> None:
    live_mockcore.set_faults("error500=/members/1")
    run_dir = _run(mockcore_url, tmp_path, "12345", debug_trace=True)
    assert (run_dir / "trace.UNREDACTED.zip").exists()
    assert "contains credentials" in (run_dir / "run_report.md").read_text()
    assert '"raw_trace_saved"' in (run_dir / "events.jsonl").read_text()
