"""Unrelated cancelled command history must not erase a current settlement question."""
import pytest
from test_settlement_followup_revision import cash_desk,ask,answer,question_in,SETTLEMENT
from test_reference_window_policy import OWNER
@pytest.mark.parametrize('history_count',[99,200])
@pytest.mark.parametrize('version',['0.15.3','1.17.1'])
def test_settlement_survives_cancelled_history(tmp_path,history_count,version):
    c=cash_desk(tmp_path)
    with c.store.write(OWNER) as tx:
        basis=c.service.commands_core._basis(tx,OWNER,c.member,False)
        for i in range(history_count):
            cid=c.service.commands_core._insert(tx,OWNER,c.member,basis,'old-'+str(i),
                {'kind':'CLARIFY','text':'synthetic cancelled old message'},c.now+120)
            row=c.service.commands_core._command(tx,OWNER,cid)
            c.service.commands_core._cancel_unsubmitted(tx,row,'TEST_CANCELLED')
    current=ask(c)
    result,_,snapshot=answer(c,version=version)
    assert question_in(snapshot,c)['command_id']==current
    assert result['accepted'] and not result['rejected'],result
    assert c.quote()['price']['settlement_date']==SETTLEMENT