"""Actual Dify 0.15.3 HTTP envelope observed in local QA, without private inputs."""
import json
import pytest
from server.negotiation.chatflow import _http_error_code

@pytest.mark.parametrize("upstream,detail,expected",[
 (502,"[SSL: UNEXPECTED_EOF_WHILE_READING] EOF occurred","DIFY_UPSTREAM_TLS_ERROR"),
 (502,"certificate_verify_failed","DIFY_UPSTREAM_TLS_ERROR"),
 (401,"unauthorized","DIFY_UPSTREAM_AUTH_FAILED"),
 (429,"rate limit","DIFY_UPSTREAM_RATE_LIMITED"),
 (503,"temporarily unavailable","DIFY_UPSTREAM_UNAVAILABLE"),
])
def test_actual_run_failed_envelope_preserves_provider_failure(upstream,detail,expected):
 nested=json.dumps({"error":{"message":detail,"type":"upstream_error"}})
 payload={"code":"invalid_param","status":400,"message":"Run failed: [openai_api_compatible] Error: API request failed with status code "+str(upstream)+": "+nested}
 assert _http_error_code(400,json.dumps(payload).encode())==expected

def test_arbitrary_run_failed_input_text_does_not_invent_a_tls_cause():
 payload={"code":"invalid_param","message":"Run failed: input field SSL is invalid","status":400}
 assert _http_error_code(400,json.dumps(payload).encode())=="DIFY_HTTP_ERROR"
