"""Parse arguments, make a qemu snapshot of SWEB and run applicable tests."""

from __future__ import annotations
from pathlib import Path

import argparse
import logging
import sys

from .utils import get_logger
from .constants import SWEB_BUILD_DIR, TEST_RUN_DIR
from .test_specification import get_test_specs, filter_test_specs
from .test_runner import TestRunner, SnapshotError
from .tortillas_config import TortillasConfig
from .progress_bar import ProgressBar


def _build_sweb(src_directory: Path, setup: bool, architecture: str):
    import os

    if setup:
        # This command is equivalent to setup_cmake.sh
        cmd = f'cmake -B"{SWEB_BUILD_DIR}" -H"{src_directory}"'

        if architecture in ("x86_32", "x86/32"):
            cmd += " -DARCH=x86/32"

        os.system(cmd)

    if os.system(f"cmake --build {SWEB_BUILD_DIR} -j4") != 0:
        sys.exit(1)
    print()


def main():
    """Tortillas main"""
    log = get_logger("global")

    parser = argparse.ArgumentParser()
    parser.add_argument(
        "-S",
        "--sweb-path",
        type=Path,
        help="Path to a sweb src directory",
        default=Path.cwd(),
    )

    parser.add_argument(
        "-C",
        "--config-path",
        type=Path,
        help="Path to a tortillas config file",
        default=None,
    )

    parser.add_argument(
        "--arch",
        type=str,
        help="Set sweb architecture target. Supported: x86_64, x86_32",
        default="x86_64",
    )

    parser.add_argument(
        "-a", "--skip-setup", action="store_true", help="If set, skip the build setup"
    )

    parser.add_argument(
        "-s", "--skip-build", action="store_true", help="If set, skip building sweb"
    )

    parser.add_argument(
        "--no-progress", action="store_true", help="Turn of the progress bar"
    )

    parser.add_argument(
        "-g",
        "--glob",
        help="Glob of tests in the testfolder\n"
        "e.g. -g test_pthread* "
        "(tests userspace/tests/test_pthread*.c)\n"
        "Note: zsh users must escape: test_pthread\\*",
        default="*",
    )

    parser.add_argument(
        "-c",
        "--category",
        type=str,
        nargs="*",
        help="Category or a list of categories to test",
    )

    parser.add_argument(
        "-t", "--tag", type=str, nargs="*", help="Tag or list of tags to test"
    )

    parser.add_argument(
        "-r",
        "--repeat",
        type=int,
        help="Run the specified tests multiple times. -r 2 will run all tests 2 times",
        default=1,
    )

    parser.add_argument(
        "--endurance",
        action="store_true",
        help="Endurance mode: run the selected tests over and over until "
        "stopped (Ctrl-C) or until --rounds is reached. Only failures are "
        "logged; a report is written to <build-dir>/endurance_report.md",
    )

    parser.add_argument(
        "--rounds",
        type=int,
        default=0,
        help="Endurance mode only: stop after this many rounds (0 = run until stopped)",
    )

    parser.add_argument(
        "--endurance-report",
        action="store_true",
        help="Regenerate endurance_report.md from the endurance session data "
        "of the last run (rounds.jsonl) and exit",
    )

    # Internal flags used by the endurance supervisor to spawn a worker.
    parser.add_argument(
        "--endurance-round", type=int, default=None, help=argparse.SUPPRESS
    )
    parser.add_argument(
        "--endurance-result", type=Path, default=None, help=argparse.SUPPRESS
    )

    args = parser.parse_args()

    if args.endurance_report:
        from .endurance import regenerate_report

        ok = regenerate_report()
        logging.shutdown()
        sys.exit(not ok)

    sweb_src_folder = args.sweb_path
    if not args.config_path:
        args.config_path = sweb_src_folder / "tortillas_config.yml"

    # Endurance supervisor prints its own round log, so no progress bar.
    progress_bar = ProgressBar(args.no_progress or args.endurance)
    log.info("Starting tortillas© test system\n")

    config = TortillasConfig(args.config_path)

    all_specs = get_test_specs(sweb_src_folder, args.glob)
    selected_specs = filter_test_specs(all_specs, args.category, args.tag)
    if len(selected_specs) == 0:
        log.error("No test specs were found")
        sys.exit(1)

    TEST_RUN_DIR.mkdir(parents=True, exist_ok=True)

    # Worker process spawned by the endurance supervisor: run one round only.
    if args.endurance_round is not None:
        from .endurance import run_endurance_round

        run_endurance_round(args, selected_specs, config)
        logging.shutdown()
        sys.exit(0)

    progress_bar.update_main_status("Building SWEB")
    if not args.skip_build:
        _build_sweb(sweb_src_folder, not args.skip_setup, args.arch)

    if args.endurance:
        from .endurance import run_endurance

        success = run_endurance(selected_specs, args, config)
        logging.shutdown()
        sys.exit(not success)

    test_runner = TestRunner(
        selected_specs, args.repeat, args.arch, config, progress_bar
    )

    progress_bar.update_main_status("Creating snapshot")
    try:
        test_runner.create_snapshot()
    except SnapshotError as exc:
        log.error(f"Snapshot creation failed: {exc}")
        sys.exit(1)

    progress_bar.update_main_status("Running tests")
    test_runner.start()

    if not test_runner.success:
        log.error("Tortillas has failed!")

    log.info("Completed tortillas© test system\n")

    summary = test_runner.get_markdown_test_summary()
    log.info("")
    log.info(summary)

    logging.shutdown()
    sys.exit(not test_runner.success)


if __name__ == "__main__":
    main()
