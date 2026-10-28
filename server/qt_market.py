"""Request-local QT projection and cross-source bests. No private public-cache writes."""
from copy import deepcopy
from decimal import Decimal, InvalidOperation


def order(row):
    return (row.get('flowId') or 0,str(row.get('pageKey') or row.get('bondUniCode') or row.get('code') or ''))


def page(rows, before, before_uni, limit):
    rows=sorted(rows,key=order,reverse=True)
    if before:
        rows=[r for r in rows if order(r)<(before,str(before_uni))] if before_uni else [r for r in rows if order(r)[0]<before]
    return rows[:limit]


def numeric(value):
    if value is None or isinstance(value,bool):return None
    try:result=Decimal(str(value))
    except (ValueError,InvalidOperation):return None
    return result if result.is_finite() else None


def enriched(service,owner,records):
    from .market import parse_filter
    status,floors=service._overlays(owner)
    codes=list(dict.fromkeys(row['code'] for row in records));public={}
    # Read only bonds that have QT records. The public book is never fully scanned.
    for start in range(0,len(codes),500):
        batch=codes[start:start+500]
        rows,_=service._public_query(owner,parse_filter(None),codes=batch,limit=501)
        public.update({row['code']:row for row in rows})
    result=[]
    for source in records:
        row={**deepcopy(public.get(source['code'],{})),**deepcopy(source)}
        if not row.get('bondUniCode'):row['bondUniCode']=public.get(source['code'],{}).get('bondUniCode')
        row['status']=status.get(row['code'],'待询价');row['scheme']=None
        row['floor']=deepcopy(floors.get(row['code']))
        row['pageKey']=row['rowId']
        # The four leading columns retain the user's PRIVATE floor semantics.
        # BidPx / OfferPx are counterpart prices, never internal limits.
        row['ib']=row['io']=None
        floor=row['floor'] or {};value=numeric(floor.get('value'))
        if value is not None:
            if floor.get('kind')=='yield':row['ib']=row['io']=format(value,'f')
            elif floor.get('kind')=='bp':
                valuation=numeric(row.get('val_yield'))
                if valuation is not None:
                    row['ib']=format(valuation+value/100,'f');row['io']=format(valuation-value/100,'f')
        clears=set(x for x in row['qt_settlement'].values() if x)
        row['clearing']=next(iter(clears)) if len(clears)==1 else None
        result.append(row)
    return result,public


def details(service,owner,f,*,before=0,before_uni=0,limit=50,codes=None,excluded=()):
    from .market import matches
    if service.qt_provider is None:return [],0
    records=service.qt_provider.rows(owner)
    rows,_=enriched(service,owner,records)
    wanted=set(codes) if codes is not None else None;omitted=set(excluded)
    rows=[row for row in rows if (wanted is None or row['code'] in wanted)
          and row['rowId'] not in omitted and matches(row,f)]
    return page(rows,before,before_uni,limit),len(rows)


def combined(service,owner,f,*,before=0,before_uni=0,limit=50,codes=None,excluded=()):
    from .market import matches,CHIPS
    records=service.qt_provider.rows(owner)
    if not records:return service._public_query(owner,f,before=before,before_uni=before_uni,limit=limit,codes=codes,excluded=excluded)
    records,public=enriched(service,owner,records)
    by_code={}
    for row in records:by_code.setdefault(row['code'],[]).append(row)
    combined=[];wanted=set(codes) if codes is not None else None;omitted=set(excluded)
    for code,quotes in by_code.items():
        if code in omitted or wanted is not None and code not in wanted:continue
        base=deepcopy(public.get(code))
        if base is None:
            # Static document facts are allowed, but the document's generic estimate
            # must not masquerade as a ChinaBond valuation.
            first=quotes[0]
            base={k:deepcopy(v) for k,v in first.items() if not k.startswith('qt_') and k not in ('rowId','pageKey')}
            for k in ('ob','obv','obs','oo','oov','oos','ib','io','ibv','iov'):base[k]=None
        base['rowId']=code;base['pageKey']=base.get('bondUniCode') or code
        base['flowId']=max([base.get('flowId') or 0]+[row['flowId'] for row in quotes])
        clearing=CHIPS['清算'][f['chips']['清算']] if f['chips'].get('清算') else public.get(code,{}).get('clearing')
        if not clearing:
            clearing=next((clear for row in quotes for side,clear in row['qt_settlement'].items()
                           if clear and not row['qt_compare_reasons'][side]),None)
        base['clearing']=clearing
        rejected={}
        for side,price,volume,source in [('bid','ob','obv','obs'),('ofr','oo','oov','oos')]:
            winning=None;best=numeric(base.get(price))
            for row in quotes:
                reason=row['qt_compare_reasons'][side]
                if not reason and row['qt_settlement'][side]!=clearing:reason='SETTLEMENT_NOT_COMPARABLE'
                if reason:
                    rejected[reason]=rejected.get(reason,0)+1;continue
                value=numeric(row.get(price))
                if value is None:continue
                # Yields invert prices: best bid is LOWEST yield; best offer HIGHEST.
                # Ties keep the existing broker winner, otherwise the newest QT row.
                if best is None or (value<best if side=='bid' else value>best):
                    best=value;winning=row
            if winning is not None:
                base[price]=winning[price];base[volume]=winning[volume];base[source]='QT'
                base['issueTime']=max(base.get('issueTime') or 0,winning.get('issueTime') or 0)
                base['winning_'+side]={'source':'QT','row_id':winning['rowId'],
                                      'quote_time':winning['issueTime'],'settlement':clearing,'price_kind':'MATURITY_YIELD'}
        base['qt_excluded']=rejected
        if numeric(base.get('ob')) is None and numeric(base.get('oo')) is None:continue
        if matches(base,f):combined.append(base)
    # The broker-only shard remains a bounded SQL page, excluding touched bonds.
    other,total=service._public_query(owner,f,before=before,before_uni=before_uni,limit=limit,
                                     codes=codes,excluded=tuple(set(excluded)|set(by_code)))
    total+=len(combined)
    return page(other+page(combined,before,before_uni,limit),before,before_uni,limit),total
