"""Owner-scoped durable QA conversations; no dependency on collector availability."""
from contextlib import contextmanager
import json
from pathlib import Path
import re
import sqlite3
import time
import uuid


class QAHistory:
    session_table = "qa_session"
    turn_table = "qa_turn"
    meta_table = "qa_history_meta"
    import_table = "qa_history_import"

    def __init__(self,path):
        self.path=Path(path).absolute()
        if self.path.absolute()!=self.path.resolve():raise ValueError('QA_HISTORY_UNSAFE')
        self.path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
        with self.db(write=False) as db:
            db.executescript('''CREATE TABLE IF NOT EXISTS qa_session(
                id TEXT PRIMARY KEY,owner TEXT NOT NULL,title TEXT NOT NULL,created_at REAL,updated_at REAL);
                CREATE TABLE IF NOT EXISTS qa_turn(
                id TEXT PRIMARY KEY,owner TEXT NOT NULL,session_id TEXT NOT NULL,request_id TEXT NOT NULL,
                question TEXT NOT NULL,state TEXT NOT NULL,result TEXT NOT NULL DEFAULT '{}',
                created_at REAL,updated_at REAL,UNIQUE(owner,request_id));
                CREATE INDEX IF NOT EXISTS qa_session_owner ON qa_session(owner,updated_at,id);
                CREATE INDEX IF NOT EXISTS qa_turn_session ON qa_turn(session_id,created_at,id);
                CREATE TABLE IF NOT EXISTS qa_history_meta(key TEXT PRIMARY KEY,value REAL);
                CREATE TABLE IF NOT EXISTS qa_history_import(source TEXT PRIMARY KEY,session_id TEXT,imported_at REAL);''')
            db.execute("INSERT OR IGNORE INTO qa_history_meta VALUES('created_at',?)",(time.time(),))
        self.path.chmod(0o600)
        self.receipt_source = self.path.parent / "qa-workflow.sqlite3"

    @contextmanager
    def db(self, *, write=True):
        c=sqlite3.connect(self.path,timeout=15);c.row_factory=sqlite3.Row;c.execute('PRAGMA synchronous=FULL')
        try:
            with c:
                if write:c.execute("BEGIN IMMEDIATE")
                yield c
        finally:c.close()

    def owner_lock(self,db,owner):
        # SQLite serializes writers with BEGIN IMMEDIATE.
        pass

    def create(self,owner):
        now=time.time();sid=uuid.uuid4().hex
        with self.db() as db:db.execute(f'INSERT INTO {self.session_table}(id,owner,title,created_at,updated_at) VALUES(?,?,?,?,?)',(sid,owner,'新会话',now,now))
        return {'id':sid,'title':'新会话','created_at':now,'updated_at':now}

    def list(self,owner,before=None):
        with self.db(write=False) as db:
            params=[owner];where='owner=?'
            if before:
                cursor=db.execute(f'SELECT updated_at FROM {self.session_table} WHERE owner=? AND id=?',(owner,before)).fetchone()
                if cursor is None:raise LookupError('QA_SESSION_NOT_FOUND')
                where+=' AND (updated_at<? OR (updated_at=? AND id<?))';params.extend([cursor['updated_at'],cursor['updated_at'],before])
            rows=db.execute(f'SELECT id,title,created_at,updated_at FROM {self.session_table} WHERE '+where+' ORDER BY updated_at DESC,id DESC LIMIT 51',params).fetchall()
        return {'items':[dict(row) for row in rows[:50]],'has_more':len(rows)>50,'next_before':rows[49]['id'] if len(rows)>50 else None}

    def read(self,owner,sid):
        with self.db(write=False) as db:
            session=db.execute(f'SELECT id,title,created_at,updated_at FROM {self.session_table} WHERE id=? AND owner=?',(sid,owner)).fetchone()
            if session is None:raise LookupError('QA_SESSION_NOT_FOUND')
            rows=db.execute(f'SELECT * FROM {self.turn_table} WHERE session_id=? AND owner=? ORDER BY created_at,id',(sid,owner)).fetchall()
        messages=[]
        for row in rows:
            common={'request_id':row['request_id'],'created_at':row['created_at']}
            if row['question']:messages.append({**common,'id':row['id']+'-user','role':'user','text':row['question'],'state':'complete'})
            result=json.loads(row['result'])
            state='failed' if row['state']=='pending' and time.time()-row['updated_at']>600 else row['state']
            text=result.get('answer','') if state=='complete' else '正在生成回答…' if state=='pending' else '上次问答未完成，请重试'
            messages.append({**common,'id':row['id']+'-ai','role':'ai','text':text,'state':state,'result':result})
        return {'session':dict(session),'messages':messages}

    def restore_receipts(self,owner,workflow):
        """Preserve real old query receipts, explicitly not invented user messages."""
        source=self.receipt_source
        if source is None or not source.is_file() or source.resolve()!=source.absolute():return
        with self.db(write=False) as db:
            cutoff=db.execute(f"SELECT value FROM {self.meta_table} WHERE `key`='created_at'").fetchone()['value']
            imported={r['source'] for r in db.execute(f'SELECT source FROM {self.import_table}')}
        original=sqlite3.connect(source.as_uri()+'?mode=ro',uri=True,timeout=2);original.row_factory=sqlite3.Row
        try:
            if not original.execute("SELECT 1 FROM sqlite_master WHERE name='qa_run'").fetchone():return
            rows=original.execute("SELECT nonce,expires,facts FROM qa_run WHERE owner=? AND closed=1 AND receipt<>'' AND expires<? ORDER BY expires DESC",(owner,cutoff+180))
            count=0
            for row in rows:
                if row['nonce'] in imported:continue
                if len(row['facts'])>262144:continue
                try:
                    result=workflow.render(json.loads(row['facts']))
                    result['answer']='【从已保存查询回执恢复，原始问题未保存】\n'+result['answer']
                    result.update(requires_selection=False,candidates=[],notice='历史查询回执，不代表当前报价。')
                except (ValueError,KeyError,TypeError,AttributeError):continue
                sid=uuid.uuid4().hex;tid=uuid.uuid4().hex;stamp=row['expires']-180
                with self.db() as db:
                    self.owner_lock(db,owner)
                    if db.execute(f'SELECT 1 FROM {self.import_table} WHERE source=?',(row['nonce'],)).fetchone():continue
                    db.execute(f'INSERT INTO {self.session_table}(id,owner,title,created_at,updated_at) VALUES(?,?,?,?,?)',(sid,owner,'历史查询回执',stamp,stamp))
                    db.execute(f'INSERT INTO {self.turn_table}(id,owner,session_id,request_id,question,state,result,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)',(tid,owner,sid,'restored-'+row['nonce'],'','complete',json.dumps(result,ensure_ascii=False),stamp,stamp))
                    db.execute(f'INSERT INTO {self.import_table}(source,session_id,imported_at) VALUES(?,?,?)',(row['nonce'],sid,time.time()))
                count+=1
                if count>=50:break
        finally:original.close()

    def claim(self,owner,sid,request_id,question):
        if not isinstance(request_id,str) or re.fullmatch(r'[A-Za-z0-9_-]{8,128}',request_id) is None:raise ValueError('请求标识无效')
        with self.db() as db:
            self.owner_lock(db,owner)
            session=db.execute(f'SELECT * FROM {self.session_table} WHERE id=? AND owner=?',(sid,owner)).fetchone()
            if session is None:raise LookupError('QA_SESSION_NOT_FOUND')
            row=db.execute(f'SELECT * FROM {self.turn_table} WHERE owner=? AND request_id=?',(owner,request_id)).fetchone()
            if row:
                if row['session_id']!=sid or row['question']!=question:raise ValueError('请求标识已用于另一个问题')
                if row['state']=='complete':return row['id'],json.loads(row['result'])
                if row['state']=='pending' and time.time()-row['updated_at']<=600:raise BlockingIOError('该问题仍在处理，请刷新会话查看结果')
                # A new attempt identity fences late completion from an old
                # process. Only this explicit user request retries the read.
                tid=uuid.uuid4().hex if row['state']=='pending' else row['id']
                db.execute(f"UPDATE {self.turn_table} SET id=?,state='pending',updated_at=? WHERE id=?",(tid,time.time(),row['id']))
                return tid,None
            now=time.time();tid=uuid.uuid4().hex
            first=db.execute(f'SELECT 1 FROM {self.turn_table} WHERE session_id=? LIMIT 1',(sid,)).fetchone() is None
            db.execute(f'INSERT INTO {self.turn_table}(id,owner,session_id,request_id,question,state,result,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)',(tid,owner,sid,request_id,question,'pending','{}',now,now))
            db.execute(f'UPDATE {self.session_table} SET title=?,updated_at=? WHERE id=?',(question[:30] if first else session['title'],now,sid))
            return tid,None

    def finish(self,owner,tid,result=None):
        with self.db() as db:
            self.owner_lock(db,owner)
            row=db.execute(f'SELECT session_id FROM {self.turn_table} WHERE owner=? AND id=?',(owner,tid)).fetchone()
            if not row:raise LookupError('QA_REQUEST_NOT_FOUND')
            now=time.time()
            db.execute(f'UPDATE {self.turn_table} SET state=?,result=?,updated_at=? WHERE owner=? AND id=?',
                ('complete' if result is not None else 'failed',json.dumps(result or {},ensure_ascii=False),now,owner,tid))
            db.execute(f'UPDATE {self.session_table} SET updated_at=? WHERE id=?',(now,row['session_id']))


