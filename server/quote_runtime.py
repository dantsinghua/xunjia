"""Explicit public QB cache adapter and authenticated quote websocket."""
from __future__ import annotations
import asyncio
import json
import time
from contextlib import suppress
from urllib.parse import urlparse

from fastapi import WebSocket, WebSocketDisconnect

PUBLIC_DELTA=(('best_bid','bb'),('best_bid_vol','bbv'),('best_bid_side','bbs'),('best_ofr','bo'),('best_ofr_vol','bov'),('best_ofr_side','bos'),('firmness','fm'),('clearing','cl'))


def public_static(raw):
    from server.market_cache import STATIC_COLS
    if not isinstance(raw,dict):return {}
    return {k:v for k,v in raw.items() if k in STATIC_COLS and (v is None or isinstance(v,(str,int,float,bool)) or k=='tags' and isinstance(v,list) and all(isinstance(t,str) for t in v))}


def public_row(latest,static):
    return {'code':static['windcode'],'name':static['sec_name'],'bondUniCode':str(static['bond_uni_code']),
            'flowId':latest['flow_id'],'issueTime':latest['issue_time'],
            **{key:latest.get(col) for col,key in [('best_bid','ob'),('best_bid_vol','obv'),('best_bid_side','obs'),('best_ofr','oo'),('best_ofr_vol','oov'),('best_ofr_side','oos'),('firmness','firmness'),('clearing','clearing')]},
            **{key:static.get(col) for col,key in [('residual_maturity','term'),('residual_days','residual_days'),('bond_type','bondType'),('bond_rating','bondRating'),('issuer_rating','issuerRating'),('implied_rating','implied'),('issuer','issuer'),('industry','industry'),('prov','prov'),('nature','nature'),('tags','tags'),('val_yield','val_yield'),('net_price','net_price'),('val_date','valDate'),('mod_dur','dur'),('coupon','coupon')]}}


class QBMarket:
    """Created only when the administrator explicitly enables the live source.

    Reuses the verified incremental QB reader. It never launches a collector or
    authenticates a market account on application import.
    """
    def __init__(self,cache_path):
        from server import market_cache as db
        from server.qb_source import QBSource
        db.init_db(None,path=cache_path)
        self.db=db;self.source=QBSource();self.groups={}

    MAX_BROKER_BOOK = 20000

    def rows(self,channel,broker):
        # Compatibility for callers that explicitly need an export-sized set.
        from .market import parse_filter
        rows,total=self.query(parse_filter({'channel':channel,'broker':broker}),{},limit=100001)
        if total>100000:raise ValueError('行情超过10万行，请缩小范围')
        return rows

    def query(self,f,statuses,*,before=0,before_uni=0,limit=50,codes=None,excluded=()):
        if f['channel'] in ('qt','mm'):return [],0
        if f['channel']=='broker' and f['broker'] and f['broker']!='best':
            return self._query_broker(f,statuses,before=before,before_uni=before_uni,
                                      limit=limit,codes=codes,excluded=excluded)
        rows,total=self.db.query_public(f,statuses,before=before,before_uni=before_uni,
                                        limit=limit,codes=codes,excluded=excluded)
        return [public_row(r['latest'],r['static']) for r in rows],total

    def _query_broker(self,f,statuses,*,before,before_uni,limit,codes,excluded):
        # Per-broker quotes live in QBSource's current-day book, not quote_latest.
        # Hold its RLock while making a public snapshot; fail explicitly beyond
        # capacity instead of returning an incomplete selection or stale bests.
        from .market import matches
        try:broker_id=int(f['broker'])
        except ValueError:raise ValueError('经纪商编号无效') from None
        with self.source._lock:
            if len(self.source.book)>self.MAX_BROKER_BOOK:
                raise ValueError('单经纪商行情超过2万券容量，需扩容书簿查询后重试')
            records,*_=self.source.page_one_broker(broker_id,0,0,self.MAX_BROKER_BOOK+1,['bond-all'])
            sources=[public_row({**r['best'],'flow_id':r['best']['issue_time']},r['static']) for r in records]
        wanted=set(codes) if codes is not None else None
        omitted=set(excluded)
        rows=[r for r in sources if (wanted is None or r['code'] in wanted) and r['code'] not in omitted
              and matches({**r,'status':statuses.get(r['code'],'待询价'),'scheme':None},f)]
        rows.sort(key=lambda r:(r['flowId'],str(r['bondUniCode'])),reverse=True)
        total=len(rows)
        if before:
            rows=[r for r in rows if r['flowId']<before or (r['flowId']==before and before_uni and str(r['bondUniCode'])<str(before_uni))]
        return rows[:limit],total

    def valuation(self, code):
        """Return only explicitly labelled ChinaBond valuation fields in Inno.
        Neither best_bid/best_ofr nor an arbitrary broker quote can become our
        internal valuation. Date/value provenance stays separate from peers.
        """
        from .negotiation.prices import Valuation, iso_day, number, InvalidFact
        import hashlib
        from datetime import date
        with self.source._lock:
            uni=self.source.code2uni.get(code)
            rows=list(self.source.book.get(uni,{}).values()) if uni is not None else []
            candidates=[]
            for row in rows:
                day=row.get('cnbd_date');value=row.get('cnbd_yield_mat')
                if day is None or value is None:continue
                try:
                    day=iso_day(day.isoformat() if isinstance(day,date) else str(day))
                    value=format(number(str(value)),'f')
                    stamp=row.get('update_time')
                    stamp=stamp.timestamp() if hasattr(stamp,'timestamp') else 0
                    sequence=int(row.get('seq_ns') or row.get('id') or 0)
                except (ValueError,TypeError,OverflowError):continue
                candidates.append((day,stamp,sequence,value))
        if not candidates:return None
        newest=max((day,stamp,seq) for day,stamp,seq,_value in candidates)
        values={number(value) for day,stamp,seq,value in candidates if (day,stamp,seq)==newest}
        if len(values)!=1:
            raise InvalidFact('INNO_CHINABOND_REVISION_CONFLICT:'+str(code)+':'+newest[0])
        day,_stamp,_seq=newest
        value=format(next(iter(values)),'f')
        digest=hashlib.sha256(json.dumps([code,day,value],separators=(',',':')).encode()).hexdigest()
        return Valuation(code,day,value,'innodealing-cnbd:'+digest)
    def poll(self):
        output=[]
        before=getattr(self.source,'last_id',0)
        incoming=self.source.poll()
        # Includes valuation-only updates even when the best price did not move.
        self.valuation_changed=getattr(self.source,'last_id',0)!=before
        for fid,q in incoming:
            u=str(q['bond_uni_code']);rows=self.db.static_by_codes([u])
            if not rows:continue
            static=rows[0];new=self.db.groups_of(static,q.get('clearing'));old=self.groups.get(u,new)
            self.groups[u]=new
            delta={'f':fid,'u':u,'t':q['issue_time'],'s':static}
            for column,key in PUBLIC_DELTA:
                if column in q:delta[key]=q[column]
            output.append((delta,old|new))
        return output


