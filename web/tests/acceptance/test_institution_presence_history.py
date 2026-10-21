"""Institution UI acceptance over real Vue and explicit intercepted APIs."""
import base64,hashlib,json,time
from urllib.parse import urlsplit,parse_qs
import pytest
from playwright.sync_api import expect
from test_collector_account_selection import selector,open_selector,pick_b

PNG=base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aOWQAAAAASUVORK5CYII=')

@pytest.fixture
def institutional(selector):
 page,base,s,book,fulfill=selector
 now=time.time()
 for w in s['workbenches']:
  w['updated_at']=now
  for a in w['accounts']:a['updated_at']=now
 s.update(sync=False,sync_reads=0,media_calls=[],broken_media=False,history_calls=0)
 def catalog(r):fulfill(r,{'ok':True,'data':{'contract':'its-relay-path-v1','offline_seconds':60,'workbenches':s['workbenches'],'selected':s['selected']}})
 page.route(base+'/api/im/catalog**',catalog)
 def contacts(r):
  data=book();data.update(friends_updated_at=now-10,org_updated_at=now-20,org_state='empty',org={'departments':[],'members':[]})
  if parse_qs(urlsplit(r.request.url).query).get('refresh')==['1']:
   s['sync']=True;data['refresh_requested']=True
  elif s['sync']:
   s['sync_reads']+=1
   if s['sync_reads']>=2:data.update(friends_updated_at=now+1,org_updated_at=now+1)
  fulfill(r,{'ok':True,'data':data})
 page.route(base+'/api/im/contacts**',contacts)
 h=hashlib.sha256(PNG).hexdigest()
 def history(r):
  s['history_calls']+=1
  row={'id':1,'native_id':'qd:100','peer_id':'123','direction':'in','occurred_at':now-60,'text':'native fixture','type':'image','session_kind':'private','historical':True,'media':[{'sha256':h,'name':'sample.png','size':len(PNG),'mime':'image/png','state':'ready','download_ready':True}]}
  fulfill(r,{'ok':True,'data':{'items':[row],'workbench_id':s['selected']['workbench_id'],'account_uid':s['selected']['account_uid'],'binding_version':s['selected']['version'],'retention_days':7,'has_more':False,'next_cursor':None}})
 page.route(base+'/api/im/history**',history)
 def media(r):
  s['media_calls'].append(dict(r.request.headers))
  r.fulfill(status=200,content_type='application/octet-stream',body=(b'x'*len(PNG) if s['broken_media'] else PNG))
 page.route(base+'/api/im/media/*',media)
 return selector

def test_institution_offline_disables_operations_without_mutation(institutional):
 p,_,s,*_=institutional
 s['workbenches'][0]['accounts'][0].update(stale=True,available=False,state='offline')
 open_selector(institutional)
 expect(p.get_by_test_id('collector-offline')).to_contain_text('已离线')
 for name in ('collector-connect','collector-read-saved','collector-sync-contacts'):
  expect(p.get_by_test_id(name)).to_be_disabled()
 expect(p.get_by_test_id('native-history').get_by_role('button',name='刷新主库历史')).to_be_disabled()
 assert not [c for c in s['calls'] if c[0]!='GET']

def test_client_expires_presence_at_sixty_seconds_even_cached_online(institutional):
 p,_,s,*_=institutional
 s['workbenches'][0]['accounts'][0]['updated_at']=time.time()-61
 open_selector(institutional)
 expect(p.get_by_test_id('collector-offline')).to_be_visible()
 expect(p.get_by_test_id('collector-connect')).to_be_disabled()

def test_background_directory_refresh_preserves_unsaved_draft(institutional):
 p=open_selector(institutional)
 pick_b(p);p.wait_for_timeout(5500)
 expect(p.get_by_test_id('collector-workbench')).to_have_value('wb-b')
 expect(p.get_by_test_id('collector-account')).to_have_value('same-account')
 assert not [c for c in institutional[2]['calls'] if c[0]!='GET']

def test_sync_waits_for_both_new_snapshots_not_request_success(institutional):
 p=open_selector(institutional)
 p.get_by_test_id('collector-sync-contacts').click()
 expect(p.get_by_test_id('collector-account-settings')).to_contain_text('同步请求已提交')
 expect(p.get_by_test_id('collector-account-settings')).to_contain_text('已收到新的个人好友与机构组织架构版本',timeout=10000)
 assert institutional[2]['sync_reads']>=2

def test_history_fetch_and_image_verifies_real_bytes_with_auth(institutional):
 p=open_selector(institutional);section=p.get_by_test_id('native-history')
 expect(section).to_contain_text('native fixture')
 section.get_by_role('button',name='查看实际图片').click()
 expect(section.locator('img')).to_be_visible()
 assert institutional[2]['media_calls'][0].get('authorization','').startswith('Bearer ')
 assert p.locator('[data-testid="collector-account-settings"]').count()==1

def test_corrupt_attachment_never_shows_placeholder_or_ready_preview(institutional):
 p=open_selector(institutional);institutional[2]['broken_media']=True;section=p.get_by_test_id('native-history')
 section.get_by_role('button',name='查看实际图片').click()
 expect(section.get_by_role('alert')).to_contain_text('校验失败')
 expect(section.locator('img')).to_have_count(0)

def test_later_offline_status_stops_history_and_contact_actions(institutional):
 p=open_selector(institutional);s=institutional[2]
 expect(p.get_by_test_id('collector-sync-contacts')).to_be_enabled()
 s['workbenches'][0]['accounts'][0].update(stale=True,available=False,state='offline')
 expect(p.get_by_test_id('collector-offline')).to_be_visible(timeout=9000)
 expect(p.get_by_test_id('collector-sync-contacts')).to_be_disabled()
 expect(p.get_by_test_id('native-history').get_by_role('button',name='下载原文件')).to_be_disabled()