"""QTrade V1.0.1 byte-wire tests, config contracts and durable replay checks."""
import asyncio
from copy import deepcopy
import json
from pathlib import Path
import time
import pytest

from qt_fix_mock import wire,header,quote,MockQTrade,settings,NOW
from collector.qtrade_fix import Framer,Session,FixProtocolError
from server.qt_control import QtControl
from server import qt_credentials as creds
from server.qt_quotes import QtQuoteBook


async def wait_for(predicate,timeout=5):
    until=time.monotonic()+timeout
    while not predicate():
        if time.monotonic()>until:raise AssertionError('SYNTHETIC_WAIT_TIMEOUT')
        await asyncio.sleep(.01)


@pytest.mark.parametrize('encoding',['utf-8','gb18030'])
def test_wire_bytes_fragmentation_coalescing_and_chinese(encoding):
    raw=wire(header('S',2)+list(quote().items()),encoding)
    parser=Framer(encoding);out=[]
    for byte in raw:out+=parser.feed(bytes([byte]))
    assert len(out)==1 and out[0]['55']=='合成债券' and not parser.buffer
    result=parser.feed(raw+wire(header('0',3),encoding))
    assert [r['35'] for r in result]==['S','0']
    # BodyLength counts bytes, not decoded characters.
    assert int(out[0]['9'])>len(raw.decode(encoding).split('9=',1)[1].split('\x01',1)[1][:-7])


@pytest.mark.parametrize('change',['checksum','header','duplicate','length','order','control','oversize'])
def test_malformed_wire_fails_closed(change):
    raw=wire(header('0',2))
    if change=='checksum':raw=raw[:-4]+b'999\x01'
    elif change=='header':raw=raw.replace(b'FIX.4.4',b'FIX.4.2')
    elif change=='duplicate':raw=wire(header('0',2)+[('34','2')])
    elif change=='length':raw=raw.replace(b'9=',b'9=0',1)
    elif change=='order':raw=wire([('49','QTRADE'),('35','0')]+header('0',2)[2:])
    elif change=='control':raw=wire(header('0',2)+[('58','bad\nvalue')])
    else:raw=b'8=FIX.4.4\x019=999999\x01'
    with pytest.raises(FixProtocolError):Framer().feed(raw)


@pytest.mark.parametrize('kind',['D','F','G','R','S','U29'])
def test_session_never_emits_business_instructions(kind):
    async def run():
        s=Session(settings(1),receive=lambda _:None,update=lambda **_:None)
        with pytest.raises(FixProtocolError,match='NO_BUSINESS_SEND'):await s.send(kind)
    asyncio.run(run())


def test_fix_replay_deduplicates_across_reset_sequences_and_processes(tmp_path):
    def make():return QtQuoteBook(tmp_path/'qt.sqlite',source_id='qt-test',account_owners={'qt-alice':'alice'},clock=lambda:NOW)
    b=make();original={'35':'S',**quote()}
    assert b.ingest_fix(original) is True
    assert make().ingest_fix(original) is False
    revised={**original,'18045':'H','60':'20261028-10:00:01','132':'1.99'}
    assert make().ingest_fix(revised) is True
    with b.db() as db:
        assert [r[0] for r in db.execute('SELECT source_seq FROM qt_event ORDER BY source_seq')]==[1,2]
    assert b.rows('alice')[0]['ob']=='1.99'
    assert b.current_fix_keys(original)==set()


def test_shared_config_secret_retention_cas_and_no_owner_takeover(tmp_path):
    async def run():
        c=QtControl(tmp_path/'config.json',tmp_path/'state',clock=lambda:NOW)
        body={k:v for k,v in settings(45001).items() if k in creds.EDITABLE}
        body.update(expected_revision='new',qt_account_ids=['qt-alice'])
        a=await c.save('alice',body);raw=c.path.read_bytes()
        assert a['username']=='synthetic-login' and a['qt_account_ids']==['qt-alice']
        assert 'synthetic-password' not in json.dumps(c.status('bob'))
        b=await c.save('bob',{'expected_revision':a['revision'],'password':'','qt_account_ids':['qt-bob']})
        assert b['qt_account_ids']==['qt-bob'] and b['mapped_account_count']==2
        assert creds.load(c.path)[0]['password']=='synthetic-password'
        for patch in ({'qt_account_ids':['qt-alice']},{'account_owners':{'qt-alice':'bob'}},{'owner_id':'alice'},
                      {'enabled':True},{'host':'abc\nsecret'},{'port':True},{'username':'new-login'}):
            before=c.path.read_bytes()
            with pytest.raises(ValueError):await c.save('bob',{'expected_revision':b['revision'],**patch})
            assert c.path.read_bytes()==before
        with pytest.raises(ValueError,match='CONFIG_CHANGED'):await c.save('alice',body)
        assert c.path.stat().st_mode & 0o077==0
        assert c.status('alice')['qt_account_ids']==['qt-alice']
    asyncio.run(run())


