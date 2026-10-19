import json
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import tortillas.endurance as endurance
from tortillas.endurance import (
    _load_records,
    _max_round_seconds,
    _worker_command,
    build_report,
    regenerate_report,
)


def _meta(tests: list[str]) -> dict:
    return {
        "type": "meta",
        "start": "2026-10-16T22:00:00+02:00",
        "tests": tests,
    }


def _round(
    number: int,
    fail: list[dict],
    partial: bool = False,
    ms: int = 1000,
) -> dict:
    return {
        "type": "round",
        "round": number,
        "start": f"2026-10-16T22:0{number}:00+02:00",
        "ms": ms,
        "partial": partial,
        "fail": fail,
    }


def test_build_report_empty_session():
    report = build_report([])
    assert "# Tortillas endurance report" in report
    assert "_No completed rounds yet._" in report


def test_build_report_no_completed_rounds():
    report = build_report([_meta(["a"]), _round(1, [], partial=True)])
    assert "_No completed rounds yet._" in report


def test_build_report_shows_failures_ranking_and_aborted_rounds():
    records = [
        _meta(["a", "b", "c"]),
        _round(1, []),
        _round(2, [{"test": "a", "status": "FAILED"}]),
        _round(
            3,
            [
                {"test": "a", "status": "FAILED"},
                {"test": "c", "status": "PANIC"},
            ],
        ),
        _round(4, [], partial=True),
    ]

    report = build_report(records)

    assert "3 completed, 1 aborted" in report
    assert "| a | 3 | 2 | 0 | 0 | 66.7 % | 2 | 3 |" in report
    assert "| c | 3 | 0 | 0 | 1 | 33.3 % | 3 | 3 |" in report
    assert "- b" in report
    assert "- round 4: aborted" in report
    assert "- round 2: a:FAILED" in report


def test_build_report_lists_all_tests_and_never_failed():
    records = [
        _meta(["pass", "fail_once"]),
        _round(1, [{"test": "fail_once", "status": "TIMEOUT"}]),
        _round(2, []),
    ]

    report = build_report(records)

    assert "fail_once" in report
    assert "- pass" in report


def test_load_records_reads_valid_lines(tmp_path: Path):
    path = tmp_path / "records.jsonl"
    path.write_text(json.dumps(_meta(["a"])) + "\n" + json.dumps(_round(1, [])) + "\n")

    records = _load_records(path)
    assert len(records) == 2
    assert records[0]["type"] == "meta"
    assert records[1]["round"] == 1


def test_load_records_ignores_truncated_last_line(tmp_path: Path):
    path = tmp_path / "trunc.jsonl"
    path.write_text(
        json.dumps(_meta(["a"]))
        + "\n"
        + json.dumps(_round(1, []))
        + "\n"
        + '{"type": "round", "round": 2'  # truncated
    )

    records = _load_records(path)
    assert len(records) == 2
    assert records[1]["round"] == 1


def test_load_records_empty_or_missing_returns_empty(tmp_path: Path):
    assert _load_records(tmp_path / "missing.jsonl") == []

    empty = tmp_path / "empty.jsonl"
    empty.write_text("")
    assert _load_records(empty) == []


def test_max_round_seconds_calculates_expected_margin():
    config = SimpleNamespace(
        default_test_timeout_secs=10,
        threads=2,
        bootup_timeout_secs=30,
    )
    specs = [SimpleNamespace(timeout=0) for _ in range(3)]

    # waves = ceil(3/2)=2, longest=10 -> 2*2*10 + 30 + 120 = 190
    assert _max_round_seconds(config, specs) == 190


def test_max_round_seconds_uses_spec_timeout_if_longer():
    config = SimpleNamespace(
        default_test_timeout_secs=5,
        threads=1,
        bootup_timeout_secs=10,
    )
    specs = [SimpleNamespace(timeout=20)]

    # waves=1, longest=20 -> 2*1*20 + 10 + 120 = 170
    assert _max_round_seconds(config, specs) == 170


