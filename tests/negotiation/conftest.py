"""Synthetic local test fixture; never uses live STS, OSS, Dify or IM."""
from collections import defaultdict
from pathlib import Path
import pytest
from server.negotiation.commands import Commands
from server.negotiation.prices import LimitPolicy, Price
from server.negotiation.store import Store
class Clock:
    value=1800000000.0
    def __call__(self):return self.value
    def tick(self,seconds=0.1):self.value+=seconds;return self.value
class World:
    owner='synthetic-owner'
    def __init__(self,path):
        self.clock=Clock()
        self.url='sqlite:///'+str(path)
        self.store=Store(self.url,clock=self.clock,initialize=True)
        self.commands=Commands(self.store)
        self.seq=defaultdict(int)
        self.request=0
    def make(self,bond='SYN-A',peer='peer-01',target='2000',limit='3.12',side='BUY',enabled=True,policy=None,valuation=None,expected_valuation_date=None):
        gid=self.store.create_group(self.owner,bond_code=bond,our_side=side,target_volume=target,
            trade_day='2026-10-17',policy=policy or LimitPolicy('ABSOLUTE',limit),
            send_allowed=enabled,valuation=valuation,expected_valuation_date=expected_valuation_date)
        mid,tid=self.store.add_member(self.owner,gid,account_uid='synthetic-account',peer_uid=peer)
        return gid,mid,tid
    def message(self,thread,text='synthetic quote',day='2026-10-17',**kwargs):
        self.seq[thread]+=1
        self.clock.tick()
        return self.store.ingest(self.owner,thread,event_id='evt-'+str(self.seq[thread]),
            source_seq=self.seq[thread],text=text,trade_day=day,occurred_at=self.clock(),**kwargs)
    @staticmethod
    def observation(member,message,changes,side='ofr'):
        return {'member_id':member,'side':side,'changes':changes,
                'evidence':{key:[message] for key in changes}}
    def analyze(self,thread,scopes=None,observations=None):
        job=self.store.claim_analysis(self.owner,thread,force=True)
        assert job
        all_scopes={m['message_id']:[] for m in job['snapshot']['messages']}
        all_scopes.update(scopes or {})
        return self.store.commit_analysis(self.owner,job['analysis_id'],job['lease_token'],
            scopes=all_scopes,observations=observations or [])
    def quote(self,member,thread,price='3.10',volume='500',finality='NEGOTIABLE',day='2026-10-17',side='ofr'):
        msg=self.message(thread,day=day)
        changes={'price':Price('YIELD',price).dump(),'volume_wan':volume,
                 'confirmed_day':day,'validity':'ACTIVE','finality':finality}
        result=self.analyze(thread,{msg:[member]},[self.observation(member,msg,changes,side)])
        assert not result['rejected'],result
        return msg,result
    def queue(self,member,kind='REPLY',basis=None,validator=None,expires=None,request=None):
        self.request+=1
        return self.commands.propose(self.owner,member,
            basis=basis or self.commands.basis(self.owner,member),
            request_id=request or 'request-'+str(self.request),
            proposal={'kind':kind,'text':'synthetic test, never actually sent',
                      'our_price':Price('YIELD','3.14').dump()},
            # Deliberately not a production semantic validator.
            validator=validator or (lambda value:value),
            expires_at=expires or self.clock()+120)
    def member(self,mid):
        with self.store.db.transaction(write=False) as tx:
            return self.store._member(tx,self.owner,mid)
    def receipt(self,cid,status,native_id=None,stamp=None):
        row=self.commands.get(self.owner,cid)
        with self.store.db.transaction(write=False) as tx:
            thread=self.store._thread(tx,self.owner,row['thread_id'])
        return self.commands.receipt(self.owner,cid,account_uid=thread['account_uid'],peer_uid=thread['peer_uid'],
            content_hash=row['content_hash'],status=status,evidence='synthetic-'+status,
            native_id=native_id,occurred_at=stamp)
@pytest.fixture
def world(tmp_path):
    return World(tmp_path/'v22.sqlite')
