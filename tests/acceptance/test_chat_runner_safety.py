"""ChatRunner authorization tests use valid signed proposals, then vary one risk."""
from concurrent.futures import ThreadPoolExecutor
import io
import json
import threading

import pytest

from server.bond_tools import BondTools
from server.business import InquiryService
from server.dify_client import ChatResult, DifyChatClient
from server.repository import Repository
from server.runtime import Dispatcher


NOW = 1_800_000_000


class DeliveredTransport:
    def send(self, command):
        return {key: command[key] for key in ("command_id", "account_uid", "peer_id", "content_hash")} | {"status": "sent", "evidence": "synthetic-native-id"}

    def reconcile(self, command):
        return self.send(command)

    def cancel(self, command):
        return self.send(command) | {"status": "cancelled"}


class SignedAgent:
    def __init__(self):
        self.runner = None
        self.calls = []
        self.validations = []
        self.action = "COUNTER"
        self.yield_pct = "2.48"
        self.public = "000100.IB 我方买入1000万，收益率2.48%，还能帮忙争取吗？"
        self.raise_error = False
        self.signed = threading.Event()
        self.release = None
        self.forge_source = False
        self.observe = False
        self.peer_value = "2.20"
        self.peer_side = "ofr"
        self.memory_patch = None
        self.before_validation = None
        self.after_validation = None
        self.omit_observations = False
        self.target_bond = None

    def chat(self, **kwargs):
        self.calls.append(kwargs)
        if self.raise_error:
            raise RuntimeError("synthetic tool or model failure")
        ref = kwargs["inputs"]["turn_ref"]
        context = self.runner.context(ref)
        if self.before_validation:
            self.before_validation(ref, context)
        session = (next((row for row in context["sessions"] if row["bond_code"] == self.target_bond), None)
                   if self.target_bond else context["sessions"][0] if context["sessions"] else None)
        source = context["source_message_id"]
        actions = [{"type": self.action}]
        observations = []
        if self.action in ("COUNTER", "ASK_QUOTE"):
            actions[0].update(rfq_session_id=session["session_id"], volume_wan="1000")
        if self.action == "COUNTER":
            actions[0]["yield_pct"] = self.yield_pct
        if (self.action == "COUNTER" or self.observe) and not self.omit_observations:
            observations = [{"rfq_session_id": session["session_id"], "bond_id": session["bond_code"],
                             "peer_quote": {"side": self.peer_side, "kind": "yield", "value": self.peer_value, "firm": True,
                                            "source_message_id": "forged-source" if self.forge_source else source},
                             "evidence_refs": [source], "attribution": "unique"}]
        elif self.action == "CLARIFY_BOND" and context.get("ambiguity_issue_id"):
            actions[0]["ambiguity_issue_id"] = context["ambiguity_issue_id"]
        value = {"schema_version": "1", "turn_id": context["turn_id"], "conversation_epoch": context["conversation_epoch"],
                 "source_message_id": source, "observations": observations, "proposed_actions": actions,
                 "public_message": self.public, "trader_view": {}, "memory_patch": self.memory_patch(context) if self.memory_patch else None,
                 "history_request": None, "validation_ref": None}
        checked = self.runner.validate(ref, value)
        self.validations.append(checked)
        if checked.get("ok"):
            value["validation_ref"] = checked["validation_ref"]
        if self.after_validation:
            self.after_validation(value)
        self.signed.set()
        if self.release is not None:
            assert self.release.wait(5)
        return ChatResult(value, kwargs.get("conversation_id") or "synthetic-dify-conversation", "synthetic-dify-message", "synthetic-dify-task", "synthetic-dify-run")