def _round_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point endurance at a temp dir so _run_round does not touch real session data."""
    monkeypatch.setattr(endurance, "ENDURANCE_DIR", tmp_path)
    (tmp_path / "workers").mkdir()


def test_hung_worker_is_killed_and_marked_partial(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    _round_env(tmp_path, monkeypatch)

    def fake_command(_args: Any, _round_id: int, _result_path: Path) -> list[str]:
        return ["sleep", "60"]

    monkeypatch.setattr(endurance, "_worker_command", fake_command)

    start = time.monotonic()
    record = endurance._run_round(1, SimpleNamespace(), max_secs=1)
    elapsed = time.monotonic() - start

    assert elapsed < 30
    assert record["partial"] is True
    assert record["fail"] == []
    assert record["round"] == 1


def test_completed_round_reads_result_from_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    _round_env(tmp_path, monkeypatch)

    def fake_command(_args: Any, _round_id: int, _result_path: Path) -> list[str]:
        # Write the result the worker would write, then exit quickly
        _result_path.write_text(
            json.dumps(
                {
                    "partial": False,
                    "fail": [{"test": "foo", "status": "FAILED"}],
                }
            )
        )
        return ["true"]

    monkeypatch.setattr(endurance, "_worker_command", fake_command)

    record = endurance._run_round(2, SimpleNamespace(), max_secs=10)

    assert record["partial"] is False
    assert record["fail"] == [{"test": "foo", "status": "FAILED"}]
    assert record["round"] == 2


def test_prepare_dir_handles_backup_name_collision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Two runs in the same second must not collide on the backup name."""
    monkeypatch.setattr(endurance, "ENDURANCE_DIR", tmp_path / "endurance")
    monkeypatch.setattr(
        endurance, "ENDURANCE_FAILURES_DIR", tmp_path / "endurance" / "failures"
    )

    endurance._prepare_dir()
    assert len(list(tmp_path.glob("endurance-*"))) == 0

    endurance._prepare_dir()
    assert len(list(tmp_path.glob("endurance-*"))) == 1

    endurance._prepare_dir()
    assert len(list(tmp_path.glob("endurance-*"))) == 2
    assert (tmp_path / "endurance" / "failures").is_dir()


def test_build_report_tolerates_unknown_status():
    records = [
        _meta(["weird"]),
        _round(1, [{"test": "weird", "status": "WEIRD"}]),
    ]

    report = build_report(records)

    # Unknown statuses still count as failures instead of crashing.
    assert "| weird | 1 | 0 | 0 | 0 | 100.0 % | 1 | 1 |" in report
    assert "- weird" not in report


def test_build_report_skips_failure_without_test_name():
    records = [
        _meta(["a"]),
        _round(1, [{"status": "FAILED"}]),
        _round(2, []),
    ]

    report = build_report(records)

    assert "| a | 2 | 0 | 0 | 0 | 0.0 % | - | - |" in report
    assert "- a" in report


def test_regenerate_report_missing_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(endurance, "ENDURANCE_JSONL", tmp_path / "missing.jsonl")
    assert regenerate_report() is False


def test_regenerate_report_writes_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    jsonl = tmp_path / "rounds.jsonl"
    jsonl.write_text(json.dumps(_meta(["a"])) + "\n" + json.dumps(_round(1, [])) + "\n")
    report_path = tmp_path / "endurance_report.md"
    monkeypatch.setattr(endurance, "ENDURANCE_JSONL", jsonl)
    monkeypatch.setattr(endurance, "ENDURANCE_REPORT", report_path)

    assert regenerate_report() is True
    assert "# Tortillas endurance report" in report_path.read_text()


def test_worker_command_contains_selection_args():
    args = SimpleNamespace(
        sweb_path=Path("/sweb"),
        arch="x86_64",
        config_path=Path("/sweb/tortillas_config.yml"),
        glob="test_*",
        category=["malloc"],
        tag=["pthread"],
    )

    cmd = _worker_command(args, 7, Path("/tmp/result.json"))

    assert "--endurance-round" in cmd
    assert cmd[cmd.index("--endurance-round") + 1] == "7"
    assert "--endurance-result" in cmd
    assert "-S" in cmd and "/sweb" in cmd
    assert "-g" in cmd and "test_*" in cmd
    assert "-c" in cmd and "malloc" in cmd
    assert "-t" in cmd and "pthread" in cmd
    assert "--no-progress" in cmd


def test_worker_command_minimal_args():
    args = SimpleNamespace(
        sweb_path=Path("/sweb"),
        arch="riscv64",
        config_path=None,
        glob="*.py",
        category=None,
        tag=None,
    )

    cmd = _worker_command(args, 1, Path("/tmp/r.json"))

    assert "--endurance-round" in cmd
    assert "--endurance-result" in cmd
    assert "-S" in cmd
    assert "-g" in cmd
    assert "-c" not in cmd
    assert "-t" not in cmd
    assert "-C" not in cmd
    assert "--no-progress" in cmd
