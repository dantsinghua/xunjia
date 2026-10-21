"""Shared synthetic files MQ wire for retained protocol/mail/business tests."""
from urllib.parse import urlsplit
from tests.cutover.file_gateway_fakes import Gateway
from server.file_gateway_transport import FileGatewayTransport


class MQWire(Gateway):
    def __init__(self):
        super().__init__()
        self.uploaded = []

    def open(self, request, timeout):
        url = urlsplit(request.full_url)
        queue = self.queues[url.netloc]
        size = len(queue)
        try:
            return super().open(request, timeout)
        finally:
            if url.path.endswith('/upload') and len(queue) > size:
                name, blob = queue[-1]
                self.uploaded.append((url.netloc, name, blob))

    def transport(self, binding, path, *, role, **kwargs):
        up, down = self.client('up'), self.client('down')
        return FileGatewayTransport(binding, up if role == 'service' else down,
                                    down if role == 'service' else up, path,
                                    role=role, **kwargs)
