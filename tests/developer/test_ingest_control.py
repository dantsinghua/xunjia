import pytest
from server.repository import Repository
from server.business import InquiryService


def test_ingest_dedup_scope_takeover_and_resume_watermark(tmp_path):
    svc=InquiryService(Repository('sqlite:///'+str(tmp_path/'x.db')),clock=lambda:1800000000)
    svc.repo.bind_account('a','a1','wb')
    data={'event_id':'m1','account_uid':'a1','peer_id':'p1','kind':'message','source_seq':1,'text':'您好','occurred_at':1800000000,'historical':False}
    first=svc.ingest('a',data)
    assert svc.ingest('a',data)['message_id']==first['message_id']
    with pytest.raises(PermissionError):svc.ingest('a',{**data,'event_id':'m2','account_uid':'b1'})
    conv=svc.conversations('a')[0]
    assert conv['mode']=='HUMAN' and not conv['social_authorized']
    svc.takeover('a',conv['conversation_id'])
    svc.ingest('a',{**data,'event_id':'m2','source_seq':2})
    svc.set_bot('a',True)
    svc.resume('a',conv['conversation_id'])
    assert svc.conversations('a')[0]['resume_seq']==2
    assert first['processing']=='ignored'
