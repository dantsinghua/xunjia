import json,hashlib,time,base64
import pytest
from mock_chain import Chain,AUTH,AUTH_B
from path_gateway_http import MockPathGateway
from relay.file_gateway import FileGatewayClient,FileGatewayError,GatewayAuth
from relay.gateway_paths import DIRECTORY_PATH,ORGANIZATION_PATH,account_paths

@pytest.fixture
def chain(tmp_path,monkeypatch):
    c=Chain(tmp_path,monkeypatch)
    try:yield c
    finally:c.close()

def test_mock_contract_auth_fifo_names_and_institution_isolation(tmp_path):
    now=time.time()
    with MockPathGateway(tmp_path,[AUTH,AUTH_B],lambda:now,max_bytes=100) as g:
        a=FileGatewayClient(g.base,AUTH,path='same',allow_http=True,clock=lambda:now)
        b=FileGatewayClient(g.base,AUTH_B,path='same',allow_http=True,clock=lambda:now)
        bad=FileGatewayClient(g.base,GatewayAuth('bad',AUTH.sm4_key),path='same',allow_http=True,clock=lambda:now)
        late=FileGatewayClient(g.base,AUTH,path='same',allow_http=True,clock=lambda:now-61)
        for rejected in (bad,late):
            with pytest.raises(FileGatewayError) as e:rejected.pending()
            assert e.value.status==401
        first=a.upload('same.bin',b'one');second=a.upload('same.bin',b'two')
        assert first['file_key']!=second['file_key'] and a.pending()==2 and b.pending()==0
        assert a.download().content==b'one' and a.download().content==b'two' and a.download() is None
        with pytest.raises(FileGatewayError) as e:a.upload('big.bin',b'x'*101)
        assert e.value.status==413 and a.pending()==0

def test_real_directory_two_selected_accounts_and_shared_organization(chain):
    c=chain;c.select();c.select('owner-b','10002')
    for owner,uid in [('owner-a','10001'),('owner-b','10002')]:
        c.login(owner)
        r=c.api.get('/api/im/catalog');assert r.status_code==200,r.text
        assert r.json()['data']['selected']['account_uid']==uid
        r=c.api.get('/api/im/contacts');assert r.status_code==200,r.text
        book=r.json()['data'];assert book['contacts'][0]['uin']=='peer-'+uid and book['org']['members'][0]['uin']=='org-peer'
    assert any(r['op']=='download' and r['path']==DIRECTORY_PATH for r in c.gateway.calls)
    assert c.bus.calls==[]

def test_command_and_native_receipt_end_to_end_no_repeat(chain):
    c=chain;c.select();c.confirm();c.pump(3)
    commands=c.app.state.inquiry.commands('owner-a')
    assert len(commands)==1 and commands[0]['state']=='SENT',commands
    assert len(c.bus.calls)==1 and c.bus.calls[0].account_id=='qd01'
    c.pump(3);assert len(c.bus.calls)==1

def test_live_message_crosses_native_host_http_and_real_business_store(chain):
    c=chain;c.select();h=c.collector.hosts['qd01','10001']
    event={'event':'message','channel':'qidian','account_id':'qd01','event_id':'native-event-1','source_seq':1,
        'payload':{'session':{'id':'qd01:peer-10001','native_id':'peer-10001','kind':'private'},'ext_msg_id':'qd-native-1','dir':'in','text':'synthetic incoming','type':'text','ts_ms':int(c.clock()*1000)}}
    h.observe(**event);c.pump(2)
    messages=c.messages();assert len(messages)==1,messages
    h.observe(**event);c.pump(2);assert len(c.messages())==1
    c.select('owner-b','10002');assert len(c.messages())==1

def test_sixty_second_offline_blocks_send_and_recovers(chain):
    c=chain;c.select();c.now[0]+=61
    r=c.api.post('/api/im/send',json={'uin':'peer-10001','text':'synthetic','request_id':'off'})
    assert r.status_code==403,r.text
    assert c.bus.calls==[]
    c.advertise();assert c.runtime.online('wb-a','10001')
    assert c.api.get('/api/im/history').status_code==200

def test_reset_control_handshake_via_same_http_carrier(chain):
    c=chain;c.select();c.confirm();c.pump(3)
    r=c.api.post('/api/im/session-reset',json={'request_id':'reset-1','expected_binding_version':1,'confirmed':True})
    assert r.status_code==200,r.text
    for _ in range(8):
        c.pump();r=c.api.get('/api/im/session-reset')
        if r.json()['data']['state']=='complete':break
        c.now[0]+=6
    assert r.json()['data']['state']=='complete',r.text
    assert len(c.bus.calls)==1
