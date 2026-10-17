"""Console harness restricted to the new project's loopback API."""
import base64
import http.cookiejar
import json
from pathlib import Path
import subprocess
import urllib.error
import urllib.request

PROJECT='xunjia-dify-verify-20261013'
ROOT=Path('/tmp')/PROJECT


class Console:
    def __init__(self):
        address=subprocess.check_output(['docker','compose','-p',PROJECT,'-f',str(ROOT/'compose.yml'),'port','api','5001'],text=True).strip()
        if not address.startswith('127.0.0.1:'):raise RuntimeError('nonloopback endpoint refused')
        self.base='http://'+address;self.secret=json.loads((ROOT/'credentials.json').read_text())
        self.jar=http.cookiejar.MozillaCookieJar(str(ROOT/'cookies.txt'))
        if (ROOT/'cookies.txt').exists():self.jar.load(ignore_discard=True,ignore_expires=True)
        self.opener=urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))
    def request(self,path,body=None,*,method=None,file=None):
        headers={'Origin':self.base,'Referer':self.base+'/','Content-Type':'application/json'}
        for cookie in self.jar:
            if cookie.name in ('csrf_token','__Host-csrf_token'):headers['X-CSRF-Token']=cookie.value
        data=json.dumps(body).encode() if body is not None else None
        if file is not None:
            boundary='xunjia-isolated-upload-boundary'
            data=(f'--{boundary}\r\nContent-Disposition: form-data; name="pkg"; filename="{file.name}"\r\nContent-Type: application/octet-stream\r\n\r\n'.encode()+file.read_bytes()+f'\r\n--{boundary}--\r\n'.encode())
            headers['Content-Type']='multipart/form-data; boundary='+boundary
        request=urllib.request.Request(self.base+'/console/api'+path,data=data,method=method or ('POST' if data is not None else 'GET'),headers=headers)
        try:
            with self.opener.open(request,timeout=180) as response:raw=response.read();status=response.status
        except urllib.error.HTTPError as exc:
            raw=exc.read();status=exc.code
        self.jar.save(ignore_discard=True,ignore_expires=True);(ROOT/'cookies.txt').chmod(0o600)
        try:result=json.loads(raw)
        except ValueError:result={'raw':raw.decode(errors='replace')[:1000]}
        if status>=400:
            message=json.dumps(result,ensure_ascii=False)
            for secret in self.secret.values():message=message.replace(secret,'[REDACTED]')
            raise RuntimeError(f'{path} HTTP {status}: '+message[:1800])
        return result
    def bootstrap(self):
        status=self.request('/setup')
        if status['step']=='not_started':
            self.request('/init',{'password':self.secret['owner_password'][:24]})
            self.request('/setup',{'email':'xunjia-verify@example.com','name':'Xunjia Verify','password':self.secret['owner_password'],'language':'en-US'})
        result=self.request('/login',{'email':'xunjia-verify@example.com','password':base64.b64encode(self.secret['owner_password'].encode()).decode(),'remember_me':True})
        print(json.dumps({'endpoint':self.base,'login_result':result.get('result'),'cookie_names':[c.name for c in self.jar]}))


if __name__=='__main__':Console().bootstrap()
