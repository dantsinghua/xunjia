"""Group history remains a transport concern, never a negotiation thread."""
from types import SimpleNamespace
import pytest
from server.negotiation.runtime import Inbox
from server.negotiation.prices import InvalidFact

class ReachedPrivateContext(Exception):pass

class Store:
    def binding_for(self,owner):return {'account_uid':'account-a','workbench_id':'workbench-a'}
    def preferences(self,owner):raise ReachedPrivateContext()

def env(peer='peer-a',session=None):
    payload={'peer_id':peer}
    if session is not None:payload['session']=session
    return {'owner_user_id':'owner-a','account_uid':'account-a','workbench_id':'workbench-a','channel':'qidian','kind':'event','peer_id':peer,'payload':payload}

def inbox():
    return Inbox(SimpleNamespace(store=Store()),None),SimpleNamespace(owner_user_id='owner-a',account_uid='account-a',workbench_id='workbench-a',channel='qidian')

@pytest.mark.parametrize('peer,session',[('g_123',{'kind':'group'}),('opaque',{'kind':'group'}),('g_123',None),('123@chatroom',None)])
def test_nonprivate_native_event_never_opens_business_thread(peer,session):
    box,binding=inbox();assert box.apply(binding,env(peer,session)) is None

@pytest.mark.parametrize('session',[None,{'kind':'private'}])
def test_private_legacy_and_current_event_keep_existing_business_path(session):
    box,binding=inbox()
    with pytest.raises(ReachedPrivateContext):box.apply(binding,env(session=session))

@pytest.mark.parametrize('session',['group',{'kind':'unknown'},{'kind':None}])
def test_malformed_explicit_session_is_rejected_not_treated_as_private(session):
    box,binding=inbox()
    with pytest.raises(InvalidFact):box.apply(binding,env(session=session))

def test_group_filter_does_not_bypass_owner_isolation():
    box,binding=inbox();message=env('g_123',{'kind':'group'});message['owner_user_id']='other-owner'
    with pytest.raises(PermissionError):box.apply(binding,message)
