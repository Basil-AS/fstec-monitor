from pathlib import Path

ROOT = Path(__file__).parents[1]


def test_standalone_systemd_timer_runs_once_daily_at_noon():
    timer = (ROOT / "systemd/fstec-monitor-scan.timer").read_text()

    assert "OnCalendar=*-*-* 12:00:00" in timer
    assert "00/2" not in timer
    assert "RandomizedDelaySec" not in timer


def test_bot_unit_uses_internal_scheduler_without_a_parallel_timer():
    bot_unit = (ROOT / "systemd/fstec-monitor.service").read_text()

    assert "ExecStart=" in bot_unit
    assert "OnCalendar=" not in bot_unit
    assert "Wants=telegram-bot-api.service" in bot_unit
    assert "After=telegram-bot-api.service" in bot_unit
    assert "Type=notify" in bot_unit
    assert "WatchdogSec=60s" in bot_unit
    assert "Restart=on-failure" in bot_unit
    assert "RestartSec=5s" in bot_unit
    assert "TasksMax=512" in bot_unit
    assert "CPUWeight=100" in bot_unit
    assert "MemoryHigh=768M" in bot_unit
    assert "MemoryMax=1G" in bot_unit
    assert "Requires=telegram-bot-api.service" not in bot_unit
    assert "EnvironmentFile=-/srv/runtime/secrets/telegram-admin.env" in bot_unit
    assert "TELEGRAM_API_ROOT=http://127.0.0.1:18081" in bot_unit
    assert "ReadWritePaths=/srv/runtime/apps/fstec-monitor/data" in bot_unit
    assert not (ROOT / "systemd/fstec-monitor.timer").exists()
    assert not (ROOT / "systemd/fstec-monitor-bot.timer").exists()


def test_readme_describes_daily_schedule_and_single_runner_choice():
    readme = (ROOT / "README.md").read_text()

    assert "раз в сутки в 12:00" in readme
    assert "один автоматический запуск" in readme
    assert "telegram-bot-api.service" in readme
    assert "том же user manager" in readme
    assert "Wants=" in readme and "After=" in readme
    assert "Requires=" in readme