def test_download_consumed_then_connection_lost_recovers_once(chain):
    c=chain;c.select();h=c.collector.hosts['qd01','10001'];paths=account_paths('wb-a','10001')
    event={'event':'message','channel':'qidian','account_id':'qd01','event_id':'loss-event','source_seq':4,
        'payload':{'session_id':'qd01:peer-10001','ext_msg_id':'lost-native','dir':'in','text':'synthetic lost transfer','type':'text','ts_ms':int(c.clock()*1000)}}
    h.observe(**event)
    import asyncio
    asyncio.run_coroutine_threadsafe(h.run_once(),c.loop).result(10)
    c.gateway.fault('download',paths['uplink_path'],'drop_after_commit')
    c.app.state.inbox.poll_once();assert c.messages()==[]
    c.now[0]+=31;c.advertise();c.pump(4)
    assert len(c.messages())==1
    c.pump(3);assert len(c.messages())==1 and not c.bus.calls
    adapter=c.app.state.transport.adapters['owner-a','10001','oss']
    assert any(r['state']=='UNKNOWN' for r in adapter.delivery_diagnostics()['download_issues'])

def test_upload_response_lost_does_not_execute_command_twice(chain):
    c=chain;c.select();c.confirm()
    c.gateway.fault('upload',account_paths('wb-a','10001')['downlink_path'],'drop_after_commit')
    c.pump(3)
    assert c.app.state.inquiry.commands('owner-a')[0]['state']=='SENT'
    assert len(c.bus.calls)==1
    c.now[0]+=6;c.pump(3);assert len(c.bus.calls)==1

def test_native_unknown_survives_service_restart_without_resend(chain):
    c=chain;c.select();c.bus.mode='unknown';c.confirm();c.pump(3)
    before=c.app.state.inquiry.commands('owner-a')[0]
    assert before['state']=='UNKNOWN' and len(c.bus.calls)==1
    c.restart_service();c.login();c.now[0]+=6;c.advertise();c.pump(3)
    after=c.app.state.inquiry.commands('owner-a')[0]
    assert after['state']=='UNKNOWN' and after['command_id']==before['command_id']
    assert len(c.bus.calls)==1

def test_cancel_beats_queued_native_send(chain):
    c=chain;c.select();c.confirm();c.app.state.dispatcher.run_once()
    r=c.api.post('/api/bot/toggle',json={'status':'\u505c\u6b62'});assert r.status_code==200,r.text
    c.pump(3)
    assert not c.bus.calls
    assert c.app.state.inquiry.commands('owner-a')[0]['state']=='CANCELLED'

def test_forged_file_does_not_block_following_authenticated_message(chain):
    c=chain;c.select();path=account_paths('wb-a','10001')['uplink_path']
    c.client(path).upload('invalid.bin',b'not an authenticated protocol packet')
    h=c.collector.hosts['qd01','10001']
    h.observe(event='message',channel='qidian',account_id='qd01',event_id='valid-after-bad',source_seq=1,
        payload={'session_id':'qd01:peer-10001','ext_msg_id':'valid-1','dir':'in','text':'synthetic valid','type':'text','ts_ms':int(c.clock()*1000)})
    c.pump(3);assert len(c.messages())==1
    adapter=c.app.state.transport.adapters['owner-a','10001','oss']
    with adapter.store.db() as db:
        rows=db.execute('SELECT state FROM fg_download WHERE scope=?',(adapter.store.scope,)).fetchall()
        assert any(r['state']=='QUARANTINED' for r in rows),[r['state'] for r in rows]

def test_contact_refresh_request_roundtrip_does_not_prematurely_succeed(chain):
    c=chain;c.select();c.pump(2)
    c.now[0]+=11;c.advertise()
    r=c.api.get('/api/im/contacts?refresh=1');assert r.status_code==200,r.text
    c.pump(2)
    requests=c.collector.publisher.pending_sync();assert requests and requests[0]['organization']==1
    c.collector.metadata_once();c.now[0]+=11;c.advertise();c.runtime.tick()
    r=c.api.get('/api/im/contacts');assert r.status_code==200,r.text
    assert r.json()['data']['org_state']=='ready'
    assert not c.collector.publisher.pending_sync()

def test_group_message_never_reaches_negotiation_or_native_send(chain):
    c=chain;c.select();h=c.collector.hosts['qd01','10001']
    h.observe(event='message',channel='qidian',account_id='qd01',event_id='group-event',source_seq=1,
        payload={'session':{'id':'qd01:g_9001','native_id':'g_9001','kind':'group'},'ext_msg_id':'group-1','dir':'in','text':'synthetic group','type':'text','ts_ms':int(c.clock()*1000)})
    c.pump(2);assert c.messages()==[] and c.bus.calls==[]
