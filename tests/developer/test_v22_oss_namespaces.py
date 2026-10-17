"""The collector's shared legacy/deposit directory must not be overwritten."""
from relay.protocol import Binding
from server.transports import OSSTransport


def test_v22_contact_document_isolated_from_existing_deposit_contacts(tmp_path):
    class Objects:
        def __init__(self):self.data={'wb/synthetic-wb/qidian/synthetic-account/contacts.json':b'legacy-deposit-document'}
        def request(self,method,name,body=None,timeout=20):
            if method=='PUT':self.data[name]=body;return 200,b''
            return (200,self.data[name]) if name in self.data else (404,b'')
    binding=Binding('synthetic-owner','synthetic-account','synthetic-wb',b's'*32,b'h'*32)
    objects=Objects();transport=OSSTransport(binding,objects,tmp_path/'transport.db',clock=lambda:100)
    payload={'account_uid':binding.account_uid,'contacts':[{'uin':'synthetic-peer','name':'Synthetic peer'}]}
    transport.publish_contacts(payload)
    assert objects.data['wb/synthetic-wb/qidian/synthetic-account/contacts.json']==b'legacy-deposit-document'
    assert 'wb/synthetic-wb/out/xunjia/synthetic-account/contacts.bin' in objects.data
    assert transport.contacts()==payload
