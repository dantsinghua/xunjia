"""Configuration precedes directory discovery; real Vue with isolated API fixture."""
import json
from urllib.parse import urlsplit

import pytest
from playwright.sync_api import expect
from test_collector_account_selection import selector, open_selector


@pytest.fixture
def unconfigured(ui):
    page, base, _, requests, counts = ui
    state = {'configured':False,'applied':False,'save_error':False,'catalog_calls':[], 'api_calls':[]}
    def fulfill(route,data,status=200):
        route.fulfill(status=status,content_type='application/json',body=json.dumps(data,ensure_ascii=False))
    def route(r):
        req=r.request;path=urlsplit(req.url).path;state['api_calls'].append((req.method,path))
        requests.append(req);counts[path]+=1
        if path=='/api/auth/me':
            fulfill(r,{'ok':True,'data':{'user_id':'qa-trader','display_name':'验收交易员','binding':None,'account_uid':None}});return
        if path.startswith('/api/im/catalog'):
            state['catalog_calls'].append((req.method,path))
            fulfill(r,{'ok':True,'data':{'selected':None,'workbenches':[{'workbench_id':'wb-authenticated',
                'name':'已认证合成工作台','updated_at':1792480000,'stale':False,'accounts':[
                    {'account_uid':'account-authenticated','account_nick':'合成企点号',
                     'stale':False,'available':True,'capabilities':['negotiation-v22-watermark']}]}]}});return
        if req.method=='PUT':
            if state['save_error']:
                fulfill(r,{'ok':False,'hint':'合成配置冲突未保存'},409);return
            state['configured']=True
        if path.endswith('/test'):
            fulfill(r,{'ok':True,'data':{'passed':True,'connected':True,'saved':False,'steps':[
                {'name':'pending','ok':True,'detail':'合成pending通过'}]}});return
        fulfill(r,{'ok':True,'data':{'editable':True,'configured':state['configured'],'backend':'file_gateway',
            'base_url':'https://relay.synthetic.invalid' if state['configured'] else '',
            'credentials_set':True,'revision':'saved-revision' if state['configured'] else '',
            'applied':state['applied'],'account_uid':'','workbench_id':'','note':'合成配置状态'}})
    for pattern in ['/api/auth/me','/api/im/mq-config**','/api/im/catalog**']:
        page.route(base+pattern,route)
    return ui,state


def open_config(s):
    ui,state=s;page,base,*_=ui
    page.goto(base+'/#/link');expect(page.get_by_test_id('mq-config')).to_be_visible()
    expect(page.locator('[name=mq-base-url]')).to_be_enabled()
    return page,state


def fill_draft(page):
    page.locator('[name=mq-base-url]').fill('https://relay.synthetic.invalid')
    page.locator('[name=mq-client-cert]').fill('/synthetic/client.crt')


def test_unsaved_connection_draft_and_pending_test_never_visit_directory(unconfigured):
    page,state=open_config(unconfigured)
    expect(page.get_by_test_id('collector-account-settings')).to_have_count(0)
    assert state['catalog_calls']==[]
    fill_draft(page)
    page.get_by_test_id('mq-test').click()
    expect(page.get_by_test_id('mq-test-result')).to_be_visible()
    expect(page.get_by_test_id('collector-account-settings')).to_have_count(0)
    assert state['catalog_calls']==[]
    assert not any(method=='PUT' for method,_ in state['api_calls'])


@pytest.mark.parametrize('save_error,applied',[(True,False),(False,False)])
def test_rejected_or_unapplied_save_does_not_open_or_query_directory(unconfigured,save_error,applied):
    page,state=open_config(unconfigured);state.update(save_error=save_error,applied=applied)
    fill_draft(page);page.get_by_test_id('mq-save').click()
    expect(page.get_by_test_id('mq-save')).to_be_enabled()
    expect(page.get_by_test_id('collector-account-settings')).to_have_count(0)
    assert state['catalog_calls']==[]


def test_successfully_saved_and_loaded_connection_precedes_authenticated_directory_selection(unconfigured):
    page,state=open_config(unconfigured);state['applied']=True
    fill_draft(page);page.get_by_test_id('mq-save').click()
    panel=page.get_by_test_id('collector-account-settings');expect(panel).to_be_visible()
    expect(page.get_by_test_id('collector-workbench').locator('option[value=wb-authenticated]')).to_have_count(1)
    calls=state['api_calls'];saved_index=calls.index(('PUT','/api/im/mq-config'))
    assert state['catalog_calls']==[('POST','/api/im/catalog/refresh')]
    assert calls.index(state['catalog_calls'][0])>saved_index
    assert not any(path=='/api/im/binding/select' for _,path in calls)
    expect(page.locator('[name=mq-config-yaml]')).to_have_count(0)
    expect(panel).not_to_contain_text('固定账号')


