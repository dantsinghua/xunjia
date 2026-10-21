"""Durable institutional directory. One consumer, many authenticated workspaces."""
from __future__ import annotations
from contextlib import contextmanager
import json
import math
import os
from pathlib import Path
import sqlite3
import threading
import time
from .gateway_paths import CONTRACT, DIRECTORY_PATH, ORGANIZATION_PATH, HISTORY_SECONDS, OFFLINE_SECONDS, account_paths
from .protocol import Codec, OwnerKeys, ProtocolError, canonical, digest, token

class InstitutionDirectory:
    def __init__(self, path, keys, *, clock=time.time):
        self.path=Path(path);self.path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
        self.keys,self.clock=keys,clock;self.lock=threading.RLock()
        self.codec=Codec(OwnerKeys(keys.owner_id,keys.seal_key,keys.sign_key),clock=clock)
        with self.db() as db:
            db.executescript("CREATE TABLE IF NOT EXISTS ip_directory_identity(singleton INTEGER PRIMARY KEY,identity TEXT); CREATE TABLE IF NOT EXISTS ip_snapshot(kind TEXT,workbench TEXT,account TEXT,revision INTEGER,stamp REAL,body TEXT,fingerprint TEXT,PRIMARY KEY(kind,workbench,account)); CREATE TABLE IF NOT EXISTS ip_input(seq INTEGER PRIMARY KEY AUTOINCREMENT,path TEXT,state TEXT,name TEXT,blob BLOB,code TEXT,at REAL); CREATE TABLE IF NOT EXISTS ip_directory_error(kind TEXT PRIMARY KEY,code TEXT,at REAL);")
            row=db.execute("SELECT identity FROM ip_directory_identity WHERE singleton=1").fetchone()
            if row and row[0]!=keys.institution_id:raise ProtocolError("DIRECTORY_INSTITUTION_CHANGED")
            db.execute("INSERT OR IGNORE INTO ip_directory_identity VALUES(1,?)",(keys.institution_id,))
        self.path.chmod(0o600)

    @contextmanager
    def db(self):
        db=sqlite3.connect(self.path,timeout=15);db.row_factory=sqlite3.Row
        db.execute("PRAGMA synchronous=FULL")
        try:
            with db:yield db
        finally:db.close()

    def _decode(self, blob):
        value=self.codec._authenticated(blob)
        if value.get("owner_user_id")!=self.keys.owner_id:raise ProtocolError("INSTITUTION_DIRECTORY_OWNER_MISMATCH")
        binding=self.keys.binding(token(value.get("workbench_id")),token(value.get("account_uid")))
        Codec(binding,clock=self.clock)._validate(value,allow_expired=True)
        return value

    def put(self, data):
        if data.get("owner_user_id")!=self.keys.owner_id:raise ProtocolError("INSTITUTION_DIRECTORY_OWNER_MISMATCH")
        p=data["payload"]
        if p.get("contract")!=CONTRACT:raise ProtocolError("DIRECTORY_CONTRACT_UNSUPPORTED")
        kind=p.get("op");wb=token(data["workbench_id"]);uid=token(data["account_uid"])
        stamp=p.get("updated_at");revision=p.get("revision")
        if type(stamp) not in (int,float) or not math.isfinite(stamp) or stamp>self.clock()+60 or type(revision) is not int or revision<1:raise ProtocolError("DIRECTORY_REVISION_INVALID")
        if kind=="directory":
            if data["kind"]!="manifest" or uid!="directory":raise ProtocolError("DIRECTORY_IDENTITY_INVALID")
            accounts=p.get("accounts")
            if not isinstance(accounts,list) or len(accounts)>100:raise ProtocolError("DIRECTORY_ACCOUNTS_INVALID")
            seen=set()
            for item in accounts:
                if not isinstance(item,dict) or item.get("channel")!="qidian" or type(item.get("online")) is not bool:raise ProtocolError("DIRECTORY_ACCOUNT_INVALID")
                token(item.get("account_uid"));token(item.get("account_id"))
                if item["account_uid"] in seen:raise ProtocolError("DIRECTORY_DUPLICATE_ACCOUNT")
                seen.add(item["account_uid"])
                capabilities=item.get("capabilities",[])
                if not isinstance(capabilities,list) or any(not isinstance(v,str) or len(v)>80 for v in capabilities):raise ProtocolError("DIRECTORY_CAPABILITIES_INVALID")
                if item.get("paths")!=account_paths(wb,item["account_uid"]):raise ProtocolError("DIRECTORY_PATH_MISMATCH")
        elif kind=="friends":
            if data["kind"]!="contacts" or not isinstance(p.get("contacts"),list):raise ProtocolError("FRIENDS_INVALID")
        elif kind=="organization":
            if data["kind"]!="manifest" or uid!="organization":raise ProtocolError("ORGANIZATION_IDENTITY_INVALID")
            state=p.get("state")
            if state not in ("ready","empty"):
                with self.db() as db:db.execute("INSERT INTO ip_directory_error VALUES(?,?,?) ON CONFLICT(kind) DO UPDATE SET code=excluded.code,at=excluded.at",(kind,"ORGANIZATION_REFRESH_FAILED",self.clock()))
                return False
            org=p.get("org")
            if not isinstance(org,dict) or not isinstance(org.get("departments"),list) or not isinstance(org.get("members"),list):raise ProtocolError("ORGANIZATION_INVALID")
            wb="institution"
        else:raise ProtocolError("DIRECTORY_OPERATION_UNSUPPORTED")
        fp=digest(p)
        with self.lock,self.db() as db:
            old=db.execute("SELECT * FROM ip_snapshot WHERE kind=? AND workbench=? AND account=?",(kind,wb,uid)).fetchone()
            if old:
                # Different source collectors may publish the institution-wide org.
                if stamp<old["stamp"] or kind!="organization" and revision<old["revision"]:return False
                if revision==old["revision"] and stamp==old["stamp"]:
                    if fp!=old["fingerprint"]:raise ProtocolError("DIRECTORY_REVISION_CONFLICT")
                    return False
            db.execute("INSERT INTO ip_snapshot VALUES(?,?,?,?,?,?,?) ON CONFLICT(kind,workbench,account) DO UPDATE SET revision=excluded.revision,stamp=excluded.stamp,body=excluded.body,fingerprint=excluded.fingerprint",(kind,wb,uid,revision,stamp,canonical(p).decode(),fp))
            db.execute("DELETE FROM ip_directory_error WHERE kind=?",(kind,))
        return True

    def _input(self, seq, path, blob):
        value=self._decode(blob)
        operation=value["payload"].get("op")
        expected=(DIRECTORY_PATH if operation=="directory" else ORGANIZATION_PATH if operation=="organization" else account_paths(value["workbench_id"],value["account_uid"])["friends_path"])
        if path!=expected:raise ProtocolError("DIRECTORY_PHYSICAL_PATH_MISMATCH")
        self.put(value)
        with self.db() as db:db.execute("UPDATE ip_input SET state=? WHERE seq=?",("APPLIED",seq))

    def consume(self, client, *, limit=100):
        if not client.path:raise ProtocolError("EXPLICIT_PATH_REQUIRED")
        done=0
        with self.db() as db:stored=db.execute("SELECT seq,path,blob FROM ip_input WHERE state=? AND path=? ORDER BY seq LIMIT ?",("STORED",client.path,limit)).fetchall()
        for row in stored:
            try:self._input(row["seq"],row["path"],bytes(row["blob"]));done+=1
            except ProtocolError as exc:
                with self.db() as db:db.execute("UPDATE ip_input SET state=?,code=? WHERE seq=?",("QUARANTINED",str(exc),row["seq"]))
        count=min(client.pending(),max(0,limit-done))
        with self.lock:
            for _ in range(count):
                with self.db() as db:seq=db.execute("INSERT INTO ip_input(path,state,at) VALUES(?,?,?)",(client.path,"RECEIVING",self.clock())).lastrowid
                try:
                    value=client.download()
                    with self.db() as db:db.execute("UPDATE ip_input SET state=?,name=?,blob=? WHERE seq=?",("EMPTY" if value is None else "STORED",value.name if value else "",value.content if value else None,seq))
                    if value is None:break
                    self._input(seq,client.path,value.content)
                    done+=1
                except ProtocolError as exc:
                    with self.db() as db:db.execute("UPDATE ip_input SET state=?,code=? WHERE seq=?",("QUARANTINED",str(exc),seq))
                except Exception:
                    with self.db() as db:db.execute("UPDATE ip_input SET state=?,code=? WHERE seq=?",("UNKNOWN","DIRECTORY_DOWNLOAD_OR_STORE_FAILED",seq))
                    raise
        return done

    def catalog(self):
        now=self.clock()
        with self.db() as db:rows=db.execute("SELECT * FROM ip_snapshot WHERE kind=? ORDER BY workbench",("directory",)).fetchall()
        workbenches=[];online_pairs={}
        for row in rows:
            p=json.loads(row["body"]);stale=not 0<=now-row["stamp"]<OFFLINE_SECONDS
            accounts=[]
            for item in p["accounts"]:
                online=not stale and item["online"]
                value={**item,"available":online,"stale":not online,"updated_at":row["stamp"],"state":"online" if online else "offline","capabilities":item.get("capabilities",[]) if online else []}
                accounts.append(value)
                if online:online_pairs.setdefault(item["account_uid"],[]).append(value)
            workbenches.append({"workbench_id":row["workbench"],"name":p.get("workbench_name") or row["workbench"],"updated_at":row["stamp"],"stale":stale,"accounts":accounts})
        for rows in online_pairs.values():
            if len(rows)>1:
                for row in rows:row.update(available=False,stale=True,state="conflict")
        return {"workbenches":workbenches,"contract":CONTRACT,"offline_seconds":OFFLINE_SECONDS}

    def account(self, wb, uid):
        for workbench in self.catalog()["workbenches"]:
            if workbench["workbench_id"]==wb:
                for row in workbench["accounts"]:
                    if row["account_uid"]==uid:return row
        return None

    def contacts(self, wb, uid, owner):
        account=self.account(wb,uid)
        if account is None:raise ValueError("所选账号没有机构目录记录")
        with self.db() as db:
            friend=db.execute("SELECT * FROM ip_snapshot WHERE kind=? AND workbench=? AND account=?",("friends",wb,uid)).fetchone()
            org=db.execute("SELECT * FROM ip_snapshot WHERE kind=?",("organization",)).fetchone()
            error=db.execute("SELECT * FROM ip_directory_error WHERE kind=?",("organization",)).fetchone()
        friends=json.loads(friend["body"]) if friend else {};organization=json.loads(org["body"]) if org else {}
        return {"identity":{"owner_user_id":owner,"workbench_id":wb,"account_uid":uid},"source":"its-relay","channel":"qidian","workbench_id":wb,"account_uid":uid,"contacts":friends.get("contacts",[]),"org":organization.get("org",{}),"org_state":"error" if error else organization.get("state","unreported"),"org_error":error["code"] if error else None,"org_updated_at":org["stamp"] if org else None,"updated_at":account["updated_at"],"stale":not account["available"],"available":account["available"],"publication_id":"directory-"+str(account["updated_at"]),"publication_hash":digest([friends,organization]),"friends_updated_at":friend["stamp"] if friend else None}
