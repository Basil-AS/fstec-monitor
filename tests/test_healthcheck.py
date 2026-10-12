from types import SimpleNamespace

import httpx

from fstec_monitor import healthcheck


def test_healthcheck_queries_only_configured_internal_bot_api(monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(
        healthcheck,
        "settings",
        SimpleNamespace(
            telegram_bot_token="token-that-must-not-leak",
            telegram_api_root="http://telegram-bot-api:8081",
        ),
    )

    def fake_get(url, *, timeout, follow_redirects):
        calls.append((url, timeout, follow_redirects))
        return SimpleNamespace(
            status_code=200,
            json=lambda: {"ok": True, "result": {"is_bot": True}},
        )

    monkeypatch.setattr(healthcheck.httpx, "get", fake_get)

    assert healthcheck.check_health() is True
    assert calls == [
        (
            "http://telegram-bot-api:8081/bottoken-that-must-not-leak/getMe",
            healthcheck.HEALTHCHECK_TIMEOUT,
            False,
        )
    ]
    assert "token-that-must-not-leak" not in capsys.readouterr().out


def test_healthcheck_refuses_nonlocal_api_without_network(monkeypatch):
    monkeypatch.setattr(
        healthcheck,
        "settings",
        SimpleNamespace(telegram_bot_token="secret", telegram_api_root="https://example.test"),
    )
    monkeypatch.setattr(
        healthcheck.httpx,
        "get",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("network call")),
    )

    assert healthcheck.check_health() is False


def test_healthcheck_refuses_incomplete_configuration_without_network(monkeypatch):
    monkeypatch.setattr(
        healthcheck,
        "settings",
        SimpleNamespace(telegram_bot_token=None, telegram_api_root="http://127.0.0.1:18081"),
    )
    monkeypatch.setattr(
        healthcheck.httpx,
        "get",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("network call")),
    )

    assert healthcheck.check_health() is False


def test_healthcheck_treats_api_failure_and_invalid_json_as_unhealthy(monkeypatch):
    monkeypatch.setattr(
        healthcheck,
        "settings",
        SimpleNamespace(telegram_bot_token="secret", telegram_api_root="http://127.0.0.1:18081"),
    )
    monkeypatch.setattr(
        healthcheck.httpx,
        "get",
        lambda *args, **kwargs: SimpleNamespace(status_code=503, json=lambda: {"ok": True}),
    )
    assert healthcheck.check_health() is False

    def bad_json(*args, **kwargs):
        raise httpx.DecodingError("secret in opaque response", request=httpx.Request("GET", "http://local"))

    monkeypatch.setattr(healthcheck.httpx, "get", bad_json)
    assert healthcheck.check_health() is False


def test_healthcheck_cli_has_machine_status_only(monkeypatch, capsys):
    monkeypatch.setattr(healthcheck, "check_health", lambda: False)

    assert healthcheck.main() == 1
    output = capsys.readouterr()
    assert output.out == ""
    assert output.err == ""
