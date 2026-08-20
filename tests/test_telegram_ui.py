from fstec_monitor.telegram.callbacks import decode_callback, encode_callback
from fstec_monitor.telegram.keyboards import (
    admin_keyboard,
    scan_keyboard,
    settings_keyboard,
    user_keyboard,
)
from fstec_monitor.telegram.rendering import progress_bar, render_error_notice, render_scan_progress


def _all_buttons(markup: dict) -> list[dict]:
    return [button for row in markup.get("keyboard", markup.get("inline_keyboard", [])) for button in row]


def test_admin_and_user_keyboards_have_unique_labels_and_role_boundaries():
    admin = _all_buttons(admin_keyboard())
    user = _all_buttons(user_keyboard())

    assert len({button["text"] for button in admin}) == len(admin)
    assert len({button["text"] for button in user}) == len(user)
    assert "🔍 Проверить сейчас" in {button["text"] for button in admin}
    assert "🔍 Проверить сейчас" not in {button["text"] for button in user}


def test_callbacks_round_trip_and_reject_untrusted_payloads():
    encoded = encode_callback("scan", "stop")

    assert decode_callback(encoded) == ("scan", "stop")
    assert decode_callback("scan:stop") is None
    assert decode_callback("v2:scan:stop:extra") is None
    assert decode_callback("v1:unknown:value") is None
    assert decode_callback("v1:scan:" + "x" * 100) is None


def test_scan_keyboard_exposes_only_valid_actions():
    callbacks = {button["callback_data"] for button in _all_buttons(scan_keyboard("running"))}

    assert "v1:scan:stop" in callbacks
    assert all(value.startswith("v1:") for value in callbacks)


def test_settings_keyboard_has_stable_callback_schema():
    callbacks = [button["callback_data"] for button in _all_buttons(settings_keyboard(True))]

    assert callbacks
    assert all(value.startswith("v1:") for value in callbacks)
    assert len(callbacks) == len(set(callbacks))


def test_progress_and_error_rendering_are_bounded_and_redacted():
    assert progress_bar(0) == "░░░░░░░░░░"
    assert progress_bar(100) == "██████████"
    assert "50%" in render_scan_progress(type("P", (), {"stage": "Чтение", "completed": 1, "total": 2, "errors": 0, "percent": 50})())
    notice = render_error_notice("polling", "secret-token-value")
    assert "secret-token-value" not in notice
    assert len(notice) < 300
