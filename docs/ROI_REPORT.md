# Tortillas — ROI Analysis & Improvement Plan

> Working document for maintainers and AI agents.
> It records what was measured, what is actually broken, and the order in which to fix it.
> **Do not duplicate this into `README.md`.** The README stays user-facing; this file is the engineering plan.

- **Repo:** `felixnagele/Tortillas` (upstream canonical: `PointerDilemma/Tortillas`)
- **Analyzed:** 2026-11-26, `main` @ `dc52704`; CI/order revised 2026-11-27
- **Scope of this document:** priorities, root causes, evidence, PR sequence, file references.
- **Status:** plan. No code changes yet. Track progress in the checklist in §13.

Info: CI/CD can be run via `act` command locally!

---

## 0. Constraints & non-goals

Keep these invariants for every change:

- **Keep Python and the existing architecture.** No rewrite.
- **Keep the working core:** `test_specification`, `log_parser`, `log_analyzer`, snapshot creation, config loading.
- **No new runtime dependencies** beyond PyYAML. Stdlib is enough (`argparse`, `logging`, `subprocess`, `threading`, `concurrent.futures`, `shutil`, `tempfile`, `xml.etree`).
- **Do not add** `rich`, `click`, `typer`, `pydantic`, `tenacity`, `psutil`.
- **Support Linux + WSL2 + macOS.** Native Windows is a separate P2 project, not a claim to make yet.
- **Do not modify the user's test target** (`~/felix/uni/os/osw26e3`); only run Tortillas against it.
- **Split work into small, reviewable PRs.** One behavior change per PR, with a regression test. Do not mix formatting with behavior.
- **CI: pin for PRs, canary for latest.** PR CI uses a pinned `example` submodule commit; "newest sweb" runs as a non-gating scheduled canary. Never gate PRs on unpinned upstream `main`.
- **Bootstrap CI before hardening it.** A small green, artifact-rich pipeline first; lint/matrix/canary/release after the code fixes.

---

## 1. TL;DR — severity vs. execution order

Severity and execution order are **not the same**:

- **Severity** = what hurts most: the runner hang, Enlighten, red CI, wrong docs.
- **Execution order** = what to do first: a small **CI bootstrap** so every later change is measured, then the two daily-pain bugs, then tooling + CI hardening, then docs/UX.

### Execution order (best practice)

| Step  | PR                                                             | Severity         | Why here                                                                                               |
| ----- | -------------------------------------------------------------- | ---------------- | ------------------------------------------------------------------------------------------------------ |
| **0** | **CI bootstrap** (tiny, ~30 min)                               | P0               | Green, observable baseline; cheap; independent of code; unblocks measuring every later PR              |
| **1** | **Runner hardening**                                           | P0               | Biggest daily pain; now validated by CI. Include `pytest-timeout` so its regression test can't hang CI |
| **2** | **Remove Enlighten**                                           | P0               | Second daily pain (crash + ANSI spam); validated by CI                                                 |
| **3** | **Tooling/packaging** (ruff/mypy/pre-commit, version, lock)    | P1               | Prerequisite for the CI lint job; fixes the currently invalid ruff config                              |
| **4** | **CI hardening** (lint/unit/integration/matrix/canary/release) | P0               | Build on the green baseline _after_ code stabilizes; avoids churn and conflicts                        |
| **5** | **README correctness** (incl. platform support)                | P0 (user-facing) | Cheap, high impact; do after behavior is final so docs match reality                                   |
| **6** | **CLI/UX + config validation**                                 | P1               | `--build-dir`, `--threads`, `--list`, friendly errors                                                  |
| **7** | **Disk & runtime cleanup**                                     | P1               | Measure first; 1.5 GB/run matters on tmpfs                                                             |
| **8** | **Optional**                                                   | P2               | JUnit XML, `--doctor`, native Windows                                                                  |

**Highest ROI in one sentence:** get a small green CI, then fix the runner so a worker/qemu failure can never hang the process, and delete Enlighten.

> **Two rules for CI:**
>
> 1. Do **not** do the full CI rewrite first — bootstrap first, harden later (§5).
> 2. Do **not** gate PRs on unpinned `tortillas-sweb@main` — pin PR CI, run latest as a scheduled canary (§5).

---

## 2. Evidence — what was actually measured

