"""Owner-scoped QTrade dialogue quotes. Never publish these records on QuoteHub.

FIX session control lives in qt_control/collector.qtrade_fix. This book assigns
stable business identities independently of per-connection FIX MsgSeqNum. The
legacy append-only journal remains supported for existing operator deployments.
No browser-supplied owner is accepted at the ingestion boundary.
"""
from __future__ import annotations
import asyncio
from contextlib import contextmanager, suppress
from copy import deepcopy
from datetime import datetime
from decimal import Decimal, InvalidOperation
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import time
from zoneinfo import ZoneInfo

# The union of the business fields on pages 9-22 of the supplied V1.0.1 manual.
FIELDS = (
 ('35','MsgType','消息类型'), ('10176','MarketIndicator','业务类别'),
 ('537','QuoteType','报价类型'), ('117','QuoteID','意向报价ID'),
 ('18032','ClNegoID','本机构对话ID'), ('18033','NegotiationID','QT对话报价ID'),
 ('18034','QuoteSource','报价来源'), ('18035','Operator','操作方'),
 ('18004','OwnQtNO','本方QT号'), ('18003','TraderName','本方交易员'),
 ('18037','OppInstiShortName','对话人机构简称'), ('18036','OppInstiFullName','对话人机构全称'),
 ('18068','InterloOrgID','对话人统一社会信用代码'), ('18175','OppInstiInterBankID','对话人银行间代码'),
 ('18058','InterloNickID','对话人加密ID'), ('18006','InterloNickname','对话人名称'),
 ('18038','InterlocutorNote','对话人备注'), ('18021','Intermediary','QT中介代码'),
 ('18039','ChatType','聊天类型'), ('18040','ChatGroupName','群名称'), ('18136','ChatGroupID','群号'),
 ('48','SecurityID','债券代码'), ('55','Symbol','债券简称'), ('100','ExDestination','交易市场'),
 ('54','Side','操作方方向'), ('18042','BidPxType','bid价型'), ('132','BidPx','bid报价'),
 ('18052','BidPriceOri','bid原始价格'), ('18043','OfferPxType','ofr价型'),
 ('133','OfferPx','ofr报价'), ('18053','OfferPriceOri','ofr原始价格'),
 ('134','BidSize','bid面额（元）'), ('18050','BidSizeOri','bid原始面额（万）'),
 ('135','OfferSize','ofr面额（元）'), ('18051','OfferSizeOri','ofr原始面额（万）'),
 ('255','CreditRating','债项评级'), ('18023','SubjectRating','主体评级'),
 ('11050','ResidualMaturity','剩余期限'), ('10853','OriAgreementValue','QT原始估值'),
 ('58','Text','报价备注'), ('62','ValidUntilTime','报价有效时间'), ('60','TransactTime','业务发生时间'),
 ('18045','TransType','操作类型'), ('18170','TransTypeDtl','操作说明'), ('18026','Content','原始语料'),
 ('18166','BidTradeDate','bid交易日'), ('18167','BidSettlType','bid清算速度'),
 ('18168','OfrTradeDate','ofr交易日'), ('18169','OfrSettlType','ofr清算速度'),
)
COLUMNS = [{'key':name,'label':label,'tag':tag} for tag,name,label in FIELDS]
TAG_NAMES = {tag:name for tag,name,_ in FIELDS}
NAMES = {name for _,name,_ in FIELDS}
PRICE_FIELDS = {'BidPx','BidPxType','BidPriceOri','OfferPx','OfferPxType','OfferPriceOri',
                'BidSize','BidSizeOri','OfferSize','OfferSizeOri','BidTradeDate','BidSettlType',
                'OfrTradeDate','OfrSettlType','ValidUntilTime','Text','Content'}
MARKETS = {'XCFE':'IB','XSHG':'SH','XSHE':'SZ'}
QT_BROKERS = {'1':'国际','2':'信唐','3':'中诚','4':'平安','5':'国利','7':'上田'}


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',',':'))


def decimal_text(value, *, nonnegative=False):
    if value is None or value == '': return None
    if isinstance(value,bool) or not isinstance(value,(str,int,Decimal)):
        raise ValueError('QT_DECIMAL_TEXT_REQUIRED')
    text=str(value)
    if len(text)>64 or re.fullmatch(r'[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)',text) is None:
        raise ValueError('QT_DECIMAL_INVALID')
    try: number=Decimal(text)
    except InvalidOperation: raise ValueError('QT_DECIMAL_INVALID') from None
    if not number.is_finite() or nonnegative and number<0: raise ValueError('QT_DECIMAL_INVALID')
    return format(number,'f')