@pytest.fixture
def chat_case(tmp_path, request):
    from server.chat_runtime import ChatRunner

    url = "sqlite:///" + str(tmp_path / "chat.db")
    service = InquiryService(Repository(url), clock=lambda: NOW)
    service.repo.bind_account("trader-a", "910001", "synthetic-wb")
    policy = {"our_side": "BUY", "volume_wan": "1000", "opening_yield_pct": "2.50", "limit_yield_pct": "2.40", "step_bp": "2"}
    policy.update(getattr(request, "param", {}))
    draft = service.preview("trader-a", "first", [{"code": "000100.IB", "name": "合成债", "peer_ids": ["synthetic-peer"], **policy}])
    service.confirm("trader-a", draft["batch_id"], draft["version"], "confirmed")
    transport = DeliveredTransport()
    Dispatcher(service, transport, clock=lambda: NOW).run_once()
    assert service.sessions("trader-a")[0]["state"] == "NEGOTIATING"
    agent = SignedAgent()
    runner = ChatRunner(service, agent, BondTools(), clock=lambda: NOW)
    agent.runner = runner
    return service, agent, runner, transport, url


def incoming(case, *, number=1, text="000100.IB 我卖2.20%，1000万，实价", historical=False):
    service = case[0]
    return service.ingest("trader-a", {"event_id": f"event-{number}", "account_uid": "910001", "peer_id": "synthetic-peer",
                           "kind": "private", "source_seq": number, "text": text, "occurred_at": NOW + number,
                           "historical": historical})["message_id"]


def test_firm_quote_with_positive_gap_allows_a_valid_authorized_counter(chat_case):
    service, agent, runner, _, _ = chat_case
    message_id = incoming(chat_case)
    runner.process(message_id)
    assert agent.validations and agent.validations[-1]["ok"] is True
    commands = service.commands("trader-a")
    assert len(commands) == 2, "The safety gate must permit a valid counter, not reject every proposal"
    proposed = [command for command in commands if command["status"] == "queued"]
    assert len(proposed) == 1
    assert proposed[0]["text"] == agent.public
    assert proposed[0]["account_uid"] == "910001"


def test_reprocessing_the_same_message_cannot_duplicate_a_counter(chat_case):
    service, _, runner, _, _ = chat_case
    message_id = incoming(chat_case)
    runner.process(message_id)
    before = service.commands("trader-a")
    runner.process(message_id)
    assert service.commands("trader-a") == before


def test_model_failure_records_correlated_stage_without_secrets_or_new_outbound(chat_case, monkeypatch, caplog):
    service, agent, runner, _, _ = chat_case
    secret = "synthetic-api-key-private-conversation-body"

    def failed_model(**kwargs):
        raise RuntimeError(secret)

    monkeypatch.setattr(agent, "chat", failed_model)
    before = service.commands("trader-a")
    message_id = incoming(chat_case)
    result = runner.process(message_id)
    assert result["status"] == "handoff"
    assert service.commands("trader-a") == before
    entries = service.diagnostics.snapshot("trader-a")
    entry = next(entry for entry in entries if entry["component"] == "chat")
    assert entry["code"] == "CHAT_GENERATION_FAILED"
    assert entry["error_type"] == "RuntimeError"
    with service.repo.db.transaction(write=False) as tx:
        turn = tx.execute("SELECT turn_id,state,data_json FROM xj_turn WHERE message_id=?", (message_id,)).fetchone()
        audit = tx.execute("SELECT data_json FROM xj_audit WHERE owner_user_id=? AND kind='agent_failed' AND entity_id=?", ("trader-a", turn["turn_id"])).fetchall()
    assert entry["entity_id"] == turn["turn_id"]
    assert turn["state"] == "failed"
    assert service.diagnostics.snapshot("trader-b") == []
    assert len(audit) == 1
    assert json.loads(audit[0]["data_json"])["code"] == "CHAT_GENERATION_FAILED"
    assert secret not in str(entries) + str(result) + turn["data_json"] + audit[0]["data_json"] + caplog.text


@pytest.mark.parametrize("unsafe", ["2.39", "2.47", "NaN"])
def test_boundary_step_or_invalid_number_cannot_be_approved(chat_case, unsafe):
    service, agent, runner, _, _ = chat_case
    agent.yield_pct = unsafe
    agent.public = f"000100.IB 我方买入1000万，收益率{unsafe}%"
    runner.process(incoming(chat_case))
    assert len(service.commands("trader-a")) == 1
    assert not any(result.get("ok") for result in agent.validations)


