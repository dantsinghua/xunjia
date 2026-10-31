"""Independent retention acceptance: real temporary files and SQLite only."""
from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
import sqlite3
from types import SimpleNamespace

import pytest

from relay.gateway_paths import CONTRACT, HISTORY_SECONDS, CHUNK_BYTES, InstitutionKeys
from server.retained_archive import InstitutionArchive
from server.retained_directory import InstitutionDirectory
from relay.protocol import Codec, envelope
from relay.file_gateway import DownloadedFile
from relay.gateway_paths import DIRECTORY_PATH
from server import download_retention as downloads
from server import upgrade_retention as upgrades
from server.upgrade_control import UpgradeStore
from tests.cutover.file_gateway_fakes import AUTH

NOW = 1_900_000_000.0
DAY = 86400


def journal(path, *, directory=False):
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as db:
        if directory:
            db.executescript('''CREATE TABLE ip_input(seq INTEGER PRIMARY KEY,path TEXT,state TEXT,name TEXT,blob BLOB,code TEXT,at REAL);
              CREATE TABLE ip_input_data(seq INTEGER PRIMARY KEY,body TEXT);''')
        else:
            db.executescript('''CREATE TABLE fg_download(seq INTEGER PRIMARY KEY,scope TEXT,state TEXT,name TEXT,blob BLOB,code TEXT,received_at REAL);
              CREATE TABLE tx_source(scope TEXT,stream TEXT,epoch TEXT,position INTEGER,kind TEXT,id TEXT,acked INTEGER);
              CREATE TABLE tx_message(scope TEXT,kind TEXT,id TEXT,data TEXT,acked INTEGER);''')
    return path


def packet(path, seq=1, *, stamp=NOW - 31 * DAY, state='STAGED', acked=True, archived=True, scope='account-a'):
    payload = {'kind': 'event', 'message_id': f'event-{seq}', 'payload': {'text': 'retained financial fact'}}
    with sqlite3.connect(path) as db:
        db.execute('INSERT INTO fg_download VALUES(?,?,?,?,?,?,?)',
                   (seq, scope, state, f'packet-{seq}.bin', b'original-encrypted-packet', '', stamp))
        if archived:
            db.execute('INSERT INTO tx_source VALUES(?,?,?,?,?,?,?)', (scope, 'file-gateway-service', 'v1', seq, 'event', payload['message_id'], int(acked)))
            db.execute('INSERT INTO tx_message VALUES(?,?,?,?,?)', (scope, 'event', payload['message_id'], json.dumps(payload), int(acked)))
    return payload


def blob(path, seq=1, table='fg_download'):
    with sqlite3.connect(path) as db:
        return db.execute(f'SELECT blob FROM {table} WHERE seq=?', (seq,)).fetchone()[0]


@pytest.mark.parametrize('age,removed', [(30 * DAY - 1, 0), (30 * DAY, 1), (31 * DAY, 1)])
def test_acknowledged_packet_expires_at_thirty_days_and_preserves_decoded_fact(tmp_path, age, removed):
    path = journal(tmp_path / 'account.db')
    fact = packet(path, stamp=NOW - age)
    result = downloads.prune_journal(path, now=NOW)
    assert result['payloads_removed'] == removed
    assert (blob(path) is None) == bool(removed)
    with sqlite3.connect(path) as db:
        assert json.loads(db.execute('SELECT data FROM tx_message').fetchone()[0]) == fact
        assert db.execute('SELECT acked FROM tx_source').fetchone()[0] == 1
        if removed:
            sha, size = db.execute('SELECT sha256,size FROM download_retention').fetchone()
            assert sha == hashlib.sha256(b'original-encrypted-packet').hexdigest()
            assert size == len(b'original-encrypted-packet')


@pytest.mark.parametrize('state,acked,archived', [
    ('UNKNOWN', True, True), ('RECEIVING', True, True), ('STORED', True, True),
    ('QUARANTINED', True, True), ('STAGED', False, True), ('STAGED', True, False),
])
def test_uncertain_uncommitted_or_unarchived_packets_are_never_deleted(tmp_path, state, acked, archived):
    path = journal(tmp_path / 'account.db')
    packet(path, state=state, acked=acked, archived=archived)
    result = downloads.prune_journal(path, now=NOW)
    assert result['payloads_removed'] == 0 and result['protected'] == 1
    assert blob(path) == b'original-encrypted-packet'


