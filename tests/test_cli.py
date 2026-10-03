from pathlib import Path

from typer.testing import CliRunner

from cua import __version__
from cua.cli import app


def test_version_command_prints_package_version() -> None:
    result = CliRunner().invoke(app, ["version"])
    assert result.exit_code == 0
    assert result.stdout.strip() == __version__


def test_validate_accepts_repo_artifacts() -> None:
    result = CliRunner().invoke(
        app,
        [
            "validate",
            "apps/mockcore/profile.yaml",
            "capabilities/mockcore/session.sign_on/1.0.0.yaml",
            "tests/fixtures/read_savings_balance.yaml",
        ],
    )
    assert result.exit_code == 0, result.output
    assert result.output.count("OK ") == 3


def test_validate_reports_invalid_artifact(tmp_path) -> None:  # type: ignore[no-untyped-def]
    bad = tmp_path / "bad.yaml"
    bad.write_text("schema_version: '1.0'\nid: x.y\nversion: 1.0.0\n")
    result = CliRunner().invoke(app, ["validate", str(bad)])
    assert result.exit_code == 1


def test_committed_json_schemas_match_models(tmp_path) -> None:  # type: ignore[no-untyped-def]
    from pathlib import Path

    result = CliRunner().invoke(app, ["schema-export", "--out", str(tmp_path)])
    assert result.exit_code == 0
    for generated in tmp_path.glob("*.schema.json"):
        committed = Path("schema") / generated.name
        assert committed.read_text() == generated.read_text(), (
            f"{committed} is stale: run `uv run cua schema-export`"
        )


def test_merged_versions_never_overwrite_existing_files(tmp_path: Path) -> None:
    from cua.cli import _free_version

    for v in ("1.0.0", "1.1.0", "1.2.0"):
        (tmp_path / f"{v}.yaml").write_text("x")
    assert _free_version(tmp_path, "1.1.0") == "1.3.0"  # merged from 1.0.0 after 1.2.0 exists
    assert _free_version(tmp_path, "2.0.0") == "2.0.0"