@pytest.fixture
def metadata_panel(selector):
    page,base,state,*_=selector
    control={'calls':[],'hold_next':False,'held':[],'row_count':1}
    def config(route):
        route.fulfill(content_type='application/json',body=json.dumps({'ok':True,'data':{
            'editable':True,'configured':True,'backend':'file_gateway','applied':True,
            'base_url':'https://relay.synthetic.invalid','credentials_set':True,'revision':'synthetic',
            'account_uid':state['selected']['account_uid'],'workbench_id':state['selected']['workbench_id'],'note':''}}))
    def files(route):
        req=route.request;control['calls'].append((req.method,req.url))
        if control['hold_next']:
            control['hold_next']=False;control['held'].append(route);return
        pair=state['selected'];older='before=' in req.url
        item={'id':9 if older else 10,'name':('older-' if older else 'current-')+'v'+str(pair['version'])+'.bin',
            'extension':'bin','kind':'contacts','lane':'up','source_created_at':1792480000,
            'received_at':None if older else 1792480010,'state':'STORED','upload_state':'UPLOADED','receive_state':'STORED'}
        items=[{**item,'id':item['id']+n, 'name':item['name'] if n==0 else 'extra-'+str(n)+'-'+item['name']} for n in range(1 if older else control['row_count'])]
        result={'scope':'retained_authenticated_files',**pair,'items':items,'next_before':None if older else 10}
        route.fulfill(content_type='application/json',body=json.dumps({'ok':True,'data':result}))
    page.route(base+'/api/im/mq-config**',config)
    page.route(base+'/api/im/files**',files)
    return selector,control


def test_metadata_table_is_explicit_retained_history_and_paginates_without_mutations(metadata_panel):
    s,control=metadata_panel;page=open_selector(s)
    panel=page.get_by_test_id('transfer-files');expect(panel).to_be_visible()
    expect(panel.locator('caption')).to_contain_text('wb-a / same-account')
    expect(panel.locator('tbody')).to_contain_text('current-v1.bin')
    expect(panel).to_contain_text('尚未下载的文件名无法在此列出')
    expect(panel.locator('th')).to_have_text(['实际文件名','类型','文件生成时间','本机接收时间','处理状态'])
    panel.get_by_role('button',name='加载更早记录',exact=True).click()
    expect(panel.locator('tbody tr')).to_have_count(2)
    expect(panel.locator('tbody')).to_contain_text('older-v1.bin')
    expect(panel.locator('tbody')).to_contain_text('未记录')
    assert all(method=='GET' for method,_ in control['calls'])
    assert any('before=10' in url for _,url in control['calls'])
    assert not any(method!='GET' for method,_,_ in s[2]['calls'])


def test_late_metadata_response_from_old_binding_version_is_not_applied(metadata_panel):
    s,control=metadata_panel;page=open_selector(s)
    panel=page.get_by_test_id('transfer-files');expect(panel.locator('tbody')).to_contain_text('current-v1.bin')
    control['hold_next']=True
    panel.get_by_role('button',name='刷新文件记录',exact=True).click()
    expect(panel).to_contain_text('正在读取文件记录')
    assert len(control['held'])==1
    page.get_by_test_id('collector-connect').click()
    expect(panel.locator('tbody')).to_contain_text('current-v2.bin')
    old={'scope':'retained_authenticated_files','workbench_id':'wb-a','account_uid':'same-account',
        'items':[{'id':999,'name':'LATE-OLD-VERSION.bin','extension':'bin','kind':'contacts','lane':'up',
        'source_created_at':1792480000,'received_at':1792480010,'state':'STORED','upload_state':'UPLOADED','receive_state':'STORED'}],
        'next_before':None}
    control['held'].pop().fulfill(content_type='application/json',body=json.dumps({'ok':True,'data':old}))
    expect(panel.locator('tbody')).to_contain_text('current-v2.bin')
    expect(panel).not_to_contain_text('LATE-OLD-VERSION.bin')
    assert all(method=='GET' for method,_ in control['calls'])


def test_fifty_file_rows_scroll_inside_bounded_table_keep_header_and_downstream_configuration_reachable(metadata_panel):
    s,control=metadata_panel;control['row_count']=50
    page=open_selector(s)
    panel=page.get_by_test_id('transfer-files')
    expect(panel.locator('tbody tr')).to_have_count(50)
    box=panel.locator('.table-scroll');box.scroll_into_view_if_needed()
    sizes=box.evaluate("""e=>({height:e.getBoundingClientRect().height,client:e.clientHeight,
        scroll:e.scrollHeight,viewport:innerHeight,overflow:getComputedStyle(e).overflowY})""")
    assert sizes['height']<=min(480,sizes['viewport']*.55)+2
    assert sizes['scroll']>sizes['client'] and sizes['overflow'] in ['auto','scroll']
    box.evaluate('e=>{e.scrollTop=e.scrollHeight}')
    sticky=box.evaluate("""e=>{const head=e.querySelector('thead'),cell=head.querySelector('th');
        const h=getComputedStyle(head).position==='sticky'?head:cell;return {
        top:h.getBoundingClientRect().top,containerTop:e.getBoundingClientRect().top,scrollTop:e.scrollTop}}""")
    assert sticky['scrollTop']>0
    assert abs(sticky['top']-sticky['containerTop'])<=2
    lower=page.get_by_test_id('inno-config');lower.scroll_into_view_if_needed()
    expect(lower).to_be_visible()
    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
    assert all(method=='GET' for method,_ in control['calls'])
