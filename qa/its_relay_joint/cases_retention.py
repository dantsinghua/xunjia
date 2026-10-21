import hashlib,sqlite3
from types import SimpleNamespace
from qtrade_agent.ossbox.history_publish import NativeHistoryPublisher
NOW=1800000000
class Adapter:
    def __init__(self):self.calls=[]
    def publish_manifest(self,payload,*,message_id):self.calls.append((message_id,payload))
def publisher(tmp_path):return NativeHistoryPublisher(tmp_path,SimpleNamespace(),Adapter(),clock=lambda:NOW,media_reader=lambda _:b'')
def add_blob(h,data,stamp):
    digest=hashlib.sha256(data).hexdigest();p=h.media.root/digest;p.write_bytes(data)
    with h.db() as db:db.execute('INSERT INTO history_media VALUES(?,?,?)',(digest,len(data),stamp))
    return digest,p

def test_sender_retention_removes_only_expired_local_history_and_media(tmp_path):
    h=publisher(tmp_path);old,p=add_blob(h,b'old',NOW-604801);fresh,q=add_blob(h,b'fresh',NOW-10)
    with h.db() as db:
        db.execute('INSERT INTO history_sent VALUES(?,?,?,?)',('old','in','fingerprint',NOW-604801))
        db.execute('INSERT INTO history_sent VALUES(?,?,?,?)',('fresh','in','fingerprint',NOW-10))
        db.execute('INSERT INTO history_retry VALUES(?,?,?,?)',('table',1,'UNAVAILABLE',NOW-604801))
    guard=tmp_path/'native-unknown.sqlite3';guard.write_bytes(b'UNKNOWN evidence must remain');before=guard.read_bytes()
    result=h.prune()
    assert not p.exists() and q.read_bytes()==b'fresh' and result['media_removed']==1
    with h.db() as db:
        assert db.execute('SELECT native FROM history_sent').fetchall()[0][0]=='fresh'
        assert db.execute('SELECT count(*) FROM history_retry').fetchone()[0]==0
    assert guard.read_bytes()==before

def test_current_history_reference_keeps_older_shared_sender_binary(tmp_path):
    h=publisher(tmp_path);data=b'still used';digest,p=add_blob(h,data,NOW-604801)
    message={'native_id':'new-use','direction':'in','occurred_at':NOW-10,'peer_id':'peer','text':'','type':'file','session_kind':'private','historical':True,'media':[{'name':'a.txt','sha256':digest,'size':len(data),'mime':'text/plain','state':'ready'}]}
    h._publish_rows([message]);result=h.prune()
    assert p.read_bytes()==data and result['media_removed']==0

def test_sender_prune_is_bounded_and_does_not_remove_cursor(tmp_path):
    h=publisher(tmp_path)
    with h.db() as db:
        db.execute('INSERT INTO history_cursor VALUES(?,?,?)',('table',12,99))
        for i in range(5):db.execute('INSERT INTO history_sent VALUES(?,?,?,?)',(str(i),'in','fp',NOW-604801))
    result=h.prune(limit=2)
    assert result['history_removed']==2 and result['more_pending']
    with h.db() as db:assert db.execute('SELECT position FROM history_cursor').fetchone()[0]==12