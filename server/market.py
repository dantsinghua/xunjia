"""Public quotes with owner-specific overlays; one filter for page/select/export.

The provider must return public rows only. No private intent from the legacy
shared quote cache is trusted. Unconfigured providers return an explicit empty
source, never synthetic prices.
"""
from __future__ import annotations

import json
import math
import re
from copy import deepcopy

from .domain import decimal

CHIPS = {
    '当前状态':['全部','待询价','询价中','待回执','发送失败','转人工','已报价','还价中','待人工确认','已成交','已暂停','未成交','已撤价','已放弃','待核对','停止中'],
    '方案':['默认方案','Z1','D1','D3','习情','我的关注'],
    '报价类型':['全部','实盘','可部分执行','意向价'],
    '清算':['全部','+0','+1','+N'], '标签':['全部','绿色债','科创债','公募','私募'],
    '类型':['全部信用','国债','政金债','中票','短融','PPN','公司债','企业债','ABS','次级债'],
    '期限':['不限','<1Y','1–3Y','3–5Y','5–7Y','7–10Y','10Y+'],
}
PRICES = ('Bid','Ofr','Bid-中债','中债-Ofr','买量','卖量')
def parse_filter(value):
    if value is None or value=='':value={}
    if isinstance(value,str):
        try:value=json.loads(value)
        except (TypeError,ValueError):raise ValueError('筛选条件须为JSON对象') from None
    if not isinstance(value,dict):raise ValueError('筛选条件无效')
    if set(value)-{'keyword','chips','prices','channel','broker','groups'}:raise ValueError('不支持的筛选字段')
    result={'keyword':'','chips':{},'prices':{},'channel':'best','broker':'','groups':['bond-all'],**value}
    if not isinstance(result['keyword'],str) or len(result['keyword'])>200:raise ValueError('关键词无效')
    if result['channel'] not in ('best','qt','broker','mm'):raise ValueError('报价通道无效')
    if not isinstance(result['broker'],str) or len(result['broker'])>80:raise ValueError('经纪商无效')
    for field in ('chips','prices'):
        if not isinstance(result[field],dict):raise ValueError('筛选条件无效')
    for name,index in result['chips'].items():
        if name not in CHIPS or type(index) is not int or not 0<=index<len(CHIPS[name]):raise ValueError('筛选选项无效')
    for name,bounds in result['prices'].items():
        if name not in PRICES or not isinstance(bounds,dict) or set(bounds)-{'lo','hi'}:raise ValueError('价格区间无效')
        parsed=[decimal(bounds[key]) if bounds.get(key) not in (None,'') else None for key in ('lo','hi')]
        if all(x is not None for x in parsed) and parsed[0]>parsed[1]:raise ValueError('区间下限大于上限')
    groups=result['groups']
    if not isinstance(groups,list) or len(groups)>50 or any(not isinstance(g,str) or len(g)>80 for g in groups):raise ValueError('订阅组无效')
    return result


def number(value):
    try:
        n=float(value)
        return n if math.isfinite(n) else None
    except (TypeError,ValueError):return None


def maturity_years(days, term):
    value=number(days)
    if value is not None:return value/365
    match=re.search(r'(\d+(?:\.\d+)?)\s*Y',str(term or ''),re.I)
    return number(match.group(1)) if match else None


def matches(row,f):
    keyword=f['keyword'].strip().lower()
    if keyword and not any(keyword in str(row.get(k) or '').lower() for k in ('code','name','issuer')):return False
    def pick(name):return CHIPS[name][f['chips'].get(name,0)]
    for name,field in [('当前状态','status'),('方案','scheme'),('报价类型','firmness'),('清算','clearing'),('类型','bondType')]:
        if f['chips'].get(name,0) and row.get(field)!=pick(name):return False
    tags=row.get('tags') or []
    if not isinstance(tags,list):tags=[]
    if f['chips'].get('标签',0):
        aliases={'绿色债':['绿色债','绿'],'科创债':['科创债','科创']}.get(pick('标签'),[pick('标签')])
        if not any(tag in tags or tag in str(row.get('name') or '') for tag in aliases):return False
    years=maturity_years(row.get('residual_days'),row.get('term'))
    if f['chips'].get('期限',0):
        lo,hi=[(0,math.inf),(0,1),(1,3),(3,5),(5,7),(7,10),(10,math.inf)][f['chips']['期限']]
        if years is None or not lo<=years<hi:return False
    group_dims={}
    for group in f['groups']:
        if group=='bond-all':continue
        dim,_,value=group.partition('_')
        accepted=False
        if dim=='bond-type':accepted=row.get('bondType')==value
        elif dim=='bond-rating':accepted=row.get('bondRating')==value
        elif dim=='bond-prov':accepted=row.get('prov')==value
        elif dim=='bond-clearing':accepted=value in ('T0','T1','TN') and row.get('clearing')=={'T0':'+0','T1':'+1','TN':'+N'}[value]
        elif dim=='bond-tenor':
            intervals={'lt1':(0,1),'1to3':(1,3),'3to5':(3,5),'5to7':(5,7),'7to10':(7,10),'gt10':(10,math.inf)}
            lo,hi=intervals.get(value,(math.inf,math.inf));accepted=years is not None and lo<=years<hi
        group_dims[dim]=group_dims.get(dim,False) or accepted
    if not all(group_dims.values()):return False
    bid,ofr,val=number(row.get('ob')),number(row.get('oo')),number(row.get('val_yield'))
    values={'Bid':bid,'Ofr':ofr,'Bid-中债':None if bid is None or val is None else (bid-val)*100,
            '中债-Ofr':None if ofr is None or val is None else (val-ofr)*100,'买量':number(row.get('obv')),'卖量':number(row.get('oov'))}
    for key,bounds in f['prices'].items():
        for part in ('lo','hi'):
            if bounds.get(part) in (None,''):continue
            bound=float(decimal(bounds[part]));v=values[key]
            if v is None or (part=='lo' and v<bound-1e-9) or (part=='hi' and v>bound+1e-9):return False
    return True


