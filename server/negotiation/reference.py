"""Read-only, configurable field mapping for the future site reference provider.
No assumed vendor DB names, credentials or fallback market numbers.
"""
from __future__ import annotations
from copy import deepcopy
from pathlib import Path
import json
import threading
import time
from urllib.parse import urlsplit
import urllib.request
from ..reference_data import ReferenceSnapshot,ReferenceDataUnavailable
from .prices import iso_day
class HTTPReferenceProvider:
    def __init__(self,config,*,opener=None,clock=time.time):
        if not isinstance(config,dict) or set(config)-{'snapshot_url','calendar_url','timeout_seconds','cache_seconds','field_maps'}:
            raise ValueError('Invalid reference-source configuration fields')
        for key in ('snapshot_url','calendar_url'):
            value=config.get(key)
            if value and (urlsplit(value).scheme not in ('http','https') or not urlsplit(value).hostname):raise ValueError('Reference URL must be http(s)')
        if not config.get('snapshot_url'):raise ValueError('Reference snapshot URL not supplied')
        self.config=config;self.opener=opener or urllib.request.build_opener();self.clock=clock
        self._lock=threading.Lock();self._cache={}
        self.timeout=config.get('timeout_seconds',10);self.ttl=config.get('cache_seconds',5)
        if type(self.timeout) not in (int,float) or not 0<self.timeout<=30:raise ValueError('Reference timeout must be 0..30 seconds')
        if type(self.ttl) not in (int,float) or not 0<=self.ttl<=60:raise ValueError('Reference cache must be 0..60 seconds')
    @classmethod
    def from_file(cls,path):return cls(json.loads(Path(path).read_text(encoding='utf8')))
    def invalidate_valuation_cache(self):
        """The bounded recovery poll must see a restored source, not a TTL hit."""
        with self._lock:
            self._cache.pop('snapshot_url',None)
            self._cache.pop('calendar_url',None)
    def _read(self,key):
        url=self.config.get(key)
        if not url:raise ReferenceDataUnavailable('NOT_CONFIGURED:'+key)
        now=self.clock()
        with self._lock:
            old=self._cache.get(key)
            if old and now-old[0]<self.ttl:return deepcopy(old[1])
        try:
            with self.opener.open(urllib.request.Request(url,headers={'Accept':'application/json'}),timeout=self.timeout) as response:
                raw=response.read(8*1024*1024+1)
                if response.status!=200 or len(raw)>8*1024*1024:raise ValueError()
            value=json.loads(raw)
            if not isinstance(value,dict):raise ValueError()
        except Exception as exc:raise ReferenceDataUnavailable('SOURCE_UNAVAILABLE:'+key) from exc
        with self._lock:self._cache[key]=(now,value)
        return deepcopy(value)
    def _rows(self,name,rows):
        if not isinstance(rows,list):raise ReferenceDataUnavailable('FIELD_MAPPING_INVALID:'+name)
        mapping=self.config.get('field_maps',{}).get(name,{})
        if not isinstance(mapping,dict):raise ReferenceDataUnavailable('FIELD_MAPPING_INVALID:'+name)
        result=[]
        for row in rows:
            if not isinstance(row,dict):raise ReferenceDataUnavailable('FIELD_MAPPING_INVALID:'+name)
            if not mapping:result.append(row);continue
            value={}
            for target,path in mapping.items():
                field=row
                try:
                    for part in path.split('.'):field=field[part]
                except (KeyError,TypeError):field=None
                value[target]=field
            result.append(value)
        return result
    def snapshot(self):
        value=self._read('snapshot_url')
        try:
            sid=value['snapshot_id'];day=iso_day(value['as_of'])
            if not isinstance(sid,str) or not sid:raise ValueError()
            bonds=self._rows('bonds',value.get('bonds',[]));issuers=self._rows('issuers',value.get('issuers',[]))
            terms=value.get('terms',{})
            if not isinstance(terms,dict) or not isinstance(value.get('sources',[]),list):raise ValueError()
            return ReferenceSnapshot(sid,day,value.get('sources',[]),bonds,issuers,terms)
        except (ValueError,KeyError,TypeError) as exc:raise ReferenceDataUnavailable('REFERENCE_SNAPSHOT_CONTRACT_INVALID') from exc
    def trading_day(self,_stamp):
        value=self._read('calendar_url')
        try:return iso_day(value['trading_day'])
        except (ValueError,KeyError) as exc:raise ReferenceDataUnavailable('TRADING_CALENDAR_INVALID') from exc
    def latest_valuation_day(self,_stamp):
        value=self._read('calendar_url')
        try:return iso_day(value['latest_chinabond_trading_day'])
        except (ValueError,KeyError) as exc:raise ReferenceDataUnavailable('VALUATION_CALENDAR_INVALID') from exc
    def settlement_date(self,trade_day,term):
        if term=='T+0':return iso_day(trade_day)
        if term!='T+1':raise ReferenceDataUnavailable('UNSUPPORTED_SETTLEMENT_TERM')
        value=self._read('calendar_url')
        try:return iso_day(value['settlement_dates'][iso_day(trade_day)]['T+1'])
        except (ValueError,KeyError,TypeError) as exc:raise ReferenceDataUnavailable('SETTLEMENT_CALENDAR_NOT_CONFIGURED') from exc