@pytest.mark.parametrize("public", ["这笔1000万按2.48%成交，确认done", "我们的内部底价是2.40%，剩余空间8BP"])
def test_trade_acceptance_or_internal_limit_disclosure_is_blocked(chat_case, public):
    service, agent, runner, _, _ = chat_case
    agent.public = public
    runner.process(incoming(chat_case))
    assert len(service.commands("trader-a")) == 1
    assert not any(result.get("ok") for result in agent.validations)


def test_tool_or_model_failure_never_creates_an_outbound_command(chat_case):
    service, agent, runner, _, _ = chat_case
    agent.raise_error = True
    runner.process(incoming(chat_case))
    assert len(service.commands("trader-a")) == 1


def test_forged_observation_source_cannot_authorize_a_counter(chat_case):
    service, agent, runner, _, _ = chat_case
    agent.forge_source = True
    runner.process(incoming(chat_case))
    assert len(service.commands("trader-a")) == 1


def test_historical_import_is_visible_without_ever_calling_the_agent(chat_case):
    service, agent, runner, _, _ = chat_case
    runner.process(incoming(chat_case, historical=True))
    assert agent.calls == []
    assert len(service.commands("trader-a")) == 1


def test_stop_after_proposal_validation_blocks_its_late_commit(chat_case):
    service, agent, runner, _, _ = chat_case
    agent.release = threading.Event()
    message_id = incoming(chat_case)
    with ThreadPoolExecutor(max_workers=1) as pool:
        processing = pool.submit(runner.process, message_id)
        try:
            assert agent.signed.wait(3)
            assert agent.validations[-1]["ok"] is True
            service.set_bot("trader-a", False)
        finally:
            agent.release.set()
        processing.result(timeout=3)
    assert len(service.commands("trader-a")) == 1
    assert service.sessions("trader-a")[0]["state"] == "STOPPED"


@pytest.mark.parametrize("chat_case", [{"our_side": "SELL", "opening_yield_pct": "2.00", "limit_yield_pct": "2.10"}], indirect=True)
def test_sell_side_with_30bp_peer_gap_can_make_a_valid_authorized_counter(chat_case):
    service, agent, runner, _, _ = chat_case
    agent.peer_side, agent.peer_value, agent.yield_pct = "bid", "2.40", "2.02"
    agent.public = "000100.IB 我方卖出1000万，收益率2.02%，还能帮忙争取吗？"
    runner.process(incoming(chat_case, text="000100.IB 我买2.40%，1000万，实价"))
    assert agent.validations[-1]["ok"] is True
    assert len(service.commands("trader-a")) == 2


def test_reaching_the_yield_target_hands_off_without_accepting_a_trade(chat_case):
    service, agent, runner, _, _ = chat_case
    agent.action, agent.public, agent.observe, agent.peer_value = "WAIT", None, True, "2.40"
    runner.process(incoming(chat_case, text="000100.IB 我卖2.40%，1000万，实价"))
    assert len(service.commands("trader-a")) == 1
    assert service.sessions("trader-a")[0]["state"] == "HANDOFF"


def test_counterparty_explicit_buy_direction_cannot_be_relabelled_as_an_ofr(chat_case):
    service, _, runner, _, _ = chat_case
    runner.process(incoming(chat_case, text="000100.IB 我买2.20%，1000万，实价"))
    assert len(service.commands("trader-a")) == 1


def test_counterparty_request_over_the_authorized_volume_requires_handoff(chat_case):
    service, _, runner, _, _ = chat_case
    runner.process(incoming(chat_case, text="000100.IB 我卖2.20%，必须2000万一起做，实价"))
    assert len(service.commands("trader-a")) == 1


def test_new_settlement_condition_cannot_be_ignored_by_a_price_only_proposal(chat_case):
    service, _, runner, _, _ = chat_case
    runner.process(incoming(chat_case, text="000100.IB 我卖2.20%，1000万，实价，但必须改为明天交割"))
    assert len(service.commands("trader-a")) == 1