def test_corrupt_decoded_fact_keeps_recovery_packet(tmp_path):
    path = journal(tmp_path / 'account.db')
    packet(path)
    with sqlite3.connect(path) as db:
        db.execute("UPDATE tx_message SET data='not-json'")
    assert downloads.prune_journal(path, now=NOW)['protected'] == 1
    assert blob(path) is not None


@pytest.mark.parametrize('stamp', [None, 0, -1, float('inf')])
def test_legacy_unknown_timestamp_requires_full_observation_window(tmp_path, stamp):
    path = journal(tmp_path / 'account.db')
    packet(path, stamp=stamp)
    assert downloads.prune_journal(path, now=NOW)['observing'] == 1
    assert downloads.prune_journal(path, now=NOW + 30 * DAY - 1)['payloads_removed'] == 0
    assert downloads.prune_journal(path, now=NOW + 30 * DAY)['payloads_removed'] == 1


def test_legacy_table_without_timestamp_is_observed_before_cleanup(tmp_path):
    path = journal(tmp_path / 'account.db')
    packet(path)
    with sqlite3.connect(path) as db:
        db.execute('ALTER TABLE fg_download DROP COLUMN received_at')
    assert downloads.prune_journal(path, now=NOW)['observing'] == 1
    assert downloads.prune_journal(path, now=NOW + 30 * DAY)['payloads_removed'] == 1


def test_new_traffic_cannot_starve_observed_old_packet(tmp_path):
    path = journal(tmp_path / 'account.db')
    packet(path, seq=1, stamp=None)
    packet(path, seq=2, stamp=NOW)
    downloads.prune_journal(path, now=NOW, limit=1)
    for seq in range(3, 8):
        packet(path, seq=seq, stamp=NOW + 31 * DAY)
        downloads.prune_journal(path, now=NOW + 31 * DAY, limit=1)
    assert blob(path, 1) is None


def test_directory_packet_requires_durable_full_decoded_archive(tmp_path):
    path = journal(tmp_path / 'directory.db', directory=True)
    with sqlite3.connect(path) as db:
        db.execute('INSERT INTO ip_input VALUES(1,?,?,?,?,?,?)', ('directory', 'APPLIED', 'one.bin', b'one', '', NOW - 31 * DAY))
        db.execute('INSERT INTO ip_input VALUES(2,?,?,?,?,?,?)', ('directory', 'APPLIED', 'two.bin', b'two', '', NOW - 31 * DAY))
        db.execute('INSERT INTO ip_input_data VALUES(1,?)', (json.dumps({'payload': {'contacts': ['retained']}}),))
    result = downloads.prune_journal(path, now=NOW)
    assert result['payloads_removed'] == 1 and result['protected'] == 1
    assert blob(path, 1, 'ip_input') is None
    assert blob(path, 2, 'ip_input') == b'two'
    with sqlite3.connect(path) as db:
        assert json.loads(db.execute('SELECT body FROM ip_input_data').fetchone()[0])['payload']['contacts'] == ['retained']


def directory_wire(keys, *, revision, stamp):
    binding = keys.binding('workbench-a', 'directory')
    payload = {'contract': CONTRACT, 'op': 'directory', 'updated_at': stamp,
               'revision': revision, 'accounts': [], 'workbench_name': f'name-{revision}'}
    data = envelope(binding, 'manifest', payload, message_id=f'directory-{revision}',
                    created_at=stamp, expires_at=stamp + DAY)
    return data, Codec(binding, clock=lambda: stamp).pack(data)


def test_directory_consume_archives_authenticated_data_before_raw_packet_expires(tmp_path):
    keys = InstitutionKeys.from_auth(AUTH)
    clock = [NOW]
    directory = InstitutionDirectory(tmp_path / 'directory.db', keys, clock=lambda: clock[0])
    data, raw = directory_wire(keys, revision=1, stamp=NOW)
    client = SimpleNamespace(path=DIRECTORY_PATH, pending=lambda: 1,
                             download=lambda: DownloadedFile('directory.bin', raw))
    assert directory.consume(client, limit=1) == 1
    with directory.db() as db:
        assert json.loads(db.execute('SELECT body FROM ip_input_data').fetchone()[0]) == data
        assert db.execute('SELECT state FROM ip_input').fetchone()[0] == 'APPLIED'
    clock[0] += 31 * DAY
    assert downloads.prune_journal(directory.path, now=clock[0])['payloads_removed'] == 1
    with directory.db() as db:
        assert json.loads(db.execute('SELECT body FROM ip_snapshot').fetchone()[0])['revision'] == 1


