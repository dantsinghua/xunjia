from datetime import datetime,timezone
import json
import pytest
from imlink.sts_credentials import STSCredentials,CredentialsError

MQ='http://nginx.mq.dq.yingmi-inc.com/its-accesspoint/v1/api/sts'


def test_explicit_mq_endpoint_keeps_the_exact_gateway_path():
    seen=[]
    def fetch(url,headers,timeout):
        seen.append((url,headers))
        return 200,json.dumps({'access_key_id':'synthetic-id','access_key_secret':'synthetic-secret','security_token':'synthetic-token','bucket':'synthetic-bucket','expiration':'2027-01-15T08:10:00Z'}).encode()
    now=datetime(2027,1,15,8,tzinfo=timezone.utc).timestamp()
    provider=STSCredentials(auth_key='synthetic-business-key',sm4_key=b'0123456789abcdef',url=MQ,transport=fetch,clock=lambda:now)
    assert provider.get().bucket==''
    assert seen[0][0]==MQ and set(seen[0][1])=={'access_token'}


@pytest.mark.parametrize('url',[
 'http://untrusted.invalid/api/sts',MQ+'?redirect=elsewhere',MQ+'#fragment',
 'http://nginx.mq.dq.yingmi-inc.com.evil.invalid/its-accesspoint/v1/api/sts',
 'http://nginx.mq.dq.yingmi-inc.com/api/sts',
])
def test_mq_exception_does_not_allow_other_plaintext_urls(url):
    with pytest.raises(CredentialsError):STSCredentials(auth_key='synthetic-key',sm4_key=b'0123456789abcdef',url=url)
