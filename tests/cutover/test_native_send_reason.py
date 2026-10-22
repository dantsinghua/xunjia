"""Transport result classification uses proof that no native send was attempted."""
from types import SimpleNamespace
import pytest
from relay.qtrade_native_port import QTradeNativePort

@pytest.mark.parametrize("reason",["anchor_not_found:search_entry","card_identity_mismatch","input_readback_mismatch"])
def test_proven_pre_execution_failure_is_failed_not_unknown(reason):
    result=QTradeNativePort._result(SimpleNamespace(ok=False,code="SEND_FAILED",data={"native_action_attempted":False,"reason":reason}))
    assert result=={"status":"failed","evidence":"qtrade-rejected-before-native:"+reason}

@pytest.mark.parametrize("data",[{}, {"native_action_attempted":True},{"native_action_attempted":"false"}])
def test_unproven_failure_remains_unknown(data):
    assert QTradeNativePort._result(SimpleNamespace(ok=False,code="SEND_FAILED",data=data))["status"]=="unknown"