| Test                     | Result                                                                                                                                 |
| ------------------------ | -------------------------------------------------------------------------------------------------------------------------------------- |
| Full suite vs. `osw26e3` | **108/108 SUCCESS**, 2m53s, 8 threads                                                                                                  |
| 14-test subset           | 34s                                                                                                                                    |
| Example `tortillas-sweb` | `mult` SUCCESS (built into `/tmp/sweb`)                                                                                                |
| Disk after full run      | **1.5 GB** under `/tmp/sweb/tortillas`; 15–18 MB per test                                                                              |
| `/tmp` on this machine   | tmpfs, 12 GB, ~15% used → disk usage is RAM usage                                                                                      |
| Worker-exception repro   | `TestRunner.start()` never returns (`timeout` exit 124)                                                                                |
| Retry repro              | ~785 retries, then runner returned early (race)                                                                                        |
| Enlighten repro          | `ValueError: Reserve field 'count_00' specified in format, but no subcounters are configured` (Enlighten 1.14.1 + blessed 1.53.0)      |
| ANSI spam cause          | Enlighten/Blessed sends `ESC[6n` (DSR); terminal replies `ESC[row;colR` (CPR), echoed as `^[[10;51R`                                   |
| Import check             | Package imports on Python 3.8–3.13 (but 3.8/3.9 are EOL)                                                                               |
| mypy on package          | Only **2 errors** today (missing `enlighten` stubs; `counters` annotation)                                                             |
| Fork Actions             | **0 workflow runs** — Actions not producing runs on the fork                                                                           |
| `example` submodule      | Pinned at `931878a`; `.gitmodules branch=main` only affects `update --remote`; not initialized in the working tree                     |
| GitHub-hosted runners    | No KVM → qemu uses TCG; integration is slow                                                                                            |
| `tortillas-sweb@panic`   | Panics during boot and produces **no summary** — the old `panic` job passed for the wrong reason                                       |
| Boot watchdog            | `wait_until` declared `STOPPED` after ~5 s without new interrupts; false-positived on a loaded runner — heuristic removed in bootstrap |

Environment used for measurements: Ubuntu 24.04, CMake 3.28.3, GCC 13.3, Python 3.12/3.14, `uv`.

---

## 3. P0-1 — Runner can hang forever (the freeze)

**Files:** `tortillas/test_runner.py`, `tortillas/qemu_interface.py`

This is **not** a sweb deadlock. A fake worker that raises an exception reproduces a permanent hang with no sweb involved.

### Root causes (in order of likelihood)

1. **Worker exceptions are never caught.**
   `_run()` is a raw `threading.Thread` target. Any exception (`cp` fails on full disk, missing `out.log`, qemu exits early, broken pipe in `sweb_input()`, `analyze()` error) kills the thread **without calling `callback`**. `running_tests` is never emptied, so `while test_queue or running_tests` loops forever. This is the "timer stops, Tortillas freezes" symptom.
   - `test_runner.py:112-192` (scheduler), `test_runner.py:333-405` (`_run`).

2. **`QemuInterface.__exit__` calls `self.process.wait()` with no timeout.**
   `qemu_interface.py:87-105`. If qemu is wedged or blocked on an unread pipe, this blocks forever.

3. **qemu stdout/stderr are `PIPE` but never read.**
   `qemu_interface.py:141-146` (`stdout=PIPE, stderr=STDOUT`). Once qemu writes >~64 KB, it blocks forever. Classic pipe deadlock, more likely with more/longer tests.

4. **Retries are unbounded.**
   `_run` sets `test.result.retry = True` whenever qemu is not alive (`test_runner.py:367`); the callback re-queues it. A permanently failing qemu loops forever. The `retry` analyze mode has the same issue.

5. **Scheduler reads shared state without the lock.**
   The callback mutates `running_tests`/`test_queue` under `lock`, but the main loop reads them unlocked (`test_runner.py:174,182`). Reproduced a retry being dropped: after ~785 retries the runner exited while the retry was in flight. Can skip tests → `NOT_RUN`.

6. **`TestRunner.success` ignores `NOT_RUN`.**
   `test_runner.py:188-192`. A skipped/crashed test can still make the run report success.

7. **Main loop is a busy-wait.**
   `time.sleep(0.0001)` (`test_runner.py:185`) ≈ 10,000 iterations/s, each calling `progress_bar.refresh()`. Burns CPU, starves qemu threads, multiplies Enlighten DSR queries.

8. **`is_alive()` is inverted for clean exits.**
   `qemu_interface.py:148-153`: `self.process.poll()` is `0` after a clean exit, which is falsy, so `is_alive()` returns `True`. Code then writes `quit`/`exit` to a dead process → `BrokenPipeError` → kills the thread → cause #1. Use `poll() is not None`.

9. **`wait_until` can drop a partial interrupt block.**
   `qemu_interface.py:237-260`: advances `file_pos` even if the last block is incomplete. A missed bootup/completion block → timeout or false STOPPED.

10. **`STOPPED` does not set `FAILED`.**
    `log_analyzer.py:128-129` adds an error but leaves status `SUCCESS`; only the later missing-exit-code check may save it.

11. **Disk pressure triggers cause #1.**
    108 tests → 1.5 GB. If `/tmp` fills, `cp` raises, the worker dies, the runner freezes.

12. **Invalid config can hang.**
    `threads: 0`/negative → queue never drains, loop spins forever. Validate config.

13. **Boot panic detection was too eager (fixed in bootstrap).**
    `wait_until` returned `STOPPED` after 10 unchanged polls × 0.5 s ≈ **5 s** (`qemu_interface.py:244-262`). On a slow/loaded runner, boot can pause >5 s and be misreported as "Interrupts stopped... Panic?". Reproduced while a Docker image pull was running: the example boot was flagged as a panic and the run failed with no summary. Even 15 s was not reliable (the guest legitimately keeps interrupts off during init, and qemu buffers `int.log`), so the silence heuristic was removed. `wait_until` now relies on the explicit timeout, and CI raises the boot timeout to 60 s.