def test_unresolved_clarification_quota_survives_social_turn_and_runner_restart(chat_case):
    from server.chat_runtime import ChatRunner

    service, agent, runner, transport, url = chat_case
    agent.action = "CLARIFY_BOND"
    agent.public = "请确认您指的是哪只债券？"
    runner.process(incoming(chat_case, text="另一只那个呢？"))
    assert len(service.commands("trader-a")) == 2
    assert service.conversations("trader-a")[0]["ambiguity"]["count"] == 1
    Dispatcher(service, transport, clock=lambda: NOW).run_once()
    agent.action, agent.public = "SOCIAL_REPLY", "不客气，您方便时回复即可。"
    runner.process(incoming(chat_case, number=2, text="谢谢"))
    assert len(service.commands("trader-a")) == 3
    Dispatcher(service, transport, clock=lambda: NOW).run_once()
    restarted = InquiryService(Repository(url), clock=lambda: NOW)
    new_runner = ChatRunner(restarted, agent, BondTools(), clock=lambda: NOW)
    agent.runner, agent.action, agent.public = new_runner, "CLARIFY_BOND", "请确认您指的是哪只债券？"
    message_id = incoming((restarted,), number=3, text="就是那个，再想想")
    new_runner.process(message_id)
    clarifications = [command for command in restarted.commands("trader-a") if command["text"] == agent.public]
    assert len(clarifications) == 1, "Unrelated social messages do not resolve the ambiguity or replenish its quota"


def test_changing_public_text_after_validation_invalidates_the_signed_proposal(chat_case):
    service, agent, runner, _, _ = chat_case
    agent.after_validation = lambda value: value.update(public_message="修改后的未签证正文")
    runner.process(incoming(chat_case))
    assert agent.validations[-1]["ok"] is True
    assert len(service.commands("trader-a")) == 1


def test_history_budget_is_durable_across_a_new_runner_instance(chat_case):
    from server.chat_runtime import ChatRunner

    service, agent, runner, _, url = chat_case
    old = service.ingest("trader-a", {"event_id": "history-event", "account_uid": "910001", "peer_id": "synthetic-peer",
                         "kind": "private", "source_seq": 1, "text": "历史唯一标记", "occurred_at": NOW - 3600, "historical": True})
    history = []

    def request_history(ref, context):
        result = runner.history(ref, {"message_refs": [old["message_id"]], "reason": "核对旧引用"})
        history.extend(result["messages"])
        restarted = ChatRunner(InquiryService(Repository(url), clock=lambda: NOW), agent, BondTools(), clock=lambda: NOW)
        with pytest.raises((ValueError, PermissionError)):
            restarted.history(ref, {"message_refs": [old["message_id"]], "reason": "不能因重启恢复额度"})
        assert restarted.context(ref)["history_used"] == 1

    agent.before_validation = request_history
    runner.process(incoming(chat_case, number=2))
    assert [message["text"] for message in history] == ["历史唯一标记"]
    assert len(service.commands("trader-a")) == 2


@pytest.mark.parametrize("field", ["control_version", "history_count", "clarification_count", "limit_yield_pct"])
def test_memory_patch_cannot_reset_permissions_prices_or_persistent_budgets(chat_case, field):
    service, agent, runner, _, _ = chat_case
    agent.memory_patch = lambda context: {"expected_revision": context["memory_revision"], "summary": "摘要", "evidence_refs": [context["source_message_id"]], field: 0}
    runner.process(incoming(chat_case))
    assert len(service.commands("trader-a")) == 1
    assert field not in service.conversations("trader-a")[0]["memory"]


def test_evidence_backed_summary_is_saved_without_modifying_price_authorization(chat_case):
    service, agent, runner, _, _ = chat_case
    before = service.sessions("trader-a")[0]["policy"]
    agent.action, agent.public = "SOCIAL_REPLY", "好的，小王。"
    agent.memory_patch = lambda context: {"expected_revision": context["memory_revision"], "summary": "对方明确希望称呼小王", "evidence_refs": [context["source_message_id"]]}
    message_id = incoming(chat_case, text="下次叫我小王就好")
    runner.process(message_id)
    memory = service.conversations("trader-a")[0]["memory"]
    assert memory["summary"] == "对方明确希望称呼小王"
    assert message_id in memory["evidence_refs"]
    assert service.sessions("trader-a")[0]["policy"] == before


