from pathlib import Path

ROOT = Path(__file__).parents[1]


def test_systemd_timer_runs_once_daily_at_noon():
    timer = (ROOT / "systemd/fstec-monitor.timer").read_text()

    assert "OnCalendar=*-*-* 12:00:00" in timer
    assert "00/2" not in timer
    assert "RandomizedDelaySec" not in timer


def test_readme_describes_daily_schedule_and_single_runner_choice():
    readme = (ROOT / "README.md").read_text()

    assert "раз в сутки в 12:00" in readme
    assert "один автоматический запуск" in readme