def test_legacy_directory_backfill_does_not_reapply_older_snapshot(tmp_path):
    keys = InstitutionKeys.from_auth(AUTH)
    directory = InstitutionDirectory(tmp_path / 'directory.db', keys, clock=lambda: NOW)
    old, raw = directory_wire(keys, revision=1, stamp=NOW - 31 * DAY)
    latest, _ = directory_wire(keys, revision=2, stamp=NOW)
    directory.put(latest)
    with directory.db() as db:
        db.execute('INSERT INTO ip_input(path,state,name,blob,code,at) VALUES(?,?,?,?,?,?)',
                   (DIRECTORY_PATH, 'APPLIED', 'legacy.bin', raw, '', NOW - 31 * DAY))
    assert directory.archive_old_inputs(limit=1) == 1
    assert downloads.prune_journal(directory.path, now=NOW)['payloads_removed'] == 1
    with directory.db() as db:
        assert json.loads(db.execute('SELECT body FROM ip_input_data').fetchone()[0]) == old
        assert json.loads(db.execute('SELECT body FROM ip_snapshot').fetchone()[0])['revision'] == 2


def test_legacy_directory_invalid_packet_is_preserved_for_recovery(tmp_path):
    directory = InstitutionDirectory(tmp_path / 'directory.db', InstitutionKeys.from_auth(AUTH), clock=lambda: NOW)
    with directory.db() as db:
        db.execute('INSERT INTO ip_input(path,state,name,blob,code,at) VALUES(?,?,?,?,?,?)',
                   (DIRECTORY_PATH, 'APPLIED', 'invalid.bin', b'not-authenticated', '', NOW - 31 * DAY))
    assert directory.archive_old_inputs(limit=1) == 0
    assert downloads.prune_journal(directory.path, now=NOW)['protected'] == 1
    assert blob(directory.path, table='ip_input') == b'not-authenticated'


def archive_media(tmp_path, *, content=b'complete attachment', stamp=NOW - 31 * DAY, archived=True, state='ready'):
    clock = [NOW]
    archive = InstitutionArchive(tmp_path / 'archive', clock=lambda: clock[0])
    sha = hashlib.sha256(content).hexdigest()
    target = archive._media_path('workbench-a', 'account-a', sha)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content)
    with archive.db() as db:
        db.execute('INSERT INTO ip_media VALUES(?,?,?,?,?,?,?)', ('workbench-a', 'account-a', sha, len(content), 1, stamp, state))
        if archived:
            db.execute('INSERT INTO ip_media_content VALUES(?,?,?,?,?,?)', ('workbench-a', 'account-a', sha, len(content), content, stamp))
    return archive, target, sha, clock


def test_legacy_media_is_committed_to_database_before_disk_copy_is_unlinked(tmp_path, monkeypatch):
    archive, target, sha, _ = archive_media(tmp_path, archived=False)
    original_unlink = Path.unlink
    observations = []
    def guarded_unlink(path, *args, **kwargs):
        if path == target:
            with sqlite3.connect(archive.path) as db:
                saved = db.execute('SELECT data FROM ip_media_content WHERE hash=?', (sha,)).fetchone()
            assert saved and saved[0] == b'complete attachment'
            observations.append('committed-before-unlink')
        return original_unlink(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'unlink', guarded_unlink)
    assert archive.prune()['media_removed'] == 1
    assert observations == ['committed-before-unlink'] and not target.exists()


@pytest.mark.parametrize('age,removed', [(30 * DAY - 1, 0), (30 * DAY, 1), (31 * DAY, 1)])
def test_media_cache_uses_thirty_days_not_seven(tmp_path, age, removed):
    archive, target, _, _ = archive_media(tmp_path, stamp=NOW - age)
    assert archive.prune()['media_removed'] == removed
    assert target.exists() is not bool(removed)


