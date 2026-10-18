import pytest

from plugins.komodo.action_store import ActionStore, ActionStoreError


PAYLOAD = {"section": "servers", "action": "prune_images", "ids": ["a" * 24]}


def test_finished_request_survives_reopening_and_is_not_reserved_again(tmp_path):
    path = str(tmp_path / "actions.sqlite3")
    store = ActionStore(path)
    assert store.reserve("session", "request", PAYLOAD) is None
    result = {"results": [{"id": "a" * 24, "status": "submitted"}]}
    store.finish("session", "request", 200, result)
    assert ActionStore(path).reserve("session", "request", PAYLOAD) == (200, result)


def test_pending_request_after_reload_cannot_be_repeated(tmp_path):
    path = str(tmp_path / "actions.sqlite3")
    ActionStore(path).reserve("session", "request", PAYLOAD)
    with pytest.raises(ActionStoreError) as error:
        ActionStore(path).reserve("session", "request", PAYLOAD)
    assert error.value.code == "action_already_requested"
    assert error.value.status_code == 409


def test_same_uuid_cannot_change_its_target_and_sessions_are_separate():
    store = ActionStore(":memory:")
    store.reserve("alice", "request", PAYLOAD)
    with pytest.raises(ActionStoreError) as error:
        store.reserve("alice", "request", {**PAYLOAD, "ids": ["b" * 24]})
    assert error.value.code == "idempotency_conflict"
    assert store.reserve("bob", "request", PAYLOAD) is None


def test_state_failure_is_fixed_and_never_falls_back_to_memory(tmp_path):
    store = ActionStore(str(tmp_path / "missing" / "actions.sqlite3"))
    with pytest.raises(ActionStoreError) as error:
        store.reserve("session", "request", PAYLOAD)
    assert error.value.code == "action_store_unavailable"
    assert str(tmp_path) not in str(error.value)


def test_unknown_receipt_is_not_expired_by_later_requests(monkeypatch):
    store = ActionStore(":memory:")
    store.reserve("session", "unknown", PAYLOAD)
    result = {"results": [{"id": "a" * 24, "status": "outcome_unknown"}]}
    store.finish("session", "unknown", 200, result)
    monkeypatch.setattr("plugins.komodo.action_store.time.time", lambda: 9_999_999_999)
    store.reserve("session", "later", PAYLOAD)
    assert store.reserve("session", "unknown", PAYLOAD) == (200, result)