@pytest.mark.parametrize('target',['file','parent','lock'])
def test_config_symlinks_are_not_followed(tmp_path,target):
    real=tmp_path/'real';real.mkdir();cfg=real/'config.json'
    creds.write(cfg,settings(45001));before=cfg.read_bytes()
    path=tmp_path/'link'
    if target=='parent':path.symlink_to(real,target_is_directory=True);path=path/'config.json'
    elif target=='file':path.symlink_to(cfg)
    else:
        path=tmp_path/'config.json';creds.write(path,settings(45001))
        path.with_name(path.name+'.config-lock').symlink_to(cfg)
    if target=='lock':
        with pytest.raises(ValueError):
            with creds.config_lock(path):pass
    else:
        with pytest.raises(ValueError):creds.load(path)
    assert cfg.read_bytes()==before


def test_real_tcp_quote_stream_survives_no_pages_and_recovers_after_disconnect(tmp_path):
    async def run():
        with MockQTrade(seed=[quote()]) as server:
            path=tmp_path/'config.json';creds.write(path,settings(server.port,enabled=True))
            control=QtControl(path,tmp_path/'state',clock=lambda:NOW,retry_delays=(.01,.02))
            await control.startup()
            try:
                await wait_for(lambda:control.stats['quotes_written']==1)
                assert control.status('alice')['fix_session_connected']
                assert len(control.rows('alice'))==1 and not control.rows('bob')
                await asyncio.sleep(1.2)
                assert '0' in server.types and server.logons==1
                server.disconnect()
                await wait_for(lambda:server.logons>=2 and control.stats['duplicate_quotes']>=1)
                assert len(control.rows('alice'))==1 and control.stats['quotes_written']==1
                old_stats=dict(control.stats)
                reused=await control.test('bob',{'expected_revision':control.status('bob')['revision']})
                assert reused['reused'] and server.logons==2
                with pytest.raises(ValueError,match='STOP_BEFORE'):await control.save('bob',{})
                await control.stop('alice',{'expected_revision':control.status('alice')['revision']})
                assert control.rows('alice')[0]['qt_status']=='FEED_UNCONFIRMED'
                after=server.logons
                later=QtControl(path,tmp_path/'state',clock=lambda:NOW)
                await later.startup();await asyncio.sleep(.05);await later.shutdown()
                assert server.logons==after and creds.load(path)[0]['enabled'] is False
            finally:await control.shutdown()
    asyncio.run(run())


def test_restart_resumes_enabled_feed_without_browser(tmp_path):
    async def run():
        with MockQTrade(seed=[quote()]) as server:
            path=tmp_path/'config.json';creds.write(path,settings(server.port,enabled=True))
            first=QtControl(path,tmp_path/'state',clock=lambda:NOW)
            await first.startup();await wait_for(lambda:first.stats['quotes_written']==1);await first.shutdown()
            assert creds.load(path)[0]['enabled'] is True
            second=QtControl(path,tmp_path/'state',clock=lambda:NOW)
            await second.startup()
            try:
                await wait_for(lambda:second.stats['duplicate_quotes']==1)
                assert second.status('alice')['fix_session_connected'] and len(second.rows('alice'))==1
            finally:await second.shutdown()
    asyncio.run(run())


def test_auth_rejection_stops_no_retry_and_keeps_shared_credentials(tmp_path):
    async def run():
        with MockQTrade(reject=True) as server:
            path=tmp_path/'config.json';creds.write(path,settings(server.port,enabled=True));before=path.read_bytes()
            c=QtControl(path,tmp_path/'state',retry_delays=(.01,))
            await c.startup()
            try:
                await wait_for(lambda:c.state=='auth_failed')
                await asyncio.sleep(.05)
                assert server.logons==1 and not c.status('alice')['collector_active']
                assert not c.status('alice')['fix_session_connected'] and path.read_bytes()==before
                assert 'synthetic-password' not in json.dumps(c.status('alice'))
            finally:await c.shutdown()
    asyncio.run(run())


