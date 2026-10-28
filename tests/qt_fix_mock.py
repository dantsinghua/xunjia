"""Independent loopback FIX acceptor. No live endpoint, credential or production encoder."""
import hashlib
import socket
import socketserver
import threading
import time
from contextlib import contextmanager
from datetime import datetime
from zoneinfo import ZoneInfo

SOH=b'\x01'
NOW=datetime(2026,10,28,10,0,tzinfo=ZoneInfo('Asia/Shanghai')).timestamp()


def wire(fields,encoding='utf-8'):
    parts=[str(k).encode()+b'='+str(v).encode(encoding)+SOH for k,v in fields]
    body=b''.join(parts)
    prefix=b'8=FIX.4.4\x019='+str(len(body)).encode()+SOH+body
    return prefix+b'10='+('%03d'%(sum(prefix)%256)).encode()+SOH


def header(kind,seq,**extra):
    return [('35',kind),('49','QTRADE'),('56','TEST'),('34',str(seq)),('52','20261028-10:00:00')]+list(extra.items())


def quote(nego='dialogue-1',**changes):
    value={'10176':'4','537':'3','117':'-10000','18033':nego,'18004':'qt-alice','18003':'合成交易员',
           '18035':'2','54':'y','48':'000001','55':'合成债券','100':'XCFE','18042':'1','132':'2.00',
           '134':'10000000','18043':'1','133':'2.20','135':'20000000','18166':'20261028',
           '18168':'20261028','18167':'2','18169':'2','60':'20261028-10:00:00','18045':'N'}
    value.update(changes)
    return value


class Peer:
    def __init__(self,sock):
        self.sock=sock;self.lock=threading.Lock();self.sequence=1;self.buffer=b''
    def send(self,kind,values=(),*,seq=None,fragments=False):
        with self.lock:
            number=self.sequence if seq is None else seq
            self.sequence=max(self.sequence,number+1)
            items=list(values.items()) if isinstance(values,dict) else list(values)
            raw=wire(header(kind,number)+items)
            if fragments:
                for part in (raw[:1],raw[1:11],raw[11:23],raw[23:]):self.sock.sendall(part)
            else:self.sock.sendall(raw)
    def receive(self):
        while True:
            if self.buffer.startswith(b'8=FIX.4.4\x019='):
                end=self.buffer.find(SOH,12)
                if end>=0:
                    size=int(self.buffer[12:end])+end+1
                    if len(self.buffer)>=size+7:
                        raw,self.buffer=self.buffer[:size+7],self.buffer[size+7:]
                        assert raw[size:size+3]==b'10=' and int(raw[size+3:size+6])==sum(raw[:size])%256
                        values={k.decode():v.decode() for k,v in (part.split(b'=',1) for part in raw[:-1].split(SOH))}
                        assert values['49']=='TEST' and values['56']=='QTRADE'
                        return values
            data=self.sock.recv(65536)
            if not data:raise EOFError()
            self.buffer+=data
            assert len(self.buffer)<1048576


class MockQTrade:
    def __init__(self,*,seed=None,reject=False,script=None,inbound=None,heartbeats=True,tls_context=None,logon_delay=0):
        self.seed=seed or [];self.reject=reject;self.script=script;self.inbound=inbound;self.heartbeats=heartbeats
        self.errors=[];self.logons=0;self.connections=[];self.types=[];self.test_ids=[];self.logon_checks=[]
        self.closed=threading.Event();outer=self
        class Handler(socketserver.BaseRequestHandler):
            def handle(self):
                peer=Peer(self.request);outer.connections.append(peer);peer.sock.settimeout(.1)
                try:
                    deadline=time.monotonic()+5
                    while True:
                        try:message=peer.receive();break
                        except socket.timeout:
                            if time.monotonic()>deadline:raise AssertionError('MOCK_LOGON_TIMEOUT')
                    assert message['35']=='A' and message['553']=='synthetic-login' and message['554']=='synthetic-password'
                    assert message['98']=='0' and message['1137']=='6' and message['34']=='1'
                    outer.logons+=1;outer.types.append('A');outer.logon_checks.append(True)
                    if logon_delay:time.sleep(logon_delay)
                    peer.send('A',{'98':'0','108':'1','141':'N','1137':'6','1409':'5' if outer.reject else '0'},fragments=True)
                    if outer.reject:return
                    if outer.script:outer.script(peer,outer.logons)
                    else:
                        for fields in outer.seed:peer.send('S',fields,fragments=True)
                    stamp=time.monotonic()
                    while not outer.closed.is_set():
                        if outer.heartbeats and time.monotonic()-stamp>.5:
                            peer.send('0');stamp=time.monotonic()
                        try:message=peer.receive()
                        except socket.timeout:continue
                        kind=message['35'];outer.types.append(kind)
                        assert kind in ('A','0','1','2','3','4','5'),'BUSINESS_WRITE_FORBIDDEN'
                        if kind=='1':
                            outer.test_ids.append(message['112'])
                            if outer.heartbeats:peer.send('0',{'112':message['112']})
                        if outer.inbound:outer.inbound(peer,message)
                        if kind=='5':return
                except (OSError,EOFError):pass
                except BaseException as exc:outer.errors.append(type(exc).__name__+':'+str(exc))
        class Server(socketserver.ThreadingTCPServer):
            allow_reuse_address=True;daemon_threads=True
            def get_request(self):
                sock,address=super().get_request()
                if tls_context:sock=tls_context.wrap_socket(sock,server_side=True)
                return sock,address
        self.server=Server(('127.0.0.1',0),Handler)
        self.port=self.server.server_address[1]
        self.thread=threading.Thread(target=self.server.serve_forever,kwargs={'poll_interval':.02},daemon=True)
    def __enter__(self):self.thread.start();return self
    def disconnect(self):
        for peer in self.connections:
            try:peer.sock.shutdown(socket.SHUT_RDWR)
            except OSError:pass
            try:peer.sock.close()
            except OSError:pass
    def __exit__(self,*args):
        self.closed.set();self.disconnect();self.server.shutdown();self.server.server_close();self.thread.join(3)
        assert not self.thread.is_alive()
        assert not self.errors,self.errors


def settings(port,**changes):
    from server.qt_credentials import DEFAULTS
    return {**DEFAULTS,'host':'127.0.0.1','port':port,'username':'synthetic-login','password':'synthetic-password',
            'sender_comp_id':'TEST','target_comp_id':'QTRADE','transport':'tcp','plaintext_confirmed':True,
            'heartbeat_seconds':1,'connect_timeout_seconds':2,'account_owners':{'qt-alice':'alice','qt-bob':'bob'},**changes}
