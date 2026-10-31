"""Real protected SQLite diagnostics, hostile log text and API authentication."""
import json
from pathlib import Path
import pytest

from server.upgrade_control import UpgradeStore
from server.upgrade_diagnostics import event, snapshot, log_text, redact

JOB='a'*32


@pytest.fixture
def store(tmp_path):
    prefix=tmp_path/'installed'
    s=UpgradeStore(prefix/'state/upgrade')
    with s.db() as db:
        db.execute('INSERT INTO job(id,state,owner,created_at,updated_at) VALUES(?,?,?,?,?)',(JOB,'failed','private-owner',1,2))
    config=prefix/'config';config.mkdir()
    secret=config/'xunjia.env';secret.write_text("XUNJIA_AUTH_SECRET=actual-local-secret\nQB_MYSQL_PASSWORD=database-private-value\n")
    secret.chmod(0o600)
    return s


@pytest.mark.parametrize('text',[
    'password=hunter42', 'Cookie: sid=hunter42', 'Authorization: Bearer hunter42',
    'mysql://user:hunter42@host/db', '{"token":"hunter42"}',
    '-----BEGIN PRIVATE KEY-----\nhunter42\n-----END PRIVATE KEY-----',
])
def test_common_sensitive_formats_are_redacted(text):
    assert 'hunter42' not in redact(text)


def test_events_and_download_never_publish_raw_stderr_or_local_secrets(store):
    event(store,JOB,'installing','INSTALL_STARTED',message='actual-local-secret',password='database-private-value',exit_code=17)
    stage=store.root/'jobs'/JOB;stage.mkdir(parents=True)
    p=stage/'apply.log'
    p.write_text('arbitrary raw stdout actual-local-secret\nCookie: sid=unknown-session\n'+
                 json.dumps({'status':'failed','password':'database-private-value','build_id':'b'*64})+'\n')
    text=log_text(store,JOB)
    for value in ('actual-local-secret','database-private-value','unknown-session','private-owner','arbitrary raw stdout'):
        assert value not in text
    assert '17' in text and 'INSTALL_STARTED' in text and 'b'*64 in text


def test_safe_event_order_and_result_metadata_survive_reopening(store):
    one=event(store,JOB,'installing','INSTALL_STARTED',target_build='b'*64)
    two=event(store,JOB,'rolling_back','UPGRADE_RECOVERY_STARTED')
    event(store,JOB,'failed','UPGRADE_ROLLBACK_SUCCEEDED',rollback='restored',health='ready')
    report=snapshot(UpgradeStore(store.root),JOB)
    assert one['id'] < two['id']
    assert [x['phase'] for x in report['events']]==['installing','rolling_back','failed']
    assert 'owner' not in report['job']
    assert report['events'][-1]['details']['health']=='ready'


@pytest.mark.parametrize('name',['../x','/tmp/x','a/b','..',''])
def test_job_path_traversal_is_rejected(store,name):
    with pytest.raises(ValueError):log_text(store,name)


def test_symlinked_log_never_exposes_outside_file(store,tmp_path):
    outside=tmp_path/'private';outside.write_text('outside-do-not-read')
    stage=store.root/'jobs'/JOB;stage.mkdir(parents=True)
    (stage/'apply.log').symlink_to(outside)
    assert 'outside-do-not-read' not in log_text(store,JOB)


def test_oversized_log_is_bounded_and_never_copies_secret_tail(store):
    stage=store.root/'jobs'/JOB;stage.mkdir(parents=True)
    (stage/'apply.log').write_text('x'*200000+'actual-local-secret\n')
    text=log_text(store,JOB)
    assert len(text)<140000 and 'actual-local-secret' not in text


def test_diagnostics_and_logs_require_real_application_login(store,monkeypatch,tmp_path):
    from fastapi.testclient import TestClient
    from server.app import create_app
    from server.auth import AuthConfig,AuthUser
    from server import upgrade_control
    control=upgrade_control.UpgradeControl()
    control.prefix=store.root.parent.parent
    monkeypatch.setattr(control,'configured',lambda:{'operators':['*']})
    monkeypatch.setattr(upgrade_control,'UpgradeControl',lambda:control)
    app=create_app(database_url='sqlite:///'+str(tmp_path/'business.db'),enable_background=False,
        auth_config=AuthConfig(sso_secret='synthetic',session_secret='synthetic-session-secret-at-least-32'))
    token=app.state.auth.issue_token(AuthUser('reader','Reader'))
    with TestClient(app) as client:
        for path in (f'/api/system/upgrade/{JOB}/diagnostics',f'/api/system/upgrade/{JOB}/log'):
            assert client.get(path).status_code==401
            response=client.get(path,headers={'Authorization':'Bearer '+token})
            assert response.status_code==200
            assert 'private-owner' not in response.text
