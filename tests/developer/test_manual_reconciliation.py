import pytest
from server.repository import Repository
from server.business import InquiryService,digest


def test_conflict_requires_explicit_owner_evidence_and_does_not_replay(tmp_path):
    import json
    svc=InquiryService(Repository('sqlite:///'+str(tmp_path/'x.db')),clock=lambda:1800000000)
    svc.repo.bind_account('a','a1','wb');svc.repo.bind_account('b','b1','wb')
    draft=svc.preview('a','p',[{'code':'SYN001.IB','name':'合成债','peer_ids':['peer'],'our_side':'BUY','volume_wan':'1000','opening_yield_pct':'2.5','limit_yield_pct':'2.4','step_bp':'2'}])
    cmd=svc.confirm('a',draft['batch_id'],1,'c')['commands'][0]['cmd_id']
    svc.receipt(cmd,account_uid='a1',peer_id='peer',status='sent',evidence='original-native')
    svc.receipt(cmd,account_uid='a1',peer_id='peer',status='cancelled',evidence='contradictory-native')
    with svc.repo.db.transaction(write=False) as tx:
        result=json.loads(tx.execute('SELECT result_json FROM xj_command WHERE command_id=?',(cmd,)).fetchone()['result_json'])
    with pytest.raises(PermissionError):svc.reconcile_manually('b',cmd,'sent','native-record-checked',digest(result),True)
    with pytest.raises(ValueError):svc.reconcile_manually('a',cmd,'sent','native-record-checked',digest(result),False)
    svc.reconcile_manually('a',cmd,'sent','native-record-checked',digest(result),True)
    assert svc.commands('a')[0]['status']=='sent'
    assert len(svc.commands('a'))==1
    assert svc.conversations('a')[0]['mode']=='HUMAN'
    svc.receipt(cmd,account_uid='a1',peer_id='peer',status='cancelled',evidence='contradictory-native')
    assert svc.commands('a')[0]['status']=='sent'
    with pytest.raises(ValueError):svc.reconcile_manually('a',cmd,'cancelled','another-record-checked',digest(result),True)