def test_chat_inputs_carry_schema_and_current_control_epoch(chat_case):
    _, agent, runner, _, _ = chat_case
    runner.process(incoming(chat_case))
    inputs = agent.calls[0]["inputs"]
    assert inputs["schema_version"] == "1"
    assert inputs["context_epoch"] == "1"
    assert inputs["turn_ref"]


def test_tool_budget_is_six_total_across_history_and_a_new_runner_instance(chat_case):
    from server.chat_runtime import ChatRunner

    service, agent, runner, _, url = chat_case
    results = []
    arguments = {"schema_version": "1", "operation": "yield_gap", "inputs": {
        "our_side": "BUY", "peer_side": "ofr", "limit_yield_pct": "2.40", "peer_yield_pct": "2.20", "same_basis": True,
    }}

    def use_tools(ref, context):
        budget = context["tool_budget_ref"]
        for _ in range(3):
            results.append(runner.invoke_tool(budget, "bond_calc", arguments))
        runner.history(ref, {"reason": "受控补查"})
        restarted = ChatRunner(InquiryService(Repository(url), clock=lambda: NOW), agent, BondTools(), clock=lambda: NOW)
        for _ in range(3):
            results.append(restarted.invoke_tool(budget, "bond_calc", arguments))
        with pytest.raises((ValueError, PermissionError)):
            restarted.invoke_tool(budget, "bond_calc", arguments)

    agent.before_validation = use_tools
    runner.process(incoming(chat_case))
    assert len(results) == 6
    assert all(result["status"] == "ok" for result in results)
    assert len(service.commands("trader-a")) == 2


def test_memory_cannot_cite_unknown_messages_alongside_a_valid_reference(chat_case):
    service, agent, runner, _, _ = chat_case
    agent.memory_patch = lambda context: {"expected_revision": context["memory_revision"], "summary": "伪引用摘要",
                                         "evidence_refs": [context["source_message_id"], "foreign-or-forged-message"]}
    runner.process(incoming(chat_case))
    assert len(service.commands("trader-a")) == 1
    assert "foreign-or-forged-message" not in str(service.conversations("trader-a")[0]["memory"])


def test_takeover_during_a_validated_model_turn_prevents_late_reply(chat_case):
    service, agent, runner, _, _ = chat_case
    agent.release = threading.Event()
    message_id = incoming(chat_case)
    with ThreadPoolExecutor(max_workers=1) as pool:
        processing = pool.submit(runner.process, message_id)
        try:
            assert agent.signed.wait(3)
            assert agent.validations[-1]["ok"] is True
            service.takeover("trader-a", service.conversations("trader-a")[0]["conversation_id"])
        finally:
            agent.release.set()
        processing.result(timeout=3)
    assert len(service.commands("trader-a")) == 1
    assert service.conversations("trader-a")[0]["mode"] == "HUMAN"


def test_resume_ignores_messages_received_during_human_control_but_allows_new_social_reply(chat_case):
    service, agent, runner, _, _ = chat_case
    conversation = service.conversations("trader-a")[0]["conversation_id"]
    service.takeover("trader-a", conversation)
    old = incoming(chat_case, text="之前收到的旧消息")
    service.resume("trader-a", conversation)
    runner.process(old)
    assert agent.calls == []
    agent.action, agent.public = "SOCIAL_REPLY", "不客气。"
    runner.process(incoming(chat_case, number=2, text="谢谢"))
    assert len(agent.calls) == 1
    assert len(service.commands("trader-a")) == 2


def test_retraction_after_validation_invalidates_the_pending_model_result(chat_case):
    service, agent, runner, _, _ = chat_case
    agent.release = threading.Event()
    message_id = incoming(chat_case)
    with ThreadPoolExecutor(max_workers=1) as pool:
        processing = pool.submit(runner.process, message_id)
        try:
            assert agent.signed.wait(3)
            service.ingest("trader-a", {"event_id": "retraction", "account_uid": "910001", "peer_id": "synthetic-peer",
                           "kind": "retract", "target_event_id": "event-1", "source_seq": 2,
                           "text": "撤回原报价", "occurred_at": NOW + 2})
        finally:
            agent.release.set()
        processing.result(timeout=3)
    assert len(service.commands("trader-a")) == 1
    assert service.conversations("trader-a")[0]["mode"] == "HUMAN"