class OceanBaseHistory(QAHistory):
    """Reuse the authenticated business connection; schema is upgrade-owned.

    All table names and the MySQL row-lock syntax are explicit. No SQLite SQL
    is rewritten, and constructing this store performs only schema reads.
    """
    session_table = '`xunjia_business`.`xj2_qa_session`'
    turn_table = '`xunjia_business`.`xj2_qa_turn`'
    meta_table = '`xunjia_business`.`xj2_qa_meta`'
    import_table = '`xunjia_business`.`xj2_qa_import`'

    def __init__(self, database, *, receipt_source=None):
        if not database.mysql:
            raise ValueError('QA_HISTORY_OCEANBASE_CONNECTION_REQUIRED')
        self.database = database
        self.receipt_source = Path(receipt_source).absolute() if receipt_source is not None else None
        try:
            with self.db(write=False) as tx:
                row=tx.execute('SELECT DATABASE() AS database_name').fetchone()
                if row is None or row['database_name']!='xunjia_business':
                    raise ValueError('QA_HISTORY_DATABASE_IDENTITY_MISMATCH')
                for table,columns in ((self.session_table,'id,owner,title,created_at,updated_at'),
                        (self.turn_table,'id,owner,session_id,request_id,question,state,result,created_at,updated_at'),
                        (self.meta_table,'`key`,value'),(self.import_table,'source,session_id,imported_at')):
                    tx.execute(f'SELECT {columns} FROM {table} LIMIT 0').fetchall()
                if not tx.execute(f"SELECT value FROM {self.meta_table} WHERE `key`='created_at'").fetchone():
                    raise ValueError('QA_HISTORY_SCHEMA_MIGRATION_REQUIRED')
        except Exception:
            raise ValueError('QA_HISTORY_SCHEMA_MIGRATION_REQUIRED_VERIFY_XUNJIA_BUSINESS') from None

    @contextmanager
    def db(self, *, write=True):
        with self.database.transaction(write=write) as tx:
            yield tx

    def owner_lock(self, db, owner):
        if not isinstance(owner,str) or not owner or len(owner)>160:
            raise ValueError('QA_HISTORY_OWNER_INVALID')
        key='owner:'+owner
        db.execute(f'INSERT IGNORE INTO {self.meta_table} (`key`,value) VALUES(?,0)',(key,))
        db.execute(f'SELECT `key` FROM {self.meta_table} WHERE `key`=? FOR UPDATE',(key,)).fetchone()
