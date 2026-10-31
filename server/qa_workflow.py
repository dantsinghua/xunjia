"""Independent Dify Workflow intent routing with capability-scoped read-only facts.

The workflow never authorizes inquiry sending. Its output only selects a cached
receipt; public answers are rendered from facts produced by this application.
"""
from __future__ import annotations

import base64
from contextlib import contextmanager
from datetime import datetime
import fcntl
import hashlib
import hmac
from html import escape
import json
import math
import os
from pathlib import Path
import secrets
import sqlite3
import stat
import tempfile
import time
from urllib.parse import urlsplit
import urllib.error
import urllib.request
from zoneinfo import ZoneInfo


TOOL_PATH = '/api/agent/tools/qa_market/query'
ACTIONS = {'smalltalk', 'search', 'latest', 'best', 'inquiry'}
ZONE = ZoneInfo('Asia/Shanghai')
CAPABILITY_TTL = 180


class WorkflowError(RuntimeError):
    pass


def default_config_path():
    prefix = os.environ.get('XUNJIA_PREFIX', '')
    metadata = Path(__file__).resolve().parents[1] / '.deployment.json'
    if not prefix and metadata.is_file():
        prefix = json.loads(metadata.read_text())['prefix']
    if not prefix:
        state = Path(os.environ.get('XUNJIA_STATE_DIR', ''))
        if state.is_absolute() and state.name == 'state':prefix = str(state.parent)
    path = Path(prefix) if prefix else None
    return path / 'config/qa-workflow.json' if path and path.is_absolute() and path.resolve() == path else None


def _url(value, *, api=False):
    if not isinstance(value, str) or len(value) > 1000:
        raise ValueError('工作流地址格式无效')
    parts = urlsplit(value.strip())
    if parts.scheme not in ('http', 'https') or not parts.hostname or parts.username or parts.password or parts.query or parts.fragment:
        raise ValueError('工作流地址须为不含凭据及查询参数的完整 HTTP(S) 地址')
    if any(ord(c) < 33 for c in value.strip()):raise ValueError('工作流地址格式无效')
    try:parts.port
    except ValueError:raise ValueError('工作流地址端口无效') from None
    result = value.strip().rstrip('/')
    return result + '/v1' if api and not result.endswith('/v1') else result


def _finite(value):
    if value is None or isinstance(value, bool) or value == '':return None
    try:result = float(value)
    except (ValueError, TypeError, OverflowError):return None
    return result if math.isfinite(result) else None


def _text(value):
    return escape(str(value)).replace('|', '\\|').replace('\n', ' ')


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):return None


