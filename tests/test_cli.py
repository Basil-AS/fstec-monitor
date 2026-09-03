from __future__ import annotations

import os
import subprocess
import sys


def test_cli_module_is_executable():
    env = {**os.environ, "FSTEC_DATABASE_URL": "sqlite:///./test-cli.db"}
    result = subprocess.run(
        [sys.executable, "-m", "fstec_monitor.cli", "--help"],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "Usage:" in result.stdout
    assert "run" in result.stdout
