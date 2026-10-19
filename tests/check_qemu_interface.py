import subprocess
from pathlib import Path

from tortillas.constants import INT_SYSCALL
from tortillas.qemu_interface import QemuInterface, InterruptWatchdog
from tortillas.tortillas_config import TortillasConfig

BUILD_DIR = Path("/tmp/sweb")
IMAGE_PATH = BUILD_DIR / "SWEB.qcow2"
TEST_RUN_DIR = BUILD_DIR / "test"


def _config() -> TortillasConfig:
    """Config of the sweb that the image in `BUILD_DIR` was built from."""
    cache = (BUILD_DIR / "CMakeCache.txt").read_text()
    source = next(
        line.split("=", 1)[1]
        for line in cache.splitlines()
        if line.startswith("CMAKE_HOME_DIRECTORY:")
    )
    return TortillasConfig(Path(source) / "tortillas_config.yml")


def _setup():
    assert IMAGE_PATH.exists()
    if not TEST_RUN_DIR.is_dir():
        TEST_RUN_DIR.mkdir()
    else:
        subprocess.run(f"rm {TEST_RUN_DIR}/*", shell=True)


def _wait_for_boot(qemu: QemuInterface, config: TortillasConfig) -> object:
    return qemu.interrupt_watchdog.wait_until(
        int_num=INT_SYSCALL,
        int_regs={"RAX": config.sc_tortillas_bootup},
        timeout=10,
    )


def test_waiting_for_bootup():
    _setup()
    config = _config()

    with QemuInterface(tmp_dir=TEST_RUN_DIR, qcow2_path=IMAGE_PATH) as qemu:
        res = _wait_for_boot(qemu, config)

        assert res not in (
            InterruptWatchdog.Status.TIMEOUT,
            InterruptWatchdog.Status.STOPPED,
        )


def test_timeout():
    _setup()

    with QemuInterface(tmp_dir=TEST_RUN_DIR, qcow2_path=IMAGE_PATH) as qemu:
        res = qemu.interrupt_watchdog.wait_until(
            int_num=INT_SYSCALL, int_regs={"RAX": 11111}, timeout=10
        )

        assert res == InterruptWatchdog.Status.TIMEOUT


def test_run_mult():
    _setup()
    config = _config()

    with QemuInterface(tmp_dir=TEST_RUN_DIR, qcow2_path=IMAGE_PATH) as qemu:
        res = _wait_for_boot(qemu, config)

        assert res not in (
            InterruptWatchdog.Status.TIMEOUT,
            InterruptWatchdog.Status.STOPPED,
        )

        qemu.sweb_input("mult.sweb\n")

        res = qemu.interrupt_watchdog.wait_until(
            int_num=INT_SYSCALL,
            int_regs={"RAX": config.sc_tortillas_finished},
            timeout=20,
        )

        assert res not in (
            InterruptWatchdog.Status.TIMEOUT,
            InterruptWatchdog.Status.STOPPED,
        )