@pytest.mark.parametrize("truncated", [False, True])
def test_real_sse_client_and_runner_commit_only_the_complete_validated_turn(chat_case, truncated):
    service, agent, runner, _, _ = chat_case

    def opener(request, **_kwargs):
        body = json.loads(request.data)
        # The fixed validation node runs before Dify's final Answer is streamed.
        checked = agent.chat(**body)
        answer = json.dumps(checked.proposal, ensure_ascii=False)
        if truncated:
            answer = answer[:-10]
        identifiers = {"conversation_id": checked.conversation_id, "message_id": checked.message_id,
                       "task_id": checked.task_id, "workflow_run_id": checked.workflow_run_id}
        frames = [
            {"event": "message", **identifiers, "answer": answer[:80]},
            {"event": "agent_thought", **identifiers, "thought": "PRIVATE-THOUGHT"},
            {"event": "message", **identifiers, "answer": answer[80:]},
            {"event": "message_end", **identifiers},
            {"event": "workflow_finished", **identifiers, "data": {"status": "succeeded", "id": checked.workflow_run_id}},
        ]
        response = io.BytesIO(b"".join(b"data: " + json.dumps(frame, ensure_ascii=False).encode() + b"\n\n" for frame in frames))
        response.status = 200
        response.headers = {"Content-Type": "text/event-stream"}
        return response

    runner.client = DifyChatClient("https://synthetic-dify.invalid", "synthetic-app-key", opener=opener)
    runner.process(incoming(chat_case))
    assert agent.validations[-1]["ok"] is True
    commands = service.commands("trader-a")
    assert len(commands) == (1 if truncated else 2)
    assert all("PRIVATE-THOUGHT" not in command["text"] for command in commands)


def test_delayed_message_from_before_authorization_never_starts_a_new_counter(chat_case):
    service, agent, runner, _, _ = chat_case
    event = service.ingest("trader-a", {"event_id": "delayed-old", "account_uid": "910001", "peer_id": "synthetic-peer",
                          "kind": "private", "source_seq": 50, "text": "000100.IB 我卖2.20%，1000万，实价",
                          "occurred_at": NOW - 86400, "historical": False})
    runner.process(event["message_id"])
    assert agent.calls == []
    assert len(service.commands("trader-a")) == 1


def test_first_message_on_a_new_day_cannot_use_yesterdays_authorization_without_confirmation(chat_case):
    from server.chat_runtime import ChatRunner

    service, agent, _, _, url = chat_case
    next_day = InquiryService(Repository(url), clock=lambda: NOW + 86400)
    runner = ChatRunner(next_day, agent, BondTools(), clock=lambda: NOW + 86400)
    agent.runner = runner
    event = next_day.ingest("trader-a", {"event_id": "new-day", "account_uid": "910001", "peer_id": "synthetic-peer",
                            "kind": "private", "source_seq": 50, "text": "000100.IB 我卖2.20%，1000万，实价",
                            "occurred_at": NOW + 86400, "historical": False})
    runner.process(event["message_id"])
    assert len(service.commands("trader-a")) == 1


@pytest.mark.parametrize("repeat_history_request", [False, True])
def test_history_wait_branch_allows_exactly_one_second_agent_call(chat_case, repeat_history_request):
    service, signed_agent, runner, _, _ = chat_case

    class HistoryAgent:
        def __init__(self):
            self.calls = 0

        def chat(self, **kwargs):
            self.calls += 1
            if self.calls == 2 and not repeat_history_request:
                return signed_agent.chat(**kwargs)
            context = runner.context(kwargs["inputs"]["turn_ref"])
            value = {"schema_version": "1", "turn_id": context["turn_id"], "conversation_epoch": context["conversation_epoch"],
                     "source_message_id": context["source_message_id"], "observations": [],
                     "proposed_actions": [{"type": "WAIT"}], "public_message": None, "trader_view": {}, "memory_patch": None,
                     "history_request": {"message_refs": [], "reason": "核对前文"}, "validation_ref": None}
            return ChatResult(value, "synthetic-history-conversation", "synthetic-dify-message", "synthetic-task", "synthetic-run")

    client = HistoryAgent()
    runner.client = client
    runner.process(incoming(chat_case))
    assert client.calls == 2
    assert len(service.commands("trader-a")) == (1 if repeat_history_request else 2)


