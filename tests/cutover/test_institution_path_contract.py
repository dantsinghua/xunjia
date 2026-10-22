"""Approved institution/path contract; no real credentials, IM or network."""
from collections import defaultdict
from email import policy
from email.parser import BytesParser
import json
from urllib.parse import urlsplit,parse_qs,quote
import uuid
import pytest
from relay.file_gateway import FileGatewayClient,GatewayAuth,FileGatewayError
from relay.gateway_paths import CONTRACT,DIRECTORY_PATH,ORGANIZATION_PATH,InstitutionKeys,account_paths
from relay.institution_codec import InstitutionCodec
from relay.institution_directory import InstitutionDirectory
from relay.protocol import Codec,envelope,ProtocolError
from server.institution_transport import InstitutionTransport
from file_gateway_fakes import AUTH,NOW,Response,decode_token

class Paths:
    def __init__(self):
        self.queues=defaultdict(list);self.calls=[];self.now=NOW;self.lose_download=False
    def client(self,path):
        return FileGatewayClient("https://relay.invalid",AUTH,path=path,opener=self,clock=lambda:self.now)
    def open(self,request,timeout):
        u=urlsplit(request.full_url);p=parse_qs(u.query)["path"][0];op=u.path.rsplit("/",1)[-1]
        headers={k.lower():v for k,v in request.header_items()};decoded=decode_token(headers["accesstoken"])
        assert decoded["key"]==AUTH.key
        self.calls.append((p,op));q=self.queues[p]
        if op=="pending":return Response(200,json.dumps({"pending":len(q)}).encode())
        if op=="upload":
            mail=BytesParser(policy=policy.default).parsebytes(("Content-Type: "+headers["content-type"]+"\r\n\r\n").encode()+request.data)
            part=next(mail.iter_parts());assert part.get_param("name",header="content-disposition")=="file"
            name=part.get_filename();q.append((name,part.get_payload(decode=True)))
            return Response(200,json.dumps({"file_key":str(uuid.uuid4()),"file_name":name}).encode())
        if not q:return Response(204)
        name,raw=q.pop(0)
        if self.lose_download:raise TimeoutError("synthetic consumed file")
        return Response(200,raw,{"Content-Type":"application/octet-stream","Content-Disposition":"attachment; filename=\""+name+"\""})

def pair(tmp_path):
    g=Paths();k=InstitutionKeys.from_auth(AUTH);paths=account_paths("wb-a","10001")
    local=k.binding("wb-a","10001","workspace-a");wire=k.binding("wb-a","10001")
    s=InstitutionTransport(local,g.client(paths["uplink_path"]),g.client(paths["downlink_path"]),tmp_path/"s.db",role="service",institution_owner=k.owner_id,clock=lambda:g.now)
    c=InstitutionTransport(wire,g.client(paths["downlink_path"]),g.client(paths["uplink_path"]),tmp_path/"c.db",role="collector",institution_owner=k.owner_id,clock=lambda:g.now)
    s.online_guard=lambda:True
    return g,k,s,c

@pytest.mark.parametrize("path",["","x/y","a.b","a b","中文","x"*21,1,[],True])
def test_path_validation_before_io(path):
    with pytest.raises(FileGatewayError,match="PATH_INVALID"):FileGatewayClient("https://relay.invalid",AUTH,path=path)

def test_fixed_path_and_same_host_isolation():
    a=account_paths("wb-a","10001");b=account_paths("wb-b","10001")
    assert len(set(a.values())|set(b.values()))==6
    assert all(len(v)==20 for v in a.values())
    g=Paths();up=g.client(a["uplink_path"]);down=g.client(a["downlink_path"])
    assert up.queue_identity!=down.queue_identity
    up.upload("same.txt",b"one");assert up.pending()==1 and down.pending()==0
    assert up.download().content==b"one" and up.download() is None

def test_different_key_same_path_has_different_ledger_identity():
    one=FileGatewayClient("https://relay.invalid",AUTH,path="same",opener=Paths())
    two=FileGatewayClient("https://relay.invalid",GatewayAuth("other",AUTH.sm4_key),path="same",opener=Paths())
    assert one.queue_identity!=two.queue_identity

