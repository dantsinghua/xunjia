"""Independent synthetic 24-hour session and owner/public data contract."""
import base64
import hashlib
import hmac
import json

import pytest

from server.auth import AuthConfig, AuthService, AuthUser, AuthError

NOW = 1_800_000_000
SSO = 'independent-synthetic-sso-only'
SECRET = 'independent-synthetic-session-key-over-thirty-two'


def config(clock):
    return AuthConfig(sso_secret=SSO,session_secret=SECRET,clock=lambda:clock[0])


@pytest.mark.parametrize('age',[23*3600,24*3600-1])
def test_default_session_remains_valid_through_full_business_day_and_recreation(age):
    clock=[NOW];auth=AuthService(config(clock));user=AuthUser('alice','Synthetic Alice')
    token=auth.issue_token(user)
    clock[0]+=age
    assert auth.verify_token(token)==user
    assert AuthService(config(clock)).verify_token(token)==user


def test_exact_24_hour_deadline_and_later_are_rejected():
    clock=[NOW];auth=AuthService(config(clock));token=auth.issue_token(AuthUser('alice','Synthetic Alice'))
    assert auth.config.token_ttl_seconds==86400
    for age in (86400,86401):
        clock[0]=NOW+age
        assert auth.verify_token(token) is None


@pytest.mark.parametrize('mutation',['signature','identity','audience','other-secret'])
def test_long_session_keeps_forgery_audience_and_secret_guards(mutation):
    clock=[NOW];auth=AuthService(config(clock));token=auth.issue_token(AuthUser('alice','Synthetic Alice'))
    payload,signature=token.split('.')
    if mutation=='signature': token=payload+'.'+('0' if signature[0]!='0' else '1')+signature[1:]
    elif mutation in ('identity','audience'):
        data=json.loads(base64.urlsafe_b64decode(payload+'='*(-len(payload)%4)))
        data['sub' if mutation=='identity' else 'aud']='bob' if mutation=='identity' else 'ibquote'
        encoded=base64.urlsafe_b64encode(json.dumps(data).encode()).decode().rstrip('=')
        token=encoded+'.'+(hmac.new(SECRET.encode(),encoded.encode(),hashlib.sha256).hexdigest() if mutation=='audience' else signature)
    else:
        auth=AuthService(AuthConfig(sso_secret=SSO,session_secret='another-synthetic-session-secret-over-thirty-two',clock=lambda:clock[0]))
    assert auth.verify_token(token) is None


@pytest.mark.parametrize('age,valid',[(60,True),(-60,True),(61,False),(-61,False)])
def test_sso_proof_window_stays_sixty_seconds(age,valid):
    clock=[NOW];auth=AuthService(config(clock));ts=str(NOW+age)
    proof={'userName':'alice','nickName':'Synthetic Alice','ts':ts,'sign':hashlib.md5(f'alice|Synthetic Alice|{ts}|{SSO}'.encode()).hexdigest()}
    if valid:assert auth.verify_sso(proof).user_id=='alice'
    else:
        with pytest.raises(AuthError):auth.verify_sso(proof)