class MarketService:
    def __init__(self,repository,*,rows_provider=None,static_provider=None,detail_provider=None,brokers_provider=None):
        self.repo=repository
        self.rows_provider=rows_provider
        self.static_provider=static_provider
        self.detail_provider=detail_provider
        self.brokers_provider=brokers_provider

    MAX_SELECTION = 100000

    def _overlays(self,owner):
        return self.repo.market_overlays(owner)

    @staticmethod
    def _project(sources,status,floors):
        # Construct a whitelist projection: source extras cannot leak intent.
        keys=('code','name','bondUniCode','flowId','issueTime','ob','obv','obs','oo','oov','oos','val_yield','net_price',
              'dur','coupon','valDate','bondType','bondRating','issuerRating','implied','issuer','industry','prov','nature',
              'term','residual_days','tags','firmness','clearing')
        result=[]
        for source in sources:
            row={k:deepcopy(source.get(k)) if source.get(k) is None or isinstance(source.get(k),(str,int,float,bool)) or k=='tags' and isinstance(source.get(k),list) and all(isinstance(t,str) for t in source[k]) else None for k in keys}
            if not row['code'] or not row['name']:continue
            row.update(status=status.get(row['code'],'待询价'),ib=None,io=None,ibv=None,iov=None,scheme=None)
            vy,np=row['val_yield'],row['net_price']
            row['val']=f'{vy}/{np}' if vy is not None and np is not None else str(vy) if vy is not None else None
            own=floors.get(row['code'])
            if own:row['floor']=deepcopy(own)
            result.append(row)
        return result

    def _query(self,owner,f,*,before=0,before_uni=0,limit=50,codes=None,excluded=()):
        if f['channel'] in ('qt','mm') or self.rows_provider is None:return [],0
        status,floors=self._overlays(owner)
        provider=getattr(self.rows_provider,'__self__',None)
        query=getattr(provider,'query',None)
        if callable(query):
            sources,total=query(f,status,before=before,before_uni=before_uni,limit=limit,codes=codes,excluded=excluded)
            return self._project(sources,status,floors),total
        # Explicitly injected iterable providers support small offline fixtures.
        # The production QB bound provider always takes the query path above.
        rows=self._project(self.rows_provider(f['channel'],f['broker']),status,floors)
        wanted=set(codes) if codes is not None else None
        omitted=set(excluded)
        rows=[r for r in rows if matches(r,f) and (wanted is None or r['code'] in wanted) and r['code'] not in omitted]
        rows.sort(key=lambda r:(r.get('flowId') or 0,str(r['bondUniCode'] or '')),reverse=True)
        total=len(rows)
        if before:
            rows=[r for r in rows if (r.get('flowId') or 0)<before or ((r.get('flowId') or 0)==before and before_uni and str(r['bondUniCode'])<str(before_uni))]
        return rows[:limit],total

    def rows(self,owner,f):
        rows,total=self._query(owner,f,limit=self.MAX_SELECTION+1)
        if total>self.MAX_SELECTION:raise ValueError('选择或导出超过10万行，请缩小范围')
        return rows

    def page(self,owner,*,filter_spec=None,before=0,before_uni=0,limit=50):
        f=parse_filter(filter_spec)
        if type(limit) is not int or not 1<=limit<=500:raise ValueError('每页须1至500行')
        rows,total=self._query(owner,f,before=before,before_uni=before_uni,limit=limit+1)
        picked=rows[:limit]
        reason={'qt':'QT意向报价尚未接入','mm':'做市商报价尚未接入'}.get(f['channel'])
        if self.rows_provider is None:reason='行情数据源尚未配置'
        return {'ok':True,'data':picked,'total':total,'hasMore':len(rows)>limit,'nextBefore':picked[-1]['flowId'] if picked else 0,
                'nextUni':picked[-1]['bondUniCode'] if picked else 0,'limit':limit,'emptyReason':reason,'source':'public' if self.rows_provider else 'not_configured','groups':f['groups']}

    def resolve(self,owner,selection):
        if not isinstance(selection,dict):raise ValueError('选择条件无效')
        f=parse_filter(selection.get('filter') or {'groups':selection.get('groups') or ['bond-all']})
        excluded=selection.get('except',[])
        if not isinstance(excluded,list) or len(excluded)>self.MAX_SELECTION or any(not isinstance(x,str) for x in excluded):raise ValueError('排除列表无效')
        if selection.get('selectAll') is True:
            rows,total=self._query(owner,f,excluded=excluded,limit=self.MAX_SELECTION+1)
            if total>self.MAX_SELECTION:raise ValueError('选择或导出超过10万行，请缩小范围')
            return rows
        codes=selection.get('windcodes',[])
        if not isinstance(codes,list) or len(codes)>500 or any(not isinstance(x,str) for x in codes):raise ValueError('券列表无效')
        if not codes:return []
        rows,total=self._query(owner,f,codes=list(dict.fromkeys(codes)),limit=self.MAX_SELECTION+1)
        if total>self.MAX_SELECTION:raise ValueError('券代码对应行情记录超过上限')
        return rows