def test_connection_probe_does_not_enable_or_write_quotes(tmp_path):
    async def run():
        with MockQTrade(seed=[quote()]) as server:
            path=tmp_path/'config.json';creds.write(path,settings(server.port));before=path.read_bytes()
            c=QtControl(path,tmp_path/'state')
            result=await c.test('alice',{'expected_revision':c.status('alice')['revision']})
            assert result['ok'] and not result['saved'] and path.read_bytes()==before
            assert not c.rows('alice') and not c.status('alice')['collector_active']
    asyncio.run(run())


def test_session_gap_resend_applies_quotes_in_order(tmp_path):
    def script(peer,_):peer.send('S',quote('third'),seq=3)
    def inbound(peer,msg):
        if msg['35']=='2':
            assert (msg['7'],msg['16'])==('2','2')
            peer.send('S',quote('second'),seq=2)
    async def run():
        with MockQTrade(script=script,inbound=inbound) as server:
            path=tmp_path/'config.json';creds.write(path,settings(server.port,enabled=True))
            c=QtControl(path,tmp_path/'state',clock=lambda:NOW)
            await c.startup()
            try:
                await wait_for(lambda:c.stats['quotes_written']==2)
                with c._book.db() as db:
                    rows=list(db.execute('SELECT fields FROM qt_event ORDER BY source_seq'))
                assert [json.loads(x[0])['NegotiationID'] for x in rows]==['second','third']
                assert c.stats['resend_requests']==1 and c.state=='streaming'
            finally:await c.shutdown()
    asyncio.run(run())


def test_unmapped_quote_is_rejected_without_stopping_known_account_feed(tmp_path):
    async def run():
        with MockQTrade(seed=[quote('other',**{'18004':'not-mapped'}),quote('mine')]) as server:
            path=tmp_path/'config.json';creds.write(path,settings(server.port,enabled=True))
            c=QtControl(path,tmp_path/'state',clock=lambda:NOW);await c.startup()
            try:
                await wait_for(lambda:c.stats['quotes_received']==2)
                await wait_for(lambda:c.stats['quotes_written']==1)
                assert c.stats['rejected_quotes']==1 and c.stats['last_error_code']=='QT_ACCOUNT_MAPPING_MISSING'
                assert c.state=='streaming' and len(c.rows('alice'))==1 and not c.rows('mallory')
            finally:await c.shutdown()
    asyncio.run(run())


def test_single_connection_lease_across_controller_instances(tmp_path):
    async def run():
        with MockQTrade() as server:
            path=tmp_path/'config.json';creds.write(path,settings(server.port,enabled=True))
            a=QtControl(path,tmp_path/'state');b=QtControl(path,tmp_path/'state')
            await a.startup()
            try:
                await wait_for(lambda:a.status('alice')['fix_session_connected'])
                with pytest.raises(ValueError):await b.start('alice',{'expected_revision':b.status('alice')['revision']})
                with pytest.raises(ValueError):await b.save('alice',{'expected_revision':b.status('alice')['revision'],'host':'127.0.0.1'})
                with pytest.raises(ValueError):await b.stop('alice',{'expected_revision':b.status('alice')['revision']})
                assert server.logons==1 and a.status('bob')['collector_active']
            finally:await a.shutdown()
    asyncio.run(run())