def test_history_and_attachment_metadata_remain_in_database_after_cache_cleanup(tmp_path):
    archive, target, sha, _ = archive_media(tmp_path)
    history = {'text': 'audited text', 'media': [{'sha256': sha}]}
    with archive.db() as db:
        db.execute('INSERT INTO ip_archive(workbench,account,native,peer,direction,stamp,fingerprint,body) VALUES(?,?,?,?,?,?,?,?)',
                   ('workbench-a', 'account-a', 'event-a', 'peer-a', 'in', NOW - 31 * DAY, 'fp', json.dumps(history)))
    result = archive.prune()
    assert result['history_removed'] == 0 and result['media_removed'] == 1
    with archive.db() as db:
        assert json.loads(db.execute('SELECT body FROM ip_archive').fetchone()[0]) == history
        assert db.execute('SELECT hash FROM ip_media').fetchone()[0] == sha
        assert db.execute('SELECT data FROM ip_media_content').fetchone()[0] == b'complete attachment'


@pytest.mark.parametrize('condition', ['pending', 'corrupt-content', 'recent-reference'])
def test_incomplete_corrupt_or_currently_referenced_media_is_protected(tmp_path, condition):
    archive, target, sha, _ = archive_media(tmp_path, state='pending' if condition == 'pending' else 'ready')
    with archive.db() as db:
        if condition == 'corrupt-content':
            db.execute('UPDATE ip_media_content SET data=?', (b'damaged',))
        elif condition == 'recent-reference':
            db.execute('INSERT INTO ip_native_media_ref VALUES(?,?,?,?,?,?,?,?)',
                       ('workbench-a', 'account-a', 'event-a', 'peer-a', 'one.txt', sha, len(target.read_bytes()), NOW))
    assert archive.prune()['protected'] == 1
    assert target.read_bytes() == b'complete attachment'


def test_recent_reference_restores_expired_cache_from_durable_content(tmp_path):
    archive, target, sha, clock = archive_media(tmp_path)
    archive.prune()
    assert not target.exists()
    archive.accept({'workbench_id': 'workbench-a', 'account_uid': 'account-a', 'payload': {
        'contract': CONTRACT, 'op': 'native_media', 'event_id': 'new-reference', 'peer_id': 'peer-a',
        'attachments': [{'name': 'one.txt', 'sha256': sha, 'size': len(b'complete attachment')}]}})
    assert archive.media('workbench-a', 'account-a', sha).read_bytes() == b'complete attachment'
    assert archive.prune()['media_removed'] == 0


def test_completed_media_chunks_are_archived_as_verified_full_content(tmp_path):
    archive = InstitutionArchive(tmp_path / 'archive', clock=lambda: NOW)
    content = b'a' * CHUNK_BYTES + b'final bytes'
    sha = hashlib.sha256(content).hexdigest()
    def accept(position, part):
        archive.accept({'workbench_id': 'workbench-a', 'account_uid': 'account-a', 'payload': {
            'contract': CONTRACT, 'op': 'media_chunk', 'sha256': sha, 'size': len(content),
            'chunks': 2, 'position': position, 'data': base64.b64encode(part).decode()}})
    accept(0, content[:CHUNK_BYTES])
    assert not archive._media_path('workbench-a', 'account-a', sha).exists()
    with archive.db() as db:
        assert db.execute('SELECT COUNT(*) FROM ip_media_content').fetchone()[0] == 0
        assert db.execute('SELECT COUNT(*) FROM ip_chunk').fetchone()[0] == 1
    accept(1, content[CHUNK_BYTES:])
    with archive.db() as db:
        assert db.execute('SELECT data FROM ip_media_content').fetchone()[0] == content
        assert db.execute('SELECT COUNT(*) FROM ip_chunk').fetchone()[0] == 0
        assert db.execute('SELECT state FROM ip_media').fetchone()[0] == 'ready'
    assert archive._media_path('workbench-a', 'account-a', sha).read_bytes() == content


