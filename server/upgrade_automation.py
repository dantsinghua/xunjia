"""Daily Shanghai-midnight upgrade trigger and durable diagnostic return queue."""
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import time
from zoneinfo import ZoneInfo

ZONE=ZoneInfo('Asia/Shanghai')
LOG_QUEUE='xjup_158_logs'


def schedule(store):
    value=store.get('schedule',{})
    return {'enabled':value.get('enabled',True),'time':'00:00','timezone':'Asia/Shanghai',
            'last_day':store.get('scheduled_day',''),'log_queue':LOG_QUEUE}


def save_schedule(control,owner,body):
    config=control.configured()
    if '*' not in config['operators'] and owner not in config['operators']:raise PermissionError('没有升级配置权限')
    if not isinstance(body,dict) or set(body)!={'enabled'} or type(body['enabled']) is not bool:
        raise ValueError('自动升级配置仅接受enabled布尔值')
    store=control.store()
    with store.lock():store.set('schedule',{'enabled':body['enabled'],'owner':owner})
    return schedule(store)


def upload_log(prefix,job_id):
    from .upgrade_control import UpgradeStore
    from .upgrade_diagnostics import log_text
    from relay.file_gateway import GatewayAuth,FileGatewayError
    from relay.file_gateway_connection import connection_client,resolve_tls_files,protected_auth_path
    from deploy.common import read_env,effective_env
    prefix=Path(prefix);store=UpgradeStore(prefix/'state/upgrade')
    with store.db() as db:
        row=db.execute('SELECT state,code FROM job WHERE id=?',(job_id,)).fetchone()
    if row is None or row[0] not in ('succeeded','failed','unknown'):return {'state':'pending'}
    if row[1]=='UPGRADE_OPERATOR_REVOKED':return {'state':'not_authorized'}
    raw=log_text(store,job_id).encode('utf8');digest=hashlib.sha256(raw).hexdigest()
    key='log_delivery:'+job_id
    # Claim before network I/O; an uncertain send is not blindly retried.
    with store.db() as db:
        db.execute('BEGIN IMMEDIATE')
        previous=db.execute('SELECT value FROM setting WHERE key=?',(key,)).fetchone()
        previous=json.loads(previous[0]) if previous else {}
        if previous.get('state') in ('sending','unknown'):return previous
        if previous.get('sha256')==digest and previous.get('state')=='uploaded':return previous
        if time.time()-previous.get('at',0)<300:return previous
        record={'state':'sending','sha256':digest,'at':time.time(),'queue':LOG_QUEUE}
        db.execute('INSERT INTO setting VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',(key,json.dumps(record)))
    attempted=False
    try:
        env=effective_env(read_env(prefix/'config/xunjia.env'),prefix,prefix/'app')
        os.environ.setdefault('XUNJIA_EGRESS_GATEWAY',env.get('XUNJIA_EGRESS_GATEWAY',''))
        path=Path(env['XUNJIA_INSTITUTION_CONFIG'])
        config=resolve_tls_files(json.loads(path.read_text()),path.parent)
        auth=GatewayAuth.from_file(protected_auth_path(path,config['auth_file']))
        client=connection_client(config,auth,path=LOG_QUEUE)
        attempted=True
        client.upload('xj-upgrade-'+job_id+'-'+digest[:8]+'.txt',raw)
        record['state']='uploaded'
    except FileGatewayError as exc:
        record['state']='unknown' if exc.ambiguous else 'failed'
        record['code']=exc.code
    except Exception:
        record['state']='unknown' if attempted else 'failed';record['code']='LOG_UPLOAD_NOT_CONFIRMED'
    store.set(key,record)
    return record


def tick(control,now=None):
    if control.prefix is None or not (control.prefix/'config/upgrade-channel.json').is_file():return
    store=control.store();settings=schedule(store)
    local=datetime.fromtimestamp(time.time() if now is None else now,ZONE)
    # A boot or manual enable during business hours must not trigger an upgrade.
    if settings['enabled'] and local.hour==0 and local.minute<10:
        day=local.date().isoformat()
        if settings['last_day']!=day:
            config=control.configured()
            owner=store.get('schedule',{}).get('owner','system')
            if '*' in config['operators'] or owner in config['operators']:
                try:control.start(owner,scheduled_day=day)
                except (ValueError,BlockingIOError):pass  # Retry during the midnight window after a manual job.
    with store.db() as db:
        cursor=store.get('log_scan_cursor',0)
        jobs=db.execute("SELECT rowid AS position,id FROM job WHERE state IN ('succeeded','failed','unknown') AND rowid>? ORDER BY rowid LIMIT 5",(cursor,)).fetchall()
        if not jobs:
            jobs=db.execute("SELECT rowid AS position,id FROM job WHERE state IN ('succeeded','failed','unknown') ORDER BY rowid LIMIT 5").fetchall()
    for job in jobs:
        try:upload_log(control.prefix,job['id'])
        except Exception:pass  # Log delivery never takes the business service down.
        finally:store.set('log_scan_cursor',job['position'])