def test_actual_authenticated_api_configures_shared_fix_with_private_rows_and_independent_inno(tmp_path,monkeypatch):
    from fastapi.testclient import TestClient
    from server.app import create_app
    from server.auth import AuthConfig,AuthUser
    from server.market import MarketService
    from server.quote_control import QuoteControl
    from server.quote_credentials import save_quote
    def wait(predicate):
        until=time.monotonic()+6
        while not predicate():
            assert time.monotonic()<until,'HTTP_FIX_WAIT_TIMEOUT'
            time.sleep(.01)
    clock=[NOW];ticks=[0]
    async def inno_session(_values,update):
        while True:
            ticks[0]+=1;update(state='streaming',quotes_received=ticks[0],quotes_written=ticks[0])
            await asyncio.sleep(.01)
    inno=QuoteControl(directory=tmp_path/'inno',reader=object(),session=inno_session,db_check=lambda:{'ok':True})
    save_quote({'account':'inno-synthetic','sid':'synthetic-sid','cookie':'','device_id':'synthetic-device'},inno.directory)
    monkeypatch.setenv('XUNJIA_QT_CONFIG',str(tmp_path/'qt.json'))
    market=MarketService(None,rows_provider=lambda *_:[{'code':'000001.IB','name':'合成债券','bondUniCode':'1',
           'flowId':10,'ob':'2.05','obv':'500','obs':'国际','oo':'2.15','oov':'600','oos':'国利','clearing':'+1'}])
    with MockQTrade(seed=[quote(),quote('bob',**{'18004':'qt-bob','132':'1.90','134':'30000000'})]) as server:
        app=create_app(database_url='sqlite:///'+str(tmp_path/'business.sqlite'),enable_background=False,
            market=market,quote_control=inno,auth_config=AuthConfig(sso_secret='synthetic',session_secret='s'*40,clock=lambda:clock[0]))
        def headers(owner):return {'Authorization':'Bearer '+app.state.auth.issue_token(AuthUser(owner,owner))}
        with TestClient(app) as api:
            for method,path in [('GET',''),('PUT',''),('POST','/test'),('POST','/start'),('POST','/stop')]:
                assert api.request(method,'/api/im/qt-quote'+path,json={}).status_code==401
            a,b=headers('alice'),headers('bob')
            revision=api.get('/api/im/qt-quote',headers=a).json()['data']['revision']
            body={k:v for k,v in settings(server.port).items() if k in creds.EDITABLE}
            body.update(expected_revision=revision,qt_account_ids=['qt-alice'])
            response=api.put('/api/im/qt-quote',headers=a,json=body)
            assert response.status_code==200,response.text
            assert 'synthetic-password' not in response.text
            shared=api.get('/api/im/qt-quote',headers=b).json()['data']
            assert shared['username']=='synthetic-login' and shared['password_set'] and shared['qt_account_ids']==[]
            response=api.put('/api/im/qt-quote',headers=b,json={'expected_revision':shared['revision'],'qt_account_ids':['qt-bob']})
            assert response.status_code==200,response.text
            revision=response.json()['data']['revision']
            assert api.post('/api/im/quote/start',headers=a).status_code==200
            response=api.post('/api/im/qt-quote/start',headers=b,json={'expected_revision':revision})
            assert response.status_code==200,response.text
            c=app.state.qt_control;wait(lambda:c.stats['quotes_written']==2)
            assert api.get('/api/bonds?channel=qt',headers=a).json()['total']==1
            assert api.get('/api/bonds?channel=qt',headers=b).json()['data'][0]['qt_fields']['OwnQtNO']=='qt-bob'
            assert api.get('/api/bonds',headers=a).json()['data'][0]['ob']=='2.00'
            assert api.get('/api/bonds',headers=b).json()['data'][0]['ob']=='1.90'
            assert not app.state.quote_hub.clients
            # Expire BOTH browser sessions; the service keeps receiving and Inno keeps ticking.
            clock[0]+=86401;previous=ticks[0]
            assert api.get('/api/im/qt-quote',headers=a).status_code==401
            server.connections[-1].send('S',quote('after-browser-expired'))
            wait(lambda:c.stats['quotes_written']==3 and ticks[0]>previous)
            assert c.status('alice')['collector_active'] and inno.status('bob')['collector_active']
            a=headers('alice');revision=api.get('/api/im/qt-quote',headers=a).json()['data']['revision']
            assert api.post('/api/im/qt-quote/stop',headers=a,json={'expected_revision':revision}).status_code==200
            previous=ticks[0];wait(lambda:ticks[0]>previous)
            assert inno.status('bob')['collector_active']


