"""Snapshot input contract and real HTTPX passthrough, never live credentials."""
import json
import zlib
import httpx
import pytest
from collector import market_snapshot as snapshot


@pytest.mark.parametrize('kind',['quotes','trades'])
@pytest.mark.parametrize('gateway',['','172.26.64.36'])
@pytest.mark.parametrize('compressed',[False,True])
def test_snapshot_uses_known_query_and_verified_original_host(monkeypatch,kind,gateway,compressed):
    monkeypatch.setenv('XUNJIA_EGRESS_GATEWAY',gateway)
    raw=b'\x1a\x00'
    seen=[]
    def response(request):
        seen.append(request)
        assert request.method=='POST'
        assert request.headers['host']=='rest.innodealing.com'
        assert request.headers['cookie']=='sid=synthetic'
        body=json.loads(request.content)
        assert body['isSnapshot']==0
        assert ('validQuoteStatus' in body)==(kind=='quotes')
        return httpx.Response(200,content=zlib.compress(raw) if compressed else raw)
    def factory(**options):
        assert options['verify'] and not options['trust_env'] and options['proxy'] is None
        return httpx.Client(transport=httpx.MockTransport(response),**options)
    decoder='parse_best_quotes' if kind=='quotes' else 'parse_trades'
    def parse(value):
        assert value==raw
        return [{'source':'actual-decoder-input'}]
    monkeypatch.setattr(snapshot.qb_apollo,decoder,parse)
    assert snapshot.fetch_snapshot(kind,'sid=synthetic','synthetic-device',client_factory=factory)==[{'source':'actual-decoder-input'}]
    assert len(seen)==1
    if gateway:
        assert seen[0].url.host==gateway and seen[0].url.port==12443
        assert seen[0].extensions['sni_hostname']=='rest.innodealing.com'


@pytest.mark.parametrize('body',[b'<html>login</html>',b'{"code":401}',b'[]',b''])
def test_rejected_or_empty_snapshot_is_never_decoded_as_market_rows(monkeypatch,body):
    def factory(**options):
        return httpx.Client(transport=httpx.MockTransport(lambda _:httpx.Response(200,content=body)),**options)
    with pytest.raises(ValueError,match='SNAPSHOT_RESPONSE_INVALID'):
        snapshot.fetch_snapshot('trades','synthetic','device',client_factory=factory)


def test_compression_bomb_is_bounded(monkeypatch):
    monkeypatch.setattr(snapshot,'LIMIT',128)
    def factory(**options):
        return httpx.Client(transport=httpx.MockTransport(lambda _:httpx.Response(200,content=zlib.compress(b'x'*2000))),**options)
    with pytest.raises(ValueError,match='COMPRESSION_INVALID'):
        snapshot.fetch_snapshot('quotes','synthetic','device',client_factory=factory)
