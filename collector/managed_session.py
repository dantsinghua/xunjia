"""Managed live quote session. Uses the existing wire decoder and database writer."""
from __future__ import annotations
import asyncio
import time
from contextlib import suppress
from collector import qb_ws,qb_apollo
from collector.qb_gateway import WS,authenticate,merge_sid,QuoteAuthenticationError
from collector.store import Store
from server.quote_probe import market_connect
async def _join_io(call,*args):
    task=asyncio.create_task(asyncio.to_thread(call,*args))
    try:return await asyncio.shield(task)
    except asyncio.CancelledError:
        # Finish an already-issued database write before closing its connection.
        with suppress(Exception):await task
        raise
async def collect(values,update,database):
    cookie=merge_sid(values['cookie'],values['sid'])
    tok=await _join_io(authenticate,cookie,values['device_id'])
    token=tok.get('t') or tok.get('token')
    if not isinstance(token,str) or not token:raise QuoteAuthenticationError('QUOTE_TOKEN_MISSING')
    expiration=tok.get('ea')
    deadline=float(expiration)/1000-60 if isinstance(expiration,(int,float)) and expiration else None
    if deadline and deadline<=time.time():raise QuoteAuthenticationError('QUOTE_TOKEN_EXPIRED')
    holder=[]
    def open_store():
        import pymysql
        store=Store(False)
        store.conn=pymysql.connect(**database)
        holder.append(store)
        return store
    stats={'quotes_received':0,'quotes_written':0,'trades_written':0,'decode_errors':0,'last_quote_at':None,'last_frame_at':None}
    heartbeat=None
    try:
        store=await _join_io(open_store)
        async with market_connect(qb_ws.ws_url(WS,token),cookie,token,15) as ws:
            groups=[f'{kind}_{broker}' for kind in (1,5) for broker in (1,2,3,6,7,8,9)]
            await ws.send(qb_ws.encode_packet(qb_ws.PT_EVENT,qb_ws.EV_JOIN_GROUP,qb_ws.encode_join_group(groups),ack_id=1))
            update(state='streaming',**stats)
            async def beat():
                while True:
                    await asyncio.sleep(20)
                    await ws.send(qb_ws.encode_packet(qb_ws.PT_PING))
            heartbeat=asyncio.create_task(beat())
            while deadline is None or time.time()<deadline:
                if heartbeat.done():heartbeat.result()
                try:raw=await asyncio.wait_for(ws.recv(),timeout=5)
                except asyncio.TimeoutError:continue
                stats['last_frame_at']=time.time()
                if isinstance(raw,str):continue
                try:
                    packet=qb_ws.decode_packet(raw)
                    if packet['eventType']!=qb_ws.EV_GROUP_MESSAGE_NOTIFICATION:continue
                    group=qb_ws.decode_group_message(packet['data'])
                    payload=group['messageContent'];kind=group['messageType']
                    if not payload or kind not in (1,5):continue
                    row=qb_apollo.parse_push_quote(payload) if kind==1 else qb_apollo.parse_push_trade(payload)
                    if not row:continue
                except (ValueError,TypeError,IndexError,KeyError,OverflowError):
                    stats['decode_errors']+=1;update(**stats);continue
                if kind==1:stats['quotes_received']+=1
                written=await _join_io(store.save,qb_apollo.BEST_QUOTES if kind==1 else qb_apollo.TRADES,[row])
                if kind==1:
                    stats['quotes_written']+=written
                    stats['last_quote_at']=time.time()
                else:stats['trades_written']+=written
                update(**stats)
    finally:
        if heartbeat:
            heartbeat.cancel()
            with suppress(asyncio.CancelledError,Exception):await heartbeat
        for store in holder:
            def close():
                with store._lock:
                    if store.conn:store.conn.close();store.conn=None
            await _join_io(close)