def test_fix_new_requote_cancel_and_expiry_traverse_actual_socket(tmp_path):
    async def run():
        n=quote('life');h=quote('life',**{'18045':'H','60':'20261028-10:00:01','132':'1.99'})
        h.pop('134')  # V1.0.1 H is a full economic snapshot, not an IM partial update.
        z={'10176':'4','537':'3','117':'-10000','18033':'life','18004':'qt-alice','18035':'2',
           '55':'合成债券','60':'20261028-10:00:02','18045':'Z','18170':'4'}
        expired={**z,'18033':'expired','18045':'S','18170':'2','60':'20261028-10:00:03'}
        with MockQTrade(seed=[n,h,z,quote('expired'),expired,n]) as server:
            path=tmp_path/'qt.json';creds.write(path,settings(server.port,enabled=True))
            c=QtControl(path,tmp_path/'state',clock=lambda:NOW);await c.startup()
            try:
                await wait_for(lambda:c.stats['quotes_received']==6 and c.stats['duplicate_quotes']==1)
                rows={r['qt_fields']['NegotiationID']:r for r in c.rows('alice')}
                assert rows['life']['qt_status']=='WITHDRAWN' and rows['life']['ibv'] is None
                assert rows['life']['qt_fields']['BidPx']=='1.99'
                assert rows['expired']['qt_status']=='ENDED'
                assert all(r['qt_compare_reasons']['bid'] for r in rows.values())
            finally:await c.shutdown()
    asyncio.run(run())


def test_missing_heartbeat_triggers_test_request_then_reconnect(tmp_path):
    async def run():
        with MockQTrade(heartbeats=False) as server:
            path=tmp_path/'qt.json';creds.write(path,settings(server.port,enabled=True))
            c=QtControl(path,tmp_path/'state',retry_delays=(.01,));await c.startup()
            try:
                await wait_for(lambda:server.logons>=2,timeout=5)
                assert server.test_ids and c.stats['reconnects']>=1
            finally:await c.shutdown()
    asyncio.run(run())


def test_peer_testrequest_gets_same_identifier_without_order_message(tmp_path):
    def script(peer,_):peer.send('1',{'112':'vendor-test-id'})
    received=[]
    def inbound(peer,fields):
        if fields['35']=='0' and '112' in fields:received.append(fields['112'])
    async def run():
        with MockQTrade(script=script,inbound=inbound) as server:
            path=tmp_path/'qt.json';creds.write(path,settings(server.port,enabled=True))
            c=QtControl(path,tmp_path/'state');await c.startup()
            try:
                await wait_for(lambda:received==['vendor-test-id'])
                assert set(server.types)<=set('A012345')
            finally:await c.shutdown()
    asyncio.run(run())


def test_wrong_counterparty_comp_id_stops_without_quote_persistence(tmp_path):
    def script(peer,_):
        bad=header('S',2)+list(quote().items());bad=[(k,'OTHER' if k=='49' else v) for k,v in bad]
        peer.sock.sendall(wire(bad))
    async def run():
        with MockQTrade(script=script) as server:
            path=tmp_path/'qt.json';creds.write(path,settings(server.port,enabled=True))
            c=QtControl(path,tmp_path/'state');await c.startup()
            try:
                await wait_for(lambda:c.state=='protocol_error')
                assert not c.rows('alice') and c.stats['quotes_written']==0
                assert not c.status('alice')['fix_session_connected']
            finally:await c.shutdown()
    asyncio.run(run())


def test_tls_mutual_authentication_with_synthetic_certificates(tmp_path):
    import ssl,ipaddress
    from datetime import datetime,timedelta,timezone
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes,serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID,ExtendedKeyUsageOID
    now=datetime.now(timezone.utc)
    ca_key=rsa.generate_private_key(public_exponent=65537,key_size=2048)
    ca_name=x509.Name([x509.NameAttribute(NameOID.COMMON_NAME,'Synthetic QT CA')])
    ca=(x509.CertificateBuilder().subject_name(ca_name).issuer_name(ca_name).public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number()).not_valid_before(now-timedelta(minutes=1)).not_valid_after(now+timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True,path_length=None),critical=True).sign(ca_key,hashes.SHA256()))
    cap=tmp_path/'ca.pem';cap.write_bytes(ca.public_bytes(serialization.Encoding.PEM))
    def issue(name,purpose):
        key=rsa.generate_private_key(public_exponent=65537,key_size=2048)
        cert=(x509.CertificateBuilder().subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME,name)]))
              .issuer_name(ca_name).public_key(key.public_key()).serial_number(x509.random_serial_number())
              .not_valid_before(now-timedelta(minutes=1)).not_valid_after(now+timedelta(days=1))
              .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address('127.0.0.1'))]),critical=False)
              .add_extension(x509.ExtendedKeyUsage([purpose]),critical=False).sign(ca_key,hashes.SHA256()))
        cp=tmp_path/(name+'.crt');kp=tmp_path/(name+'.key')
        cp.write_bytes(cert.public_bytes(serialization.Encoding.PEM));kp.write_bytes(key.private_bytes(serialization.Encoding.PEM,serialization.PrivateFormat.PKCS8,serialization.NoEncryption()));kp.chmod(0o600)
        return str(cp),str(kp)
    server_cert,server_key=issue('server',ExtendedKeyUsageOID.SERVER_AUTH)
    client_cert,client_key=issue('client',ExtendedKeyUsageOID.CLIENT_AUTH)
    tls=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER);tls.load_cert_chain(server_cert,server_key);tls.verify_mode=ssl.CERT_REQUIRED;tls.load_verify_locations(str(cap))
    async def run():
        with MockQTrade(seed=[quote()],tls_context=tls) as server:
            path=tmp_path/'qt.json';creds.write(path,settings(server.port,enabled=True,transport='tls',plaintext_confirmed=False,
                ca_file=str(cap),client_cert_file=client_cert,client_key_file=client_key))
            c=QtControl(path,tmp_path/'state',clock=lambda:NOW);await c.startup()
            try:
                await wait_for(lambda:c.stats['quotes_written']==1)
                assert c.status('alice')['fix_session_connected']
                assert all(server.logon_checks)
            finally:await c.shutdown()
    asyncio.run(run())