class QAWorkflow:
    def __init__(self, market, *, config_path=None, opener=None, clock=time.time):
        self.market = market
        self.path = Path(config_path) if config_path is not None else default_config_path()
        self.opener = opener or urllib.request.build_opener(_NoRedirect())
        self.clock = clock

    def _load(self):
        base = os.environ.get('XUNJIA_DIFY_URL', '').rstrip('/')
        if base:
            try:base = _url(base, api=True)
            except ValueError:base = ''
        defaults = {'enabled': False, 'base_url': base,
                    'tool_base_url': '', 'app_key': '', 'revision': ''}
        if self.path is None or not self.path.exists():return defaults
        if self.path.resolve() != self.path.absolute():raise WorkflowError('QA_CONFIG_UNSAFE')
        fd = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, 'rb') as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_size > 65536:
                raise WorkflowError('QA_CONFIG_UNSAFE')
            value = json.loads(stream.read(65537))
        if not isinstance(value, dict):raise WorkflowError('QA_CONFIG_INVALID')
        result = {**defaults, **value}
        if type(result['enabled']) is not bool or not isinstance(result['app_key'], str):raise WorkflowError('QA_CONFIG_INVALID')
        if result['base_url']:result['base_url'] = _url(result['base_url'], api=True)
        if result['tool_base_url']:result['tool_base_url'] = _url(result['tool_base_url'])
        if result['enabled'] and (not result.get('capability_secret') or not result['app_key']):raise WorkflowError('QA_CONFIG_INVALID')
        return result

    def status(self):
        try:config = self._load()
        except (ValueError, OSError, WorkflowError):
            return {'available': self.path is not None, 'enabled': False, 'configured': False,
                    'app_key_set': False, 'base_url': '', 'tool_base_url': '', 'revision': '',
                    'tool_path': TOOL_PATH, 'hint': '工作流配置无法安全读取，请检查配置文件权限。'}
        configured = bool(config['base_url'] and config['tool_base_url'] and config['app_key'])
        return {'available': self.path is not None, 'enabled': bool(config['enabled'] and configured),
                'configured': configured, 'app_key_set': bool(config['app_key']),
                'base_url': config['base_url'], 'tool_base_url': config['tool_base_url'],
                'revision': config['revision'], 'tool_path': TOOL_PATH,
                'hint': '工作流已启用；实际连通性以问答执行结果为准。' if config['enabled'] and configured
                        else '智能问答工作流未启用，当前保留原有只读查券和本人会话查询。'}

    def save(self, body):
        if self.path is None:raise ValueError('未确定受管安装目录，请先配置 XUNJIA_PREFIX')
        if self.path.parent.resolve() != self.path.parent.absolute():raise WorkflowError('QA_CONFIG_UNSAFE')
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(self.path.parent / '.qa-workflow.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            return self._save(body)
        finally:os.close(fd)

    def _save(self, body):
        if not isinstance(body, dict) or set(body) - {'enabled', 'base_url', 'tool_base_url', 'app_key', 'expected_revision'}:
            raise ValueError('工作流配置字段无效')
        old = self._load()
        if body.get('expected_revision') != old['revision']:raise ValueError('工作流配置已变化，请重新读取后保存')
        if type(body.get('enabled')) is not bool:raise ValueError('须明确是否启用工作流')
        key = body.get('app_key', '')
        if not isinstance(key, str) or len(key) > 512 or any(ord(c) < 33 for c in key):
            raise ValueError('应用密钥格式无效')
        key = key or old['app_key']
        base = _url(body.get('base_url', old['base_url']), api=True)
        tool = _url(body.get('tool_base_url', old['tool_base_url']))
        if body['enabled'] and not key:raise ValueError('启用前请填写新工作流的 App API Key')
        value = {'enabled': body['enabled'], 'base_url': base, 'tool_base_url': tool, 'app_key': key,
                 'revision': secrets.token_hex(16),
                 'capability_secret': old.get('capability_secret') or secrets.token_hex(32)}
        if self.path.parent.resolve() != self.path.parent.absolute() or self.path.is_symlink():
            raise WorkflowError('QA_CONFIG_UNSAFE')
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd, name = tempfile.mkstemp(prefix='.qa-workflow-', dir=self.path.parent)
        temporary = Path(name)
        try:
            with os.fdopen(fd, 'w', encoding='utf8') as stream:
                json.dump(value, stream, ensure_ascii=False)
                stream.flush(); os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        finally:
            if temporary.exists():temporary.unlink()
        return self.status()

    @contextmanager
    def _db(self):
        if self.path is None:raise WorkflowError('QA_CONFIG_MISSING')
        root = self.path.parent.parent / 'state'
        if root.resolve() != root.absolute():raise WorkflowError('QA_STATE_UNSAFE')
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = root / 'qa-workflow.sqlite3'
        fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:raise WorkflowError('QA_STATE_UNSAFE')
        finally:os.close(fd)
        db = sqlite3.connect(path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                db.execute('''CREATE TABLE IF NOT EXISTS qa_run(
                    nonce TEXT PRIMARY KEY, owner TEXT NOT NULL, expires REAL NOT NULL,
                    used INTEGER NOT NULL DEFAULT 0, closed INTEGER NOT NULL DEFAULT 0,
                    receipt TEXT NOT NULL DEFAULT '', facts TEXT NOT NULL DEFAULT '{}')''')
                yield db
        finally:db.close()

    @staticmethod
    def _sign(payload, secret):
        return hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()

    def _capability(self, owner, config):
        nonce = secrets.token_hex(24)
        expires = int(self.clock()) + CAPABILITY_TTL
        payload = base64.urlsafe_b64encode(json.dumps({'nonce': nonce, 'exp': expires,
            'owner': hashlib.sha256(owner.encode()).hexdigest()}, separators=(',', ':')).encode()).decode().rstrip('=')
        with self._db() as db:
            db.execute('DELETE FROM qa_run WHERE expires<?', (self.clock(),))
            db.execute('INSERT INTO qa_run(nonce,owner,expires) VALUES(?,?,?)', (nonce, owner, expires))
        return nonce, payload + '.' + self._sign(payload, config['capability_secret'])

    def _verify(self, token):
        if not isinstance(token, str) or len(token) > 2048:raise WorkflowError('QA_CAPABILITY_INVALID')
        try:
            payload, signature = token.split('.')
            config = self._load()
            if not config['enabled'] or not hmac.compare_digest(signature, self._sign(payload, config['capability_secret'])):
                raise ValueError
            value = json.loads(base64.urlsafe_b64decode(payload + '=' * (-len(payload) % 4)))
            if value['exp'] <= self.clock():raise ValueError
            with self._db() as db:run = db.execute('SELECT * FROM qa_run WHERE nonce=?', (value['nonce'],)).fetchone()
            if run is None or run['closed'] or run['expires'] != value['exp'] or value['owner'] != hashlib.sha256(run['owner'].encode()).hexdigest():
                raise ValueError
            return dict(run)
        except (ValueError, KeyError, TypeError, OSError):
            raise WorkflowError('QA_CAPABILITY_INVALID') from None

    def read_market(self, token, body):
        run = self._verify(token)
        if not isinstance(body, dict) or set(body) != {'action', 'queries'} or not isinstance(body.get('action'), str) or body['action'] not in ACTIONS:
            raise ValueError('只读工具仅接受已声明的查询动作')
        action, queries = body['action'], body['queries']
        if not isinstance(queries, list) or len(queries) > 10 or any(not isinstance(q, str) or not 1 <= len(q.strip()) <= 80 for q in queries):
            raise ValueError('每次最多查询10个明确债券关键词')
        if action != 'smalltalk' and not queries:raise ValueError('行情查询须提供债券关键词')
        if action == 'smalltalk' and queries:raise ValueError('闲聊不能附带行情查询')
        cost = max(1, len(queries))
        with self._db() as db:
            updated = db.execute('UPDATE qa_run SET used=used+? WHERE nonce=? AND used+?<=10 AND closed=0 AND expires>?',
                                 (cost, run['nonce'], cost, self.clock()))
            if updated.rowcount != 1:raise WorkflowError('QA_QUERY_BUDGET_EXHAUSTED')
        now = self.clock()
        groups = []
        quote_cache = {}
        for query in queries:
            try:page = self.market.page(run['owner'], filter_spec={'keyword': query.strip(), 'channel': 'best'}, limit=500)
            except Exception:raise WorkflowError('QA_MARKET_UNAVAILABLE') from None
            rows = sorted(page['data'], key=lambda r: _finite(r.get('issueTime')) or 0, reverse=True)
            candidates = []
            for row in rows[:20]:
                code = str(row['code'])
                if code not in quote_cache:quote_cache[code] = self._quotes(run['owner'], code, now)
                candidates.append(self._candidate(row, quote_cache[code], action))
            groups.append({'query': query.strip(), 'candidates': candidates,
                           'truncated': bool(page.get('hasMore') or len(rows) > 20),
                           'notice': page.get('emptyReason') or ''})
        receipt = secrets.token_hex(24)
        facts = {'action': action, 'as_of': datetime.fromtimestamp(now, ZONE).isoformat(), 'groups': groups}
        with self._db() as db:
            updated = db.execute('UPDATE qa_run SET receipt=?,facts=? WHERE nonce=? AND closed=0 AND expires>?',
                (receipt, json.dumps(facts, ensure_ascii=False, allow_nan=False), run['nonce'], self.clock()))
            if updated.rowcount != 1:raise WorkflowError('QA_CAPABILITY_EXPIRED')
        return {'ok': True, 'receipt_id': receipt, 'data': facts}

    @staticmethod
    def _current(stamp, now):
        stamp = _finite(stamp)
        if stamp is None or stamp > now:return False
        try:return datetime.fromtimestamp(stamp, ZONE).date() == datetime.fromtimestamp(now, ZONE).date()
        except (ValueError, OverflowError, OSError):return False

    def _quotes(self, owner, code, now):
        """Use each original record's time, never a merged row's latest update.

        The existing QB detail adapter exposes validated yield-only broker records.
        QT detail projection already fences owner, expiry, price kind and settlement.
        Missing adapters yield no prices; a public snapshot is not a dated receipt.
        """
        result = []
        provider = getattr(self.market, 'detail_provider', None)
        try:detail = provider(code) if provider else None
        except Exception:raise WorkflowError('QA_MARKET_UNAVAILABLE') from None
        for item in (detail or {}).get('quotes', []):
            if not self._current(item.get('updated_at'), now):continue
            result.append({'at': float(item['updated_at']), 'source': str(item.get('channel') or 'QB'),
                'clearing': item.get('clearing'), 'bid': _finite(item.get('best_bid')),
                'ofr': _finite(item.get('best_ofr')), 'bid_volume_wan': _finite(item.get('best_bid_vol')),
                'ofr_volume_wan': _finite(item.get('best_ofr_vol'))})
        if getattr(self.market, 'qt_provider', None) is not None:
            try:page = self.market.page(owner, filter_spec={'keyword': code, 'channel': 'qt'}, limit=500)
            except Exception:raise WorkflowError('QA_MARKET_UNAVAILABLE') from None
            # Never label a partial quote book's winner as the market's best.
            if page.get('hasMore'):raise WorkflowError('QA_QUOTE_CAPACITY_EXCEEDED')
            for item in page['data']:
                at = _finite(item.get('issueTime'))
                if item.get('code') != code or at is None or not self._current(at / 1000, now):continue
                for side, price, volume in (('bid', 'ob', 'obv'), ('ofr', 'oo', 'oov')):
                    if item.get('qt_compare_reasons', {}).get(side, 'UNVERIFIED'):continue
                    result.append({'at': at / 1000, 'source': 'QT',
                        'clearing': item.get('qt_settlement', {}).get(side), side: _finite(item.get(price)),
                        side + '_volume_wan': _finite(item.get(volume))})
        return result

    @staticmethod
    def _candidate(row, quotes, action):
        clearing = row.get('clearing')
        if clearing not in ('+0', '+1', '+N'):
            clearing = next((item['clearing'] for item in sorted(quotes, key=lambda q: q['at'], reverse=True)
                             if item['clearing'] in ('+0', '+1', '+N')), None)
        result = {'code': str(row['code']), 'name': str(row['name']), 'clearing': clearing,
                  'current_day': False, 'source_at': None}
        times = []
        for side in ('bid', 'ofr'):
            eligible = [q for q in quotes if clearing is not None and q['clearing'] == clearing and q.get(side) is not None]
            if action == 'latest':eligible.sort(key=lambda q: q['at'], reverse=True)
            else:eligible.sort(key=lambda q: (q[side] if side == 'bid' else -q[side], -q['at']))
            winner = eligible[0] if eligible else None
            result[side] = winner[side] if winner else None
            result[side + '_volume_wan'] = winner.get(side + '_volume_wan') if winner else None
            result[side + '_source'] = winner['source'] if winner else ''
            result[side + '_at'] = datetime.fromtimestamp(winner['at'], ZONE).isoformat() if winner else None
            if winner:times.append(winner['at'])
        if times:
            result['current_day'] = True
            result['source_at'] = datetime.fromtimestamp(max(times), ZONE).isoformat()
        return result

    def answer(self, owner, question):
        config = self._load()
        if not config['enabled']:raise WorkflowError('QA_WORKFLOW_DISABLED')
        nonce, capability = self._capability(owner, config)
        body = {'inputs': {'question': question, 'tool_url': _url(config['tool_base_url']) + TOOL_PATH,
                           'capability': capability}, 'response_mode': 'blocking',
                'user': 'qa-' + hashlib.sha256(owner.encode()).hexdigest()}
        request = urllib.request.Request(_url(config['base_url'], api=True) + '/workflows/run',
            data=json.dumps(body, ensure_ascii=False).encode(), method='POST',
            headers={'Authorization': 'Bearer ' + config['app_key'], 'Content-Type': 'application/json'})
        try:
            try:
                with self.opener.open(request, timeout=120) as response:
                    raw = response.read(2 * 1024 * 1024 + 1)
                    if response.status != 200 or len(raw) > 2 * 1024 * 1024:raise WorkflowError('QA_DIFY_RESPONSE_INVALID')
            except urllib.error.HTTPError as exc:
                exc.close()
                raise WorkflowError('QA_DIFY_HTTP_ERROR') from None
            except (OSError, urllib.error.URLError):raise WorkflowError('QA_DIFY_UNAVAILABLE') from None
            try:
                result = json.loads(raw)['data']
                receipt = result['outputs']['receipt_id']
                if result.get('status') != 'succeeded' or not isinstance(receipt, str):raise ValueError
            except (ValueError, KeyError, TypeError):raise WorkflowError('QA_DIFY_OUTPUT_INVALID') from None
            with self._db() as db:
                row = db.execute('SELECT * FROM qa_run WHERE nonce=? AND owner=? AND receipt=? AND expires>?',
                                 (nonce, owner, receipt, self.clock())).fetchone()
            if row is None or not row['receipt']:raise WorkflowError('QA_UNVERIFIED_FACTS')
            return self.render(json.loads(row['facts']))
        finally:
            with self._db() as db:db.execute('UPDATE qa_run SET closed=1 WHERE nonce=?', (nonce,))

    @staticmethod
    def render(facts):
        action = facts['action']; candidates = {}; lines = []
        if action == 'smalltalk':
            lines = ['你好，我可以帮你查找债券、查看今天已采集的最新或最优报价。需要询价时，我会列出候选债券，由你设置策略、选择对手并确认。']
        else:
            mode = '各方向最新有效' if action == 'latest' else '同清算口径最优'
            lines = ['查询时点：' + _text(facts['as_of']) + '（Asia/Shanghai）。' + mode + '收益率；仅使用本服务已采集的当日行情，不代表成交价。']
            for group in facts['groups']:
                lines.append('**' + _text(group['query']) + '**')
                if not group['candidates']:lines.append('未找到匹配债券或当日行情，不生成报价。')
                for row in group['candidates']:
                    candidates[row['code']] = {'code': row['code'], 'name': row['name'], 'current_day': row['current_day']}
                    text = _text(row['code']) + ' ' + _text(row['name'])
                    if row['current_day']:
                        values = []
                        for side, label in (('bid', 'Bid'), ('ofr', 'Ofr')):
                            price = row[side]
                            values.append(label + (' 未接入或缺失' if price is None else f' {price:g}%') +
                                          (' · ' + _text(row[side + '_source']) + ' · ' + _text(row[side + '_at']) if row[side + '_source'] else ''))
                        text += '：' + '；'.join(values) + '。清算 ' + _text(row.get('clearing'))
                    else:text += '：暂无可核验的当日报价。'
                    lines.append('- ' + text)
                if group['truncated']:lines.append('匹配较多，仅列最多20个候选，请补充完整代码。')
            if action == 'inquiry':lines.append('请明确选择债券，再在本页设置询价策略、选择对手并确认。尚未发送任何询价。')
        return {'answer': '\n\n'.join(lines), 'workflow_enabled': True, 'action': action,
                'candidates': list(candidates.values()), 'requires_selection': action == 'inquiry',
                'as_of': facts['as_of'], 'notice': '答案价格来自服务端核验的行情快照，模型没有发送权限。'}