def test_failed_cache_unlink_preserves_durable_content_and_can_be_retried(tmp_path, monkeypatch):
    archive, target, _, _ = archive_media(tmp_path, archived=False)
    original = Path.unlink
    def failure(path, *args, **kwargs):
        if path == target:
            raise OSError('synthetic disk failure')
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'unlink', failure)
    with pytest.raises(OSError, match='synthetic disk failure'):
        archive.prune()
    with archive.db() as db:
        assert db.execute('SELECT data FROM ip_media_content').fetchone()[0] == target.read_bytes()
        assert db.execute('SELECT pruned_at FROM ip_media_cache').fetchone()[0] is None
    monkeypatch.setattr(Path, 'unlink', original)
    assert archive.prune()['media_removed'] == 1
    assert not target.exists()


def test_media_symlink_never_unlinks_an_external_file(tmp_path):
    archive, target, _, _ = archive_media(tmp_path)
    outside = tmp_path / 'outside.txt'
    outside.write_text('must survive')
    target.unlink()
    target.symlink_to(outside)
    with pytest.raises(ValueError, match='ARCHIVE_MEDIA_STORAGE_CHANGED'):
        archive.prune()
    assert outside.read_text() == 'must survive' and target.is_symlink()


def test_known_journals_include_inactive_accounts_and_exclude_symlinks(tmp_path):
    root = tmp_path / 'state'
    keys = InstitutionKeys.from_auth(AUTH)
    assert len(keys.institution_id) == 32
    older = journal(root / ('a' * 32) / 'accounts' / ('b' * 64 + '.db'))
    current = journal(root / keys.institution_id / 'directory.db', directory=True)
    outside = journal(tmp_path / 'outside.db')
    link = root / ('a' * 32) / 'accounts' / ('c' * 64 + '.db')
    link.symlink_to(outside)
    assert {p for _, p in downloads.known_journals(root)} == {older, current}


def test_bad_journal_does_not_prevent_other_account_cleanup(tmp_path):
    root = tmp_path / 'state'
    broken = root / ('a' * 32) / 'accounts' / ('a' * 64 + '.db')
    broken.parent.mkdir(parents=True)
    broken.write_bytes(b'not sqlite')
    valid = journal(broken.parent / ('b' * 64 + '.db'))
    packet(valid)
    result = downloads.prune_known_downloads(root, now=NOW)
    assert len(result['errors']) == 1 and result['payloads_removed'] == 1
    assert blob(valid) is None and broken.read_bytes() == b'not sqlite'


def test_inactive_archive_scan_uses_service_only_retention(tmp_path):
    root = tmp_path / 'institution-path-v1'
    archive, target, sha, _ = archive_media(root / ('a' * 32))
    result = downloads.prune_known_downloads(root, now=NOW)
    assert result['media_removed'] == 1 and not result['errors']
    assert not target.exists()
    with archive.db() as db:
        assert db.execute('SELECT hash FROM ip_media').fetchone()[0] == sha
        assert db.execute('SELECT data FROM ip_media_content').fetchone()[0] == b'complete attachment'


def test_service_runtime_uses_retention_extensions_without_replacing_shared_classes(tmp_path, monkeypatch):
    from relay.institution_archive import InstitutionArchive as SharedArchive
    from relay.institution_directory import InstitutionDirectory as SharedDirectory
    from tests.institution_test_support import build_harness
    s = build_harness(tmp_path / 'runtime', monkeypatch, selected=False)
    try:
        assert type(s['runtime'].archive) is InstitutionArchive
        assert type(s['runtime'].directory) is InstitutionDirectory
        assert isinstance(s['runtime'].archive, SharedArchive)
        assert isinstance(s['runtime'].directory, SharedDirectory)
        shared = SharedArchive(tmp_path / 'shared-collector')
        with shared.db() as db:
            names = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert 'ip_media_content' not in names and 'ip_media_cache' not in names
    finally:
        s['stack'].close()


def age_tree(path, stamp):
    for item in [*path.rglob('*'), path] if path.is_dir() else [path]:
        if not item.is_symlink():
            os.utime(item, (stamp, stamp))