def normal_fields(raw):
    if not isinstance(raw,dict) or len(raw)>160: raise ValueError('QT_FIELDS_INVALID')
    result={}
    for key,value in raw.items():
        name=TAG_NAMES.get(str(key),str(key))
        if name not in NAMES:
            # Never retain FIX Logon passwords, arbitrary owner labels or vendor secrets.
            raise ValueError('QT_UNSUPPORTED_FIELD')
        if value is None: continue
        if isinstance(value,bool) or not isinstance(value,(str,int)) or len(str(value))>16384:
            raise ValueError('QT_FIELD_VALUE_INVALID')
        value=str(value)
        if '\x00' in value: raise ValueError('QT_FIELD_VALUE_INVALID')
        if name in result and result[name]!=value: raise ValueError('QT_FIELD_ALIAS_CONFLICT')
        result[name]=value
    if len(canonical(result).encode())>65536: raise ValueError('QT_RECORD_TOO_LARGE')
    if (result.get('MsgType'),result.get('MarketIndicator'),result.get('QuoteType'))!=('S','4','3'):
        raise ValueError('QT_DIALOGUE_QUOTE_REQUIRED')
    for key in ('OwnQtNO','NegotiationID','TransactTime','TransType'):
        if not result.get(key) or len(result[key])>160: raise ValueError('QT_REQUIRED_FIELD_MISSING')
    if result['TransType'] not in ('N','H','Z','S'): raise ValueError('QT_OPERATION_INVALID')
    if result['TransType'] in ('N','H'):
        for key in ('SecurityID','Symbol','ExDestination','Operator','Side'):
            if not result.get(key): raise ValueError('QT_REQUIRED_FIELD_MISSING')
        if result['ExDestination'] not in MARKETS or result['Operator'] not in ('1','2') or result['Side'] not in ('1','2','y'):
            raise ValueError('QT_IDENTITY_OR_SIDE_INVALID')
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,40}',result['SecurityID']):
            raise ValueError('QT_SECURITY_ID_INVALID')
    for key in ('BidPx','OfferPx'):
        if key in result: result[key]=decimal_text(result[key])
    for key in ('BidSize','OfferSize'):
        if key in result: result[key]=decimal_text(result[key],nonnegative=True)
    for key in ('BidPxType','OfferPxType'):
        if key in result and result[key] not in ('1','2','3','4'): raise ValueError('QT_PRICE_TYPE_INVALID')
    return result