### Recommended fix (contained, not a rewrite)

Replace the callback scheduler with a small executor-based one:

```python
def run_one(tr):
    try:
        _run(tr, ...)
    except Exception as exc:
        tr.result = TestResult(TestStatus.FAILED, [f"runner error: {exc}"])
    finally:
        cleanup(tr)

with ThreadPoolExecutor(max_workers=threads) as pool:
    pending = {pool.submit(run_one, tr) for tr in runs}
    while pending:
        done, pending = wait(pending, timeout=0.2, return_when=FIRST_COMPLETED)
        for f in done:
            tr = f.result()
            if tr.result.retry and tr.attempts < max_retries:
                pending.add(pool.submit(run_one, tr))
        update_progress_once()
```

Plus:

- `QemuInterface.close(timeout=10)` → `wait(timeout)`, then `kill()`, then `wait()`.
- Redirect qemu stdout/stderr to `<tmp>/qemu.log` (never `PIPE`).
- Bound retries (`--max-retries`, default 2–3), then mark FAILED.
- Catch all exceptions and always produce a result.
- Delete passed test dirs; keep failed ones (see §8).
- Per-test hard deadline (`timeout + cleanup margin`).
- Signal handler: stop dispatching, kill qemu, exit 130 on Ctrl-C.
- Make threads daemon **or** guarantee join via the executor.

**Effort:** ~150–250 lines incl. tests. Removes the whole class of freezes.

**Regression tests to add:**

- monkeypatch `_run` to raise → `start()` returns and marks FAILED.
- a test that always retries → bounded, terminates.
- every test runs exactly once; no `NOT_RUN` silently passes.

> **Branch note:** `feature/endurance-mode` already has several of these fixes (`process.wait(timeout)`, `DEVNULL`, `is_alive`, FIFO cleanup, snapshot retry). **Extract them into this PR; do not merge the branch as-is** — its `test_runner.py` diff regresses the recent long-name markdown fix (fixed `[40, 20]` widths + raises on long names). Rebase its table code onto the long-name fix.

---

## 4. P0-2 — `^[[10;51R` spam & progress-bar crash (remove Enlighten)

**File:** `tortillas/progress_bar.py`, `pyproject.toml`, `README.md`

Those sequences are **CPR responses** (`CSI row;col R`) to a **DSR cursor-position query** (`ESC[6n`). They are not from sweb.

Confirmed chain:

1. `progress_bar.py` uses Enlighten/Blessed.
2. Enlighten's manager calls `term.get_location(timeout=1)` on flush → writes `ESC[6n`.
3. The terminal replies `ESC[10;51R` on stdin.
4. Tortillas never reads stdin (qemu has `stdin=DEVNULL`), so the reply sits in the tty buffer and is echoed as literal `^[[10;51R`.
5. The 10 kHz main loop refreshes constantly and test threads log to the same terminal → looks like a flood.

Also a hard crash with current Enlighten: `count_00` is referenced in the format (`progress_bar.py:89-100`) before subcounters exist.

**Decision: remove Enlighten entirely.** It is an optional dependency causing a crash, terminal corruption, extra CPU, and two install variants. The README's `tortillas[fancy]` install is currently broken.

Options (pick one):

1. **Minimal/safest:** no progress bar by default; one line per test event + final summary. Keep `--no-progress` for compatibility; add `--progress` later.
2. **Better UX:** ~50-line stdlib renderer:
   - single `\r` line to **stderr**,
   - only when `sys.stderr.isatty()` and not in CI,
   - one writer + lock; clear line → log → redraw,
   - never query cursor position.

Until the built-in renderer exists, default **off**.

**Also:** remove the `[fancy]` extra, collapse to one install command, fix the `enligthen` typo in the README.

---

## 5. P0-3 — CI is broken, and how to build it properly

**Files:** `.github/workflows/demo_ci.yml`, `.github/workflows/internal_tests_ci.yml`, `gitlab-ci-example*.yml`

### Current state (evidence)

All recent upstream runs are red:

- `Demo` / `main`: `base` job fails at **Run tortillas** and **Adding markdown**.
- `Demo` / `main`: `panic` job's tortillas step "succeeds" only because `!` inverts the exit code; **Adding markdown** fails.
- `Internal tests` / `main`: fails at **Setup and compile sweb**; pytest is skipped.
- Warning: `actions/checkout@v3` targets deprecated Node.js 20.
- The fork has **0 workflow runs**.

The `base` job fails in ~2 s → not a boot timeout; it fails before booting. Most likely the `example` submodule is not checked out (empty `./example` → "No test specs" → exit 1) or an immediate CLI/import error. The `panic` job explicitly checks out sweb, so it does not test the submodule path and can pass for the wrong reason.

### Two-phase approach

Do **not** do the full CI rewrite first.