class QuoteHub:
    def __init__(self):self.clients={}

    def publish(self,delta,groups):
        # Defensive projection even if an adapter accidentally includes intent.
        allowed={'f','u','t','s','c'}|{key for _,key in PUBLIC_DELTA}
        clean={k:v for k,v in delta.items() if k in allowed}
        if 's' in clean:clean['s']=public_static(clean['s'])
        for state in tuple(self.clients.values()):
            for group in state['groups'] & set(groups):
                bucket=state['pending'].setdefault(group,{})
                if clean['f']>bucket.get(clean['u'],{}).get('f',-1):bucket[clean['u']]=clean
                if len(bucket)>5000:
                    state['overflow']=True;bucket.clear()

    async def websocket(self,ws:WebSocket,auth,allowed_origins=()):
        origin=ws.headers.get('origin')
        if origin:
            same=urlparse(origin).netloc==ws.headers.get('host') and urlparse(origin).scheme in ('https','http')
            if not same and origin not in allowed_origins:
                await ws.close(code=4403);return
        await ws.accept()
        try:
            first=await asyncio.wait_for(ws.receive_json(),timeout=10)
            token=first.get('token') if isinstance(first,dict) and first.get('op')=='auth' else None
            if not isinstance(token,str) or not auth.verify_token(token):
                await ws.close(code=4401);return
        except (ValueError,asyncio.TimeoutError,WebSocketDisconnect):
            await ws.close(code=4401);return
        state={'groups':set(),'pending':{},'overflow':False};self.clients[ws]=state
        await ws.send_json({'op':'auth','ok':True})
        send_lock=asyncio.Lock()
        async def flush():
            while True:
                await asyncio.sleep(.2)
                if not auth.verify_token(token):await ws.close(code=4401);return
                if state['overflow']:await ws.close(code=1013);return
                pending,state['pending']=state['pending'],{}
                async with send_lock:
                    for group,rows in pending.items():
                        if group in state['groups']:
                            await ws.send_json({'op':'batch','group':group,'sendTime':int(time.time()*1000),'items':sorted(rows.values(),key=lambda r:r['f'])})
        task=asyncio.create_task(flush())
        try:
            while True:
                message=await ws.receive_text()
                if len(message)>16384:await ws.close(code=1009);break
                data=json.loads(message)
                if not isinstance(data,dict):continue
                op=data.get('op')
                if op in ('sub','unsub'):
                    groups=data.get('groups')
                    if not isinstance(groups,list) or len(groups)>50 or any(not isinstance(g,str) or not g.startswith('bond-') or len(g)>80 for g in groups):
                        await ws.close(code=4400);break
                    if op=='sub':
                        if len(state['groups']|set(groups))>50:await ws.close(code=4400);break
                        state['groups'].update(groups)
                    else:
                        state['groups'].difference_update(groups)
                        for g in groups:state['pending'].pop(g,None)
                elif op=='ping':
                    async with send_lock:await ws.send_json({'op':'pong'})
        except (ValueError,WebSocketDisconnect):pass
        finally:
            task.cancel()
            with suppress(asyncio.CancelledError,Exception):await task
            self.clients.pop(ws,None)