def test_native_seven_day_history_image_file_and_account_scoped_download(chain):
    import sqlite3
    from qtrade_agent.adapters.qidian.maindb import LocalSqliteMainDb
    from qtrade_agent.adapters.qidian.xor import xor
    from qtrade_agent.ossbox.history_publish import NativeHistoryPublisher
    c=chain;c.select();now=int(c.clock());peer='12345'
    native=c.root/'10001.db';table='mr_friend_'+hashlib.md5(peer.encode()).hexdigest().upper()+'_New'
    png=bytes.fromhex('89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4890000000b49444154789c636000020000050001a5f645400000000049454e44ae426082')
    document=b'synthetic original file\r\n' * 50000
    base='/sdcard/Android/data/com.tencent.qidian/Tencent/Qidian/'
    imagepath=base+'diskcache/Cache_original';filepath=base+'Qidianfile_recv/document.txt'
    def vi(n):
        out=bytearray()
        while n>127:out.append((n&127)|128);n>>=7
        out.append(n);return bytes(out)
    def field(n,value):return vi(n*8+2)+vi(len(value))+value
    pic=field(1,imagepath.encode())+vi(16)+vi(len(png))+field(6,hashlib.md5(png).hexdigest().encode())
    records=[(1,now-604801,-1000,b'old'),(2,now-30,-1000,b'recent'),(3,now-20,-2000,pic),(4,now-10,-2005,b'')]
    with sqlite3.connect(native) as db:
        db.execute(f'CREATE TABLE "{table}"(_id INTEGER PRIMARY KEY,issend INTEGER,time INTEGER,msgtype INTEGER,uniseq INTEGER,senderuin BLOB,msgData BLOB,frienduin BLOB,istroop INTEGER)')
        for n,stamp,typ,raw in records:db.execute(f'INSERT INTO "{table}" VALUES(?,?,?,?,?,?,?,?,?)',(n,0,stamp,typ,100+n,xor(peer.encode()),xor(raw),xor(peer.encode()),0))
        db.execute('CREATE TABLE mr_fileManager(selfUin TEXT,peerUin TEXT,peerType INTEGER,bSend INTEGER,uniseq INTEGER,msgTime INTEGER,fileSize INTEGER,fileName TEXT,strFilePath TEXT,strFileMd5 TEXT,strFileSHA TEXT,TroopUin INTEGER)')
        db.execute('INSERT INTO mr_fileManager VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',('10001',peer,0,0,104,now-10,len(document),'document.txt',filepath,hashlib.md5(document).hexdigest(),hashlib.sha1(document).hexdigest(),0))
    before=hashlib.sha256(native.read_bytes()).hexdigest()
    adapter=c.collector.hosts['qd01','10001'].bridge.adapters['oss']
    h=NativeHistoryPublisher(c.root/'history',LocalSqliteMainDb(str(native)),adapter,clock=c.clock,media_reader=lambda p:{imagepath:png,filepath:document}[p])
    h.tick();c.pump(3)
    r=c.api.get('/api/im/history');assert r.status_code==200,r.text
    items=r.json()['data']['items'];assert len(items)==3,items
    assert {r['type'] for r in items}=={'text','image','file'}
    for message in items:
        for item in message['media']:
            assert item['state']=='ready',item
            payload=png if message['type']=='image' else document
            result=c.api.get('/api/im/media/'+item['sha256'])
            assert result.status_code==200 and result.content==payload
    before_count=len(c.gateway.calls);h.tick();assert len(c.gateway.calls)==before_count
    assert hashlib.sha256(native.read_bytes()).hexdigest()==before
    assert c.messages()==[] and c.bus.calls==[]
    c.select('owner-b','10002');assert c.api.get('/api/im/history').json()['data']['items']==[]
    assert c.api.get('/api/im/media/'+hashlib.sha256(png).hexdigest()).status_code==404
    c.restart_service();c.login();assert len(c.api.get('/api/im/history').json()['data']['items'])==3

def test_gateway_expired_file_republished_from_sender_without_new_business_id(chain):
    c=chain;c.select();h=c.collector.hosts['qd01','10001'];adapter=h.bridge.adapters['oss']
    payload={'event_id':'late-event','peer_id':'peer-10001','direction':'in','text':'synthetic retained message','occurred_at':c.clock(),'ext_msg_id':'late-native','historical':False}
    adapter.publish_event(payload)
    c.now[0]+=86401
    path=account_paths('wb-a','10001')['uplink_path']
    assert c.client(path).pending()==0
    c.advertise();c.pump(4)
    assert len(c.messages())==1,c.messages()
    c.pump(3);assert len(c.messages())==1 and not c.bus.calls
