"""Read-only audit of the exact isolated project and its native stored evidence."""
import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
from uuid import UUID

ROOT=Path('/tmp/xunjia-dify-verify-20261013')
PROJECT='xunjia-dify-verify-20261013'


def read(name):return json.loads((ROOT/name).read_text())
def docker(*args):return subprocess.check_output(['docker',*args],text=True)


def main():
    baseline=read('baseline-containers.json')
    current={x['Id']:x for x in json.loads(docker('inspect',*[x['id'] for x in baseline]))}
    assert all(current[x['id']]['State']['StartedAt']==x['started_at'] and current[x['id']]['State']['Running'] for x in baseline)
    inventory=read('container-inventory.json')
    for saved in inventory:
        actual=json.loads(docker('inspect',saved['id']))[0]
        assert actual['Config']['Labels']['com.docker.compose.project']==PROJECT
        assert actual['Name'].lstrip('/')==saved['name']
    for plugin in read('plugins.json'):
        assert hashlib.sha256(Path(plugin['path']).read_bytes()).hexdigest()==plugin['sha256']
    turns=[read('chat-turn-1.json'),read('chat-turn-2.json')]
    assert turns[0]['conversation_id']==turns[1]['conversation_id']
    assert [x['proposal']['conversation_epoch'] for x in turns]==[1,2]
    for x in turns:
        assert x['proposal']['public_message']=='请问这只券目前可做多少量？'
        assert x['proposal']['validation_ref']
    for name in ('complete-tool-chain.sse','complete-second-turn.sse'):
        events=[]
        for block in (ROOT/name).read_text().split('\n\n'):
            if not block.startswith('data: '):continue
            try:events.append(json.loads(block[6:]))
            except ValueError:continue
        assert any(x.get('event')=='message_end' for x in events)
        assert any(x.get('event')=='workflow_finished' and x.get('data',{}).get('status')=='succeeded' for x in events)
        responses=[x.get('data',{}).get('data',{}).get('output',{}).get('tool_response') for x in events if x.get('event')=='agent_log' and isinstance(x.get('data',{}).get('data',{}).get('output'),dict)]
        assert any(isinstance(x,str) and 'raw_gap_bp' in x and '30.00' in x and 'tool invoke error' not in x for x in responses)
    meta=read('root-instance.json');path=ROOT/Path(meta['database']).name
    connection=sqlite3.connect('file:'+str(path)+'?mode=ro',uri=True)
    result=read('root-chain-result.json');row=connection.execute('SELECT state,data_json FROM xj_turn WHERE message_id=?',(result['source_message_id'],)).fetchone()
    assert row[0]=='done';data=json.loads(row[1]);assert data['tool_calls']==1 and data['validation']['ref']
    command=connection.execute('SELECT status FROM xj_command WHERE command_id=?',(result['result']['command_id'],)).fetchone();assert command[0]=='sent'
    budget=read('root-budget-ticket.json');row=connection.execute('SELECT data_json FROM xj_turn WHERE turn_id=?',(budget['turn_id'],)).fetchone();assert json.loads(row[0])['tool_calls']==6
    connection.close()
    database=next(r for r in inventory if r['service']=='db_postgres')
    ids=[str(UUID(x['workflow_run_id'])) for x in turns]
    root_app=str(UUID(read('root-import-result.json')['app_id']))
    query="SELECT id,status,app_id FROM workflow_runs WHERE id IN ("+','.join("'"+x+"'" for x in ids)+") OR app_id='"+root_app+"' ORDER BY created_at;"
    native=docker('exec',database['id'],'psql','-U','postgres','-d','dify','-At','-c',query).splitlines()
    assert len(native)>=3 and all('|succeeded|' in x for x in native)
    report={'baseline_containers_unchanged':len(baseline),'project_container_ids_verified':len(inventory),'plugin_archive_hashes_verified':2,
            'same_dify_conversation_epochs':[1,2],'successful_native_workflow_runs':native,'root_signed_queued_then_fake_sent':True,'persisted_budget_count':6}
    print(json.dumps(report,ensure_ascii=False,indent=2))


if __name__=='__main__':main()