| Phase             | When                               | Goal                       | Contents                                        |
| ----------------- | ---------------------------------- | -------------------------- | ----------------------------------------------- |
| **A — bootstrap** | **first (PR 0)**                   | Green, observable baseline | Fix the actual red causes; one small runner fix |
| **B — hardening** | after runner + Enlighten + tooling | "Full" CI                  | lint, matrix, split tests, canary, release      |

Why not full CI first: it would be red for code reasons you haven't fixed, conflict with the code PRs, and add lint failures on code about to change. Bootstrap first, harden after.

### Phase A — bootstrap (the actual red causes)

- `actions/checkout@v4` (or v5) with `submodules: recursive`.
- Guard the submodule:
  ```yaml
  - run: test -f example/tortillas_config.yml || (echo "example submodule missing"; exit 1)
  ```
- Use `astral-sh/setup-uv@v6` + `uv sync --extra dev` / `uv run`; avoid system `pip` (PEP 668).
- Replace `cmake -B... -H...` with `cmake -S ./example -B /tmp/sweb` and `cmake --build /tmp/sweb --parallel "$(nproc)"`.
- Add `-DCMAKE_POLICY_VERSION_MINIMUM=3.5` so the example's old `cmake_minimum_required()` also works on CMake 4.x (ignored with a warning on 3.28).
- Copy the example config to `/tmp` and raise `bootup_timeout_secs` to 60, passing it with `-C`; this keeps the submodule checkout pristine.
- Remove the "interrupts stopped" panic heuristic (see §4 item 13); rely on the explicit boot timeout instead.
- Add a unit regression test for the watchdog (`tests/check_interrupt_watchdog.py`): silence must time out, a matching interrupt must be found.
- Pin `runs-on: ubuntu-24.04`; add `timeout-minutes` per job.
- Guard the summary:
  ```yaml
  - if: always()
    run: |
      if [ -f /tmp/sweb/tortillas_summary.md ]; then
        cat /tmp/sweb/tortillas_summary.md >> $GITHUB_STEP_SUMMARY
      else
        echo "No tortillas summary was produced" >> $GITHUB_STEP_SUMMARY
      fi
  ```
- Upload `out.log`/`qemu.log`/summary on failure (`actions/upload-artifact@v4`).
- Assert the example summary contains `SUCCESS` (not just exit 0).
- **Drop the old `panic` job from bootstrap.** The public `tortillas-sweb@panic` branch panics during boot and never writes a summary, so the old `! python -m tortillas` check passed for the wrong reason. A proper test-level panic fixture belongs in Phase B (after runner hardening).
- Run existing unit tests without qemu: `pytest --ignore=tests/check_qemu_interface.py` (proper markers come in Phase B).
- Add `permissions: contents: read`, `concurrency`, `workflow_dispatch`.

### Phase B — hardening (what "full CI" means)

| Job           | Contents                                                                                               |
| ------------- | ------------------------------------------------------------------------------------------------------ |
| `lint`        | `ruff check`, `ruff format --check`, `mypy` (needs the tooling fix first)                              |
| `unit`        | matrix 3.10–3.13, `pytest -m "not integration"`, coverage, `pytest-timeout`                            |
| `smoke`       | `uv build`, clean-venv install, `tortillas --version` / `--help` / `--list` (no qemu)                  |
| `integration` | build **pinned** example, run `tortillas`, assert summary, upload artifacts, 15 min cap                |
| `panic`       | dedicated test-level panic fixture (not the boot-panicking `@panic` branch), assert `PANIC` in summary |
| `canary`      | weekly + dispatch, latest `tortillas-sweb@main`, non-gating, open/update issue on failure              |
| `release`     | on tag: `uv build`, attach dist, install from tag, smoke test                                          |

Cross-cutting: uv/apt/ccache caching, `concurrency` cancellation, `permissions: contents: read`, pin actions by SHA, Dependabot for actions + submodule + pip.

### Two-tier sweb strategy (pinned PR vs canary)

| Tier                | Target                                          | Gating?             |
| ------------------- | ----------------------------------------------- | ------------------- |
| PR / push CI        | Pinned `example` submodule commit (`931878a…`)  | Yes                 |
| Scheduled canary    | Latest `tortillas-sweb@main`                    | No — opens an issue |
| Optional compat job | Latest `main` on PRs, `continue-on-error: true` | No — skip initially |
| Dependency bumps    | Dependabot PRs for the submodule                | Yes, reviewed       |

- The submodule is already commit-pinned; `.gitmodules branch=main` only matters with `update --remote`. **Do not use `--remote` in PR CI.**
- Why not latest-always: sweb changes syscall numbers, build scripts, grub/limine, toolchains, and timing. Unrelated upstream commits would turn your PR red → alert fatigue → ignored CI.
- Dependabot on the submodule gives you "newest sweb" as a reviewed PR — best of both worlds.

### What "full CI" is missing today

