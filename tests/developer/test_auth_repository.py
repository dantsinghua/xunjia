"""Developer checks; independent production acceptance lives in acceptance/."""
import hashlib

import pytest


def test_portal_proof_and_application_token_expiry():
    from server.auth import AuthConfig, AuthService

    now = [1800000000]
    service = AuthService(AuthConfig(sso_secret="test-portal", session_secret="test-session-secret-32-characters!", clock=lambda: now[0]))
    data = {"userName": "trader-a", "nickName": "甲", "ts": str(now[0])}
    data["sign"] = hashlib.md5(f"trader-a|甲|{now[0]}|test-portal".encode()).hexdigest()
    user = service.verify_sso(data)
    token = service.issue_token(user)
    assert service.verify_token(token) == user
    now[0] += 3601
    assert service.verify_token(token) is None


def test_binding_survives_new_repository_and_account_cannot_be_borrowed(tmp_path):
    from server.repository import Repository

    url = "sqlite:///" + str(tmp_path / "state.db")
    repo = Repository(url)
    saved = repo.bind_account("trader-a", "91001", "wb-test")
    assert saved["version"] == 1
    again = Repository(url)
    assert again.binding_for("trader-a")["account_uid"] == "91001"
    assert again.binding_for("trader-b") is None
    with pytest.raises(ValueError):
        again.bind_account("trader-b", "91001", "wb-test")


def test_database_transaction_rolls_back(tmp_path):
    from server.repository import Repository

    repo = Repository("sqlite:///" + str(tmp_path / "state.db"))
    with pytest.raises(RuntimeError):
        with repo.db.transaction() as tx:
            tx.execute("INSERT INTO xj_binding(owner_user_id,account_uid,workbench_id,transport,version,bot_enabled,config_json) VALUES(?,?,?,?,?,?,?)", ("a", "91001", "wb", "oss", 1, 0, "{}"))
            raise RuntimeError("synthetic crash")
    assert repo.binding_for("a") is None