class QtQuoteBook:
    """A private durable projection, keyed by account/dialogue/operator, not bond."""
    MAX_ROWS=20000
    configured=True
    def __init__(self, path, *, source_id, account_owners, timezone='Asia/Shanghai', clock=time.time, journal=None):
        if not isinstance(source_id,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,80}',source_id):
            raise ValueError('QT_SOURCE_ID_INVALID')
        if not isinstance(account_owners,dict) or not account_owners or any(
            not isinstance(k,str) or not k or not isinstance(v,str) or not v or len(k)>160 or len(v)>160
            for k,v in account_owners.items()): raise ValueError('QT_ACCOUNT_OWNER_MAPPING_REQUIRED')
        self.path=Path(path); self.path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
        if self.path.is_symlink(): raise ValueError('QT_DATABASE_SYMLINK_FORBIDDEN')
        self.source_id=source_id;self.owners=dict(account_owners);self.zone=ZoneInfo(timezone);self.clock=clock
        self.journal=Path(journal) if journal else None
        self._task=None;self.state='awaiting_adapter' if journal else 'adapter_not_configured';self.last_read_at=None
        with self.db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS qt_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS qt_event(id INTEGER PRIMARY KEY AUTOINCREMENT,event_id TEXT UNIQUE NOT NULL,
                  source_seq INTEGER UNIQUE NOT NULL,digest TEXT NOT NULL,fields TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS qt_current(own TEXT NOT NULL,negotiation TEXT NOT NULL,operator TEXT NOT NULL,
                  stamp REAL NOT NULL,source_seq INTEGER NOT NULL,revision INTEGER NOT NULL,status TEXT NOT NULL,fields TEXT NOT NULL,
                  PRIMARY KEY(own,negotiation,operator));
                CREATE TABLE IF NOT EXISTS qt_terminal(own TEXT NOT NULL,negotiation TEXT NOT NULL,
                  stamp REAL NOT NULL,source_seq INTEGER NOT NULL,PRIMARY KEY(own,negotiation));
            """)
            old=db.execute("SELECT value FROM qt_meta WHERE key='source_id'").fetchone()
            if old and old[0]!=source_id: raise ValueError('QT_SOURCE_NAMESPACE_MISMATCH')
            db.execute("INSERT OR IGNORE INTO qt_meta VALUES('source_id',?)",(source_id,))
        self.path.chmod(0o600)

    @contextmanager
    def db(self):
        db=sqlite3.connect(self.path,timeout=10);db.row_factory=sqlite3.Row
        try:
            with db:yield db
        finally:db.close()

    def stamp(self,text):
        if not isinstance(text,str) or not re.fullmatch(r'[0-9]{8}-[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?',text):
            raise ValueError('QT_BUSINESS_TIME_INVALID')
        try:return datetime.strptime(text,'%Y%m%d-%H:%M:%S.%f' if '.' in text else '%Y%m%d-%H:%M:%S').replace(tzinfo=self.zone).timestamp()
        except ValueError:raise ValueError('QT_BUSINESS_TIME_INVALID') from None

    def ingest(self, raw, *, event_id, sequence):
        return self._ingest(raw, event_id=event_id, sequence=sequence)

    def ingest_fix(self, raw):
        fields=normal_fields(raw)
        event_id='fix-'+hashlib.sha256(canonical(fields).encode()).hexdigest()
        return self._ingest(fields, event_id=event_id, sequence=None, allocate=True)

    def current_fix_keys(self, raw):
        fields=normal_fields(raw)
        event_id='fix-'+hashlib.sha256(canonical(fields).encode()).hexdigest()
        with self.db() as db:
            rows=db.execute('SELECT c.own,c.negotiation,c.operator FROM qt_current c JOIN qt_event e ON c.source_seq=e.source_seq WHERE e.event_id=?',(event_id,)).fetchall()
        return {(row['own'],row['negotiation'],row['operator']) for row in rows}

    def _ingest(self, raw, *, event_id, sequence, allocate=False):
        if not isinstance(event_id,str) or not event_id or len(event_id)>160 or (not allocate and (type(sequence) is not int or not 0<sequence<2**53)):
            raise ValueError('QT_STABLE_ADAPTER_SEQUENCE_REQUIRED')
        fields=normal_fields(raw);timestamp=self.stamp(fields['TransactTime'])
        if fields.get('ValidUntilTime'):self.stamp(fields['ValidUntilTime'])
        own,nego=fields['OwnQtNO'],fields['NegotiationID'];op=fields['TransType'];detail=fields.get('TransTypeDtl')
        # Do not expose unmapped accounts or silently guess ownership from names.
        if own not in self.owners:raise ValueError('QT_ACCOUNT_MAPPING_MISSING')
        operator=fields.get('Operator')
        target={'1':'1','2':'2','3':'1','4':'2','5':'1','6':'2','7':'1','8':'2'}.get(detail)
        if op in ('Z','S'):
            if detail is not None and target is None:raise ValueError('QT_CANCEL_SCOPE_INVALID')
            if operator is not None and operator not in ('1','2'):raise ValueError('QT_OPERATOR_INVALID')
            if target and operator and target!=operator:raise ValueError('QT_CANCEL_OPERATOR_CONFLICT')
            operator=target or operator or '2'
        encoded=canonical(fields);digest=hashlib.sha256(encoded.encode()).hexdigest()
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            old_event=db.execute('SELECT digest,source_seq FROM qt_event WHERE event_id=?',(event_id,)).fetchone()
            if old_event:
                if old_event['digest']!=digest or not allocate and old_event['source_seq']!=sequence:raise ValueError('QT_EVENT_ID_COLLISION')
                return False
            if allocate:sequence=db.execute('SELECT COALESCE(MAX(source_seq),0)+1 FROM qt_event').fetchone()[0]
            if db.execute('SELECT 1 FROM qt_event WHERE source_seq=?',(sequence,)).fetchone():raise ValueError('QT_SOURCE_SEQUENCE_COLLISION')
            cursor=db.execute('INSERT INTO qt_event(event_id,source_seq,digest,fields) VALUES(?,?,?,?)',(event_id,sequence,digest,encoded))
            revision=cursor.lastrowid
            terminal=db.execute('SELECT stamp,source_seq FROM qt_terminal WHERE own=? AND negotiation=?',(own,nego)).fetchone()
            old=db.execute('SELECT * FROM qt_current WHERE own=? AND negotiation=? AND operator=?',(own,nego,operator)).fetchone()
            if old and (timestamp,sequence)<=(old['stamp'],old['source_seq']):return False
            # H carries the full economic snapshot. Absent optional price/size fields are not inherited.
            previous=json.loads(old['fields']) if old else {}
            if op in ('N','H'):
                previous={k:v for k,v in previous.items() if k not in PRICE_FIELDS | {'CreditRating','SubjectRating','ResidualMaturity','OriAgreementValue','TransTypeDtl','ClNegoID'}}
                state='ENDED' if terminal else 'ACTIVE'
            else:
                state='WITHDRAWN' if op=='Z' else 'EXPIRED'
                if detail is None:state='SCOPE_UNCONFIRMED'
            merged={**previous,**fields}
            if op in ('Z','S') and detail in ('1','2','7','8'):
                if not terminal or (timestamp,sequence)>(terminal['stamp'],terminal['source_seq']):
                    db.execute('INSERT OR REPLACE INTO qt_terminal VALUES(?,?,?,?)',(own,nego,timestamp,sequence))
                    db.execute("UPDATE qt_current SET status='ENDED' WHERE own=? AND negotiation=?",(own,nego))
                state='ENDED'
            db.execute('INSERT OR REPLACE INTO qt_current VALUES(?,?,?,?,?,?,?,?)',
                       (own,nego,operator,timestamp,sequence,revision,state,canonical(merged)))
        return True

    def rows(self, owner):
        accounts=[k for k,v in self.owners.items() if v==owner]
        if not accounts:return []
        with self.db() as db:
            rows=db.execute('SELECT * FROM qt_current WHERE own IN ('+','.join('?' for _ in accounts)+') ORDER BY source_seq DESC LIMIT ?',(*accounts,self.MAX_ROWS+1)).fetchall()
        if len(rows)>self.MAX_ROWS:raise ValueError('QT_DETAIL_CAPACITY_EXCEEDED')
        result=[];today=datetime.fromtimestamp(self.clock(),self.zone).strftime('%Y%m%d')
        for record in rows:
            f=json.loads(record['fields']);market=f.get('ExDestination');security=f.get('SecurityID')
            # A cancel received before its quote is a tombstone, not an invented bond.
            if market not in MARKETS or not security:continue
            code=security+'.'+MARKETS[market]
            state=record['status']
            if state=='ACTIVE' and f['TransactTime'][:8]!=today:state='PREVIOUS_DAY'
            if state=='ACTIVE' and f.get('ValidUntilTime') and self.stamp(f['ValidUntilTime'])<=self.clock():state='EXPIRED'
            key=hashlib.sha256(canonical([self.source_id,record['own'],record['negotiation'],record['operator']]).encode()).hexdigest()
            row={'rowId':'qt-'+key,'code':code,'name':f.get('Symbol',code),'bondUniCode':None,
                 'flowId':record['source_seq'],'issueTime':int(record['stamp']*1000),'term':f.get('ResidualMaturity'),
                 'bondRating':f.get('CreditRating'),'issuerRating':f.get('SubjectRating'),'tags':[],
                 'ib':None,'io':None,'ibv':None,'iov':None,'ob':None,'oo':None,'obv':None,'oov':None,'obs':None,'oos':None,
                 'qt_fields':f,'qt_status':state,'qt_broker_name':QT_BROKERS.get(f.get('Intermediary','')),
                 'qt_compare_reasons':{},'qt_settlement':{},'source':'qt_private'}
            for side,prefix,price,size,typ,ori,day,settle,sidevalue in (
                ('bid','ob','BidPx','BidSize','BidPxType','BidPriceOri','BidTradeDate','BidSettlType','1'),
                ('ofr','oo','OfferPx','OfferSize','OfferPxType','OfferPriceOri','OfrTradeDate','OfrSettlType','2')):
                amount=decimal_text(f.get(size),nonnegative=True)
                volume=format(Decimal(amount)/10000,'f') if amount is not None else None
                row['ibv' if side=='bid' else 'iov']=volume
                row[prefix+'v']=volume
                row[prefix]=f.get(price);row[prefix+'s']='QT' if f.get(price) is not None else None
                row['qt_settlement'][side]={'1':'+0','2':'+1'}.get(f.get(settle))
                reason=None
                if state!='ACTIVE':reason=state
                elif record['operator']!='2':reason='OWN_SIDE_NOT_COUNTERPARTY'
                elif f.get('Side') not in (sidevalue,'y'):reason='OTHER_SIDE'
                elif f.get(typ)!='1':reason='NOT_MATURITY_YIELD'
                elif f.get(price) is None:reason='NO_NUMERIC_PRICE'
                elif f.get(day) and f[day]!=today:reason='OTHER_TRADE_DATE'
                elif f.get(settle) not in (None,'1','2'):reason='SETTLEMENT_UNCONFIRMED'
                elif f.get(ori):
                    try:
                        if Decimal(decimal_text(f[ori]))!=Decimal(f[price]):reason='QUALIFIED_PRICE'
                    except (ValueError,InvalidOperation):reason='QUALIFIED_PRICE'
                row['qt_compare_reasons'][side]=reason
            result.append(row)
        return result

    def status(self, owner):
        return {'configured':True,'mapped':owner in self.owners.values(),'state':self.state,
                'last_read_at':self.last_read_at,'scope':'own_received_dialogue_quotes',
                'fix_session_connected':False,'note':'本接口读取机构FIX接入器的已解码报价日志；不提供FIX登录会话'}

    def poll_once(self):
        if self.journal is None:return 0
        if self.journal.is_symlink():raise ValueError('QT_JOURNAL_SYMLINK_FORBIDDEN')
        flags=os.O_RDONLY|getattr(os,'O_NOFOLLOW',0)
        fd=os.open(self.journal,flags)
        with os.fdopen(fd,'rb') as stream:
            info=os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:raise ValueError('QT_JOURNAL_MUST_BE_PRIVATE')
            identity=f'{info.st_dev}:{info.st_ino}'
            with self.db() as db:
                old=db.execute("SELECT value FROM qt_meta WHERE key='journal'").fetchone()
            cursor=json.loads(old[0]) if old else {}
            offset=cursor.get('offset',0) if cursor.get('identity')==identity else 0
            if offset>info.st_size:raise ValueError('QT_JOURNAL_TRUNCATED')
            stream.seek(offset);count=0
            for _ in range(200):
                line=stream.readline(131073)
                if not line:break
                if len(line)>131072:raise ValueError('QT_JOURNAL_RECORD_TOO_LARGE')
                if not line.endswith(b'\n'):break
                try:item=json.loads(line)
                except (ValueError,UnicodeError):raise ValueError('QT_JOURNAL_RECORD_INVALID') from None
                if not isinstance(item,dict) or set(item)!={'event_id','sequence','fields'}:raise ValueError('QT_JOURNAL_ENVELOPE_INVALID')
                self.ingest(item['fields'],event_id=item['event_id'],sequence=item['sequence'])
                with self.db() as db:
                    db.execute("INSERT OR REPLACE INTO qt_meta VALUES('journal',?)",(canonical({'identity':identity,'offset':stream.tell()}),))
                count+=1
            self.last_read_at=self.clock();self.state='journal_readable'
            return count

    async def startup(self):
        if self.journal is None or self._task and not self._task.done():return
        async def follow():
            while True:
                try:await asyncio.to_thread(self.poll_once)
                except asyncio.CancelledError:raise
                except FileNotFoundError:self.state='awaiting_adapter'
                except (ValueError,OSError,sqlite3.Error):self.state='input_error'
                await asyncio.sleep(1)
        self._task=asyncio.create_task(follow(),name='qt-dialogue-journal')

    async def shutdown(self):
        if self._task:
            self._task.cancel()
            with suppress(asyncio.CancelledError):await self._task
            self._task=None


def configured_book(state_dir):
    path=os.environ.get('XUNJIA_QT_CONFIG','').strip()
    if not path:return None
    p=Path(path)
    if not p.is_absolute() or p.is_symlink() or p.stat().st_mode & 0o077 or p.stat().st_size>65536:
        raise ValueError('QT_CONFIG_MUST_BE_PRIVATE_ABSOLUTE_FILE')
    data=json.loads(p.read_text(encoding='utf8'))
    required={'enabled','source_id','account_owners','timezone','journal_path'}
    if not isinstance(data,dict) or set(data)!=required or type(data['enabled']) is not bool:
        raise ValueError('QT_CONFIG_INVALID')
    if not data['enabled']:return None
    journal=Path(data['journal_path'])
    if not journal.is_absolute():raise ValueError('QT_JOURNAL_ABSOLUTE_PATH_REQUIRED')
    return QtQuoteBook(Path(state_dir)/'qt-dialogues.sqlite',source_id=data['source_id'],
                       account_owners=data['account_owners'],timezone=data['timezone'],journal=journal)


class UnavailableQtBook:
    """An optional misconfigured private feed must not take public market service down."""
    configured=False
    state='configuration_error'
    def rows(self,owner):return []
    def status(self,owner):
        return {'configured':False,'mapped':False,'state':self.state,'code':'QT_CONFIGURATION_INVALID',
                'fix_session_connected':False,'note':'QT配置无效，私有报价未启用；公共行情继续运行'}
    async def startup(self):pass
    async def shutdown(self):pass
