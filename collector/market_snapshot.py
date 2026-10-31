"""Read existing Apollo day snapshots with the same verified site-egress route."""
import json
import zlib
import httpx
from collector import qb_apollo
from collector.qb_gateway import API, FE_VER
from collector.qb_sign import sign_params
from relay.site_egress import httpx_request

LIMIT = 32 * 1024 * 1024


def fetch_snapshot(kind, cookie, device_id, *, client_factory=None):
    if kind not in ('quotes', 'trades'):raise ValueError('SNAPSHOT_KIND_INVALID')
    path = qb_apollo.BEST_QUOTES if kind == 'quotes' else qb_apollo.TRADES
    url = API.rstrip('/') + '/onshore-broker-quotation-apollo' + path
    headers = {'Cookie':cookie,'Content-Type':'application/json',
               'Origin':'https://web.innodealing.com','Referer':'https://web.innodealing.com/'}
    url,headers,extensions = httpx_request(url,headers)
    payload = {'isSnapshot':0}
    if kind == 'quotes':payload['validQuoteStatus']=1
    with (client_factory or httpx.Client)(proxy=None,timeout=20,verify=True,trust_env=False) as client:
        with client.stream('POST',url,params=sign_params({},device_id,FE_VER),headers=headers,
                           content=json.dumps(payload),extensions=extensions) as response:
            response.raise_for_status()
            raw=bytearray()
            for chunk in response.iter_bytes():
                raw.extend(chunk)
                if len(raw)>LIMIT:raise ValueError('SNAPSHOT_TOO_LARGE')
    raw=bytes(raw)
    if raw.startswith(b'x'):
        decoder=zlib.decompressobj()
        raw=decoder.decompress(raw,LIMIT+1)
        if len(raw)>LIMIT or not decoder.eof or decoder.unconsumed_tail or decoder.unused_data:
            raise ValueError('SNAPSHOT_COMPRESSION_INVALID')
    if not raw or raw.lstrip().startswith((b'<',b'{',b'[')):
        raise ValueError('SNAPSHOT_RESPONSE_INVALID')
    return qb_apollo.parse_best_quotes(raw) if kind=='quotes' else qb_apollo.parse_trades(raw)
