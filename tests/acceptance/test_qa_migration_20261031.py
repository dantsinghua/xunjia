"""Independent upgrade and durable retry acceptance; no production DB access."""
from pathlib import Path
from types import SimpleNamespace
import pytest
from server.qa_history import QAHistory
from deploy import build_incremental as builder
from deploy import qa_history_migration as migration
from tests.release.test_incremental_capsule import sample,write,seal,load,installed,FakeManager


def test_expired_read_attempt_is_explicitly_retryable_and_old_result_is_fenced(tmp_path,monkeypatch):
    now=[1000.0];monkeypatch.setattr('server.qa_history.time.time',lambda:now[0])
    history=QAHistory(tmp_path/'history.db');sid=history.create('alice')['id']
    tid,_=history.claim('alice',sid,'request-one','test')
    now[0]+=601
    reopened=QAHistory(history.path)
    assert reopened.read('alice',sid)['messages'][-1]['state']=='failed'
    # Reading does not dispatch a model or create another attempt.
    with reopened.db() as db:assert db.execute('SELECT count(*) FROM qa_turn').fetchone()[0]==1
    second,_=reopened.claim('alice',sid,'request-one','test');assert second!=tid
    with pytest.raises(LookupError):history.finish('alice',tid,{'answer':'late'})
    reopened.finish('alice',second,{'answer':'current'})
    assert reopened.claim('alice',sid,'request-one','test')[1]=={'answer':'current'}
    with pytest.raises(LookupError):reopened.read('bob',sid)


@pytest.mark.parametrize('failure',[False,True])
def test_signed_capsule_automatically_migrates_at_stop_boundary_and_recovers(tmp_path,failure):
    root=Path(__file__).resolve().parents[2]
    old=sample(tmp_path/'old','base-one','old source\n')
    new=sample(tmp_path/'new','target-release','new source\n')
    for name in ('deploy/qa_history_migration.py','deploy/database_upgrade.py','sql/oceanbase/005_qa_history.sql'):
        write(new,name,(root/name).read_bytes())
    write(new,'app/server/qa_history.py',(root/'server/qa_history.py').read_bytes())
    seal(new,'target-release');output=tmp_path/'upgrade.py'
    builder.build([old],new,output);capsule=load(output)
    assert capsule.CAPSULE['automatic_qa_history_migration'] is True
    bundle,prefix,nginx=installed(SimpleNamespace(module=capsule,old=old,root=tmp_path/'site'))
    for name in capsule.CAPSULE['installed_files']['base-one']:
        source=old/('app/'+name) if ('app/'+name) in builder.inspect(old)[1] else old/name
        write(prefix,'app/'+name,source.read_bytes())
    ctl=FakeManager(bundle,prefix);running=[True];events=[]
    ctl.validate_release=lambda _: {'build_id':'base-one'}
    ctl.verify_installed=lambda **_:None
    def stop():events.append('stop');running[0]=False
    def start():events.append('start');running[0]=True
    ctl.service=SimpleNamespace(active=lambda:running[0],stop=stop,start=start)
    def migrate(_,apply):
        if not apply:return {'status':'MIGRATION_REQUIRED'}
        assert not running[0];events.append('migration')
        if failure:raise migration.MigrationError('QA_HISTORY_DDL_PERMISSION_DENIED')
        return {'status':'MIGRATION_VERIFIED','ddl_executed':True}
    def install():
        try:
            ctl.service.stop();events.append('switch');ctl.service.start()
            return {'release_id':'new'}
        except BaseException:ctl.service.start();raise
    ctl.install=install
    def apply():return capsule.deploy(bundle,prefix,apply=True,manager_factory=lambda *_:ctl,
        mount_function=lambda:{},nginx_config=nginx,qa_history_function=migrate)
    if failure:
        with pytest.raises(migration.MigrationError):apply()
        assert events==['stop','migration','start']
    else:
        assert apply()['database_ddl_executed']
        assert events==['stop','migration','switch','start']
    assert running[0] and ctl.service.stop==stop


def test_migration_sql_only_creates_four_owned_tables():
    sql=migration.statements()
    assert set(sql)=={'xj2_qa_session','xj2_qa_turn','xj2_qa_meta','xj2_qa_import'}
    for name,statement in sql.items():
        assert statement.startswith('CREATE TABLE IF NOT EXISTS `xunjia_business`.`'+name+'`')
        assert not any(word in statement.upper().split() for word in ('DROP','TRUNCATE','GRANT','DELETE'))


def test_already_imported_turn_can_finish_a_recovered_attempt_without_blocking_next_upgrade(monkeypatch):
    original={'id':'a'*32,'owner':'alice','session_id':'s'*32,'request_id':'request-one',
              'question':'test','state':'pending','result':'{}','created_at':1000.,'updated_at':1000.}
    current={**original,'id':'b'*32,'state':'complete','result':'{"answer":"restored"}','updated_at':1700.}
    monkeypatch.setattr(migration,'source_rows',lambda _:iter([('xj2_qa_turn',original)]))
    def query(_,sql,args=(),one=False):
        if sql.startswith('SELECT source'):return {'source':'already-verified-import'}
        if 'WHERE `id`=' in sql:return None
        if 'owner=%s' in sql and 'request_id=%s' in sql:
            assert args==('alice','request-one')
            return current
        raise AssertionError(sql)
    monkeypatch.setattr(migration,'query',query)
    result=migration.transfer(object(),object(),{'xj2_qa_turn','xj2_qa_import'},'source')
    assert result['missing_rows']==result['unmarked_rows']==0
    current['question']='unexpected mutation'
    with pytest.raises(migration.MigrationError,match='IDENTITY_CHANGED'):
        migration.transfer(object(),object(),{'xj2_qa_turn','xj2_qa_import'},'source')