@pytest.fixture
def managed_files(tmp_path, monkeypatch):
    prefix = tmp_path / 'xunjia'
    (prefix / 'config').mkdir(parents=True)
    (prefix / 'deployments').mkdir()
    store = UpgradeStore(prefix / 'state/upgrade')
    candidates = []
    def release(name, build, previous=None):
        path = prefix / 'releases' / name
        (path / 'app').mkdir(parents=True)
        (path / 'RELEASE.json').write_text(json.dumps({'previous': str(previous) if previous else None}))
        (path / 'app/VERSION.json').write_text(json.dumps({'build_id': build}))
        age_tree(path, NOW - 31 * DAY)
        candidates.append((path, 'release'))
        return path
    previous = release('previous', 'b' * 64)
    current = release('current-release', 'c' * 64, previous)
    (prefix / 'current').symlink_to(current)
    # Discovery seam is constrained to these known temp artifacts. Never scan /root.
    def discover(s, p):
        assert s.root.is_relative_to(tmp_path) and p.is_relative_to(tmp_path)
        for path, kind in candidates:
            if path.exists() and not path.is_symlink():
                upgrades.register_artifact(s, path, kind)
    monkeypatch.setattr(upgrades, 'discover', discover)
    clock = [NOW]
    monkeypatch.setattr(upgrades.time, 'time', lambda: clock[0])
    def bundle(name, build='d' * 64):
        path = store.root / 'bundles' / ('xunjia-update-' + name)
        path.mkdir(parents=True)
        (path / 'VERSION.json').write_text(json.dumps({'build_id': build}))
        (path / 'SHA256SUMS.txt').write_text('synthetic artifact inventory')
        age_tree(path, NOW - 31 * DAY)
        candidates.append((path, 'bundle'))
        return path
    return SimpleNamespace(prefix=prefix, store=store, previous=previous, current=current,
                           release=release, bundle=bundle, candidates=candidates, clock=clock)


def test_cleanup_keeps_current_rollback_and_base_but_removes_expired_releases(managed_files):
    s = managed_files
    old = s.release('old', 'a' * 64)
    base = s.bundle('base', 'e' * 64)
    stale = s.bundle('unused')
    s.store.set('base_bundle', str(base))
    result = upgrades.prune_artifacts(s.store, s.prefix)
    assert result['removed'] == 2
    assert not old.exists() and not stale.exists()
    assert s.current.exists() and s.previous.exists() and base.exists()


def test_cleanup_keeps_bundle_matching_rollback_build(managed_files):
    s = managed_files
    rollback_bundle = s.bundle('rollback', 'b' * 64)
    assert upgrades.prune_artifacts(s.store, s.prefix)['removed'] == 0
    assert rollback_bundle.exists()


def test_unfinished_deployment_prevents_any_file_deletion(managed_files):
    s = managed_files
    stale = s.bundle('unused')
    (s.prefix / 'deployments/unresolved.json').write_text(json.dumps({'phase': 'SWITCHING'}))
    upgrades.prune_artifacts(s.store, s.prefix)
    assert stale.exists() and s.store.get('retention')['blocked'] == 1


@pytest.mark.parametrize('state', ['queued', 'downloading', 'verifying', 'installing', 'unknown'])
def test_active_or_unknown_job_files_are_protected(managed_files, state):
    s = managed_files
    identity = 'a' * 32
    job = s.store.root / 'jobs' / identity
    job.mkdir(parents=True)
    (job / 'package.zip').write_bytes(b'package')
    age_tree(job, NOW - 31 * DAY)
    with s.store.db() as db:
        db.execute('INSERT INTO job(id,state,owner,created_at,updated_at) VALUES(?,?,?,?,?)', (identity, state, 'owner', NOW - 31 * DAY, NOW))
    s.candidates.append((job, 'job'))
    assert upgrades.prune_artifacts(s.store, s.prefix)['removed'] == 0
    assert (job / 'package.zip').read_bytes() == b'package'


def test_terminal_job_cache_expires_but_job_audit_remains(managed_files):
    s = managed_files
    identity = 'a' * 32
    job = s.store.root / 'jobs' / identity
    job.mkdir(parents=True)
    (job / 'package.zip').write_bytes(b'package')
    age_tree(job, NOW - 31 * DAY)
    with s.store.db() as db:
        db.execute('INSERT INTO job(id,state,owner,created_at,updated_at) VALUES(?,?,?,?,?)', (identity, 'succeeded', 'owner', NOW - 31 * DAY, NOW - 31 * DAY))
    s.candidates.append((job, 'job'))
    assert upgrades.prune_artifacts(s.store, s.prefix)['removed'] == 1
    assert not job.exists() and s.store.latest()['state'] == 'succeeded'