- Unit vs integration split (`check_qemu_interface.py` needs `/tmp/sweb`).
- Lint / format / type (ruff config invalid; mypy never runs).
- Multi-Python (3.10–3.13).
- Packaging/entry-point smoke (`uv build` + clean install).
- Summary-content assertions (`SUCCESS`/`PANIC`, not just exit code).
- Hard timeouts (job + `pytest-timeout`).
- Artifact capture on failure (`out.log`, `qemu.log`, summary).
- CLI smoke (`--list`, `--version`, `--help`, config parse).
- Config/spec validation tests.
- Disk guard (1.5 GB/run on tmpfs).
- Canary + release install test.
- Action pinning + Dependabot.
- macOS smoke (optional; full qemu integration on macOS runners is slow).

### Honest limits

- GitHub-hosted runners have **no KVM** → qemu is TCG and slow. Don't run all 108 tests per PR; the example `mult` is the right smoke test. The full suite belongs in your course GitLab CI or the nightly canary.
- Pin `ubuntu-24.04` (and the qemu version if timing flakiness appears); `ubuntu-latest` drifts.
- The fork has 0 runs: enable Actions on the fork and push there to gate. Upstream PRs run upstream's CI (currently red) regardless.
- Don't auto-retry hangs (`pytest-rerunfailures` on genuine integration flake is fine; never on the runner tests).
- "Fully robust" is asymptotic. Stop at green + fast + artifact-rich + one canary. A pinned dev-container image is the next step _if_ CI gets flaky — not now.

### GitLab CI examples

Both are stale:

- `debian:bullseye-slim` / `alpine:3.16` are old/EOL.
- `build` and `tortillas` run in separate containers; `/tmp/sweb` from `build` is not shared, so `build` is redundant — `tortillas` rebuilds anyway.
- `tortillas` clones upstream `main` unpinned.
- No artifacts, `interruptible`, timeout, or `cmake -S`.
- `setup_cmake.sh` is old convention; prefer `cmake -S . -B /tmp/sweb`.
- If ISEC GitLab disables artifacts, document that clearly instead of the current "I think" note.
- Course repo: install a **tagged** Tortillas release and run the full suite there.

---

## 6. P0-4 — README has concrete wrong information

**File:** `README.md`

| Location         | Problem                                     | Fix                                                                                                    |
| ---------------- | ------------------------------------------- | ------------------------------------------------------------------------------------------------------ |
| :244, :246, :355 | Says `tortillas-config.yml` (hyphen)        | Actual file is `tortillas_config.yml` (underscore)                                                     |
| :238             | `--category malloc,brk`                     | `argparse` `nargs="*"` does not split commas; use `--category malloc brk` or implement comma splitting |
| :29              | `enligthen` typo                            | `enlighten` — or remove the dependency (preferred)                                                     |
| :372             | `add_as_error_last` adds the **last** match | Code uses `logs[0:1]` (first). Fix code to `logs[-1:]`                                                 |
| :422             | "first or seconds line"                     | "first or second line"                                                                                 |
| :27              | Python `>=3.8`                              | 3.8/3.9 EOL; use `>=3.10` (or `>=3.11`)                                                                |
| :53–80           | Two install variants, `.[fancy]` escaping   | One command after removing Enlighten                                                                   |
| :74              | `git clone` without `--recurse-submodules`  | Example submodule is empty otherwise                                                                   |
| :5–6             | Badges point upstream                       | Update to active repo or accept upstream status                                                        |
| :203             | "I think artifacts are disabled"            | Verify and state clearly                                                                               |
| :308–320         | `tortillas_expect` uses `__VA_OPT__`        | Works in GNU modes; fails under `-std=c11/c17 -pedantic-errors`; document or make portable             |
| :432–439         | "Does not rely on log output"               | True for boot/completion/panic; exit codes/asserts do use logs — clarify                               |
| :460–471         | "Multiple processes known issue"            | Current sweb fork/exec tests pass; update or qualify                                                   |

Add sections:

- **Development:** `uv sync --extra dev`, `pre-commit install`, `pytest`, `git submodule update --init --recursive`.
- **Platform support:** Linux, WSL2, macOS. Native Windows **not** supported.
- **Exit codes:** 0 success, 1 failure/no tests (for CI users).
- **Troubleshooting:** stalls / ANSI spam (after fixing them).
- **Maintainers / contributing / release.**

Do **not** rewrite the README wholesale; it is detailed and mostly useful.

---

## 7. P1 — Tooling: ruff, mypy, pre-commit, tests

### Ruff is currently broken

`pyproject.toml` selects `ANN102`, which modern ruff removed:

```text
Rule `ANN102` was removed and cannot be selected.
```

Also: top-level `select` is deprecated; `ANN001` is listed twice; Black and ruff-format are both configured.

```toml
[tool.ruff.lint]
select = ["E", "F", "ANN", "C4", "RET", "SIM", "I", "B"]

[tool.ruff.lint.per-file-ignores]
"tests/*" = ["ANN"]
```

Use `ruff format`, drop Black.

### Pre-commit is outdated

- `pre-commit-hooks` v3.2.0 (2020)
- ruff v0.0.254 (2023)
- Black 23.1.0 (2023)

Update versions; add `ruff-format`, `mypy`, `check-yaml`, `check-toml`, `check-added-large-files`, `check-merge-conflict`; remove Black.

### Mypy is cheap

