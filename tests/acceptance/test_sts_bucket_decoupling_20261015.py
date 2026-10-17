import json
from datetime import datetime,timezone
from imlink.sts_credentials import STSCredentials
from imlink.oss_http import OSSHTTP

def test_no_sts_bucket_can_sign_configured_oss_put_and_get():
    now=1800000000
    wire={"access_key_id":"STS.synthetic","access_key_secret":"synthetic-sk","security_token":"synthetic-security","expiration":datetime.fromtimestamp(now+3600,timezone.utc).isoformat()}
    seen=[]
    sts=STSCredentials(auth_key="synthetic-auth",sm4_key=b"0123456789abcdef",clock=lambda:now,transport=lambda *args:(200,json.dumps(wire).encode()))
    oss=OSSHTTP(endpoint="https://oss.example.invalid",bucket="qtrade-nyoss-lab",credentials=sts,transport=lambda *args:seen.append(args) or (200,b"ok"))
    assert oss.request("GET","qa/synthetic.bin",timeout=1)[0]==200
    assert oss.request("PUT","qa/synthetic.bin",b"body",timeout=1)[0]==200
    assert [row[0] for row in seen]==["GET","PUT"]
    assert all(row[1]=="https://qtrade-nyoss-lab.oss.example.invalid/qa/synthetic.bin" for row in seen)
    assert all(row[2]["x-oss-security-token"]=="synthetic-security" for row in seen)
