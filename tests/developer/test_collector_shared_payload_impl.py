"""The shared collector wheel must contain the modules the current host imports."""
from scripts.build_collector_runtime import FILES

def test_current_collector_shared_protocol_imports_are_deployable():
    modules={name[:-3].replace('/','.') for name in FILES}
    assert {'relay.collector_bridge','relay.file_gateway','relay.file_gateway_connection',
            'relay.gateway_paths','relay.protocol','relay.qtrade_native_port','relay.wechat_capture',
            'server.institution_transport','server.receiver_ledger','server.wechat_capture_transport'} <= modules
