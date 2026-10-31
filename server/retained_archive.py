"""Service-only archive retention; the collector's shared contract stays unchanged."""
from __future__ import annotations
from contextlib import contextmanager
import hashlib
import math
import os
import re
import sqlite3
import tempfile
import time

from relay.gateway_paths import HISTORY_SECONDS
from relay.institution_archive import InstitutionArchive as BaseInstitutionArchive, MediaIntegrityError
from relay.protocol import ProtocolError


class InstitutionArchive(BaseInstitutionArchive):
    def __init__(self, root, *, clock=time.time, db_timeout=20):
        self.db_timeout=db_timeout
        super().__init__(root,clock=clock)
        with self.db() as db:
            db.execute("CREATE TABLE IF NOT EXISTS ip_media_content(workbench TEXT,account TEXT,hash TEXT,size INTEGER,data BLOB NOT NULL,stored_at REAL,PRIMARY KEY(workbench,account,hash))")
            db.execute("CREATE TABLE IF NOT EXISTS ip_media_cache(workbench TEXT,account TEXT,hash TEXT,observed_at REAL NOT NULL,cached_at REAL,pruned_at REAL,code TEXT,PRIMARY KEY(workbench,account,hash))")
            db.execute("CREATE TABLE IF NOT EXISTS ip_archive_cleanup(singleton INTEGER PRIMARY KEY,position INTEGER,ceiling INTEGER)")
            if "ceiling" not in {r[1] for r in db.execute("PRAGMA table_info(ip_archive_cleanup)")}:db.execute("ALTER TABLE ip_archive_cleanup ADD COLUMN ceiling INTEGER")

    @contextmanager
    def db(self):
        db=sqlite3.connect(self.path,timeout=self.db_timeout);db.row_factory=sqlite3.Row;db.execute("PRAGMA synchronous=FULL")
        try:
            with db:yield db
        finally:db.close()

    def _chunk(self, wb, uid, payload):
        # Validation, duplicate handling and durable assembly belong to the
        # shared implementation. Persist the resulting bytes before returning
        # to the service manifest handler, which then acknowledges the input.
        super()._chunk(wb,uid,payload)
        h=payload['sha256']
        with self.db() as db:
            media=db.execute("SELECT size,state FROM ip_media WHERE workbench=? AND account=? AND hash=?",(wb,uid,h)).fetchone()
            stored=db.execute("SELECT 1 FROM ip_media_content WHERE workbench=? AND account=? AND hash=?",(wb,uid,h)).fetchone()
        if media is None or media['state']!='ready' or stored:return
        target=self._checked_media_path(wb,uid,h)
        content=target.read_bytes()
        if len(content)!=media['size'] or hashlib.sha256(content).hexdigest()!=h:raise MediaIntegrityError("附件内容校验失败")
        with self.db() as db:
            db.execute("INSERT OR IGNORE INTO ip_media_content VALUES(?,?,?,?,?,?)",(wb,uid,h,media['size'],content,self.clock()))
            db.execute("INSERT OR IGNORE INTO ip_media_cache(workbench,account,hash,observed_at,code) VALUES(?,?,?,?,'CACHED')",(wb,uid,h,self.clock()))

    def page(self, wb, uid, *, before=None, limit=50, peer=None):
        result=super().page(wb,uid,before=before,limit=limit,peer=peer)
        with self.db() as db:
            for row in result['items']:
                for media in row['media']:
                    if media.get('sha256') and not media['download_ready']:
                        media['download_ready']=db.execute("SELECT 1 FROM ip_media_content WHERE workbench=? AND account=? AND hash=?",(wb,uid,media['sha256'])).fetchone() is not None
        return result

    def media(self, wb, uid, h):
        try:return super().media(wb,uid,h)
        except MediaIntegrityError:raise
        except LookupError:
            # Restore only a missing cache for a currently authorized reference.
            # The parent performs the final reference, size and hash checks.
            target=self._checked_media_path(wb,uid,h)
            if target.exists():raise
            with self.db() as db:
                recent=self.clock()-HISTORY_SECONDS
                reference=db.execute("SELECT 1 FROM ip_native_media_ref WHERE workbench=? AND account=? AND hash=? AND stamp>=? LIMIT 1",(wb,uid,h,recent)).fetchone()
                if not reference:
                    reference=db.execute("SELECT 1 FROM ip_archive a,json_each(a.body,'$.media') m WHERE a.workbench=? AND a.account=? AND a.stamp>=? AND json_extract(m.value,'$.sha256')=? LIMIT 1",(wb,uid,recent,h)).fetchone()
                state=db.execute("SELECT state FROM ip_media WHERE workbench=? AND account=? AND hash=?",(wb,uid,h)).fetchone()
            if not reference or state is None or state['state']!='ready':raise
            self._restore_media(wb,uid,h)
            return super().media(wb,uid,h)

    def _checked_media_path(self, wb, uid, h):
        if not re.fullmatch(r"[a-f0-9]{64}",h):raise ProtocolError("ARCHIVE_STORED_HASH_INVALID")
        target=self._media_path(wb,uid,h)
        if (target.is_symlink() or any(parent.is_symlink() for parent in target.parents)
                or not target.resolve().is_relative_to(self.root.resolve())):
            raise ProtocolError("ARCHIVE_MEDIA_STORAGE_CHANGED")
        return target

    def _restore_media(self, wb, uid, h):
        """A recent authorized reference may reconstruct its cache from SQLite."""
        with self.db() as db:
            row=db.execute("SELECT size,data FROM ip_media_content WHERE workbench=? AND account=? AND hash=?",(wb,uid,h)).fetchone()
        if row is None:return
        content=bytes(row['data'])
        if len(content)!=row['size'] or hashlib.sha256(content).hexdigest()!=h:raise MediaIntegrityError("附件内容校验失败")
        target=self._checked_media_path(wb,uid,h);target.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
        fd,temp=tempfile.mkstemp(prefix='.media-',dir=target.parent)
        try:
            with os.fdopen(fd,'wb') as stream:
                stream.write(content);stream.flush();os.fsync(stream.fileno())
            with self.db() as db:
                db.execute("BEGIN IMMEDIATE")
                os.replace(temp,target);target.chmod(0o600)
                db.execute("INSERT INTO ip_media_cache VALUES(?,?,?,?,?,NULL,'CACHED') ON CONFLICT(workbench,account,hash) DO UPDATE SET cached_at=excluded.cached_at,pruned_at=NULL,code='CACHED'",(wb,uid,h,self.clock(),self.clock()))
        finally:
            if os.path.exists(temp):os.unlink(temp)

    def prune(self, *, limit=500):
        """Expire redundant media files; preserve history, references and content."""
        if type(limit) is not int or not 1<=limit<=1000:raise ValueError("ARCHIVE_CLEANUP_LIMIT_INVALID")
        now=self.clock();cutoff=now-30*86400;limit=min(limit,50)
        report={"history_removed":0,"media_removed":0,"protected":0,"observing":0,
                "retention_days":7,"download_retention_days":30,"command_ledgers_touched":False,
                "history_preserved":True,"more_pending":False}
        with self.db() as db:
            cursor=db.execute("SELECT position,ceiling FROM ip_archive_cleanup WHERE singleton=1").fetchone()
            position=cursor[0] if cursor else 0;ceiling=cursor[1] if cursor else None
            if ceiling is None or position>=ceiling:
                position=0;ceiling=db.execute("SELECT COALESCE(MAX(rowid),0) FROM ip_media").fetchone()[0]
            query="SELECT rowid AS position,* FROM ip_media WHERE rowid>? AND rowid<=? ORDER BY rowid LIMIT ?"
            rows=db.execute(query,(position,ceiling,limit)).fetchall()
            if not rows and position:
                ceiling=db.execute("SELECT COALESCE(MAX(rowid),0) FROM ip_media").fetchone()[0]
                rows=db.execute(query,(0,ceiling,limit)).fetchall()
            if rows:db.execute("INSERT INTO ip_archive_cleanup VALUES(1,?,?) ON CONFLICT(singleton) DO UPDATE SET position=excluded.position,ceiling=excluded.ceiling",(rows[-1]['position'],ceiling))
        for row in rows:
            wb,uid,h=row['workbench'],row['account'],row['hash']
            with self.db() as db:
                db.execute("INSERT OR IGNORE INTO ip_media_cache(workbench,account,hash,observed_at) VALUES(?,?,?,?)",(wb,uid,h,now))
                cache=db.execute("SELECT * FROM ip_media_cache WHERE workbench=? AND account=? AND hash=?",(wb,uid,h)).fetchone()
            stamp=cache['cached_at'] if cache['cached_at'] is not None else row['received_at']
            if type(stamp) not in (int,float) or not math.isfinite(stamp) or stamp<=0:stamp=cache['observed_at']
            if stamp>cutoff:
                report['observing']+=1;continue
            if cache['pruned_at'] is not None:continue
            if row['state']!='ready':
                report['protected']+=1
                with self.db() as db:db.execute("UPDATE ip_media_cache SET code='INCOMPLETE_MEDIA_PRESERVED' WHERE workbench=? AND account=? AND hash=?",(wb,uid,h))
                continue
            target=self._checked_media_path(wb,uid,h)
            with self.db() as db:stored=db.execute("SELECT size,data FROM ip_media_content WHERE workbench=? AND account=? AND hash=?",(wb,uid,h)).fetchone()
            content=bytes(stored['data']) if stored is not None else target.read_bytes() if target.is_file() else None
            if content is None or len(content)!=row['size'] or hashlib.sha256(content).hexdigest()!=h:
                report['protected']+=1
                with self.db() as db:db.execute("UPDATE ip_media_cache SET code='CONTENT_NOT_VERIFIED' WHERE workbench=? AND account=? AND hash=?",(wb,uid,h))
                continue
            # Commit verified bytes before unlink; a crash can only leave an extra copy.
            with self.db() as db:
                db.execute("INSERT OR IGNORE INTO ip_media_content VALUES(?,?,?,?,?,?)",(wb,uid,h,row['size'],content,now))
            with self.db() as db:
                db.execute("BEGIN IMMEDIATE")
                current=db.execute("SELECT cached_at,pruned_at FROM ip_media_cache WHERE workbench=? AND account=? AND hash=?",(wb,uid,h)).fetchone()
                if current is None or current['cached_at']!=cache['cached_at'] or current['pruned_at'] is not None:continue
                # A currently readable reference protects concurrent FileResponse
                # readers; the next hourly round retries after it leaves the view.
                recent=now-HISTORY_SECONDS-3600
                referenced=db.execute("SELECT 1 FROM ip_native_media_ref WHERE workbench=? AND account=? AND hash=? AND stamp>=? LIMIT 1",(wb,uid,h,recent)).fetchone()
                if not referenced:
                    referenced=db.execute("SELECT 1 FROM ip_archive a,json_each(a.body,'$.media') m WHERE a.workbench=? AND a.account=? AND a.stamp>=? AND json_extract(m.value,'$.sha256')=? LIMIT 1",(wb,uid,recent,h)).fetchone()
                if referenced:
                    report['protected']+=1
                    db.execute("UPDATE ip_media_cache SET code='RECENT_MEDIA_REFERENCE' WHERE workbench=? AND account=? AND hash=?",(wb,uid,h))
                    continue
                target=self._checked_media_path(wb,uid,h)
                existed=target.is_file();target.unlink(missing_ok=True)
                db.execute("UPDATE ip_media_cache SET pruned_at=?,code='FILE_CACHE_PRUNED' WHERE workbench=? AND account=? AND hash=?",(now,wb,uid,h))
            report['media_removed']+=int(existed)
        return report
