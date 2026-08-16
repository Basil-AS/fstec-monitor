from types import SimpleNamespace

from fstec_monitor.access import access_request_text, is_allowed


def test_only_approved_users_are_allowed():
    assert is_allowed(SimpleNamespace(status="approved"))
    assert not is_allowed(SimpleNamespace(status="pending"))
    assert not is_allowed(None)


def test_access_request_contains_identity_and_id():
    text = access_request_text(42, "alice", "Alice")
    assert "@alice" in text
    assert "42" in text
