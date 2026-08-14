from fstec_monitor.telegram_bot import api_url, is_admin


def test_api_url_uses_shared_local_bot_api():
    assert api_url("http://127.0.0.1:8081", "token", "getUpdates") == (
        "http://127.0.0.1:8081/bottoken/getUpdates"
    )


def test_only_configured_admin_is_authorized():
    assert is_admin(151599744, 151599744)
    assert not is_admin(151599745, 151599744)
