import hashlib
import pytest
from qtrade_agent.adapters.qidian.history_media import NativeMediaResolver
from qtrade_agent.adapters.qidian.history_native import NativeHistoryReader, HistoryTable
from qtrade_agent.adapters.qidian.msgdata import MainDbRow
from qtrade_agent.adapters.qidian.xor import xor
BASE='/sdcard/Android/data/com.tencent.qidian/Tencent/Qidian/diskcache/'
PNG=bytes.fromhex('89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4890000000b49444154789c636000020000050001a5f645400000000049454e44ae426082')
def vi(n):
 b=bytearray()
 while n>127:b.append((n&127)|128);n>>=7
 b.append(n);return bytes(b)
def f(n,v):return vi(n*8+2)+vi(len(v))+v
def n(n,v):return vi(n*8)+vi(v)
def pic(path='',md5=None,size=None,raw_url='',thumb=''):
 md5=md5 or hashlib.md5(PNG).hexdigest()
 return f(1,path.encode())+n(2,len(PNG) if size is None else size)+f(6,md5.encode())+f(10,raw_url.encode())+f(8,thumb.encode())
def test_native_picrec_extensionless_original_is_read(tmp_path):
 p=BASE+'Cache_verified';r=NativeMediaResolver(tmp_path,lambda path:PNG if path==p else pytest.fail('unexpected read'))
 items,error=r(-2000,pic(p));assert error is None and items[0]['state']=='ready'
 assert r.blobs[items[0]['sha256']].read_bytes()==PNG

def test_native_md5_mismatch_is_never_ready(tmp_path):
 r=NativeMediaResolver(tmp_path,lambda _:PNG)
 items,error=r(-2000,pic(BASE+'original.png',md5='0'*32))
 assert error=='NATIVE_MEDIA_MD5_MISMATCH' and items[0]['state']=='unavailable'

def test_native_declared_size_mismatch_is_never_ready(tmp_path):
 r=NativeMediaResolver(tmp_path,lambda _:PNG)
 items,error=r(-2000,pic(BASE+'original.png',size=len(PNG)+1))
 assert error=='NATIVE_MEDIA_SIZE_MISMATCH' and items[0]['state']=='unavailable'

def test_thumbnail_field_is_not_original_even_with_innocent_filename(tmp_path):
 r=NativeMediaResolver(tmp_path,lambda _:pytest.fail('thumbnail must not be read'))
 items,error=r(-2000,pic(thumb=BASE+'photo.png'))
 assert error and not any(i['state']=='ready' for i in items)

def test_system_type_2006_is_not_picture(tmp_path):
 class Source: pass
 row=MainDbRow(1,0,1800000000,-2006,101,'',xor(pic(BASE+'photo.png')).hex())
 out,issue=NativeHistoryReader(Source()).message(HistoryTable('mr_friend_'+'A'*32+'_New','123','private'),row,lambda *a:pytest.fail('system is not image'))
 assert out is None and issue=='HISTORY_UNSUPPORTED_MESSAGE_TYPE'