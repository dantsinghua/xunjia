"""Institution-visible discovery with one account per authenticated workspace."""
from __future__ import annotations
import json
import uuid
from relay.gateway_paths import CONTRACT
from relay.protocol import token

class InstitutionSelection:
    def __init__(self, repository, transport, runtime):
        self.repository,self.transport,self.institution=repository,transport,runtime
        self._lock=runtime._lock

    def _record_check(self, tx, owner, binding, book, route='oss'):
        row=tx.execute('SELECT config_json FROM xj2_binding WHERE owner_id=?',(owner,)).fetchone()
        preferences=json.loads(row['config_json'])
        checks=preferences.setdefault('collector_directory_checks',{})
        checks[route]={'workbench_id':binding['workbench_id'],'account_uid':binding['account_uid'],
                       'version':binding['version'],'stale':bool(book.get('stale',True)),
                       'updated_at':book.get('updated_at'),'checked_at':self.repository.clock(),
                       'publication_id':book.get('publication_id'),'publication_hash':book.get('publication_hash')}
        tx.execute('UPDATE xj2_binding SET config_json=? WHERE owner_id=?',
                   (json.dumps(preferences,ensure_ascii=False,separators=(',',':')),owner))

    @property
    def configured(self):return True

    def _ready(self):
        if not self.institution.configured:raise ValueError("请先保存本机构文件中转配置")

    def catalog(self, owner, *, refresh=False):
        current=self.repository.binding_for(owner)
        selected={k:current[k] for k in ("workbench_id","account_uid","version")} if current else None
        if not self.institution.configured:return {"workbenches":[],"selected":selected,"note":"请先保存机构文件中转连接"}
        if refresh:self.institution.tick()
        result=self.institution.directory.catalog()
        failures=sorted(set(str(value)[:120] for value in self.institution.errors.values()))
        return {**result,"selected":selected,"refresh_error":("文件中转接收未完成："+", ".join(failures)) if failures else "","note":"目录按10秒心跳更新，超过60秒标记心跳过期；企点登录状态以采集端原生界面核验为准"}

    def require_online(self, owner):
        self._ready();b=self.repository.binding_for(owner)
        if not b:raise ValueError("请先选择当前工作台使用的企点号")
        channel=self.institution.availability(b["workbench_id"],b["account_uid"])
        if not channel["ready"]:raise ValueError(channel["hint"])
        return b

    def contacts(self, owner, *, refresh=False):
        self._ready();b=self.repository.binding_for(owner)
        if not b:raise ValueError("请先选择当前工作台使用的企点号")
        if refresh:
            self.require_online(owner)
            adapter=self.institution.current_adapter(owner)
            if adapter is None:raise ValueError("当前账号尚未完成接入，请保存选择；未提交同步请求")
            adapter.publish_manifest({"contract":CONTRACT,"op":"sync_contacts","organization":True,"request_id":uuid.uuid4().hex})
        book=self.institution.directory.contacts(b["workbench_id"],b["account_uid"],owner)
        with self.repository.write(owner) as tx:
            current=self.repository.require_binding(tx,owner)
            if any(current[k]!=b[k] for k in ("account_uid","workbench_id","version")):
                raise ValueError("当前账号已变化，请重新读取通讯录")
            self._record_check(tx,owner,current,book)
        if refresh:book["refresh_requested"]=True;book["note"]="同步请求已提交，等待采集端回传；旧业务不会迁入"
        return book

    def freshness(self, owner):
        self._ready();b=self.repository.binding_for(owner)
        if not b:raise ValueError("尚未选择企点号")
        row=self.institution.directory.account(b["workbench_id"],b["account_uid"])
        return {"updated_at":row["updated_at"] if row else None,"stale":not bool(row and row["available"]),"publication_id":"presence-"+str(row["updated_at"] if row else 0),"publication_hash":"","offline_seconds":60}

    def check(self, owner):return self.contacts(owner)

    def _candidate(self, owner, body):
        self._ready()
        if not isinstance(body,dict) or set(body)!={"workbench_id","account_uid","expected_version"}:raise ValueError("请选择目录中的工作台和企点号并提交版本")
        wb,uid,version=body["workbench_id"],body["account_uid"],body["expected_version"]
        token(wb);token(uid)
        if len(wb)>160 or len(uid)>80 or type(version) is not int or version<0:raise ValueError("账号或版本格式无效")
        current=self.repository.binding_for(owner)
        if (current["version"] if current else 0)!=version:raise ValueError("当前选择已变化，请刷新后重试")
        row=self.institution.directory.account(wb,uid)
        channel=self.institution.availability(wb,uid)
        if not channel["ready"]:raise ValueError(channel["hint"])
        if "negotiation-v22-watermark" not in row.get("capabilities",[]):raise ValueError("采集端尚未就绪，不能创建发送通道")
        return wb,uid,version,current

    def select(self, owner, body):
        from .fresh_state import clear_workspace
        with self._lock:
            wb,uid,version,previous=self._candidate(owner,body)
            book=self.institution.directory.contacts(wb,uid,owner)
            old_adapter=self.institution.current_adapter(owner)
            old_prefs=self.repository.preferences(owner) if previous else {}
            same=bool(previous and (previous["workbench_id"],previous["account_uid"])==(wb,uid))
            reset=bool(previous and (not same or old_prefs.get("relay_contract")!=CONTRACT
                or old_prefs.get("relay_institution_id")!=self.institution.keys.institution_id
                or not old_prefs.get("context_id")))
            fresh=not previous or reset
            preferences=dict(old_prefs) if not fresh else {}
            if fresh:
                preferences.update(context_id=uuid.uuid4().hex,context_version=max(int(self.repository.clock()*1000000),int(old_prefs.get("context_version",0))+1),
                    context_started_at=self.repository.clock(),context_ready=False)
            preferences.update(relay_contract=CONTRACT,relay_institution_id=self.institution.keys.institution_id,
                collector_account_id=self.institution.directory.account(wb,uid)["account_id"])
            staged=self.institution.adapter(owner,wb,uid,preferences=preferences)
            with self.repository.write(owner) as tx:
                old=tx.execute("SELECT * FROM xj2_binding WHERE owner_id=?",(owner,)).fetchone()
                if (int(old["version"]) if old else 0)!=version:raise ValueError("账号选择已变化，请刷新后重试")
                occupied=tx.execute("SELECT owner_id FROM xj2_binding WHERE account_uid=?",(uid,)).fetchone()
                if occupied and occupied["owner_id"]!=owner:raise ValueError("该企点号正在另一个工作台使用")
                if not self.institution.online(wb,uid):raise ValueError("采集端状态已变化，请刷新后重试")
                removed=clear_workspace(tx,owner) if reset else {}
                if old:
                    tx.execute("UPDATE xj2_binding SET account_uid=?,workbench_id=?,transport=?,version=version+?,config_json=? WHERE owner_id=?",
                        (uid,wb,"oss",1 if reset else 0,json.dumps(preferences,ensure_ascii=False),owner))
                else:
                    tx.execute("INSERT INTO xj2_binding(owner_id,account_uid,workbench_id,transport,version,config_json) VALUES(?,?,?,?,1,?)",
                        (owner,uid,wb,"oss",json.dumps(preferences,ensure_ascii=False)))
                    tx.execute("UPDATE xj2_owner SET send_allowed=0 WHERE owner_id=?",(owner,))
                current=self.repository.require_binding(tx,owner);self._record_check(tx,owner,current,book)
                self.repository.audit(tx,owner,"workspace_reset" if reset else "institution_account_selected",uid,
                    {"workbench_id":wb,"contract":CONTRACT,"removed_counts":removed})
            self.transport.adapters={k:v for k,v in self.transport.adapters.items() if k[0]!=owner or k[2]!="oss"} | {(owner,uid,"oss"):staged}
            if reset and old_adapter is not None:self.institution.retire_context(old_adapter,notify=not same)
            if fresh:self.institution.request_context(staged)
            binding=self.repository.binding_for(owner)
            binding={**binding,"context_id":preferences["context_id"]}
            return {"binding":binding,"selected":{k:binding[k] for k in ("workbench_id","account_uid","version")},
                "contacts":book,"connected":True,"reset":reset,"removed_counts":removed,
                "send_ready":bool(staged.context_ready),"note":"已清空旧业务并切换账号；原生历史按当前号读取" if reset else "已保存选择并读取通讯录"}

    def files(self, owner, *, before=None, limit=50):
        import hashlib
        from pathlib import PurePosixPath
        from relay.protocol import ProtocolError
        b=self.require_online(owner)
        if type(limit) is not int or not 1<=limit<=100:raise ValueError("文件分页大小无效")
        if before is not None and (type(before) is not int or not 1<=before<=9223372036854775807):raise ValueError("文件分页游标无效")
        adapter=self.institution.current_adapter(owner)
        if adapter is None:raise ValueError("当前账号尚未完成接入，请保存选择")
        cursor=before if before is not None else 9223372036854775807
        items=[]; scanned=0; exhausted=False
        # Both lanes are local retained journals. Reading never consumes a queue.
        query="""SELECT seq*2 AS id,name,blob,state,received_at,0 AS outgoing FROM fg_download
            WHERE scope=? AND blob IS NOT NULL AND seq*2<?
            UNION ALL SELECT rowid*2+1 AS id,NULL AS name,blob,NULL AS state,NULL AS received_at,1 AS outgoing
            FROM tx_packet WHERE scope=? AND rowid*2+1<? ORDER BY id DESC LIMIT ?"""
        with adapter.store.db() as db:
            while len(items)<limit+1 and scanned<512:
                rows=db.execute(query,(adapter.store.scope,cursor,adapter.store.scope,cursor,min(8,512-scanned))).fetchall()
                if not rows:exhausted=True;break
                for row in rows:
                    cursor=row["id"];scanned+=1
                    try:
                        blob=bytes(row["blob"]);env=adapter.codec._authenticated(blob)
                        adapter.codec._validate(env,allow_expired=True)
                        if env["kind"]=="manifest" and env["payload"].get("op") in ("activate_context","context_ready","data_ack"):continue
                    except (ProtocolError,ValueError,TypeError):continue
                    if row["outgoing"]:
                        identity=hashlib.sha256(blob).hexdigest()
                        upload=db.execute("SELECT state FROM fg_upload WHERE scope=? AND id=?",(adapter.store.scope,identity)).fetchone()
                        if upload is None or env["kind"] not in adapter.outgoing_kinds:continue
                        name="xunjia-v22-"+env["kind"]+"-"+identity[:32]+".bin"
                        state=upload["state"];received=None;lane="down"
                    else:
                        if env["kind"] not in adapter.incoming_kinds:continue
                        name=row["name"];state=row["state"];received=row["received_at"];lane="up"
                    if not isinstance(name,str):continue
                    items.append({"id":row["id"],"name":name,"extension":PurePosixPath(name).suffix,
                        "kind":env["kind"],"lane":lane,"source_created_at":env["created_at"],
                        "received_at":received,"state":state,"upload_state":state if lane=="down" else None,
                        "receive_state":state if lane=="up" else None})
                    if len(items)>limit:break
        current=self.repository.binding_for(owner)
        if not current or any(current[k]!=b[k] for k in ("account_uid","workbench_id","version")):
            raise ValueError("\u91c7\u96c6\u7aef\u9009\u62e9\u5df2\u53d8\u5316\uff0c\u8bf7\u91cd\u65b0\u8bfb\u53d6\u6587\u4ef6\u8bb0\u5f55")
        next_before=items[limit-1]["id"] if len(items)>limit else cursor if not exhausted else None
        return {"items":items[:limit],"next_before":next_before,"workbench_id":b["workbench_id"],
                "account_uid":b["account_uid"],"scope":"retained_authenticated_files"}


    def history(self, owner, *, before=None, limit=50, peer=None):
        b=self.require_online(owner)
        result=self.institution.archive.page(b["workbench_id"],b["account_uid"],before=before,limit=limit,peer=peer)
        current=self.require_online(owner)
        if any(current[k]!=b[k] for k in ("workbench_id","account_uid","version")):raise ValueError("Selected account changed during history read")
        return {**result,"workbench_id":b["workbench_id"],"account_uid":b["account_uid"],"binding_version":b["version"]}

    def media(self, owner, sha256):
        b=self.require_online(owner)
        result=self.institution.archive.media(b["workbench_id"],b["account_uid"],sha256)
        current=self.require_online(owner)
        if any(current[k]!=b[k] for k in ("workbench_id","account_uid","version")):raise ValueError("Selected account changed during media read")
        return result