@pytest.mark.parametrize('reference', ['relative-json', 'symlink-cert', 'external-json'])
def test_referenced_credentials_protect_old_bundle(managed_files, reference):
    s = managed_files
    old = s.bundle('certificate')
    cert = old / 'client.crt'
    cert.write_bytes(b'PUBLIC SYNTHETIC CERTIFICATE')
    if reference == 'relative-json':
        relative = os.path.relpath(cert, s.prefix / 'config')
        (s.prefix / 'config/connection.json').write_text(json.dumps({'client_cert_file': relative}))
    elif reference == 'symlink-cert':
        (s.prefix / 'config/client.crt').symlink_to(cert)
    else:
        outside = s.prefix.parent / 'external'
        outside.mkdir()
        (outside / 'connection.json').write_text(json.dumps({'client_cert_file': str(cert)}))
        (s.prefix / 'config/xunjia.env').write_text('XUNJIA_INSTITUTION_CONFIG=' + str(outside / 'connection.json'))
    age_tree(old, NOW - 31 * DAY)
    upgrades.prune_artifacts(s.store, s.prefix)
    assert cert.read_bytes() == b'PUBLIC SYNTHETIC CERTIFICATE'


def test_malformed_live_configuration_fails_closed_before_deletion(managed_files):
    s = managed_files
    stale = s.bundle('unused')
    (s.prefix / 'config/bad.json').write_text('{not-json')
    with pytest.raises(ValueError):
        upgrades.prune_artifacts(s.store, s.prefix)
    assert stale.exists()


def test_external_connection_symlink_resolves_certificates_from_original_alias_directory(managed_files):
    """Match resolve_tls_files(config, config_path.parent), including symlink aliases."""
    from relay.file_gateway_connection import resolve_tls_files
    s = managed_files
    old = s.bundle('still-used-cert')
    cert = old / 'client.crt'
    cert.write_bytes(b'PUBLIC SYNTHETIC CERTIFICATE')
    external = s.prefix.parent / 'external'
    canonical = external / 'canonical'
    alias = external / 'alias'
    canonical.mkdir(parents=True)
    alias.mkdir()
    (canonical / 'connection.json').write_text(json.dumps({'client_cert_file': 'client.crt'}))
    connection = alias / 'connection.json'
    connection.symlink_to(canonical / 'connection.json')
    (alias / 'client.crt').symlink_to(cert)
    actual = resolve_tls_files(json.loads(connection.read_text()), connection.parent)
    assert Path(actual['client_cert_file']).resolve() == cert
    (s.prefix / 'config/xunjia.env').write_text('XUNJIA_INSTITUTION_CONFIG=' + str(connection))
    age_tree(old, NOW - 31 * DAY)
    upgrades.prune_artifacts(s.store, s.prefix)
    assert cert.exists(), 'The live TLS certificate resolved by the connection loader must survive cleanup'


def test_malformed_expired_bundle_is_protected_without_starving_other_artifacts(managed_files):
    s = managed_files
    broken = s.bundle('bad')
    (broken / 'VERSION.json').write_text('broken metadata')
    age_tree(broken, NOW - 32 * DAY)
    valid = s.bundle('valid')
    result = upgrades.prune_artifacts(s.store, s.prefix)
    assert result['blocked'] >= 1 and broken.exists()
    assert not valid.exists(), 'An unreadable old cache must not stop cleanup of other verified artifacts'


def test_missing_previously_registered_bundle_does_not_block_other_cleanup(managed_files):
    s = managed_files
    missing = s.bundle('missing')
    upgrades.register_artifact(s.store, missing, 'bundle')
    for child in missing.iterdir():
        child.unlink()
    missing.rmdir()
    other = s.bundle('other')
    assert upgrades.prune_artifacts(s.store, s.prefix)['removed'] == 1
    assert not other.exists()
    with s.store.db() as db:
        assert db.execute('SELECT removed_at FROM artifact WHERE path=?', (str(missing),)).fetchone()[0] == NOW


