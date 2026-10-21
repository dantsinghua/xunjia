"""Additional media identity checks; generated metadata and bytes only."""
import hashlib,json,sqlite3,struct
from pathlib import Path
from types import SimpleNamespace
import pytest
from qtrade_agent.adapters.qidian.media_metadata import troop_file_reference,picture_references
from qtrade_agent.adapters.qidian.media_download import validated_original_url,OriginalImageDownloader
from qtrade_agent.adapters.qidian.media_file_index import file_manager_reference
from qtrade_agent.adapters.qidian.maindb import LocalSqliteMainDb
from qtrade_agent.adapters.qidian.history_native import HistoryTable

def test_typed_troop_java_metadata_reads_primitive_fields_without_object_execution():
    def utf(s):b=s.encode();return len(b).to_bytes(2,'big')+b
    text=b'ACED-independent-file';md5=hashlib.md5(text).hexdigest();sha1=hashlib.sha1(text).hexdigest()
    fields=[('J','lfileSize'),('L','fileName'),('L','fileUrl'),('L','md5'),('L','sha1')]
    raw=b'\xac\xed\x00\x05\x73\x72'+utf('com.tencent.mobileqq.data.TroopFileData')+b'\0'*8+b'\x02'+len(fields).to_bytes(2,'big')
    for kind,key in fields:
        raw+=kind.encode()+utf(key)
        if kind=='L':raw+=b'\x74'+utf('Ljava/lang/String;')
    raw+=b'\x78\x70'+struct.pack('>q',len(text))
    path='/sdcard/Android/data/com.tencent.qidian/Tencent/Qidian/Qidianfile_recv/plain.txt'
    for value in ('plain.txt',path,md5,sha1):raw+=b'\x74'+utf(value)
    ref=troop_file_reference(raw);assert ref.paths==(path,) and ref.md5==md5 and ref.sha1==sha1 and ref.size==len(text)
    with pytest.raises(ValueError):troop_file_reference(raw+b'junk')

@pytest.mark.parametrize('url',['file:///etc/passwd','http://127.0.0.1:8080/a','https://qpic.cn.evil.invalid/download','https://gchat.qpic.cn:8443/download','https://user:pass@gchat.qpic.cn/download','https://gchat.qpic.cn/not-media'])
def test_original_image_url_rejects_non_media_or_credential_targets(url):
    with pytest.raises(ValueError):validated_original_url(url)

def test_file_index_rejects_cross_account_direction_and_peer(tmp_path):
    path=tmp_path/'10001.db';source=LocalSqliteMainDb(str(path))
    columns='selfUin TEXT,peerUin TEXT,peerType INTEGER,bSend INTEGER,uniseq INTEGER,msgTime INTEGER,fileSize INTEGER,fileName TEXT,strFilePath TEXT,strFileMd5 TEXT,strFileSHA TEXT,TroopUin INTEGER'
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE mr_fileManager('+columns+')')
        db.execute('INSERT INTO mr_fileManager VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',('10001','12345',0,0,99,1800000000,1,'a.txt','/sdcard/Tencent/Qidian/Qidianfile_recv/a.txt','', '',0))
    table=HistoryTable('unused','12345','private');row=SimpleNamespace(uniseq=99,issend=0,time=1800000000)
    assert file_manager_reference(source,table,row).name=='a.txt'
    for field,value in [('selfUin','10002'),('peerUin','54321'),('bSend',1),('msgTime',1800000005)]:
        with sqlite3.connect(path) as db:
            old=db.execute('SELECT '+field+' FROM mr_fileManager').fetchone()[0]
            db.execute('UPDATE mr_fileManager SET '+field+'=?',(value,))
        with pytest.raises(ValueError,match='IDENTITY_MISMATCH'):file_manager_reference(source,table,row)
        with sqlite3.connect(path) as db:db.execute('UPDATE mr_fileManager SET '+field+'=?',(old,))