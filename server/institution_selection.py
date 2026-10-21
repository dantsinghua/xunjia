"""Institution-visible discovery with one account per authenticated workspace."""
from __future__ import annotations
import json
import uuid
from relay.gateway_paths import CONTRACT
from relay.protocol import token
from .collector_selection import CollectorSelection

class InstitutionSelection(CollectorSelection):
    def __init__(self, repository, transport, runtime):
        self.repository,self.transport,self.institution=repository,transport,runtime
        self.runtime=None;self._lock=runtime._lock

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
        return {**result,"selected":selected,"refresh_error":("文件中转接收未完成："+", ".join(failures)) if failures else "","note":"目录按10秒心跳更新，超过60秒标记已离线"}

    def require_online(self, owner):
        self._ready();b=self.repository.binding_for(owner)
        if not b:raise ValueError("请先选择当前工作台使用的企点号")
        if not self.institution.online(b["workbench_id"],b["account_uid"]):raise PermissionError("所选企点号已离线；请在采集端恢复登录后继续操作")
        return b

    def contacts(self, owner, *, refresh=False):
        b=self.require_online(owner)
        if refresh:
            adapter=self.institution.install(owner,b["workbench_id"],b["account_uid"])
            adapter.publish_manifest({"contract":CONTRACT,"op":"sync_contacts","organization":True,"request_id":uuid.uuid4().hex})
        book=self.institution.directory.contacts(b["workbench_id"],b["account_uid"],owner)
        with self.repository.write(owner) as tx:
            current=self.repository.require_binding(tx,owner)
            if all(current[k]==b[k] for k in ("account_uid","workbench_id","version")):self._record_check(tx,owner,current,book)
        if refresh:book["refresh_requested"]=True;book["note"]="同步请求已提交；当前返回的是最近成功缓存，等待采集端回传新版本"
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
        if not row or not row["available"]:raise ValueError("该企点号不在线或存在重复登录状态，请在采集端恢复后重试")
        if "negotiation-v22-watermark" not in row.get("capabilities",[]):raise ValueError("采集端尚未就绪，不能创建发送通道")
        return wb,uid,version,current

    def _switch_allowed(self, tx, owner, old):
        if old is None:return
        reset=self.repository._account_reset(tx,owner,old["account_uid"])
        if reset and reset["state"]!="complete":raise ValueError("当前账号的会话同步尚未完成")
        control=tx.execute("SELECT send_allowed FROM xj2_owner WHERE owner_id=?",(owner,)).fetchone()
        groups=tx.execute("SELECT group_id FROM xj2_group WHERE owner_id=? AND archived=0 AND state=? AND send_allowed=1 LIMIT 1",(owner,"OPEN")).fetchone()
        peers=tx.execute("SELECT p.member_id FROM xj2_peer p JOIN xj2_group g ON g.group_id=p.group_id WHERE g.owner_id=? AND g.archived=0 AND g.state=? AND p.send_allowed=1 LIMIT 1",(owner,"OPEN")).fetchone()
        pending=tx.execute("SELECT c.command_id FROM xj2_command c JOIN xj2_thread t ON t.thread_id=c.thread_id WHERE t.owner_id=? AND c.state IN (?,?,?) LIMIT 1",(owner,"QUEUED","PENDING","UNKNOWN")).fetchone()
        if control["send_allowed"] or groups or peers or pending:raise ValueError("请先停止机器人及券组发送并完成在途命令对账，再切换账号；UNKNOWN不会被清除")

    def select(self, owner, body):
        with self._lock:
            wb,uid,version,previous=self._candidate(owner,body)
            book=self.institution.directory.contacts(wb,uid,owner)
            # Construct only; no remote consume/send or runtime replacement until commit.
            staged=self.institution.adapter(owner,wb,uid)
            with self.repository.write(owner) as tx:
                old=tx.execute("SELECT * FROM xj2_binding WHERE owner_id=?",(owner,)).fetchone()
                if (int(old["version"]) if old else 0)!=version:raise ValueError("账号选择版本已变化")
                occupied=tx.execute("SELECT owner_id FROM xj2_binding WHERE account_uid=?",(uid,)).fetchone()
                if occupied and occupied["owner_id"]!=owner:raise ValueError("该企点号正在另一已登录工作台使用，请先在原工作台停止并切换")
                same=bool(old and (old["workbench_id"],old["account_uid"])==(wb,uid))
                preferences=json.loads(old["config_json"]) if old else {}
                if not same or preferences.get("relay_contract")!=CONTRACT:self._switch_allowed(tx,owner,old)
                if not self.institution.online(wb,uid):raise ValueError("采集端状态已变化，请刷新后重试")
                preferences["relay_contract"]=CONTRACT
                preferences["relay_institution_id"]=self.institution.keys.institution_id
                if old:
                    tx.execute("UPDATE xj2_binding SET account_uid=?,workbench_id=?,transport=?,version=version+?,config_json=? WHERE owner_id=?",(uid,wb,"oss",0 if same else 1,json.dumps(preferences,ensure_ascii=False),owner))
                else:
                    tx.execute("INSERT INTO xj2_binding(owner_id,account_uid,workbench_id,transport,version,config_json) VALUES(?,?,?,?,1,?)",(owner,uid,wb,"oss",json.dumps(preferences,ensure_ascii=False)))
                if not same:
                    tx.execute("UPDATE xj2_owner SET send_allowed=0,control_version=control_version+1 WHERE owner_id=?",(owner,))
                    tx.execute("UPDATE xj2_group SET send_allowed=0,control_version=control_version+1 WHERE owner_id=?",(owner,))
                    tx.execute("UPDATE xj2_peer SET send_allowed=0,control_version=control_version+1 WHERE group_id IN (SELECT group_id FROM xj2_group WHERE owner_id=?)",(owner,))
                current=self.repository.require_binding(tx,owner);self._record_check(tx,owner,current,book)
                self.repository.audit(tx,owner,"institution_account_selected",uid,{"workbench_id":wb,"contract":CONTRACT})
            self.transport.adapters={k:v for k,v in self.transport.adapters.items() if k[0]!=owner or k[2]!="oss"} | {(owner,uid,"oss"):staged}
            binding=self.repository.binding_for(owner)
            return {"binding":binding,"selected":{k:binding[k] for k in ("workbench_id","account_uid","version")},"contacts":book,"connected":True,"note":"账号已选择；使用机构内置独立收发路径，发送仍遵循原有授权规则"}

    def files(self, owner, *, before=None, limit=50):
        import hashlib
        from pathlib import PurePosixPath
        from relay.protocol import ProtocolError
        b=self.require_online(owner)
        if type(limit) is not int or not 1<=limit<=100:raise ValueError("文件分页大小无效")
        if before is not None and (type(before) is not int or not 1<=before<=9223372036854775807):raise ValueError("文件分页游标无效")
        adapter=self.institution.install(owner,b["workbench_id"],b["account_uid"])
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