def test_protected_first_page_does_not_starve_later_expired_file(managed_files):
    s = managed_files
    for i in range(105):
        s.bundle(f'protected-{i:03}', 'b' * 64)
    expired = s.bundle('zz-expired')
    upgrades.prune_artifacts(s.store, s.prefix)
    assert expired.exists()
    s.clock[0] += 3601
    upgrades.prune_artifacts(s.store, s.prefix)
    assert not expired.exists()


def test_legacy_allowlist_excludes_private_packages_and_maintenance_scripts(tmp_path, monkeypatch):
    monkeypatch.setattr(upgrades, 'LEGACY_DIRECTORIES', (str(tmp_path),))
    assert upgrades.legacy_file(tmp_path / 'upgrade.py')
    assert upgrades.legacy_file(tmp_path / 'net-result-123.json')
    for name in ('signing.key', 'watch.py', 'net.py', 'xjkeep.sh', 'private.zip', 'account.db', 'institution-auth.yaml'):
        assert not upgrades.legacy_file(tmp_path / name)
    assert not upgrades.legacy_file(Path('/root/Xunjia-Private-20261026.zip'))


@pytest.mark.parametrize('name', ['install.py', 'install.sh', 'channel.json'])
def test_bootstrap_files_expire_only_inside_registered_download_namespace(managed_files, tmp_path, monkeypatch, name):
    s = managed_files
    root = tmp_path / 'downloads' / 'xjbridge'
    downloaded = root / 'remote' / 'xj-remote-upgrade' / name
    downloaded.parent.mkdir(parents=True)
    downloaded.write_text('{}' if name.endswith('.json') else 'public bootstrap source')
    protected = s.prefix / 'config' / name
    protected.write_text('{}' if name.endswith('.json') else 'active configuration')
    unrelated = tmp_path / name
    unrelated.write_text('unrelated operator file')
    monkeypatch.setattr(upgrades, 'LEGACY_DIRECTORIES', (str(root),))
    for path in (downloaded, protected, unrelated):
        age_tree(path, NOW - 31 * DAY)
        s.candidates.append((path, 'legacy'))
    assert upgrades.legacy_file(downloaded), 'Only these exact bootstrap names in known download directories should expire'
    assert not upgrades.legacy_file(Path('/root') / name)
    result = upgrades.prune_artifacts(s.store, s.prefix)
    assert result['removed'] == 1 and not downloaded.exists()
    assert protected.exists() and unrelated.read_text() == 'unrelated operator file'


def test_safe_removal_of_registered_tree_never_follows_child_symlink(tmp_path):
    outside = tmp_path / 'outside'
    outside.mkdir()
    (outside / 'sentinel').write_bytes(b'keep')
    owned = tmp_path / 'owned'
    owned.mkdir()
    (owned / 'link').symlink_to(outside, target_is_directory=True)
    upgrades.remove_tree(owned)
    assert not owned.exists() and (outside / 'sentinel').read_bytes() == b'keep'


def test_safe_removal_refuses_symlink_parent(tmp_path):
    outside = tmp_path / 'outside'
    outside.mkdir()
    victim = outside / 'victim'
    victim.write_text('keep')
    link = tmp_path / 'link'
    link.symlink_to(outside, target_is_directory=True)
    with pytest.raises(OSError):
        upgrades.remove_tree(link / 'victim')
    assert victim.read_text() == 'keep'


def test_cleaning_error_is_reported_without_blocking_institution_intake(tmp_path, monkeypatch):
    from tests.institution_test_support import build_harness
    s = build_harness(tmp_path / 'runtime', monkeypatch, selected=False)
    try:
        def failed(*args, **kwargs):
            raise OSError('synthetic cleanup failure')
        monkeypatch.setattr(downloads, 'prune_known_downloads', failed)
        before = len(s['wire'].calls)
        s['runtime']._last_archive_prune = 0
        s['runtime'].tick()
        after = s['wire'].calls[before:]
        assert any(operation == 'pending' for _, operation in after)
        assert s['runtime'].download_retention['state'] == 'error'
        assert s['runtime'].errors['download_retention'] == 'DOWNLOAD_CLEANUP_FAILED'
    finally:
        s['stack'].close()