Only 2 errors today. Add `mypy`, `types-PyYAML`, pragmatic config (`check_untyped_defs = true`, not strict), and a lint job. Catches real lifecycle bugs (`self.process`/`self.input` used before assignment).

> **Ordering:** this tooling PR (step 3) is a prerequisite for the CI `lint` job (step 4). Fix ruff/mypy here, then enable the job; otherwise the new lint job is red on arrival.

### Tests

Current tests are good for parser/analyzer, but:

- Use relative paths (`./tests/assets/...`) → fail from another cwd. Use `Path(__file__).parent / "assets"`.
- `check_qemu_interface.py` is an integration test requiring built `/tmp/sweb`; mark `@pytest.mark.integration` and skip by default.
- Add unit tests for:
  - `InterruptWatchdog.parse_interrupt`, `match_registers`, `search_interrupt`
  - `TestSpec` parsing (missing fields, empty file, string tags, invalid timeout)
  - `TortillasConfig` validation (missing file, bad types, invalid mode/status)
  - `filter_test_specs` (categories, tags, comma splitting)
  - `add_as_error_last` (must be last)
  - `TestRunner.success` with `NOT_RUN`
  - scheduler: worker exception returns, retries bounded, every test runs once
  - summary: errors from passing tests/warnings are not silently dropped
- Add `pytest-timeout` (regression tests must fail, not hang CI).
- Add `pytest-cov`; target ~70–80% on pure modules, not qemu integration.

### Python floor

Code imports on 3.8–3.13, but 3.8/3.9 are EOL and 3.10 reached EOL in Oct 2026. Use `requires-python = ">=3.10"` (Ubuntu 22.04 default) or `>=3.11`. Test 3.10–3.13. `uv` can install a modern Python on older devices.

### Packaging

- Add `__version__` and `--version`; bump to `0.3.0` after P0.
- Add `readme`, `license`, classifiers, keywords, Issues URL.
- Fix description: says IAIK/sweb; repo says ISEC/sweb.
- Add `[tool.setuptools] packages = ["tortillas"]` (avoid packaging tests).
- Pin `pyyaml>=6,<7`.
- Add `uv.lock` for reproducibility.
- Add `CHANGELOG.md` + tags (`uv tool install ...@v0.3.0`, not `@main`).
- Add SPDX/GPL headers to Python files (low priority).

### `salsa.py` is stale

Expects old header `#Tortillas test config`; current tests use `---`. It marks current tests as missing metadata. Update it to use `TestSpec`, or replace with `tortillas --list --markdown` and delete it. README currently advertises it.

---

## 8. P1 — CLI/UX, validation, correctness

High ROI, mostly small:

- `--build-dir PATH` / `TORTILLAS_BUILD_DIR`: stop hardcoding `/tmp/sweb` (`constants.py:8`). Also fixes multiple-sweb runs, tmpfs RAM usage, and concurrent runs sharing `/tmp/sweb/tortillas`.
- `--threads N`: override `config.threads`; consider `min(4, os.cpu_count())` default or document.
- `--list` / `--list --markdown`: discovered tests, categories, tags, disabled; validate headers before a long run. Replaces `salsa.py`.
- `--version`.
- `--log-level` / `--verbose` / `--quiet` (`LOG_LEVEL` is hardcoded in `constants.py:6`).
- `--keep-logs` (default: keep failed logs, remove passed).
- `--max-retries N` (default 2–3).
- Comma-separated `--category malloc,brk` / `--tag a,b` (or fix README to spaces).
- Validate config/specs with friendly errors instead of tracebacks/`sys.exit` in library code:
  - `threads >= 1`, `timeout > 0`, `repeat >= 1`
  - `mode` in known set; `set_status` in `PANIC/FAILED`
  - `expect_exit_codes` list of ints; `tags` list (normalize string)
  - `pattern` has group 1 and compiles
  - `yaml.safe_load` returns a dict
  - empty test file must not `IndexError`
- Fix `TestRun.__repr__`: run numbers are 0-based; with `-r 2` first run has no label, second says "Run 1". Use 1-based and always label when `repeat > 1`.
- Fix `TestRunner.success`: treat `NOT_RUN` as failure.
- Fix summary: errors from `SUCCESS` tests (`lock_logs`, `backtrace` without `set_status`) are hidden because "Errors" only prints when `self.success` is false. Show warnings for all runs with errors, or make those configs fail explicitly.
- Replace shell/OS-specific ops:
  - `os.system` (`__main__.py:28,30`) → `subprocess.run(list, check=True)`
  - `cp`/`mkdir`/`rm` → `shutil.copyfile`/`mkdir`/`rmtree`
  - `cmake -H` → `cmake -S`
  - `-j4` → `--jobs`/`os.cpu_count()`
- Handle `KeyboardInterrupt`, always kill qemu, exit 130.
- Add `--doctor`/`--check-setup` (qemu, cmake, config, build image, test headers). Optional but very user-friendly.

---

## 9. P1 — Disk & runtime cleanup

Every test copies `SWEB-snapshot.qcow2` and keeps it forever → 1.5 GB / 108 tests.

