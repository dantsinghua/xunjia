import io,json,ssl,urllib.error
import pytest
from server.negotiation.chatflow import ChatflowClient,FlowUnavailable

@pytest.mark.parametrize("status,body,expected",[
 (400,{"message":"[openai_api_compatible] Error: API request failed with status code 502: [SSL: UNEXPECTED_EOF_WHILE_READING] secret-app-key"},"DIFY_UPSTREAM_TLS_ERROR"),
 (400,{"message":"[openai_api_compatible] Error: API request failed with status code 502: upstream unavailable"},"DIFY_UPSTREAM_UNAVAILABLE"),
 (401,{"message":"secret-app-key invalid"},"DIFY_AUTH_FAILED"),
 (403,{},"DIFY_ACCESS_DENIED"),
 (429,{},"DIFY_RATE_LIMITED"),
 (400,{"message":"input field SSL is invalid"},"DIFY_HTTP_ERROR"),
 (503,None,"DIFY_HTTP_ERROR"),
])
def test_safe_http_failure_classification(status,body,expected):
 class Opener:
  def open(self,*a,**kw):raise urllib.error.HTTPError("https://synthetic.invalid",status,"secret-app-key",{},io.BytesIO(json.dumps(body).encode()))
 client=ChatflowClient("https://synthetic.invalid","secret-app-key",stage="analysis",opener=Opener())
 with pytest.raises(FlowUnavailable) as error:client.call({})
 assert error.value.code==expected
 assert error.value.status==status
 assert client.last_evidence["error_code"]==expected
 assert "secret-app-key" not in str(error.value)+json.dumps(client.last_evidence)

def test_direct_tls_error_is_not_reported_as_a_generic_network_failure():
 class Opener:
  def open(self,*a,**kw):raise urllib.error.URLError(ssl.SSLError("synthetic TLS EOF"))
 client=ChatflowClient("https://synthetic.invalid","secret-app-key",stage="reply",opener=Opener())
 with pytest.raises(FlowUnavailable) as error:client.call({})
 assert error.value.code=="DIFY_TLS_ERROR"

def test_error_body_read_is_bounded_and_invalid_body_stays_generic():
 class Body(io.BytesIO):
  def read(self,size=-1):
   assert 0<size<=16385
   return super().read(size)
 class Opener:
  def open(self,*a,**kw):raise urllib.error.HTTPError("https://synthetic.invalid",400,"private",{},Body(b"x"*20000))
 client=ChatflowClient("https://synthetic.invalid","private",stage="analysis",opener=Opener())
 with pytest.raises(FlowUnavailable) as error:client.call({})
 assert error.value.code=="DIFY_HTTP_ERROR"