def test_message_to_actual_chatflow_client_reply_command_receipt(chain):
    c=chain;c.select();c.confirm();c.pump(3)
    h=c.collector.hosts['qd01','10001']
    h.observe(event='message',channel='qidian',account_id='qd01',event_id='priced-reply',source_seq=1,
        payload={'session_id':'qd01:peer-10001','ext_msg_id':'priced-native','dir':'in','text':'SYN-MOCK 3.10\u51fa500\u4e07','type':'text','ts_ms':int(c.clock()*1000)})
    c.pump(2)
    outcome=c.app.state.negotiation_engine.run_once()
    c.pump(3)
    assert outcome['analyzed']==1,outcome
    assert [x['stage'] for x in c.model.calls]==['analysis','reply'],c.model.calls
    assert len(c.bus.calls)==2,[x.args for x in c.bus.calls]
    assert c.bus.calls[-1].args['text']=='3.19%\u6536\uff0c500\u4e07'
    assert all(x['state']=='SENT' for x in c.app.state.inquiry.commands('owner-a'))

def test_invalid_model_reply_does_not_erase_valid_parsed_quote(chain):
    c=chain;c.select();c.confirm();c.pump(3);c.model.bad_reply=True
    h=c.collector.hosts['qd01','10001']
    h.observe(event='message',channel='qidian',account_id='qd01',event_id='bad-reply-event',source_seq=1,
        payload={'session_id':'qd01:peer-10001','ext_msg_id':'bad-reply-native','dir':'in','text':'SYN-MOCK 3.10\u51fa500\u4e07','type':'text','ts_ms':int(c.clock()*1000)})
    c.pump(2);c.app.state.negotiation_engine.run_once();c.pump(2)
    member=c.app.state.repository.group_rows('owner-a')[0]['peers'][0]
    quote=c.app.state.repository.quote('owner-a',member['member_id'],'ofr')
    assert quote.price.value=='3.10' and quote.volume_wan=='500'
    assert len(c.bus.calls)==1
def test_two_workspaces_same_peer_dispatch_on_their_own_native_accounts(chain):
    c=chain;c.select();c.select('owner-b','10002')
    c.confirm('owner-a','same-peer','desk-a');c.confirm('owner-b','same-peer','desk-b');c.pump(4)
    assert sorted(x.account_id for x in c.bus.calls)==['qd01','qd02']
    assert all(x.args['session']=='same-peer' for x in c.bus.calls)
    for owner,uid in [('owner-a','10001'),('owner-b','10002')]:
        commands=c.app.state.inquiry.commands(owner)
        assert len(commands)==1 and commands[0]['state']=='SENT'
        c.login(owner);assert c.api.get('/api/auth/me').json()['data']['account_uid']==uid

def test_offline_account_change_before_native_claim_is_not_executed(chain):
    c=chain;c.select();c.confirm();c.app.state.dispatcher.run_once()
    c.native.rows[0]['state']='login_required';c.advertise();c.pump(3)
    assert not c.bus.calls
    assert not c.runtime.online('wb-a','10001')
    assert c.api.post('/api/im/send',json={'uin':'peer-10001','text':'synthetic','request_id':'offline-now'}).status_code==403
    c.native.rows[0]['state']='running';c.advertise();c.pump(2)
    assert c.runtime.online('wb-a','10001') and not c.bus.calls

def test_query_reconciles_unknown_from_native_evidence_without_reexecution(chain):
    c=chain;c.select();c.bus.mode='unknown';c.confirm();c.pump(3)
    cmd=c.bus.calls[0];assert c.app.state.inquiry.commands('owner-a')[0]['state']=='UNKNOWN'
    trace='settled-'+cmd.idempotency_key
    ah=hashlib.sha256(json.dumps(cmd.args,sort_keys=True,ensure_ascii=False,separators=(',',':')).encode()).hexdigest()
    c.native.idem[cmd.account_id,cmd.idempotency_key]={'op':'send_text','args_hash':ah,'status':'DONE','trace_id':trace}
    c.native.results[trace]={'ok':True,'code':'DELIVERED','data':{'ext_msg_id':'native-late-confirmed','confirmed_by':'synthetic-readonly-reconciliation'}}
    # The transport deduplicates queries within thirty-second windows. Advance past it,
    # rather than re-execute the command or alter production query scheduling.
    c.now[0]+=31;c.pump(4)
    assert c.app.state.inquiry.commands('owner-a')[0]['state']=='SENT'
    assert len(c.bus.calls)==1