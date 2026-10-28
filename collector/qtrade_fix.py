"""Receive-only QTrade FIX 4.4 session (V1.0.1, sections 4-7).

Only session messages are emitted. No order, quote, inquiry or trade submission
exists here. Each QTrade connection has a fresh inbound sequence (manual p8);
business replay identity belongs to the durable quote book, not MsgSeqNum.
"""
from __future__ import annotations

import asyncio
from collections import OrderedDict
from datetime import datetime
import hashlib
import inspect
import re
import ssl
import time
from zoneinfo import ZoneInfo

SOH = b'\x01'
MAX_BODY = 256 * 1024
SESSION_TYPES = frozenset(('A', '0', '1', '2', '3', '4', '5'))
HEADER_TAGS = frozenset(('8', '9', '10', '49', '56', '34', '52', '43', '97', '122', '369'))


class FixProtocolError(ValueError):
    """Fixed safe code only; never include raw Logon or vendor Text."""


class FixAuthenticationError(FixProtocolError):
    pass


class FixDisconnected(ConnectionError):
    pass


def encode(fields, *, encoding='utf-8'):
    """Produce byte-counted SOH framing; no display-pipe substitution."""
    if not isinstance(fields, (list, tuple)) or not fields or str(fields[0][0]) != '35':
        raise FixProtocolError('QT_FIX_MSGTYPE_FIRST')
    seen = set()
    body = bytearray()
    for tag, value in fields:
        tag = str(tag)
        if not re.fullmatch(r'[1-9][0-9]{0,5}', tag) or tag in seen or tag in ('8', '9', '10'):
            raise FixProtocolError('QT_FIX_TAG_INVALID')
        if not isinstance(value, str) or any(ord(c) < 32 or ord(c) == 127 for c in value):
            raise FixProtocolError('QT_FIX_VALUE_INVALID')
        seen.add(tag)
        try:
            body.extend(tag.encode('ascii') + b'=' + value.encode(encoding) + SOH)
        except (UnicodeError, LookupError):
            raise FixProtocolError('QT_FIX_ENCODING_INVALID') from None
    if not 0 < len(body) <= MAX_BODY:
        raise FixProtocolError('QT_FIX_BODY_TOO_LARGE')
    wire = b'8=FIX.4.4\x019=' + str(len(body)).encode() + SOH + body
    return wire + b'10=' + f'{sum(wire) % 256:03d}'.encode() + SOH


class Framer:
    """Bounded incremental parser; validates length and checksum before decoding."""
    def __init__(self, encoding='utf-8'):
        if encoding not in ('utf-8', 'gb18030', 'ascii'):
            raise FixProtocolError('QT_FIX_ENCODING_INVALID')
        self.encoding = encoding
        self.buffer = bytearray()

    def feed(self, chunk):
        if not isinstance(chunk, bytes):
            raise FixProtocolError('QT_FIX_BYTES_REQUIRED')
        self.buffer.extend(chunk)
        if len(self.buffer) > MAX_BODY * 8:
            raise FixProtocolError('QT_FIX_BUFFER_LIMIT')
        messages = []
        prefix = b'8=FIX.4.4\x019='
        while self.buffer:
            if not (self.buffer.startswith(prefix) or prefix.startswith(self.buffer)):
                raise FixProtocolError('QT_FIX_HEADER_INVALID')
            end = self.buffer.find(SOH, len(prefix))
            if end < 0:
                if len(self.buffer) > len(prefix) + 8:
                    raise FixProtocolError('QT_FIX_LENGTH_INVALID')
                break
            length = self.buffer[len(prefix):end]
            if not re.fullmatch(rb'[1-9][0-9]{0,5}', length) or int(length) > MAX_BODY:
                raise FixProtocolError('QT_FIX_LENGTH_INVALID')
            trailer = end + 1 + int(length)
            size = trailer + 7
            if len(self.buffer) < size:
                break
            raw = bytes(self.buffer[:size])
            if raw[trailer - 1:trailer] != SOH or not re.fullmatch(rb'10=[0-9]{3}\x01', raw[trailer:]):
                raise FixProtocolError('QT_FIX_TRAILER_INVALID')
            if sum(raw[:trailer]) % 256 != int(raw[trailer + 3:trailer + 6]):
                raise FixProtocolError('QT_FIX_CHECKSUM_INVALID')
            values = {}
            pairs = raw[:-1].split(SOH)
            for part in pairs:
                tag, separator, value = part.partition(b'=')
                if not separator or not re.fullmatch(rb'[1-9][0-9]{0,5}', tag):
                    raise FixProtocolError('QT_FIX_TAG_INVALID')
                key = tag.decode('ascii')
                if key in values:
                    raise FixProtocolError('QT_FIX_DUPLICATE_TAG')
                try:
                    decoded = value.decode(self.encoding)
                except UnicodeError:
                    raise FixProtocolError('QT_FIX_ENCODING_INVALID') from None
                if any((ord(c) < 32 and not (key == '18026' and c in '\r\n\t')) or ord(c) == 127 for c in decoded):
                    raise FixProtocolError('QT_FIX_VALUE_INVALID')
                values[key] = decoded
            if list(values)[:3] != ['8', '9', '35'] or list(values)[-1] != '10':
                raise FixProtocolError('QT_FIX_FIELD_ORDER_INVALID')
            del self.buffer[:size]
            messages.append(values)
        return messages


