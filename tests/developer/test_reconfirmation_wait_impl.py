"""Developer checks for actual SENT daily-question proof, synthetic SQLite."""
import json

from server.negotiation.reply_render import reply_requirements
from test_reference_window_policy import OWNER
from test_reconfirmation_wait_after_ack import daily_question, analyzed_message


def test_real_sent_daily_question_projects_waiting_without_confirming_the_quote(tmp_path):
    case,command_id=daily_question(tmp_path)
    analyzed_message(case,'稍等')
    ctx=case.reply_context()
    command=case.service.commands_core.get(OWNER,command_id)
    assert ctx['reconfirmation_waiting'] is True
    assert ctx['reconfirmation_sent_at']==command['sent_at']
    assert ctx['reconfirm_day']=='2026-10-18'
    assert ctx['quotes']['ofr']['confirmed_day'] is None
    assert reply_requirements(ctx)['question'] is None
    assert 'SOCIAL' in reply_requirements(ctx)['allowed_kinds']


def test_current_social_command_gate_rechecks_the_actual_sent_native_proof(tmp_path):
    case,command_id=daily_question(tmp_path)
    analyzed_message(case,'稍等')
    commands=case.service.commands_core
    with case.store.write(OWNER) as tx:
        basis=commands._basis(tx,OWNER,case.member,True)
        assert commands._gate(tx,OWNER,case.member,basis,kind='SOCIAL') is None
        tx.execute('UPDATE xj2_command SET native_id=? WHERE command_id=?',('wrong-native',command_id))
        assert commands._gate(tx,OWNER,case.member,basis,kind='SOCIAL')=='DAILY_RECONFIRMATION_REQUIRED'


def test_sent_label_without_matching_immutable_body_and_scope_does_not_unlock_waiting(tmp_path):
    case,command_id=daily_question(tmp_path)
    analyzed_message(case,'稍等')
    with case.store.write(OWNER) as tx:
        row=tx.execute('SELECT body_json FROM xj2_command WHERE command_id=?',(command_id,)).fetchone()
        body=json.loads(row['body_json']);body['text']='A different synthetic question'
        tx.execute('UPDATE xj2_command SET body_json=? WHERE command_id=?',(json.dumps(body),command_id))
    assert not case.reply_context().get('reconfirmation_waiting')


def test_valid_clarify_daily_question_uses_the_existing_native_reconfirmation_semantics(tmp_path):
    case,command_id=daily_question(tmp_path,first_kind='CLARIFY')
    command=case.service.commands_core.get(OWNER,command_id)
    assert json.loads(command['body_json'])['kind']=='RECONFIRM'
    analyzed_message(case,'稍等')
    ctx=case.reply_context()
    assert ctx['reconfirmation_waiting'] is True
    assert ctx['reconfirm_command_id']==command_id
    assert ctx['reconfirm_day']=='2026-10-18' and ctx['quotes']['ofr']['confirmed_day'] is None
