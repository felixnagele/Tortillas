"""
Endurance mode: runs selected tests repeatedly until stopped.

Each round runs in a fresh worker process. If a test hangs or crashes,
only that worker is stopped - the supervisor keeps going.
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import math
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path
from typing import TextIO

from .constants import (
    ENDURANCE_DIR,
    ENDURANCE_FAILURES_DIR,
    ENDURANCE_JSONL,
    ENDURANCE_LOCK,
    ENDURANCE_REPORT,
    QEMU_VMSTATE_TAG,
)
from .log_analyzer import TestStatus
from .progress_bar import ProgressBar
from .test_runner import SnapshotError, TestRun, TestRunner, _create_snapshot
from .test_specification import TestSpec
from .tortillas_config import TortillasConfig
from .utils import get_logger

FAILED = (TestStatus.FAILED, TestStatus.TIMEOUT, TestStatus.PANIC)
MAX_FAILURE_LOGS = 200
MAX_WORKER_LOGS = 50

_stop = False  # set by Ctrl-C handler
_current_process: subprocess.Popen | None = None  # worker of the running round
_lock_file: TextIO | None = None  # held open for the lifetime of the supervisor


def _on_signal(signum: int, frame: object) -> None:  # noqa: ARG001
    global _stop
    if _stop:
        # Kill the current worker (and its qemu) before forcing the exit,
        # as it lives in its own session and would survive as an orphan.
        if _current_process is not None:
            _stop_worker(_current_process)
        os._exit(130)
    _stop = True
    get_logger("global").warning("Stopping now (press Ctrl-C again to force quit)")


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _hhmmss(iso: str) -> str:
    """'2026-10-16T22:03:00+02:00' -> '22:03:00'"""
    return iso[11:19]


def _append_jsonl(record: dict) -> None:
    with ENDURANCE_JSONL.open("a") as jsonl_file:
        jsonl_file.write(json.dumps(record) + "\n")
        jsonl_file.flush()
        os.fsync(jsonl_file.fileno())


def _acquire_lock() -> bool:
    """Prevent two supervisors from writing into the same session directory."""
    global _lock_file

    lock_file = ENDURANCE_LOCK.open("w")
    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        lock_file.close()
        return False

    lock_file.write(str(os.getpid()))
    lock_file.flush()
    _lock_file = lock_file
    return True


def _load_records(path: Path) -> list[dict]:
    if not path.exists():
        return []

    records = []
    for line in path.read_text().splitlines():
        if line.strip():
            with contextlib.suppress(json.JSONDecodeError):
                records.append(json.loads(line))
    return records


def _prepare_dir() -> None:
    if ENDURANCE_DIR.exists():
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        moved = ENDURANCE_DIR.with_name(f"{ENDURANCE_DIR.name}-{stamp}")
        counter = 1
        while moved.exists():
            moved = ENDURANCE_DIR.with_name(f"{ENDURANCE_DIR.name}-{stamp}-{counter}")
            counter += 1
        ENDURANCE_DIR.rename(moved)
        get_logger("global").info(f"Moved previous session to {moved}")

        for old in sorted(ENDURANCE_DIR.parent.glob(f"{ENDURANCE_DIR.name}-*"))[:-2]:
            shutil.rmtree(old, ignore_errors=True)

    ENDURANCE_DIR.mkdir(parents=True)
    ENDURANCE_FAILURES_DIR.mkdir()


def _max_round_seconds(config: TortillasConfig, specs: list[TestSpec]) -> int:
    timeouts = [config.default_test_timeout_secs]
    timeouts += [spec.timeout for spec in specs if spec.timeout]
    longest = max(timeouts)

    waves = math.ceil(len(specs) / max(1, config.threads))

    return int(2 * waves * longest + config.bootup_timeout_secs + 120)


def _worker_command(
    args: argparse.Namespace, round_id: int, result_path: Path
) -> list[str]:
    cmd = [
        sys.executable,
        "-m",
        "tortillas",
        "-S",
        str(args.sweb_path),
        "--arch",
        args.arch,
        "--no-progress",
        "-a",
        "-s",
        "--endurance-round",
        str(round_id),
        "--endurance-result",
        str(result_path),
        "-g",
        args.glob,
    ]

    if args.config_path:
        cmd += ["-C", str(args.config_path)]
    if args.category:
        cmd += ["-c", *args.category]
    if args.tag:
        cmd += ["-t", *args.tag]

    return cmd


def _stop_worker(process: subprocess.Popen) -> None:
    """Kill a worker and its qemu child by killing their process group."""
    if process.poll() is not None:
        return

    for sig in (signal.SIGTERM, signal.SIGKILL):
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(process.pid, sig)
        with contextlib.suppress(subprocess.TimeoutExpired):
            process.wait(timeout=10)
            return


def _prune(paths: Iterable[Path], keep: int) -> None:
    for path in sorted(paths)[:-keep]:
        path.unlink(missing_ok=True)


def _forward(stream: TextIO, worker_log: TextIO) -> None:
    for line in stream:
        sys.stdout.write(line)
        sys.stdout.flush()
        worker_log.write(line)
        worker_log.flush()


def _run_round(round_id: int, args: argparse.Namespace, max_secs: int) -> dict:
    global _current_process
    log = get_logger("global")

    result_path = ENDURANCE_DIR / f"round-{round_id}.result.json"
    result_path.unlink(missing_ok=True)

    worker_log_path = ENDURANCE_DIR / "workers" / f"round-{round_id:06d}.log"
    worker_log_path.parent.mkdir(exist_ok=True)
    worker_log = worker_log_path.open("w")

    start = _now()
    begin = time.monotonic()
    killed = False

    log.info(f"[{_hhmmss(start)}] round {round_id}  running ...")

    with worker_log:
        process = subprocess.Popen(
            _worker_command(args, round_id, result_path),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            text=True,
            bufsize=1,
            start_new_session=True,
        )
        _current_process = process
        assert process.stdout is not None

        try:
            reader = threading.Thread(
                target=_forward, args=(process.stdout, worker_log), daemon=True
            )
            reader.start()

            deadline = begin + max_secs
            while process.poll() is None:
                if _stop or time.monotonic() > deadline:
                    if not _stop:
                        log.error(
                            f"Round {round_id}: exceeded {max_secs}s, killing worker"
                        )
                    _stop_worker(process)
                    killed = True
                    break
                time.sleep(0.5)

            reader.join(timeout=5)
        finally:
            _current_process = None
            with contextlib.suppress(OSError):
                process.stdout.close()

    failures: list[dict[str, str]] = []
    partial = True

    if not killed and result_path.exists():
        data = json.loads(result_path.read_text())
        failures, partial = data["fail"], data["partial"]
    result_path.unlink(missing_ok=True)

    if partial:
        # Keep logs of aborted rounds (hangs and crashes) for diagnosis.
        _prune(worker_log_path.parent.glob("round-*.log"), keep=MAX_WORKER_LOGS)
    else:
        worker_log_path.unlink(missing_ok=True)

    return {
        "type": "round",
        "round": round_id,
        "start": start,
        "ms": int((time.monotonic() - begin) * 1000),
        "partial": partial,
        "fail": failures,
    }


def _log_round(record: dict, num_tests: int) -> None:
    log = get_logger("global")
    timestamp = _hhmmss(record["start"])
    secs = record["ms"] // 1000

    if record["partial"]:
        log.warning(f"[{timestamp}] round {record['round']}  ABORTED   ({secs}s)")
        return

    failures = record["fail"]
    status = "FAIL" if failures else "ok  "
    log.info(
        f"[{timestamp}] round {record['round']}  {status}  "
        f"{num_tests - len(failures)}/{num_tests}  ({secs}s)"
    )
    for failure in failures:
        log.warning(f"            -> {failure['test']}:{failure['status']}")


def _sleep(seconds: float) -> None:
    end = time.monotonic() + seconds
    while not _stop and time.monotonic() < end:
        time.sleep(0.5)


def _create_snapshot_with_retry(architecture: str, config: TortillasConfig) -> None:
    log = get_logger("global")
    delay = 5

    while not _stop:
        try:
            log.info("Creating snapshot")
            _create_snapshot(architecture, QEMU_VMSTATE_TAG, config)
            return
        except SnapshotError as exc:
            log.error(f"Snapshot failed: {exc}. Retrying in {delay}s")
            _sleep(delay)
            delay = min(delay * 2, 60)


def run_endurance(
    specs: list[TestSpec],
    args: argparse.Namespace,
    config: TortillasConfig,
) -> bool:
    global _stop
    log = get_logger("global")

    if not _acquire_lock():
        log.error(f"Another endurance run is already active ({ENDURANCE_LOCK})")
        return False

    _prepare_dir()
    _stop = False
    signal.signal(signal.SIGINT, _on_signal)
    signal.signal(signal.SIGTERM, _on_signal)

    tests = [spec for spec in specs if not spec.disabled]
    test_names = [spec.test_name for spec in tests]
    _append_jsonl({"type": "meta", "start": _now(), "tests": test_names})

    max_round_secs = _max_round_seconds(config, tests)
    log.info("")
    log.info(
        f"Endurance mode: {len(test_names)} tests, "
        f"{config.threads} threads, max {max_round_secs}s per round"
    )
    if args.rounds > 0:
        log.info(f"Stopping after {args.rounds} rounds")
    else:
        log.info("Press Ctrl-C to stop")

    _create_snapshot_with_retry(args.arch, config)

    round_id = 0
    all_ok = True
    while not _stop and (args.rounds <= 0 or round_id < args.rounds):
        round_id += 1
        record = _run_round(round_id, args, max_round_secs)
        _append_jsonl(record)
        _log_round(record, len(test_names))
        if record["partial"] or record["fail"]:
            # Aborted rounds count as failure too: a run, where every round
            # was aborted (e.g. broken qemu), must not exit with success.
            all_ok = False

    _write_report()
    log.info("")
    log.info(f"Endurance finished. Report: {ENDURANCE_REPORT}")
    return all_ok


def run_endurance_round(
    args: argparse.Namespace, specs: list[TestSpec], config: TortillasConfig
) -> None:
    test_runner = TestRunner(specs, 1, args.arch, config, ProgressBar(True))
    test_runner.start()

    failures: list[dict[str, str]] = []
    for test_run in test_runner.test_runs:
        if test_run.result.status in FAILED:
            failures.append(
                {"test": test_run.name, "status": test_run.result.status.name}
            )
            _archive_failure_log(test_run, args.endurance_round)

        shutil.rmtree(test_run.tmp_dir, ignore_errors=True)

    if args.endurance_result:
        result = {"partial": False, "fail": failures}
        # Atomic write, so the supervisor never sees a truncated file.
        tmp_path = args.endurance_result.with_suffix(".tmp")
        tmp_path.write_text(json.dumps(result))
        os.replace(tmp_path, args.endurance_result)


def _archive_failure_log(test_run: TestRun, round_id: int) -> None:
    log_file = test_run.tmp_dir / "out.log"
    if not log_file.exists():
        return

    if sum(1 for _ in ENDURANCE_FAILURES_DIR.rglob("*.log")) >= MAX_FAILURE_LOGS:
        return

    dest_dir = ENDURANCE_FAILURES_DIR / test_run.name
    dest_dir.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(log_file, dest_dir / f"round-{round_id:06d}.log")


def build_report(records: list[dict], failures_dir: Path | None = None) -> str:
    failures_dir = failures_dir or ENDURANCE_FAILURES_DIR
    meta = next((r for r in records if r["type"] == "meta"), {})
    rounds = [r for r in records if r["type"] == "round"]
    completed = [r for r in rounds if not r["partial"]]
    tests = meta.get("tests", [])

    # Per test: failure counts per status plus the first/last round it failed in.
    stats: dict[str, dict] = {}
    for record in completed:
        for failure in record["fail"]:
            name = failure.get("test")
            status = failure.get("status", "UNKNOWN")
            if not name:
                continue
            entry = stats.setdefault(
                name, {"counts": {}, "first": record["round"], "last": record["round"]}
            )
            entry["counts"][status] = entry["counts"].get(status, 0) + 1
            entry["last"] = record["round"]

    def fails(name: str) -> int:
        return sum(stats.get(name, {"counts": {}})["counts"].values())

    lines = [
        "# Tortillas endurance report",
        "",
        f"- Session started: {meta.get('start', '?')}",
        f"- Rounds: {len(completed)} completed, {len(rounds) - len(completed)} aborted",
        f"- Tests: {len(tests)} selected",
        f"- Failure logs: {failures_dir}",
        "",
        "## Flakiness (worst first)",
        "",
    ]

    if not completed:
        lines += ["_No completed rounds yet._", ""]
    else:
        lines += [
            "| Test | Runs | Fail | Timeout | Panic | Fail % | First | Last |",
            "|------|------|------|---------|-------|--------|-------|------|",
        ]
        for name in sorted(dict.fromkeys([*tests, *stats]), key=fails, reverse=True):
            entry = stats.get(name, {})
            counts = entry.get("counts", {})
            percent = fails(name) / len(completed) * 100
            lines.append(
                f"| {name} | {len(completed)} | {counts.get('FAILED', 0)} | "
                f"{counts.get('TIMEOUT', 0)} | {counts.get('PANIC', 0)} | "
                f"{percent:.1f} % | {entry.get('first', '-')} | "
                f"{entry.get('last', '-')} |"
            )
        lines.append("")

        never_failed = [name for name in tests if fails(name) == 0]
        lines += ["## Never failed", ""]
        lines += (
            [f"- {name}" for name in never_failed]
            if never_failed
            else ["_Every test failed at least once._"]
        )
        lines.append("")

    dirty = [r for r in rounds if r["partial"] or r["fail"]]
    if dirty:
        lines += ["## Rounds with failures", ""]
        for record in dirty:
            if record["partial"]:
                lines.append(f"- round {record['round']}: aborted")
            else:
                details = ", ".join(
                    f"{f.get('test', '?')}:{f.get('status', '?')}"
                    for f in record["fail"]
                )
                lines.append(f"- round {record['round']}: {details}")
        lines.append("")

    return "\n".join(lines)


def _write_report(jsonl: Path | None = None, report: Path | None = None) -> None:
    jsonl = jsonl or ENDURANCE_JSONL
    report = report or ENDURANCE_REPORT

    tmp_path = report.with_suffix(".md.tmp")
    tmp_path.write_text(build_report(_load_records(jsonl), jsonl.parent / "failures"))
    os.replace(tmp_path, report)


def regenerate_report(session_dir: Path | None = None) -> bool:
    """Rebuild the report from the current session or a rotated session directory."""
    log = get_logger("global")

    if session_dir is None:
        jsonl = ENDURANCE_JSONL
        report = ENDURANCE_REPORT
    else:
        jsonl = session_dir / "rounds.jsonl"
        report = session_dir / "endurance_report.md"

    if not jsonl.exists():
        log.error(f"No endurance session found at {jsonl}")
        return False

    _write_report(jsonl, report)
    log.info(f"Report written to {report}")
    return True