@pytest.fixture
def private_desk(tmp_path,monkeypatch):
    from fastapi.testclient import TestClient
    from server.app import create_app
    from server.quote_control import QuoteControl
    for name in ('XUNJIA_TRANSPORT_CONFIG','XUNJIA_MQ_CONNECTION_CONFIG','XUNJIA_WEB_DIST',
                 'XUNJIA_QUOTE_SOURCE','XUNJIA_REFERENCE_CONFIG','XUNJIA_DIFY_URL','XUNJIA_AGGREGATION_PROFILE'):
        monkeypatch.delenv(name,raising=False)
    monkeypatch.setenv('XUNJIA_STATE_DIR',str(tmp_path/'state'))
    monkeypatch.setenv('XUNJIA_INSTITUTION_CONFIG',str(tmp_path/'absent-synthetic-institution.json'))
    monkeypatch.setenv('XUNJIA_QB_AUTOSTART','0')
    clock=[NOW]
    quote=QuoteControl(directory=tmp_path/'quotes')
    app=create_app(database_url='sqlite:///'+str(tmp_path/'business.sqlite'),auth_config=config(clock),
                   enable_background=False,submission_guard=lambda owner:None,quote_control=quote)
    # Admission is deliberately synthetic. No dispatcher, collector or model is
    # run; only real HTTP authorization, local ownership and queued records.
    repo=app.state.repository;service=app.state.inquiry;identities={}
    for owner,code,price in [('alice','000101.IB','3.11'),('bob','000202.IB','3.22')]:
        repo.bind_account(owner,'account-'+owner,'synthetic-wb')
        repo.save_preferences(owner,{'context_id':'context-'+owner,'strategy':{'template_id':'private-'+owner,'step_bp':price}})
        preview=service.preview(owner,'seed-preview',[{'code':code,'name':'private-bond-'+owner,'peer_ids':['peer-'+owner],
            'our_side':'BUY','volume_wan':'1000','opening_yield_pct':price,'limit_yield_pct':'3.00','step_bp':'2'}])
        service.confirm(owner,preview['batch_id'],preview['version'],'seed-confirm')
        group=repo.group_rows(owner)[0];peer=group['peers'][0]
        repo.ingest(owner,peer['thread_id'],event_id='private-event-'+owner,source_seq=1,
                    text='private-history-'+owner,trade_day=group['trade_day'],occurred_at=NOW)
        identities[owner]={'token':app.state.auth.issue_token(AuthUser(owner,'Synthetic '+owner)),
                           'group':group,'peer':peer,'code':code,'price':price}
    public=[{'code':'900001.IB','name':'shared-public-bond','bondUniCode':'900001','flowId':1,
             'ob':'2.10','oo':'2.20','tags':[],'clearing':'+1'}]
    app.state.market.rows_provider=lambda channel,broker:public
    with TestClient(app) as client:
        yield {'app':app,'client':client,'users':identities,'clock':clock,'public':public,'quote':quote}


def owner_headers(desk,owner,context=None):
    return {'Authorization':'Bearer '+desk['users'][owner]['token'],
            'X-Xunjia-Context':context if context is not None else 'context-'+owner}


@pytest.mark.parametrize('owner,other',[('alice','bob'),('bob','alice')])
def test_private_state_quotes_strategy_and_history_use_verified_owner(private_desk,owner,other):
    d=private_desk;c=d['client'];headers=owner_headers(d,owner);own=d['users'][owner];foreign=d['users'][other]
    # The URL's spoofed identity cannot replace the signed session owner.
    for path in ('/api/negotiation/state','/api/monitor/detail','/api/strategy'):
        response=c.get(path,params={'user_id':other,'owner_user_id':other,'account_uid':'account-'+other},headers=headers)
        assert response.status_code==200,response.text
        assert 'private-'+other not in response.text and 'private-bond-'+other not in response.text
        assert foreign['code'] not in response.text
    state=c.get('/api/negotiation/state',headers=headers).json()['data']
    assert [g['bond_code'] for g in state['groups']]==[own['code']]
    assert state['groups'][0]['plan']['opening_yield_pct']==own['price']
    own_history=c.get('/api/negotiation/messages/'+own['peer']['thread_id'],headers=headers)
    assert own_history.status_code==200 and 'private-history-'+owner in own_history.text
    foreign_history=c.get('/api/negotiation/messages/'+foreign['peer']['thread_id'],headers=headers)
    assert foreign_history.status_code==400 and foreign_history.json()['ok'] is False and 'private-history-'+other not in foreign_history.text
    context=c.get('/api/negotiation/members/'+foreign['peer']['member_id']+'/context',headers=headers)
    assert context.status_code==400 and context.json()['ok'] is False and foreign['code'] not in context.text
    chat=c.get('/api/monitor/chat',params={'cp':'peer-'+other},headers=headers)
    assert chat.status_code==200 and chat.json()['data']['messages']==[]
    before=d['app'].state.repository.snapshot(other)
    attempt=c.post('/api/negotiation/groups/'+foreign['group']['group_id']+'/sending',json={'allowed':False},headers=headers)
    assert attempt.status_code==400 and attempt.json()['ok'] is False
    attempt=c.post('/api/negotiation/members/'+foreign['peer']['member_id']+'/sending',json={'allowed':False},headers=headers)
    assert attempt.status_code==400 and attempt.json()['ok'] is False
    saved=c.post('/api/strategy',json={'template_id':'changed-'+owner,'step_bp':'1','owner_user_id':other},headers=headers)
    assert saved.status_code==200
    assert d['app'].state.repository.snapshot(other)==before
    assert c.get('/api/strategy',headers=owner_headers(d,other)).json()['data']['active_template_id']=='private-'+other