def test_private_agent_capabilities_and_public_tool_credentials_are_separated_at_http_boundary(chat_case):
    from fastapi.testclient import TestClient
    from server.legacy_app import create_app
    from server.auth import AuthConfig

    service, agent, runner, _, url = chat_case
    agent.release = threading.Event()
    private_token, public_token = "private-service-" + "p" * 40, "public-tools-" + "t" * 40
    app = create_app(database_url=url, enable_background=False,
                     auth_config=AuthConfig(sso_secret="sso", session_secret="synthetic-session-secret-over-32", clock=lambda: NOW),
                     agent_service_token=private_token)
    with ThreadPoolExecutor(max_workers=1) as pool:
        processing = pool.submit(runner.process, incoming(chat_case))
        try:
            assert agent.signed.wait(3)
            ref = agent.calls[0]["inputs"]["turn_ref"]
            with TestClient(app) as client:
                context = client.post("/internal/agent/context", headers={"Authorization": "Bearer " + private_token}, json={"turn_ref": ref})
                assert context.status_code == 200
                budget = context.json()["context"]["decision_context"]["tool_budget_ref"]
                assert client.post("/internal/agent/context", headers={"Authorization": "Bearer " + public_token}, json={"turn_ref": ref}).status_code == 403
                assert client.post("/internal/agent/context", headers={"Authorization": "Bearer " + private_token}, json={"turn_ref": "forged-capability"}).status_code == 403
                body = {"tool_budget_ref": budget, "arguments_json": json.dumps({"schema_version": "1", "operation": "yield_gap", "inputs": {
                    "our_side": "BUY", "peer_side": "ofr", "limit_yield_pct": "2.40", "peer_yield_pct": "2.20", "same_basis": True,
                }})}
                assert client.post("/api/agent/tools/bond_calc", json=body).status_code == 200
                public = client.post("/api/agent/tools/bond_calc", json=body)
                assert public.status_code == 200
                assert public.json()["status"] == "ok"
                assert ref not in public.text and "910001" not in public.text
        finally:
            agent.release.set()
        processing.result(timeout=3)
    assert len(service.commands("trader-a")) == 2


def test_counterparty_explicit_refusal_to_continue_blocks_price_reply(chat_case):
    service, _, runner, _, _ = chat_case
    runner.process(incoming(chat_case, text="000100.IB 我卖2.20%，1000万，但不谈了，请不要再问价"))
    assert len(service.commands("trader-a")) == 1


def test_bp_suffix_attached_to_number_cannot_be_relabelled_as_absolute_yield(chat_case):
    service, _, runner, _, _ = chat_case
    runner.process(incoming(chat_case, text="000100.IB 估值+2.20bp，1000万，实价"))
    assert len(service.commands("trader-a")) == 1


@pytest.mark.parametrize("public", [
    "000100.IB 我方卖出1000万，收益率2.48%，还能争取吗？",
    "000100.IB 我方买入1000亿，收益率2.48%，还能争取吗？",
])
def test_public_direction_and_volume_units_must_match_the_structured_authorization(chat_case, public):
    service, agent, runner, _, _ = chat_case
    agent.public = public
    runner.process(incoming(chat_case))
    assert len(service.commands("trader-a")) == 1


def test_ask_quote_cannot_bypass_public_direction_or_quantity_unit_checks(chat_case):
    service, agent, runner, _, _ = chat_case
    agent.action = "ASK_QUOTE"
    agent.public = "000100.IB 我方卖出1000亿，请问可以吗？"
    runner.process(incoming(chat_case))
    assert len(service.commands("trader-a")) == 1


@pytest.mark.parametrize("source", ["000100.IB 净价100元，1000万，实价", "000100.IB 我卖2.42%，1000万，实价"])
def test_counter_cannot_omit_observations_to_bypass_price_basis_or_reached_target(chat_case, source):
    service, agent, runner, _, _ = chat_case
    agent.omit_observations = True
    runner.process(incoming(chat_case, text=source))
    assert len(service.commands("trader-a")) == 1


