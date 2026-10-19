"""User examples: omitted BP units must work in the actual generated DSL/core."""
from functools import lru_cache
from types import SimpleNamespace
import json
import pytest
from dify.negotiation_v22.build import build
from dify.negotiation_v22.slot_wire import decode_slot_observation
from server.negotiation.slots import collect_candidates
from server.reference_data import ReferenceSnapshot
from test_reference_window_policy import Desk, DAY, OWNER
@lru_cache(maxsize=2)
def contract(version):
    graph=build('analysis',version=version,provider='synthetic',model='synthetic')['workflow']['graph']
    ns={};exec(next(n['data']['code'] for n in graph['nodes'] if n['id']=='contract'),ns)
    return ns['main']
@pytest.mark.parametrize('version',['0.15.3','1.17.1'])
@pytest.mark.parametrize('phrase,valuation,expected',[
    ('估值-5收5000万','2.5818','2.53'),
    ('估值-5BP收5000万','2.5818','2.53'),
    ('中债估值减5收5000万','2.5818','2.53'),
    ('估值加5出5000万','2.9872','3.04'),
    ('中债＋５ＢＰ出5000万','2.9872','3.04'),
    ('估值下浮5基点收5000万','2.5818','2.53'),
])
def test_omitted_bp_uses_full_valuation_then_rounding(tmp_path,version,phrase,valuation,expected):
    c=Desk(tmp_path/'bp.sqlite')
    c.service.reference_provider=SimpleNamespace(snapshot=lambda:ReferenceSnapshot('synthetic-new-bp',DAY,
        bonds=[{'code':'SYN-A','valuation_yield_pct':valuation,'valuation_date':DAY}]))
    c.service.calendar=SimpleNamespace(latest_valuation_day=lambda _:DAY)
    incoming=c.inbound('SYN-A '+phrase)
    side='bid' if '收' in phrase else 'ofr'
    def model(snapshot):
        context={**snapshot,'schema_version':'2.2','stage':'analysis'}
        candidates=collect_candidates(context)
        price=next(v for v in candidates if 'CHINABOND_BP' in v['roles'])
        quantity=next(v for v in candidates if v['roles']==['QUANTITY'])
        raw=c.envelope(context,[{'side':side,'slots':{'price':price['id'],'volume_wan':quantity['id']},
          'meaning':{'intent':'QUOTE','validity':'ACTIVE','negotiation_space':'UNKNOWN','reason':'本人明确中债偏移报价'}}])
        return json.loads(contract(version)(json.dumps(raw),json.dumps(context))['result'])
    c.flows.analyze=model
    result=c.engine.analyze_thread(OWNER,c.thread,force=True)
    assert result['accepted'] and not result['rejected'],result
    saved=c.store.quote(OWNER,c.member,side)
    assert saved.price.value==expected
    assert saved.volume_wan=='5000'
@pytest.mark.parametrize('phrase',['估值-5收5000万','中债估值减5收5000万'])
def test_offset_cannot_be_accepted_as_negative_absolute_yield(tmp_path,phrase):
    c=Desk(tmp_path/'wrong.sqlite');ref=c.inbound('SYN-A '+phrase)
    obs=c.observation(ref,price={'kind':'YIELD','value':'-5'},volume_wan='5000',assertion='QUOTE',validity='ACTIVE')
    obs['side']='bid'
    result=c.analyze([obs])
    assert not result['accepted'] and result['rejected'],result
    assert c.store.quote(OWNER,c.member,'bid').price is None
@pytest.mark.parametrize('text,kind',[
    ('SYN-A -0.05收5000万','YIELD'),
    ('SYN-A 2.20，0.5亿','YIELD'),
    ('SYN-A 净价34.19出5000万','NET_PRICE'),
])
def test_non_bp_prices_are_not_forced_into_offsets(text,kind):
    candidates=collect_candidates({'bond_code':'SYN-A','messages':[{'message_id':'synthetic','text':text,'direction':'in'}]})
    assert not any('CHINABOND_BP' in row['roles'] for row in candidates)
    assert any(kind in row['roles'] for row in candidates)