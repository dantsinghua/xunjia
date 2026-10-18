"""Confirming a new RFQ batch must not resume another globally paused bond."""
import pytest

from test_production_flow import OWNER, confirm, desk, draft, item, raw
from test_acceptance_contract import current, setup_groups


@pytest.mark.parametrize("intake", ["before_global_stop", "during_global_stop"])
def test_new_batch_confirmation_does_not_resume_old_bond_or_its_old_input(desk, intake):
    app, client, _, transport, _ = desk
    a = setup_groups(desk, stopped=False)["SYN-A"]
    peer = a["peers"][0]
    assert len(transport.sent) == 1
    if intake == "before_global_stop":
        raw(app, peer["thread_id"], "SYN-A 3.10出500万")
        # Commit the real fact without generating its automatic reply yet.
        assert app.state.negotiation_engine.analyze_thread(OWNER, peer["thread_id"])["facts_committed"]
    stopped = client.post("/api/bot/toggle", json={"status": "停止"})
    assert stopped.status_code == 200
    assert app.state.repository.snapshot(OWNER)["send_allowed"] is False
    if intake == "during_global_stop":
        raw(app, peer["thread_id"], "SYN-A 3.10出500万")
        app.state.negotiation_engine.run_once()
    assert current(app, peer).price.value == "3.10"
    assert current(app, peer).volume_wan == "500"
    assert len(transport.sent) == 1

    # This explicit authorization contains only SYN-B / peer-b.
    preview = draft(client, [item("SYN-B", ["peer-b"])], key="new-batch-b")
    authorized = confirm(client, preview, key="confirm-b-only")
    assert len(authorized["group_ids"]) == 1
    assert app.state.repository.group(OWNER, authorized["group_ids"][0])["bond_code"] == "SYN-B"
    app.state.negotiation_engine.run_once()
    app.state.dispatcher.run_once()

    assert [command["peer_id"] for command in transport.sent].count("peer-b") == 1, (
        "The newly confirmed batch must still be able to send its own authorized inquiry"
    )
    a_sends = [command for command in transport.sent if command["peer_id"] == "peer-a"]
    assert len(a_sends) == 1, (
        "Confirming B resumed A and sent an unauthorized reply to its pre-stop or pause-period input: "
        + repr([command["text"] for command in a_sends])
    )
    assert current(app, peer).price.value == "3.10", "Pausing sends must preserve the accepted analysis"
