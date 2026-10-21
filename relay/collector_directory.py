"""Read-only projections of authenticated collector publications."""
import math
import time

STALE_AFTER_SEC = 300


def publication_time(env):
    return env['payload'].get('updated_at', env['created_at'])


def is_stale(env, now):
    stamp = publication_time(env)
    return (type(stamp) not in (int, float) or not math.isfinite(stamp)
            or stamp > now + 60 or now - stamp > STALE_AFTER_SEC
            or env['expires_at'] <= now)


def is_qidian(env):
    """An explicit channel declaration takes precedence over legacy omission."""
    payload = env['payload']
    if 'channel' in payload and payload['channel'] != 'qidian':
        return False
    accounts = payload.get('accounts', [])
    if isinstance(accounts, list):
        for account in accounts:
            if (isinstance(account, dict)
                    and str(account.get('account_uid', '')) == env['account_uid']
                    and 'channel' in account and account['channel'] != 'qidian'):
                return False
    return True


def project(envelopes, *, workbench_id=None, account_uid=None, now=None):
    now = time.time() if now is None else now
    latest = {}
    for env in envelopes:
        if env['kind'] not in ('catalog', 'contacts'):
            continue
        key = (env['workbench_id'], env['account_uid'], env['kind'])
        if key not in latest or env['created_at'] >= latest[key]['created_at']:
            latest[key] = env
    # A contacts publication without channel must not resurrect an identity
    # whose authenticated catalog explicitly identifies QQ or WeChat.
    excluded = {(wid, uid) for (wid, uid, _), env in latest.items() if not is_qidian(env)}
    latest = {key: env for key, env in latest.items() if key[:2] not in excluded}
    if workbench_id is not None:
        env = latest.get((workbench_id, account_uid, 'contacts'))
        if env is None:
            raise LookupError('CONTACTS_NOT_AVAILABLE')
        payload = env['payload']
        return {'identity': {key: env[key] for key in ('owner_user_id', 'workbench_id', 'account_uid')},
                'source': 'its-relay', 'channel': 'qidian', 'workbench_id': workbench_id, 'account_uid': account_uid,
                'contacts': payload['contacts'], 'org': payload.get('org', {}),
                'org_state': payload.get('org_state', 'unreported'),
                'org_updated_at': payload.get('org_updated_at'), 'org_error': payload.get('org_error'),
                'updated_at': payload.get('updated_at', env['created_at']),
                'stale': is_stale(env, now), 'available': True,
                'publication_id': env['message_id'], 'publication_hash': env['payload_hash'],
                'source_path': f'wb/{workbench_id}/qidian/{account_uid}/contacts.json'}
    workbenches = {}
    for (wid, uid, kind), env in sorted(latest.items()):
        payload = env['payload']
        wb = workbenches.setdefault(wid, {'workbench_id': wid, 'name': wid,
                                         'updated_at': None, 'stale': True, 'accounts': {}})
        stamp = publication_time(env)
        if type(stamp) not in (int, float) or not math.isfinite(stamp):
            stamp = env['created_at']
        if wb['updated_at'] is None or stamp > wb['updated_at']:
            wb['updated_at'] = stamp
        wb['stale'] = wb['stale'] and is_stale(env, now)
        if kind == 'catalog':
            wb['name'] = payload.get('workbench_name') or payload.get('name') or wid
        accounts = payload.get('accounts') if kind == 'catalog' else None
        if not isinstance(accounts, list) or not accounts:
            accounts = [{'account_uid': uid}]
        for account in accounts:
            if not isinstance(account, dict):
                continue
            # The catalog's authenticated account is the only publishable identity.
            # Other accounts require their own authenticated catalog/contacts.
            if str(account.get('account_uid', '')) != uid:
                continue
            old = wb['accounts'].get(uid)
            row = {'account_uid': uid, 'channel': 'qidian', 'account_nick': account.get('account_nick') or account.get('nickname') or payload.get('account_nick') or uid,
                   'updated_at': payload.get('updated_at', stamp), 'stale': is_stale(env, now),
                   'available': not is_stale(env, now),
                   'capabilities': account.get('capabilities', payload.get('capabilities', [])),
                   'contacts_path': f'wb/{wid}/qidian/{uid}/contacts.json',
                   'source_path': f'wb/{wid}/out/xunjia/{uid}/catalog.bin'}
            if old is None or kind == 'catalog':
                wb['accounts'][uid] = row
    for wb in workbenches.values():
        wb['accounts'] = list(wb['accounts'].values())
    return {'workbenches': list(workbenches.values())}