def positive(value, code):
    if not isinstance(value, str) or not re.fullmatch(r'[1-9][0-9]{0,11}', value):
        raise FixProtocolError(code)
    return int(value)


class Session:
    """One authenticated connection, with heartbeats, resend and bounded gap recovery."""
    def __init__(self, config, *, receive, update, connector=None):
        self.cfg = dict(config)
        self.receive = receive
        self.update = update
        self.connector = connector or asyncio.open_connection
        self.writer = None
        self.next_out = 1
        self.expected = 1
        self.logged_on = False
        self.pending = {}
        self.recent = OrderedDict()
        self.gap_requested = None
        self.gap_started = None
        self.test_id = None
        self.test_sent = 0.0
        self.last_rx = self.last_tx = time.monotonic()
        self.heartbeat = config['heartbeat_seconds']
        self.stats = {'frames_received': 0, 'heartbeats_sent': 0, 'resend_requests': 0,
                      'session_duplicates': 0, 'sequence_gaps': 0}

    def metric(self, name):
        self.stats[name] += 1
        self.update(**self.stats)

    def tls_context(self):
        if self.cfg['transport'] == 'tcp':
            if not self.cfg.get('plaintext_confirmed'):
                raise FixProtocolError('QT_PLAINTEXT_NOT_CONFIRMED')
            return None
        try:
            context = ssl.create_default_context(cafile=self.cfg.get('ca_file') or None)
            context.minimum_version = ssl.TLSVersion.TLSv1_2
            cert, key = self.cfg.get('client_cert_file'), self.cfg.get('client_key_file')
            if bool(cert) != bool(key):
                raise FixProtocolError('QT_CLIENT_CERT_PAIR_REQUIRED')
            if cert:
                context.load_cert_chain(cert, key, password=lambda: '')
            return context
        except (OSError, ssl.SSLError, ValueError):
            raise FixProtocolError('QT_TLS_CONFIGURATION_INVALID') from None

    async def send(self, kind, extra=(), *, sequence=None, duplicate=False):
        if kind not in SESSION_TYPES:
            raise FixProtocolError('QT_READ_ONLY_SESSION_NO_BUSINESS_SEND')
        if self.writer is None:
            raise FixDisconnected('QT_FIX_NOT_CONNECTED')
        seq = self.next_out if sequence is None else sequence
        stamp = datetime.now(ZoneInfo(self.cfg['sending_timezone'])).strftime('%Y%m%d-%H:%M:%S.%f')[:-3]
        fields = [('35', kind), ('49', self.cfg['sender_comp_id']), ('56', self.cfg['target_comp_id']),
                  ('34', str(seq)), ('52', stamp)]
        if duplicate:
            fields += [('43', 'Y'), ('122', stamp)]
        wire = encode(fields + list(extra), encoding=self.cfg['encoding'])
        self.writer.write(wire)
        await asyncio.wait_for(self.writer.drain(), self.cfg['connect_timeout_seconds'])
        if sequence is None:
            self.next_out += 1
        self.last_tx = time.monotonic()
        if kind == '0':
            self.metric('heartbeats_sent')

    def check_identity(self, fields):
        if fields.get('49') != self.cfg['target_comp_id'] or fields.get('56') != self.cfg['sender_comp_id']:
            raise FixProtocolError('QT_FIX_COMP_ID_MISMATCH')
        if not re.fullmatch(r'[0-9]{8}-[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?', fields.get('52', '')):
            raise FixProtocolError('QT_FIX_SENDING_TIME_INVALID')
        try:datetime.strptime(fields['52'],'%Y%m%d-%H:%M:%S.%f' if '.' in fields['52'] else '%Y%m%d-%H:%M:%S')
        except ValueError:raise FixProtocolError('QT_FIX_SENDING_TIME_INVALID') from None
        return positive(fields.get('34'), 'QT_FIX_SEQUENCE_INVALID')

    @staticmethod
    def fingerprint(fields):
        # Retransmissions may replace SendingTime and add PossDup/OrigSendingTime.
        fields = {k: v for k, v in fields.items() if k not in ('8','9','10','43','52','122')}
        return hashlib.sha256(repr(sorted(fields.items())).encode()).hexdigest()

    async def accept(self, fields, *, probe=False):
        seq = self.check_identity(fields)
        self.last_rx = time.monotonic()
        if not self.logged_on and fields.get('35') not in ('A','5'):
            raise FixProtocolError('QT_FIX_LOGON_REQUIRED')
        if not self.logged_on and seq != 1:
            raise FixProtocolError('QT_FIX_RECONNECT_SEQUENCE_MUST_START_AT_ONE')
        digest = self.fingerprint(fields)
        if seq < self.expected:
            if fields.get('43') != 'Y':
                raise FixProtocolError('QT_FIX_SEQUENCE_REGRESSION')
            if seq in self.recent and self.recent[seq] != digest:
                raise FixProtocolError('QT_FIX_SEQUENCE_CONTENT_CONFLICT')
            # A retransmitted GapFill can extend beyond the current expected number.
            if fields.get('35') == '4' and fields.get('123') == 'Y' and positive(fields.get('36'), 'QT_FIX_NEW_SEQUENCE_INVALID') > self.expected:
                await self.process(fields, probe=probe)
            else:
                self.metric('session_duplicates')
                return
        elif seq > self.expected:
            if len(self.pending) >= 256 or sum(len(repr(v)) for v in self.pending.values()) + len(repr(fields)) > MAX_BODY*4:
                raise FixProtocolError('QT_FIX_GAP_BUFFER_LIMIT')
            if seq in self.pending and self.fingerprint(self.pending[seq]) != digest:
                raise FixProtocolError('QT_FIX_SEQUENCE_CONTENT_CONFLICT')
            self.pending[seq] = fields
            await self.request_gap()
            return
        else:
            await self.process(fields, probe=probe)
            if fields.get('35') != '4':
                self.expected += 1
            self.recent[seq] = digest
            if len(self.recent) > 1024:
                self.recent.popitem(last=False)
        while self.expected in self.pending:
            entry = self.pending.pop(self.expected)
            await self.accept(entry, probe=probe)
        if self.pending:
            await self.request_gap()
        if self.logged_on and not self.pending:
            self.gap_requested = None
            self.gap_started = None
            self.update(state='streaming', code='QT_FIX_CONNECTED')

    async def request_gap(self):
        if not self.pending or self.gap_requested == self.expected:
            return
        self.gap_requested = self.expected
        if self.gap_started is None:
            self.gap_started = time.monotonic()
        self.metric('sequence_gaps')
        self.update(state='recovering', code='QT_FIX_SEQUENCE_GAP')
        await self.send('2', [('7', str(self.expected)), ('16', str(min(self.pending) - 1))])
        self.metric('resend_requests')

    async def process(self, fields, *, probe):
        kind = fields['35']
        if kind == 'A':
            if self.logged_on:
                raise FixProtocolError('QT_FIX_DUPLICATE_LOGON')
            if fields.get('1409', '0') not in ('0','1','2'):
                raise FixAuthenticationError('QT_FIX_AUTH_REJECTED')
            if fields.get('98') != '0' or fields.get('141', 'N') != 'N' or fields.get('1137','6') != '6':
                raise FixProtocolError('QT_FIX_LOGON_CONTRACT_INVALID')
            heartbeat = positive(fields.get('108'), 'QT_FIX_HEARTBEAT_INVALID')
            if not 1 <= heartbeat <= 120:
                raise FixProtocolError('QT_FIX_HEARTBEAT_INVALID')
            self.heartbeat = heartbeat
            self.logged_on = True
            self.update(state='streaming', code='QT_FIX_CONNECTED', connected_at=time.time())
        elif kind == '5':
            if not self.logged_on or fields.get('1409') in ('3','5','6','7','8','9'):
                raise FixAuthenticationError('QT_FIX_AUTH_REJECTED')
            await self.send('5')
            self.logged_on = False
            raise FixDisconnected('QT_FIX_REMOTE_LOGOUT')
        elif kind == '0':
            if self.test_id and fields.get('112') == self.test_id:
                self.test_id = None
        elif kind == '1':
            value = fields.get('112')
            if not value or len(value) > 128:
                raise FixProtocolError('QT_FIX_TEST_REQUEST_INVALID')
            await self.send('0', [('112', value)])
        elif kind == '2':
            start = positive(fields.get('7'), 'QT_FIX_RESEND_INVALID')
            end = fields.get('16')
            end = self.next_out - 1 if end == '0' else positive(end, 'QT_FIX_RESEND_INVALID')
            if start > end or end >= self.next_out:
                raise FixProtocolError('QT_FIX_RESEND_INVALID')
            # Every outbound message is administrative; never replay a password or business action.
            await self.send('4', [('123','Y'), ('36', str(end + 1))], sequence=start, duplicate=True)
        elif kind == '4':
            new = positive(fields.get('36'), 'QT_FIX_NEW_SEQUENCE_INVALID')
            if fields.get('123') != 'Y' or new <= self.expected:
                raise FixProtocolError('QT_FIX_UNSAFE_SEQUENCE_RESET')
            if any(k < new and v.get('35') == 'S' for k, v in self.pending.items()):
                raise FixProtocolError('QT_FIX_GAPFILL_DISCARDS_QUOTE')
            self.pending = {k:v for k,v in self.pending.items() if k >= new}
            self.expected = new
        elif kind == '3':
            raise FixProtocolError('QT_FIX_SESSION_REJECTED')
        elif kind == 'S':
            if '553' in fields or '554' in fields:
                raise FixProtocolError('QT_FIX_SECRET_IN_BUSINESS_MESSAGE')
            if not probe:
                business = {k:v for k,v in fields.items() if k not in HEADER_TAGS}
                result = self.receive(business)
                if inspect.isawaitable(result):
                    await result
        else:
            raise FixProtocolError('QT_FIX_UNSUPPORTED_MESSAGE')

    async def run(self, *, probe=False):
        parser = Framer(self.cfg['encoding'])
        self.update(state='connecting', code='QT_FIX_CONNECTING')
        context = self.tls_context()
        args = {'ssl':context, 'limit':MAX_BODY + 64}
        if context:
            args['server_hostname'] = self.cfg.get('server_name') or self.cfg['host']
        started = time.monotonic()
        try:
            reader, self.writer = await asyncio.wait_for(
                self.connector(self.cfg['host'], self.cfg['port'], **args), self.cfg['connect_timeout_seconds'])
            self.update(state='authenticating', code='QT_FIX_LOGON_PENDING')
            await self.send('A', [('98','0'), ('108',str(self.cfg['heartbeat_seconds'])),
                                  ('553',self.cfg['username']), ('554',self.cfg['password']), ('141','N'), ('1137','6')])
            started = self.last_rx = time.monotonic()
            while True:
                now = time.monotonic()
                if not self.logged_on and now - started > self.cfg['connect_timeout_seconds']:
                    raise FixDisconnected('QT_FIX_LOGON_TIMEOUT')
                if self.logged_on:
                    if self.gap_started is not None and now-self.gap_started > max(2*self.heartbeat,5):
                        raise FixDisconnected('QT_FIX_SEQUENCE_RECOVERY_TIMEOUT')
                    if self.test_id and now - self.test_sent > self.heartbeat:
                        raise FixDisconnected('QT_FIX_HEARTBEAT_TIMEOUT')
                    if now - self.last_rx > self.heartbeat * 1.5 and not self.test_id:
                        self.test_id = 'probe-' + str(time.monotonic_ns())
                        self.test_sent = now
                        await self.send('1', [('112',self.test_id)])
                    if now - self.last_tx >= self.heartbeat:
                        await self.send('0')
                try:
                    chunk = await asyncio.wait_for(reader.read(65536), min(.5, self.heartbeat / 2))
                except asyncio.TimeoutError:
                    continue
                if not chunk:
                    raise FixDisconnected('QT_FIX_CONNECTION_CLOSED')
                for message in parser.feed(chunk):
                    self.metric('frames_received')
                    await self.accept(message, probe=probe)
                    if probe and self.logged_on:
                        return
        finally:
            if self.writer is not None:
                try:
                    if self.logged_on and not self.writer.is_closing():
                        await asyncio.wait_for(self.send('5'), 1)
                except (OSError, asyncio.TimeoutError, FixProtocolError):
                    pass
                finally:
                    self.writer.close()
                    try:
                        await asyncio.wait_for(self.writer.wait_closed(), 1)
                    except (OSError, asyncio.TimeoutError):
                        pass
                    self.writer = None
                    self.logged_on = False