- After a passing test, delete its `tmp_dir` (qcow2 + log) unless `--keep-logs`.
- Keep failed/timed-out logs for the summary.
- Run-specific temp dir (`TEST_RUN_DIR / f"run-{pid}"`) to avoid collisions.
- On Linux, `cp --reflink=auto` makes copies nearly free on btrfs/xfs; fall back to `shutil.copyfile`. **Do not** switch to a qcow2 overlay for `-loadvm` without testing: VM state lives in the snapshot image and `-loadvm` may not see a backing image's snapshot table.

Measured overhead worth reducing **after measuring** (do not change blindly):

- `sweb_input()` calls `monitor_command()`, which sleeps **0.2 s per character** (`qemu_interface.py:164`) → ~4 s for a 20-char command per test.
- `time.sleep(1)` after completion and after `exit` (`test_runner.py:388,394`) → ~2 s/test.
- `InterruptWatchdog.sleep_time = 0.5` → up to 0.5 s completion latency/test.
- Main loop spins at 10 kHz.

Reduce sendkey delay (0.02–0.05 s with shorter key hold), poll at 0.1 s, replace fixed sleeps with "wait until quiet". Add `--threads` and optional `--accel kvm` (opt-in; KVM changes timing, no KVM in CI).

---

## 10. P1 — Platform support

| Platform       | Status                                                                                            |
| -------------- | ------------------------------------------------------------------------------------------------- |
| Linux          | Works                                                                                             |
| WSL2           | Works (it is Linux)                                                                               |
| macOS          | Likely works (`mkfifo`, `cp`, `rm`, `/tmp`, qemu x86_64 emulation); slow on ARM; should be tested |
| Native Windows | **Does not work**                                                                                 |

Native Windows blockers: `os.mkfifo` (Unix-only), `/tmp/sweb`, `cp`/`rm`/`mkdir`, `-serial /dev/null`, `os.killpg`/`fcntl`, process-group/signal handling.

If native Windows is ever needed: `tempfile.gettempdir()`/`--build-dir`, `shutil`, `os.devnull`, replace FIFO monitor with `-monitor stdio` + Popen pipes or TCP loopback monitor, guard `killpg`/`fcntl`. **Recommendation:** document WSL2 as the Windows path; treat native Windows as P2.

---

## 11. P2 — Architecture & future features

Module split is good; keep it. As the code evolves:

- Introduce a `RunConfig`/`RunContext` dataclass (build dir, threads, timeouts, keep logs, max retries) instead of passing `args`/globals.
- Give `QemuInterface` a proper lifecycle: `close(timeout)`, `kill()`, `is_alive` property, per-test `qemu.log`, cleanup on `__enter__` failure.
- Add `attempts`, `duration`, `exception` to `TestResult`.
- Add `--junit-xml PATH` via `xml.etree.ElementTree` (no new dependency).
- Process isolation per test (endurance-style) as the ultimate "a test can never hang the runner" guarantee — larger change; thread + timeout is enough for now.
- Add `--doctor` and `--list` before adding execution modes.
- Scheduled canary + Dependabot/Renovate for actions and deps (delivered by CI hardening, §5).
- Release workflow on tags (`uv build`, GitHub release) (delivered by CI hardening, §5).

---

## 12. Branch & fork notes

- `production` is **5 commits behind `main`**, has a divergent README, and its `tortillas_expect` macro is the old version that breaks with no variadic args.
- Its install command uses `[fancy]`, which crashes with current Enlighten.
- Installing from `@production` gives stale/broken behavior.
- Recommendation: merge/rebase `main` into `production`, drop `[fancy]`, tag `v0.3.0`, install by tag. Avoid long-lived divergent branches.
- **Decide the canonical repo before relying on CI gating:** upstream `PointerDilemma/Tortillas` (PRs run its CI) vs. the fork `felixnagele/Tortillas` (Actions must be enabled; currently 0 runs). Badges point upstream and stay red until upstream is fixed.
- Enable Actions on the fork if CI is wanted there; otherwise rely on PRs upstream. Update badges/URLs to the canonical repo.

---

## 13. Suggested PR sequence (checklist)

Order follows §1: measurement first, then the bugs, then hardening.

0. [ ] **`ci: bootstrap green pipeline`** (do this first)
       `checkout@v4` + `submodules: recursive`, submodule guard, `cmake -S`, uv, summary guard, artifacts, `timeout-minutes`, `workflow_dispatch`, `concurrency`, `ubuntu-24.04`; assert example summary contains `SUCCESS`; drop the false-positive `panic` job (it returns in step 4 with a proper fixture); run unit tests with `--ignore=tests/check_qemu_interface.py`.
1. [ ] **`fix(runner): never hang on worker/qemu failure`**
       executor/queue, `try/except/finally`, bounded retries, `process.wait(timeout)+kill`, `poll() is not None`, redirect qemu output, cleanup, signal handling, regression tests; add `pytest-timeout` here so the regression test can't hang CI.
2. [ ] **`fix(progress): remove enlighten, plain/built-in output`**
       fix crash + ANSI spam; one install command; TTY detection; no DSR queries.
