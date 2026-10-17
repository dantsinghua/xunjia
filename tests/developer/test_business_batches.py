"""Developer checks for immutable authorization and command lifecycle."""
import pytest


def service(tmp_path):
    from server.business import InquiryService
    from server.repository import Repository
    repo = Repository("sqlite:///" + str(tmp_path / "business.db"))
    repo.bind_account("a", "91001", "wb")
    repo.bind_account("b", "91002", "wb")
    return InquiryService(repo, clock=lambda: 1800000000)


def item():
    return {"code": "SYN001.IB", "name": "合成债", "peer_ids": ["peer-1"],
            "our_side": "BUY", "volume_wan": "1000", "opening_yield_pct": "2.50",
            "limit_yield_pct": "2.40", "step_bp": "1"}


def test_preview_has_no_command_and_confirmation_is_idempotent(tmp_path):
    svc = service(tmp_path)
    draft = svc.preview("a", "request-a", [item()])
    assert svc.commands("a") == []
    first = svc.confirm("a", draft["batch_id"], draft["version"], "confirm-a")
    again = svc.confirm("a", draft["batch_id"], draft["version"], "confirm-a")
    assert first["commands"][0]["command_id"] == again["commands"][0]["command_id"]
    assert len(svc.commands("a")) == 1
    assert svc.commands("b") == []
    with pytest.raises(PermissionError):
        svc.confirm("b", draft["batch_id"], draft["version"], "confirm-b")


def test_stopping_cancels_queued_command_and_never_reopens_on_late_receipt(tmp_path):
    svc = service(tmp_path)
    draft = svc.preview("a", "request-a", [item()])
    confirmed = svc.confirm("a", draft["batch_id"], draft["version"], "confirm-a")
    command = confirmed["commands"][0]
    svc.set_bot("a", False)
    assert svc.commands("a")[0]["status"] == "cancelled"
    svc.receipt(command["command_id"], account_uid="91001", peer_id="peer-1", status="sent", evidence="native-msg-1")
    assert svc.sessions("a")[0]["state"] == "STOPPED"


def test_idempotency_conflict_and_bad_direction_price_are_rejected(tmp_path):
    svc = service(tmp_path)
    svc.preview("a", "same", [item()])
    changed = {**item(), "volume_wan": "2000"}
    with pytest.raises(ValueError):
        svc.preview("a", "same", [changed])
    wrong = {**item(), "opening_yield_pct": "2.20"}
    with pytest.raises(ValueError):
        svc.preview("a", "other", [wrong])