@pytest.mark.parametrize('owner,other',[('alice','bob'),('bob','alice')])
def test_foreign_send_context_cannot_write_and_own_context_never_mutates_other_user(private_desk,owner,other):
    d=private_desk;repo=d['app'].state.repository;c=d['client']
    before={who:repo.snapshot(who) for who in (owner,other)}
    commands={who:d['app'].state.inquiry.commands(who) for who in (owner,other)}
    body={'cp':'peer-'+owner,'text':'synthetic manual only '+owner,'request_id':'same-request-id-both-users'}
    rejected=c.post('/api/monitor/send',json=body,headers=owner_headers(d,owner,'context-'+other))
    assert rejected.status_code==400 and rejected.json()['code']=='ACCOUNT_CONTEXT_CHANGED'
    assert rejected.json()['submitted'] is False
    assert {who:repo.snapshot(who) for who in (owner,other)}==before
    assert {who:d['app'].state.inquiry.commands(who) for who in (owner,other)}==commands
    submitted=c.post('/api/monitor/send',json=body,headers=owner_headers(d,owner))
    assert submitted.status_code==200,submitted.text
    command=submitted.json()['data']['cmd_id']
    hidden=c.get('/api/im/receipts/'+command,headers=owner_headers(d,other))
    assert hidden.status_code==400 and hidden.json()['ok'] is False and body['text'] not in hidden.text
    assert repo.snapshot(other)==before[other]
    assert d['app'].state.inquiry.commands(other)==commands[other]


def test_public_quotes_continue_for_both_users_after_one_session_expires(private_desk):
    d=private_desk;c=d['client'];a=owner_headers(d,'alice');b=owner_headers(d,'bob')
    for headers in (a,b):
        response=c.get('/api/bonds',headers=headers)
        assert response.status_code==200
        assert response.json()['data'][0]['code']=='900001.IB'
        assert 'private-' not in response.text
    # Bob logs in later; Alice expiring must not erase the public source or Bob.
    d['clock'][0]=NOW+3600
    d['users']['bob']['token']=d['app'].state.auth.issue_token(AuthUser('bob','Synthetic bob'))
    b=owner_headers(d,'bob')
    d['clock'][0]=NOW+86400
    assert c.get('/api/bonds',headers=a).status_code==401
    d['public'][0]['ob']='2.33'
    response=c.get('/api/bonds',headers=b)
    assert response.status_code==200 and response.json()['data'][0]['ob']=='2.33'
    assert d['public'][0]['ob']=='2.33'


def test_one_authenticated_public_socket_closing_leaves_other_feed_and_data_alive(private_desk,monkeypatch):
    d=private_desk;client=d['client'];stops=[]
    async def stop(owner):
        stops.append(owner)
        raise AssertionError('Closing an authenticated reader must not stop public ingestion')
    with monkeypatch.context() as scoped:
        scoped.setattr(d['quote'],'stop',stop)
        with client.websocket_connect('/ws/quote') as bob:
            bob.send_json({'op':'auth','token':d['users']['bob']['token']})
            assert bob.receive_json()=={'op':'auth','ok':True}
            bob.send_json({'op':'sub','groups':['bond-all']});bob.send_json({'op':'ping'})
            assert bob.receive_json()['op']=='pong'
            with client.websocket_connect('/ws/quote') as alice:
                alice.send_json({'op':'auth','token':d['users']['alice']['token']})
                assert alice.receive_json()=={'op':'auth','ok':True}
                alice.send_json({'op':'sub','groups':['bond-all']});alice.send_json({'op':'ping'})
                assert alice.receive_json()['op']=='pong'
                d['app'].state.quote_hub.publish({'f':1,'u':'900001','t':NOW,'bb':2.10,'bo':2.20},{'bond-all'})
                assert alice.receive_json()['items'][0]['bb']==2.10
                assert bob.receive_json()['items'][0]['bb']==2.10
            d['app'].state.quote_hub.publish({'f':2,'u':'900001','t':NOW+1,'bb':2.33,'bo':2.40},{'bond-all'})
            assert bob.receive_json()['items'][0]['bb']==2.33
            assert not stops and d['public'][0]['name']=='shared-public-bond'