def test_quote_original_content_preserves_line_breaks_without_frame_injection():
    original='第一行报价\n第二行数量\t备注'
    fields=quote(**{'18026':original})
    decoded=Framer().feed(wire(header('S',2)+list(fields.items())))[0]
    assert decoded['18026']==original


def test_dial_and_logon_have_separate_bounded_timeout_budgets(tmp_path):
    async def run():
        with MockQTrade(logon_delay=.6) as server:
            async def delayed_connector(*args,**kwargs):
                await asyncio.sleep(.6)
                return await asyncio.open_connection(*args,**kwargs)
            session=Session(settings(server.port,connect_timeout_seconds=1),receive=lambda _:None,update=lambda **_:None,connector=delayed_connector)
            await session.run(probe=True)
            assert server.logons==1
    asyncio.run(run())


def test_successive_gaps_request_next_range_without_waiting_for_heartbeat(tmp_path):
    requested=[]
    def script(peer,_):
        peer.send('S',quote('third'),seq=3)
        peer.send('S',quote('fifth'),seq=5)
    def inbound(peer,msg):
        if msg['35']=='2':
            requested.append((msg['7'],msg['16']))
            number=int(msg['7']);peer.send('S',quote('recovered-'+str(number)),seq=number)
    async def run():
        with MockQTrade(script=script,inbound=inbound,heartbeats=False) as server:
            path=tmp_path/'qt.json';creds.write(path,settings(server.port,enabled=True))
            c=QtControl(path,tmp_path/'state',clock=lambda:NOW);await c.startup()
            try:
                await wait_for(lambda:c.stats['quotes_written']==4,timeout=1)
                assert requested==[('2','2'),('4','4')] and c.state=='streaming'
            finally:await c.shutdown()
    asyncio.run(run())


def test_failed_candidate_storage_never_overwrites_shared_configuration(tmp_path,monkeypatch):
    import server.qt_control as module
    path=tmp_path/'qt.json';creds.write(path,settings(45001));before=path.read_bytes()
    c=QtControl(path,tmp_path/'state')
    def unavailable(*_a,**_kw):raise OSError('synthetic storage unavailable')
    monkeypatch.setattr(module,'QtQuoteBook',unavailable)
    async def run():
        with pytest.raises(OSError):await c.save('alice',{'expected_revision':c.status('alice')['revision'],'host':'127.0.0.2'})
        assert path.read_bytes()==before
    asyncio.run(run())


def test_service_resume_refuses_configuration_changed_before_lease(tmp_path):
    path=tmp_path/'qt.json';creds.write(path,settings(45001,enabled=True))
    c=QtControl(path,tmp_path/'state')
    original=c._lease_path
    def changed():
        values,_=creds.load(path);creds.write(path,{**values,'enabled':False,'host':'127.0.0.2'})
        return original()
    c._lease_path=changed
    async def run():
        try:
            with pytest.raises(ValueError,match='CONFIG_CHANGED'):await c.start('system',persist=False)
            assert c._task is None
        finally:await c.shutdown()
    asyncio.run(run())
