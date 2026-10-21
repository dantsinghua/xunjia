"""Seven-day account archive and bounded binary chunks, not negotiation input."""
from __future__ import annotations
import base64
from contextlib import contextmanager
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import tempfile
import time
from .gateway_paths import CONTRACT,HISTORY_SECONDS,MAX_FILE_BYTES,CHUNK_BYTES
from .protocol import ProtocolError,canonical,digest,token

class InstitutionArchive:
    def __init__(self, root, *, clock=time.time):
        self.root=Path(root);self.root.mkdir(parents=True,exist_ok=True,mode=0o700)
        self.path=self.root/"archive.db";self.clock=clock
        with self.db() as db:
            db.executescript("CREATE TABLE IF NOT EXISTS ip_archive(id INTEGER PRIMARY KEY AUTOINCREMENT,workbench TEXT,account TEXT,native TEXT,peer TEXT,direction TEXT,stamp REAL,fingerprint TEXT,body TEXT,UNIQUE(workbench,account,native,direction)); CREATE INDEX IF NOT EXISTS ix_ip_archive_scope ON ip_archive(workbench,account,stamp); CREATE TABLE IF NOT EXISTS ip_media(workbench TEXT,account TEXT,hash TEXT,size INTEGER,chunks INTEGER,received_at REAL,state TEXT,PRIMARY KEY(workbench,account,hash)); CREATE TABLE IF NOT EXISTS ip_chunk(workbench TEXT,account TEXT,hash TEXT,position INTEGER,data BLOB,fingerprint TEXT,PRIMARY KEY(workbench,account,hash,position));")
        self.path.chmod(0o600)

    @contextmanager
    def db(self):
        db=sqlite3.connect(self.path,timeout=20);db.row_factory=sqlite3.Row;db.execute("PRAGMA synchronous=FULL")
        try:
            with db:yield db
        finally:db.close()

    @staticmethod
    def _scope(data):
        return token(data.get("workbench_id")),token(data.get("account_uid"))

    def accept(self, data):
        wb,uid=self._scope(data);p=data["payload"]
        if p.get("contract")!=CONTRACT:raise ProtocolError("ARCHIVE_CONTRACT_MISMATCH")
        if p.get("op")=="history":return self._history(wb,uid,p)
        if p.get("op")=="media_chunk":return self._chunk(wb,uid,p)
        raise ProtocolError("ARCHIVE_OPERATION_UNSUPPORTED")

    def _message(self, row):
        if not isinstance(row,dict):raise ProtocolError("ARCHIVE_MESSAGE_INVALID")
        if set(row)-{"native_id","peer_id","direction","occurred_at","text","type","session_kind","media","historical"}:raise ProtocolError("ARCHIVE_MESSAGE_FIELDS_INVALID")
        native=token(row.get("native_id"));peer=token(row.get("peer_id"));stamp=row.get("occurred_at")
        if type(stamp) not in (int,float) or not math.isfinite(stamp) or stamp>self.clock()+60:raise ProtocolError("ARCHIVE_MESSAGE_TIME_INVALID")
        if row.get("direction") not in ("in","out") or row.get("session_kind") not in ("private","group") or row.get("type") not in ("text","image","file"):raise ProtocolError("ARCHIVE_MESSAGE_TYPE_INVALID")
        if row["session_kind"]=="group" and not peer.startswith("g_"):raise ProtocolError("ARCHIVE_GROUP_ID_INVALID")
        text=row.get("text","")
        if not isinstance(text,str) or len(text.encode("utf8"))>131072:raise ProtocolError("ARCHIVE_TEXT_INVALID")
        media=row.get("media",[])
        if not isinstance(media,list) or len(media)>20:raise ProtocolError("ARCHIVE_MEDIA_INVALID")
        for item in media:
            if not isinstance(item,dict) or set(item)-{"sha256","name","size","mime","state"}:raise ProtocolError("ARCHIVE_MEDIA_INVALID")
            h=item.get("sha256")
            if h is not None and (not isinstance(h,str) or re.fullmatch(r"[a-f0-9]{64}",h) is None):raise ProtocolError("ARCHIVE_MEDIA_HASH_INVALID")
            if type(item.get("size")) is not int or not 0<=item["size"]<=MAX_FILE_BYTES:raise ProtocolError("ARCHIVE_MEDIA_SIZE_INVALID")
            name=item.get("name","")
            if not isinstance(name,str) or len(name)>200 or any(ord(c)<32 for c in name):raise ProtocolError("ARCHIVE_MEDIA_NAME_INVALID")
            if item.get("state") not in ("ready","pending","unavailable"):raise ProtocolError("ARCHIVE_MEDIA_STATE_INVALID")
            if item["state"]=="ready" and (not h or not item["size"]):raise ProtocolError("ARCHIVE_MEDIA_READY_WITHOUT_CONTENT")
            mime=item.get("mime","application/octet-stream")
            if not isinstance(mime,str) or len(mime)>120 or re.fullmatch(r"[a-zA-Z0-9.+_-]+/[a-zA-Z0-9.+_-]+",mime) is None:raise ProtocolError("ARCHIVE_MEDIA_MIME_INVALID")
        return {**row,"text":text,"media":media,"historical":True}

    def _history(self, wb, uid, payload):
        rows=payload.get("items")
        if not isinstance(rows,list) or not 1<=len(rows)<=100:raise ProtocolError("ARCHIVE_BATCH_INVALID")
        checked=[self._message(row) for row in rows]
        with self.db() as db:
            for row in checked:
                if row["occurred_at"]<self.clock()-HISTORY_SECONDS:continue
                fp=digest(row);args=(wb,uid,row["native_id"],row["direction"])
                old=db.execute("SELECT fingerprint,body FROM ip_archive WHERE workbench=? AND account=? AND native=? AND direction=?",args).fetchone()
                if old and old["fingerprint"]!=fp:
                    previous=json.loads(old["body"])
                    if {k:v for k,v in previous.items() if k!="media"}!={k:v for k,v in row.items() if k!="media"} or len(previous["media"])!=len(row["media"]):
                        raise ProtocolError("ARCHIVE_NATIVE_ID_CONFLICT")
                    for prior,current in zip(previous["media"],row["media"]):
                        if prior==current:continue
                        if (prior.get("state")=="ready" or current.get("state")!="ready"
                                or prior.get("name","")!=current.get("name","")
                                or prior.get("sha256") not in (None,current.get("sha256"))):
                            raise ProtocolError("ARCHIVE_NATIVE_MEDIA_CONFLICT")
                    db.execute("UPDATE ip_archive SET fingerprint=?,body=? WHERE workbench=? AND account=? AND native=? AND direction=?",(fp,canonical(row).decode(),*args))
                db.execute("INSERT OR IGNORE INTO ip_archive(workbench,account,native,peer,direction,stamp,fingerprint,body) VALUES(?,?,?,?,?,?,?,?)",(wb,uid,row["native_id"],row["peer_id"],row["direction"],row["occurred_at"],fp,canonical(row).decode()))
        return len(checked)

    def _media_path(self, wb, uid, h):
        scope=hashlib.sha256(canonical([wb,uid])).hexdigest()
        return self.root/"media"/scope/h

    def _chunk(self, wb, uid, p):
        h=p.get("sha256");size=p.get("size");pos=p.get("position");count=p.get("chunks")
        if not isinstance(h,str) or re.fullmatch(r"[a-f0-9]{64}",h) is None:raise ProtocolError("MEDIA_HASH_INVALID")
        if type(size) is not int or not 1<=size<=MAX_FILE_BYTES or type(pos) is not int or type(count) is not int or count!=(size+CHUNK_BYTES-1)//CHUNK_BYTES or not 0<=pos<count:raise ProtocolError("MEDIA_CHUNK_POSITION_INVALID")
        try:raw=base64.b64decode(p["data"],validate=True)
        except (ValueError,KeyError,TypeError):raise ProtocolError("MEDIA_CHUNK_ENCODING_INVALID") from None
        expected=CHUNK_BYTES if pos<count-1 else size-CHUNK_BYTES*pos
        if len(raw)!=expected:raise ProtocolError("MEDIA_CHUNK_SIZE_INVALID")
        part_hash=hashlib.sha256(raw).hexdigest()
        with self.db() as db:
            old=db.execute("SELECT size,chunks,state FROM ip_media WHERE workbench=? AND account=? AND hash=?",(wb,uid,h)).fetchone()
            if old and (old["size"],old["chunks"])!=(size,count):raise ProtocolError("MEDIA_IDENTITY_CONFLICT")
            if old and old["state"]=="ready":return
            db.execute("INSERT OR IGNORE INTO ip_media VALUES(?,?,?,?,?,?,?)",(wb,uid,h,size,count,self.clock(),"pending"))
            prior=db.execute("SELECT fingerprint FROM ip_chunk WHERE workbench=? AND account=? AND hash=? AND position=?",(wb,uid,h,pos)).fetchone()
            if prior and prior[0]!=part_hash:raise ProtocolError("MEDIA_CHUNK_CONFLICT")
            db.execute("INSERT OR IGNORE INTO ip_chunk VALUES(?,?,?,?,?,?)",(wb,uid,h,pos,raw,part_hash))
            number=db.execute("SELECT COUNT(*) FROM ip_chunk WHERE workbench=? AND account=? AND hash=?",(wb,uid,h)).fetchone()[0]
        if number!=count:return
        target=self._media_path(wb,uid,h);target.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
        fd,temp=tempfile.mkstemp(prefix=".media-",dir=target.parent);hashing=hashlib.sha256();total=0
        try:
            with os.fdopen(fd,"wb") as output,self.db() as db:
                for row in db.execute("SELECT data FROM ip_chunk WHERE workbench=? AND account=? AND hash=? ORDER BY position",(wb,uid,h)):
                    data=bytes(row[0]);output.write(data);hashing.update(data);total+=len(data)
                output.flush();os.fsync(output.fileno())
            if total!=size or hashing.hexdigest()!=h:raise ProtocolError("MEDIA_ASSEMBLED_HASH_MISMATCH")
            os.replace(temp,target);target.chmod(0o600)
            with self.db() as db:
                db.execute("UPDATE ip_media SET state=? WHERE workbench=? AND account=? AND hash=?",("ready",wb,uid,h))
                db.execute("DELETE FROM ip_chunk WHERE workbench=? AND account=? AND hash=?",(wb,uid,h))
        finally:
            if os.path.exists(temp):os.unlink(temp)

    def page(self, wb, uid, *, before=None, limit=50, peer=None):
        token(wb);token(uid)
        if type(limit) is not int or not 1<=limit<=100:raise ValueError("历史分页大小无效")
        args=[wb,uid,self.clock()-HISTORY_SECONDS];where="workbench=? AND account=? AND stamp>=?"
        if before is not None:
            if type(before) is not int or before<1:raise ValueError("历史游标无效")
            where+=" AND id<?";args.append(before)
        if peer:token(peer);where+=" AND peer=?";args.append(peer)
        with self.db() as db:rows=db.execute("SELECT id,body FROM ip_archive WHERE "+where+" ORDER BY id DESC LIMIT ?",(*args,limit+1)).fetchall()
        items=[]
        for row in rows[:limit]:
            value=json.loads(row["body"]);value["id"]=row["id"]
            for media in value["media"]:
                media["download_ready"]=bool(media.get("sha256") and self._media_path(wb,uid,media["sha256"]).is_file())
            items.append(value)
        return {"items":items,"has_more":len(rows)>limit,"next_cursor":rows[limit-1]["id"] if len(rows)>limit else None,"retention_days":7}

    def media(self, wb, uid, h):
        token(wb);token(uid)
        if not isinstance(h,str) or re.fullmatch(r"[a-f0-9]{64}",h) is None:raise ValueError("附件标识无效")
        with self.db() as db:
            rows=db.execute("SELECT body FROM ip_archive WHERE workbench=? AND account=? AND stamp>=?",(wb,uid,self.clock()-HISTORY_SECONDS))
            referenced=any(any(v.get("sha256")==h for v in json.loads(row[0]).get("media",[])) for row in rows)
        p=self._media_path(wb,uid,h)
        if not referenced or not p.is_file():raise LookupError("附件尚未到达或已经过期")
        return p

    def prune(self, *, limit=500):
        """Remove only this archive's expired data, never command/UNKNOWN ledgers."""
        if type(limit) is not int or not 1<=limit<=1000:raise ValueError("ARCHIVE_CLEANUP_LIMIT_INVALID")
        cutoff=self.clock()-HISTORY_SECONDS
        removed=0
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            count=db.execute("DELETE FROM ip_archive WHERE id IN (SELECT id FROM ip_archive WHERE stamp<? ORDER BY id LIMIT ?)",(cutoff,limit)).rowcount
            media=db.execute("SELECT m.workbench,m.account,m.hash FROM ip_media m WHERE m.received_at<? AND NOT EXISTS (SELECT 1 FROM ip_archive a,json_each(a.body,'$.media') j WHERE a.workbench=m.workbench AND a.account=m.account AND a.stamp>=? AND json_extract(j.value,'$.sha256')=m.hash) ORDER BY m.received_at LIMIT ?",(cutoff,cutoff,min(limit,50))).fetchall()
            for row in media:
                wb,uid,h=row["workbench"],row["account"],row["hash"]
                if not re.fullmatch(r"[a-f0-9]{64}",h):raise ProtocolError("ARCHIVE_STORED_HASH_INVALID")
                referenced=db.execute("SELECT 1 FROM ip_archive a,json_each(a.body,'$.media') m WHERE a.workbench=? AND a.account=? AND a.stamp>=? AND json_extract(m.value,'$.sha256')=? LIMIT 1",(wb,uid,cutoff,h)).fetchone()
                if referenced:continue
                target=self._media_path(wb,uid,h)
                if target.is_symlink() or not target.resolve().is_relative_to(self.root.resolve()):raise ProtocolError("ARCHIVE_MEDIA_STORAGE_CHANGED")
                target.unlink(missing_ok=True)
                db.execute("DELETE FROM ip_chunk WHERE workbench=? AND account=? AND hash=?",(wb,uid,h))
                db.execute("DELETE FROM ip_media WHERE workbench=? AND account=? AND hash=?",(wb,uid,h))
                removed+=1
        return {"history_removed":count,"media_removed":removed,"more_pending":count==limit or removed==min(limit,50),"retention_days":7,"command_ledgers_touched":False}