def test_bare_code_of_another_bond_is_not_assigned_to_the_only_active_session(chat_case):
    service, _, runner, _, _ = chat_case
    runner.process(incoming(chat_case, text="240210 我卖2.20%，1000万，实价"))
    assert len(service.commands("trader-a")) == 1


def test_unrelated_native_manual_outbound_message_takes_over_the_conversation(chat_case):
    service, agent, runner, _, _ = chat_case
    service.ingest("trader-a", {"event_id": "external-manual-event", "account_uid": "910001", "peer_id": "synthetic-peer",
                               "kind": "private", "direction": "out", "source_seq": 1, "text": "我来人工继续沟通",
                               "occurred_at": NOW + 1, "historical": False})
    assert service.conversations("trader-a")[0]["mode"] == "HUMAN"
    runner.process(incoming(chat_case, number=2))
    assert agent.calls == []


def test_implicit_sender_side_in_ask_quote_cannot_reverse_the_buy_authorization(chat_case):
    service, agent, runner, _, _ = chat_case
    agent.action = "ASK_QUOTE"
    agent.public = "000100.IB 卖出1000万，能帮忙问问吗？"
    runner.process(incoming(chat_case))
    assert len(service.commands("trader-a")) == 1


def test_followup_counter_can_use_the_persisted_valid_peer_quote_without_fabricating_a_new_observation(chat_case):
    service, agent, runner, _, _ = chat_case
    agent.action, agent.public, agent.observe = "WAIT", None, True
    original = incoming(chat_case)
    runner.process(original)
    assert service.sessions("trader-a")[0]["peer_quote"]["yield_pct"] == "2.20"
    assert len(service.commands("trader-a")) == 1
    agent.action, agent.observe, agent.omit_observations = "COUNTER", False, True
    agent.public = "000100.IB 我方买入1000万，收益率2.48%，还能争取吗？"
    runner.process(incoming(chat_case, number=2, text="再让一点"))
    commands = service.commands("trader-a")
    assert len(commands) == 2, "A valid persisted peer quote remains usable for a referential follow-up"
    assert [command for command in commands if command["status"] == "queued"][0]["text"] == agent.public


def test_public_code_with_another_market_suffix_cannot_hide_behind_the_authorized_name(chat_case):
    service, agent, runner, _, _ = chat_case
    agent.public = "合成债 000100.SH 我方买入1000万，收益率2.48%，还能争取吗？"
    runner.process(incoming(chat_case))
    assert len(service.commands("trader-a")) == 1


@pytest.mark.parametrize("quoted_code,expected_commands", [("000100.IB", 3), ("000200.IB", 2)])
def test_late_quote_is_checked_against_its_selected_bonds_authorization_time(chat_case, quoted_code, expected_commands):
    from server.chat_runtime import ChatRunner

    _, agent, _, transport, url = chat_case
    service = InquiryService(Repository(url), clock=lambda: NOW + 100)
    draft = service.preview("trader-a", "second-bond", [{"code": "000200.IB", "name": "第二只合成债", "peer_ids": ["synthetic-peer"],
                           "our_side": "BUY", "volume_wan": "1000", "opening_yield_pct": "2.50", "limit_yield_pct": "2.40", "step_bp": "2"}])
    service.confirm("trader-a", draft["batch_id"], draft["version"], "confirm-second")
    Dispatcher(service, transport, clock=lambda: NOW + 100).run_once()
    assert len(service.commands("trader-a")) == 2
    runner = ChatRunner(service, agent, BondTools(), clock=lambda: NOW + 101)
    agent.runner, agent.target_bond = runner, quoted_code
    agent.public = f"{quoted_code} 我方买入1000万，收益率2.48%，还能争取吗？"
    message = service.ingest("trader-a", {"event_id": "late-between-authorizations", "account_uid": "910001", "peer_id": "synthetic-peer",
                            "kind": "message", "source_seq": 1, "text": f"{quoted_code} 我卖2.20%，1000万，实价",
                            "occurred_at": NOW + 50, "historical": False})
    runner.process(message["message_id"])
    assert len(service.commands("trader-a")) == expected_commands
