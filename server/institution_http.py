"""Authenticated, selected-account-only institutional archive reads."""
from __future__ import annotations
import hashlib
import re
from pathlib import Path
from fastapi import APIRouter,Request
from fastapi.responses import FileResponse,JSONResponse
from relay.protocol import token

def institution_router(selection):
    router=APIRouter(prefix='/api/im')
    def checked(request,allowed):
        if not getattr(request.state,'user',None):raise PermissionError('未登录')
        query=request.query_params
        if set(query)-allowed or any(len(query.getlist(k))!=1 for k in query):
            raise ValueError('只允许指定的只读筛选参数；账号由当前登录工作台选择')
        return request.state.user.user_id
    def integer(value,default,maximum,label):
        if value is None:return default
        if re.fullmatch(r'[1-9][0-9]{0,18}',value) is None or int(value)>maximum:
            raise ValueError(label+'无效')
        return int(value)
    @router.get('/history')
    def history(request:Request):
        owner=checked(request,{'before','limit','peer'})
        before=integer(request.query_params.get('before'),None,9223372036854775807,'历史游标')
        limit=integer(request.query_params.get('limit'),50,100,'历史分页大小')
        peer=request.query_params.get('peer')
        if peer is not None:
            if not peer or len(peer)>160:raise ValueError('对手标识无效')
            token(peer)
        return {'ok':True,'data':selection.history(owner,before=before,limit=limit,peer=peer)}
    @router.get('/media/{sha256}')
    def media(request:Request,sha256:str):
        owner=checked(request,set())
        if re.fullmatch(r'[a-f0-9]{64}',sha256) is None:raise ValueError('附件SHA256标识无效')
        try:path=Path(selection.media(owner,sha256))
        except LookupError:return JSONResponse({'ok':False,'hint':'附件尚未到达或已经过期'},status_code=404)
        if path.is_symlink() or not path.is_file():
            return JSONResponse({'ok':False,'hint':'附件尚未到达或已经过期'},status_code=404)
        digest=hashlib.sha256()
        with path.open('rb') as stream:
            for chunk in iter(lambda:stream.read(65536),b''):digest.update(chunk)
        if digest.hexdigest()!=sha256:
            return JSONResponse({'ok':False,'hint':'附件内容校验失败'},status_code=409)
        return FileResponse(path,media_type='application/octet-stream',filename=sha256)
    return router