3. [ ] **`chore: ruff/mypy/pre-commit/packaging`**
       fix ruff config, add mypy/types/coverage, update hooks, version/license metadata, `uv.lock`.
4. [ ] **`ci: harden — lint, matrix, split tests, canary, release`**
       lint job, unit 3.10–3.13, integration marker, packaging smoke, proper test-level panic fixture, scheduled canary vs `tortillas-sweb@main`, tag release + install test.
5. [ ] **`docs: fix config filename, category example, install, platform`**
       the concrete README errors from §6 plus the platform-support section; do it after behavior is final.
6. [ ] **`feat(cli): build-dir, threads, list, version, validation`**
       UX and weaker-device support.
7. [ ] **`perf: reduce sendkey/poll sleeps, clean logs`**
       only after measuring.
8. [ ] **`feat: junit xml / doctor`** (optional).
9. [ ] **`feat: native windows monitor`** (only if truly needed).

Each PR: one behavior change, one regression test, no formatting mixed in.

> **Dependencies:** step 4's `lint` job needs step 3. Steps 1–2 are validated by step 0. Do not gate any PR on unpinned `tortillas-sweb@main`; that is the canary's job.

---

## 14. Key file map

| File                                      | Role / what to watch                                                                                        |
| ----------------------------------------- | ----------------------------------------------------------------------------------------------------------- |
| `tortillas/test_runner.py`                | Custom scheduler; `_run`, retry/callback races, freeze root causes                                          |
| `tortillas/qemu_interface.py`             | `process.wait()` without timeout, unread `stdout=PIPE`, `is_alive()` bug, sendkey delays, interrupt polling |
| `tortillas/progress_bar.py`               | Enlighten integration, `count_00` crash, DSR spam                                                           |
| `tortillas/log_analyzer.py`               | `add_as_error_last` uses first match; `STOPPED` doesn't set FAILED                                          |
| `tortillas/__main__.py`                   | `os.system`, `cmake -H`, CLI options, build/summary flow                                                    |
| `tortillas/constants.py`                  | Hardcoded `/tmp/sweb`, hardcoded log level                                                                  |
| `tortillas/test_specification.py`         | Validation gaps, empty-file `IndexError`, `sys.exit` in library code                                        |
| `tortillas/tortillas_config.py`           | Validation gaps, invalid mode/status handling                                                               |
| `pyproject.toml`                          | EOL Python floor, `[fancy]` extra, broken ruff config, packaging metadata                                   |
| `.github/workflows/demo_ci.yml`           | Red Demo job, checkout@v3, missing summary guard                                                            |
| `.github/workflows/internal_tests_ci.yml` | Red compile step, mixed integration tests, system pip                                                       |
| `.pre-commit-config.yaml`                 | Outdated ruff/Black/pre-commit-hooks revisions                                                              |
| `README.md`                               | Wrong config filename, category example, `add_as_error_last`, install/platform claims                       |
| `tests/check_qemu_interface.py`           | Flaky integration tests requiring `/tmp/sweb`; should be marked integration                                 |

---

## 15. Quick answers (FAQ for future agents)

- **Rewrite?** No. Keep Python and the core; fix lifecycle/concurrency.
- **Is Python good?** Yes. Floor is stale; use 3.10+ (or 3.11+); let `uv` manage Python.
- **Do CI first?** Yes — but only the **bootstrap** (green, observable, one small runner fix). Full CI hardening comes after the runner/Enlighten/tooling work.
- **Highest ROI?** Small green CI → runner robustness → remove Enlighten → CI hardening → README correctness.
- **Concurrency model bad?** The custom callback scheduler is flawed. Threads are fine; `asyncio` unnecessary. Use an executor/queue with timeouts and a single UI writer.
- **Wrong locking?** Lock protects writes, not reads; main loop can drop retries / exit early.
- **Progress bar default?** Not with Enlighten. Dependency-free TTY-only renderer (default on in terminals, off in CI), or plain logs until it exists.
- **Other libs?** No. PyYAML only.
- **More tests?** Yes: scheduler, interrupt parsing, config/spec validation, `add_as_error_last`, `NOT_RUN`; mark qemu tests integration.
- **Mypy?** Yes, cheap (2 errors today); add after P0. Fix ruff (broken) and update pre-commit.
- **Newest sweb always?** **No for PR gating** — it turns unrelated upstream commits into your red builds. Pin PR CI, run latest as a non-gating scheduled canary, and use Dependabot submodule bumps for reviewed updates.
- **CI on latest sweb?** Scheduled canary vs `tortillas-sweb@main`; pin PR CI; verify panic summaries, not just exit codes. GitHub runners have no KVM, so keep PR integration to the example `mult`, not the full 108-test suite.
- **Windows?** Linux/WSL/macOS yes (macOS untested); native Windows no. Document WSL; native Windows is a separate project.
- **Risk?** Moderate risk is fine, but stage it. Core parser/analyzer/snapshot is solid; scheduler/progress/CI need work. Don't merge the endurance branch as-is.
