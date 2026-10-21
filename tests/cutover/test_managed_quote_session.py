"""Existing decoder/writer exercised through managed collection; no external traffic."""
import asyncio
import struct
import threading
import time
import pytest
import pymysql
from collector import managed_session as managed,qb_ws
from collector.store import Q_COLS
def integer(n,v):return qb_ws._varint(n<<3)+qb_ws._varint(v)
def double(n,v):return qb_ws._varint((n<<3)|1)+struct.pack('<d',v)
def data(n,value):return qb_ws._varint((n<<3)|2)+qb_ws._varint(len(value))+value
@pytest.mark.parametrize('write_fails',[False,True])
def test_live_wire_decode_store_and_cleanup(monkeypatch,write_fails):
    captured=[];closed={'ws':False,'db':False};written=threading.Event();updates=[]
    quote=data(2,b'2_100')+integer(4,100)+integer(5,2)+integer(59,1)+integer(16,3)+double(14,2.5)+double(23,500)+integer(30,1)+double(28,99.8)+integer(56,int(time.time()*1000))
    packet=qb_ws.encode_packet(qb_ws.PT_EVENT,qb_ws.EV_GROUP_MESSAGE_NOTIFICATION,data(3,b'1_2')+integer(4,1)+data(5,quote))
    class Cursor:
        def __enter__(self):return self
        def __exit__(self,*args):return False
        def executemany(self,sql,rows):
            if write_fails:raise RuntimeError('synthetic write failure')
            captured.extend(rows);written.set();return len(rows)
    class DB:
        def cursor(self):return Cursor()
        def ping(self,**kw):pass
        def begin(self):pass
        def commit(self):pass
        def rollback(self):pass
        def close(self):closed['db']=True
    class WS:
        first=True
        sent=[]
        async def __aenter__(self):return self
        async def __aexit__(self,*args):closed['ws']=True
        async def send(self,blob):self.sent.append(blob)
        async def recv(self):
            if self.first:self.first=False;return packet
            await asyncio.Event().wait()
    ws=WS()
    monkeypatch.setattr(managed,'authenticate',lambda *args:{'t':'synthetic-session-token','ea':int((time.time()+600)*1000)})
    monkeypatch.setattr(managed,'market_connect',lambda *args:ws)
    monkeypatch.setattr(pymysql,'connect',lambda **kwargs:DB())
    async def run():
        task=asyncio.create_task(managed.collect({'cookie':'sid=synthetic','sid':'','device_id':'synthetic-device'},lambda **kw:updates.append(kw),{}))
        if write_fails:
            with pytest.raises(RuntimeError,match='synthetic write'):await asyncio.wait_for(task,3)
        else:
            for _ in range(100):
                if written.is_set() and any(v.get('quotes_written')==1 for v in updates):break
                await asyncio.sleep(.02)
            else:pytest.fail('Managed session did not write quote')
            task.cancel()
            with pytest.raises(asyncio.CancelledError):await task
    asyncio.run(run())
    assert closed=={'ws':True,'db':True}
    assert ws.sent and qb_ws.decode_packet(ws.sent[0])['eventType']==qb_ws.EV_JOIN_GROUP
    if not write_fails:
        row=dict(zip(Q_COLS,captured[0]));assert row['bid_yield']==2.5 and row['bid_quote_type']==3
        assert row['ofr_net_price']==99.8 and row['ofr_yield'] is None and row['ofr_quote_type']==1
        assert row['cnbd_date'] is None
    else:assert not captured and not any(v.get('quotes_written',0)>0 for v in updates)