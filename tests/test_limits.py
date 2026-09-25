import os
import sys
import time

import psutil

from catchrate.verify.limits import run_bounded

PY = sys.executable


def _run(code: str, tmp_path, **limits):
    return run_bounded([PY, "-c", code], cwd=tmp_path, env=dict(os.environ), **limits)


def test_a_finished_run_keeps_its_output_and_code(tmp_path):
    done = _run("import sys; print('out'); sys.stderr.write('err'); sys.exit(3)", tmp_path, timeout=60)
    assert (done.returncode, done.stopped) == (3, None)
    assert [line for line in done.output().splitlines() if line] == ["out", "err"]  # stderr last


def test_a_timeout_stops_the_whole_tree(tmp_path):
    pid_file = tmp_path / "child.pid"
    code = (
        "import subprocess, sys; "
        f"child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)']); "
        f"open(r'{pid_file}', 'w').write(str(child.pid)); child.wait()"
    )
    start = time.monotonic()

    done = _run(code, tmp_path, timeout=3)

    assert (done.returncode, done.stopped) == (None, "timeout") and time.monotonic() - start < 30
    assert "over the time limit" in done.output()
    assert not psutil.pid_exists(int(pid_file.read_text())) or psutil.Process(int(pid_file.read_text())).status() == "zombie"


def test_a_run_over_the_memory_limit_is_stopped(tmp_path):
    done = _run("import time; data = bytearray(400 << 20); time.sleep(60)", tmp_path, timeout=60, max_memory=200 << 20)
    assert (done.returncode, done.stopped) == (None, "memory")
