"""Unit tests for the interrupt watchdog.

These do not need qemu: they drive `InterruptWatchdog.wait_until` against a
plain file.
"""

import logging
from pathlib import Path
from typing import cast

from tortillas.qemu_interface import InterruptWatchdog, QemuInterface


class _FakeQemu:
    def __init__(self, int_log_file: Path) -> None:
        self.logger = logging.getLogger("test interrupt watchdog")
        self.int_log_file = int_log_file


def _watchdog(tmp_path: Path) -> InterruptWatchdog:
    log_file = tmp_path / "int.log"
    log_file.touch()
    watchdog = InterruptWatchdog(cast(QemuInterface, _FakeQemu(log_file)))
    # Keep the test fast; wait_until derives its iteration count from this.
    watchdog.sleep_time = 0.01
    return watchdog


def test_silent_interrupt_log_times_out(tmp_path: Path) -> None:
    """A silent log must time out, not be reported as a panic.

    Regression test: the watchdog used to return STOPPED ("Panic?") after a
    few seconds without new interrupts, which false-positived on loaded CI
    runners while the guest was still booting.
    """
    watchdog = _watchdog(tmp_path)

    status = watchdog.wait_until(int_num=80, int_regs={"RAX": 1337}, timeout=0.2)

    assert status == InterruptWatchdog.Status.TIMEOUT


def test_matching_interrupt_is_found(tmp_path: Path) -> None:
    watchdog = _watchdog(tmp_path)
    watchdog.interrupt_logfile.write_text(
        "v=80\nRAX=0000000000000539 RBX=0000000000000000\n"
    )

    status = watchdog.wait_until(int_num=80, int_regs={"RAX": 0x539}, timeout=0.2)

    assert status == InterruptWatchdog.Status.OK