def test_immutable_event_rebind_and_ack_only_after_commit(tmp_path):
    g,k,s,c=pair(tmp_path)
    c.publish_event({"event_id":"e-1","peer_id":"peer","text":"synthetic","direction":"in","occurred_at":NOW,"ext_msg_id":"native-1"})
    rows=s.poll();assert len(rows)==1 and rows[0]["envelope"]["owner_user_id"]=="workspace-a"
    with c.store.db() as db:assert db.execute("SELECT acked FROM ip_delivery").fetchone()[0]==0
    s.ack(rows[0]["source_token"]);s.poll();c.poll()
    with c.store.db() as db:assert db.execute("SELECT acked FROM ip_delivery").fetchone()[0]==1
    before=sum(op=="upload" for _,op in g.calls)
    s.poll();c.poll();s.poll();c.poll()
    assert sum(op=="upload" for _,op in g.calls)==before

def test_consumed_download_loss_replays_data_without_double_business_id(tmp_path):
    g,k,s,c=pair(tmp_path)
    c.publish_event({"event_id":"e-1","peer_id":"peer","text":"synthetic","direction":"in","occurred_at":NOW,"ext_msg_id":"native-1"})
    g.lose_download=True
    with pytest.raises(FileGatewayError):s.poll()
    g.lose_download=False;g.now+=31;c.retry_data()
    rows=s.poll();assert [r["envelope"]["message_id"] for r in rows]==["e-1"]
    s.ack(rows[0]["source_token"]);s.poll();c.poll()
    assert any(r["state"]=="UNKNOWN" for r in s.delivery_diagnostics()["download_issues"])

def test_same_institution_other_account_rejected(tmp_path):
    g,k,s,c=pair(tmp_path)
    wrong=Codec(k.binding("wb-other","10002"),clock=lambda:NOW)
    data=envelope(wrong.binding,"manifest",{"contract":CONTRACT,"op":"data_ack","items":[]},message_id="wrong",created_at=NOW,expires_at=NOW+60)
    with pytest.raises(ProtocolError):s.codec.unpack(wrong.pack(data))

def directory_env(k,wb="wb-a",uid="10001",revision=1,stamp=NOW,online=True):
    b=k.binding(wb,"directory")
    p={"contract":CONTRACT,"op":"directory","revision":revision,"updated_at":stamp,"workbench_name":"same display name","accounts":[{"account_id":"qd01","account_uid":uid,"channel":"qidian","online":online,"paths":account_paths(wb,uid)}]}
    return envelope(b,"manifest",p,message_id="dir-"+str(revision),created_at=stamp,expires_at=stamp+86400)

def test_directory_online_timeout_rename_order_and_conflict(tmp_path):
    k=InstitutionKeys.from_auth(AUTH);clock=[NOW];d=InstitutionDirectory(tmp_path/"dir.db",k,clock=lambda:clock[0])
    d.put(directory_env(k));assert d.account("wb-a","10001")["available"]
    clock[0]+=60;assert not d.account("wb-a","10001")["available"]
    d.put(directory_env(k,revision=2,stamp=clock[0]));assert d.account("wb-a","10001")["available"]
    assert not d.put(directory_env(k));assert d.account("wb-a","10001")["available"]
    d.put(directory_env(k,wb="wb-b",revision=1,stamp=clock[0]))
    assert d.account("wb-a","10001")["state"]=="conflict"

def test_directory_foreign_path_is_quarantined(tmp_path):
    g=Paths();k=InstitutionKeys.from_auth(AUTH);d=InstitutionDirectory(tmp_path/"dir.db",k,clock=lambda:NOW)
    env=directory_env(k);blob=Codec(k.binding("wb-a","directory"),clock=lambda:NOW).pack(env)
    g.client(ORGANIZATION_PATH).upload("wrong.bin",blob)
    d.consume(g.client(ORGANIZATION_PATH))
    assert d.catalog()["workbenches"]==[]
    with d.db() as db:assert db.execute("SELECT state FROM ip_input").fetchone()[0]=="QUARANTINED"

def test_directory_recovers_stored_file_before_network(tmp_path):
    g=Paths();k=InstitutionKeys.from_auth(AUTH);p=tmp_path/"dir.db";d=InstitutionDirectory(p,k,clock=lambda:NOW)
    env=directory_env(k);blob=Codec(k.binding("wb-a","directory"),clock=lambda:NOW).pack(env)
    with d.db() as db:db.execute("INSERT INTO ip_input(path,state,blob,at) VALUES(?,?,?,?)",(DIRECTORY_PATH,"STORED",blob,NOW))
    d=InstitutionDirectory(p,k,clock=lambda:NOW);d.consume(g.client(DIRECTORY_PATH))
    assert d.account("wb-a","10001")["available"]
