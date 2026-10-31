"""Only the reviewed nullable reply column may cross a no-DDL release boundary."""
import json
from pathlib import Path
import pytest
from deploy import build_incremental as builder
from tests.release.test_incremental_capsule import sample, seal, write, load, installed, FakeManager
from types import SimpleNamespace

SQL='CREATE TABLE IF NOT EXISTS xj2_message (\n        historical INTEGER NOT NULL, UNIQUE(thread_id,event_id));\n'
CODE='def statements(mysql=False):\n    text="LONGTEXT" if mysql else "TEXT"\n    return [f"historical INTEGER NOT NULL, UNIQUE(thread_id,event_id)"]\n'
MIG='-- Reviewed manual migration only; never executed by the capsule.\nALTER TABLE xj2_message ADD COLUMN reply_json LONGTEXT NULL;\n'

@pytest.fixture
def change(tmp_path):
    old=sample(tmp_path/'old','base-one','old\n');new=sample(tmp_path/'new','target-release','new\n')
    for root in (old,new):
        write(root,'sql/oceanbase/001_xunjia_schema_mysql.sql',SQL)
        write(root,'app/server/negotiation/schema.py',CODE)
    write(new,'sql/oceanbase/001_xunjia_schema_mysql.sql',SQL.replace('NOT NULL, UNIQUE','NOT NULL, reply_json LONGTEXT, UNIQUE'))
    write(new,'app/server/negotiation/schema.py',CODE.replace('NOT NULL, UNIQUE','NOT NULL, reply_json {text}, UNIQUE'))
    write(new,'sql/oceanbase/004_message_reply.sql',MIG)
    seal(old,'base-one');seal(new,'target-release')
    return old,new,tmp_path/'upgrade.py'

def test_optional_nullable_reply_change_requires_explicit_builder_opt_in(change):
    old,new,out=change
    with pytest.raises(builder.runtime.IncrementalError,match='SCHEMA_CHANGE'):
        builder.build([old],new,out)
    assert not out.exists()

def test_reviewed_change_materializes_without_ddl_or_history_changes(change):
    old,new,out=change
    result=builder.build([old],new,out,allow_optional_reply_migration=True)
    capsule=load(out);target=capsule.materialize(old)
    assert (target/'sql/oceanbase/004_message_reply.sql').read_text()==MIG
    assert result['database_ddl_executed'] is False
    note=capsule.CAPSULE['optional_database_migrations']
    assert len(note)==1 and note[0]['path']=='sql/oceanbase/004_message_reply.sql'
    assert note[0]['automatic'] is False and note[0]['column']=='reply_json'
    assert note[0]['restart_required'] is True
    case=SimpleNamespace(module=capsule,old=old,root=out.parent)
    bundle,prefix,config=installed(case);ctl=FakeManager(bundle,prefix)
    report=capsule.deploy(bundle,prefix,apply=True,manager_factory=lambda *_:ctl,mount_function=lambda:{},nginx_config=config)
    assert report['optional_database_migrations']==note
    assert ctl.calls==['preflight','install']
    assert (prefix/'state/journal').read_text()=='UNKNOWN must survive'

@pytest.mark.parametrize('damage',['not-null','other-column','other-table','extra-index','extra-ddl','generator','missing-migration','wrapper'])
def test_opt_in_never_allows_unrelated_or_unsafe_schema_changes(change,damage):
    old,new,out=change;sql=new/'sql/oceanbase/001_xunjia_schema_mysql.sql'
    if damage=='not-null':sql.write_text(sql.read_text().replace('reply_json LONGTEXT,','reply_json LONGTEXT NOT NULL,'))
    elif damage=='other-column':sql.write_text(sql.read_text().replace('reply_json','unreviewed'))
    elif damage=='other-table':sql.write_text(sql.read_text().replace('xj2_message','xj2_command'))
    elif damage=='extra-index':sql.write_text(sql.read_text()+'CREATE INDEX extra ON xj2_message(historical);\n')
    elif damage=='extra-ddl':
        p=new/'sql/oceanbase/004_message_reply.sql';p.write_text(p.read_text()+'DROP TABLE xj2_command;\n')
    elif damage=='generator':
        p=new/'app/server/negotiation/schema.py';p.write_text(p.read_text()+'DANGEROUS_CHANGE=True\n')
    elif damage=='missing-migration':(new/'sql/oceanbase/004_message_reply.sql').unlink()
    else:(new/'app/server/schema.py').write_text('changed schema wrapper')
    seal(new,'target-release')
    with pytest.raises(builder.runtime.IncrementalError,match='SCHEMA_CHANGE'):
        builder.build([old],new,out,allow_optional_reply_migration=True)
    assert not out.exists()
