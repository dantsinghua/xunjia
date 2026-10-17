"""Revalidate a claimed command at the final native action, not at OSS PUT."""
from relay.protocol import Binding
from server.receiver_ledger import ReceiverLedger
import pytest


@pytest.mark.parametrize('barrier', ['new_inbound', 'journaled_inbound', 'cancel', 'expired'])
def test_final_permission_observes_barriers_after_native_claim(tmp_path, barrier):
    clock=[1800000000.0]
    binding=Binding('synthetic-owner','synthetic-account','synthetic-wb',b'0123456789abcdef',b's'*32)
    ledger=ReceiverLedger(tmp_path/'receiver.db',binding,clock=lambda:clock[0])
    wire={'command_id':'synthetic-command','owner_user_id':binding.owner_user_id,
          'account_uid':binding.account_uid,'workbench_id':binding.workbench_id,
          'peer_id':'synthetic-peer','kind':'send_text','text':'synthetic test message',
          'content_hash':'e'*64,'control_version':1,'expires_at':clock[0]+120,'transport':'oss',
          'business_version':'2.2','source_watermark':0,'dispatch_watermark':0}
    assert callable(getattr(ledger,'permission',None)), 'Receiver must expose a final native permission callback'
    assert ledger.permission(wire) is False, 'Unclaimed command has no native permission'
    checks=[]
    def native(command):
        checks.append(ledger.permission(command))
        if barrier in ('new_inbound','journaled_inbound'):
            ledger.begin_inbound('new-event',wire['peer_id'])
            if barrier=='journaled_inbound':ledger.finish_inbound_journal('new-event',wire['peer_id'],1)
        elif barrier=='cancel':ledger.cancel(command)
        else:clock[0]+=121
        checks.append(ledger.permission(command))
        return {'status':'cancelled','evidence':'synthetic guard stopped before native action'}
    result=ledger.execute(wire,native)
    assert checks==[True,False]
    assert result['status']=='cancelled'
    assert ledger.permission(wire) is False
