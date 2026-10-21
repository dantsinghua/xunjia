"""Actual browser + built Vue + real service routes + local HTTP file gateway."""
import base64,hashlib,json,socket,threading,time,sys,os
from pathlib import Path
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
import pytest,uvicorn
from playwright.sync_api import sync_playwright,expect
from mock_chain import Chain
from relay.gateway_paths import CONTRACT
from server.auth import AuthUser
O=Path(os.environ["ITS_RELAY_QA_OUTPUT"])

def test_browser_receives_authenticated_history_image_file_and_offline_status(tmp_path,monkeypatch):
    c=Chain(tmp_path,monkeypatch);server=None;thread=None
    try:
        c.select()
        c.runtime.background_enabled=True;c.runtime.start()
        png=bytes.fromhex('89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4890000000b49444154789c636000020000050001a5f645400000000049454e44ae426082')
        filedata=b'original synthetic document\r\n';adapter=c.collector.hosts['qd01','10001'].bridge.adapters['oss']
        messages=[]
        for i,(raw,name,mime,kind) in enumerate([(png,'original.png','image/png','image'),(filedata,'document.txt','text/plain','file')]):
            h=hashlib.sha256(raw).hexdigest()
            adapter.publish_manifest({'contract':CONTRACT,'op':'media_chunk','sha256':h,'size':len(raw),'position':0,'chunks':1,'data':base64.b64encode(raw).decode()},message_id='browser-media-'+str(i))
            messages.append({'native_id':'browser-'+str(i),'peer_id':'12345','direction':'in','occurred_at':int(c.clock())-i,'text':'','type':kind,'session_kind':'private','historical':True,'media':[{'sha256':h,'name':name,'size':len(raw),'mime':mime,'state':'ready'}]})
        adapter.publish_manifest({'contract':CONTRACT,'op':'history','items':messages},message_id='browser-history');c.pump(3)
        built=O/'web-dist';assert (built/'index.html').is_file()
        c.app.mount('/assets',StaticFiles(directory=built/'assets'),name='mock-built-assets')
        c.app.add_api_route('/',lambda:FileResponse(built/'index.html'),methods=['GET'])
        c.app.add_api_route('/logo.png',lambda:FileResponse(built/'logo.png'),methods=['GET'])
        sock=socket.socket();sock.bind(('127.0.0.1',0));sock.listen(128);port=sock.getsockname()[1]
        server=uvicorn.Server(uvicorn.Config(c.app,log_level='error',lifespan='off'))
        thread=threading.Thread(target=lambda:server.run(sockets=[sock]),daemon=True);thread.start()
        deadline=time.monotonic()+8
        while not server.started and time.monotonic()<deadline:time.sleep(.02)
        assert server.started
        errors=[]
        token=c.app.state.auth.issue_token(AuthUser('owner-a','synthetic browser trader'))
        with sync_playwright() as playwright:
            browser=playwright.chromium.launch(headless=True)
            context=browser.new_context(viewport={'width':1500,'height':1000},accept_downloads=True)
            page=context.new_page();page.on('pageerror',lambda e:errors.append(str(e)))
            page.add_init_script('localStorage.setItem("xj_auth_token",'+json.dumps(token)+');')
            page.goto('http://127.0.0.1:'+str(port)+'/#/link')
            expect(page.get_by_test_id('collector-account-settings')).to_be_visible(timeout=15000)
            expect(page.get_by_test_id('collector-current')).to_contain_text('10001')
            history=page.get_by_test_id('native-history')
            expect(history).to_be_visible()
            expect(history.get_by_text('original.png',exact=False)).to_be_visible()
            history.get_by_role('button',name='\u67e5\u770b\u5b9e\u9645\u56fe\u7247').click()
            image=history.locator('img');expect(image).to_be_visible()
            page.wait_for_function('Array.from(document.querySelectorAll("img")).some(i=>i.alt==="original.png" && i.complete && i.naturalWidth>0)')
            with page.expect_download() as event:
                history.locator('article').filter(has_text='document.txt').get_by_role('button',name='\u4e0b\u8f7d\u539f\u6587\u4ef6').click()
            download=event.value;assert Path(download.path()).read_bytes()==filedata
            page.screenshot(path=str(O/'mock-browser-online.png'),full_page=True)
            c.now[0]+=61
            page.get_by_test_id('collector-refresh').click()
            expect(page.get_by_test_id('collector-current')).to_contain_text('\u5df2\u79bb\u7ebf')
            for button in history.get_by_role('button',name='\u4e0b\u8f7d\u539f\u6587\u4ef6').all():expect(button).to_be_disabled()
            page.screenshot(path=str(O/'mock-browser-offline.png'),full_page=True)
            assert not errors,errors
            (O/'browser-result.json').write_text(json.dumps({'page_errors':errors,'original_image_decoded':True,'file_sha256':hashlib.sha256(filedata).hexdigest(),'offline_disabled':True,'real_gateway_calls':0,'real_im_sends':0,'frontend':str(built)},indent=2))
            browser.close()
    finally:
        if server:server.should_exit=True
        if thread:thread.join(6);assert not thread.is_alive()
        c.close()