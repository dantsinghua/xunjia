"""Retire executable workspace projections without deleting financial facts."""
from __future__ import annotations
from .negotiation.store import encoded, new_id

def clear_workspace(tx, owner, *, clock=None):
    """Archive old execution scopes in the caller's owner transaction.

    Audit cutoffs hide retired projections, including a later selection of the
    same native account. Raw messages, quotes, receipts and UNKNOWN stay intact.
    No receiver acknowledgement or successful native cancellation is inferred.
    """
    import time
    now=(clock or time.time)()
    # Shared connection replacement also calls this from one cross-owner
    # transaction; fence concurrent owner writers before taking retention cuts.
    tx.execute('SELECT owner_id FROM xj2_owner WHERE owner_id=?'+(' FOR UPDATE' if tx.mysql else ''),(owner,)).fetchone()
    groups="SELECT group_id FROM xj2_group WHERE owner_id=?"
    threads="SELECT thread_id FROM xj2_thread WHERE owner_id=?"
    members="SELECT member_id FROM xj2_peer WHERE group_id IN ("+groups+")"
    commands="SELECT command_id FROM xj2_command WHERE thread_id IN ("+threads+")"
    counts={}
    operations=[
      ('xj2_command_member','command_id IN ('+commands+')'),
      ('xj2_scope','member_id IN ('+members+')'),
      ('xj2_quote','member_id IN ('+members+')'),
      ('xj2_decision','member_id IN ('+members+')'),
      ('xj2_reply_job','member_id IN ('+members+')'),
      ('xj2_command','thread_id IN ('+threads+')'),
      ('xj2_analysis','thread_id IN ('+threads+')'),
      ('xj2_message','thread_id IN ('+threads+')'),
      ('xj2_peer','group_id IN ('+groups+')'),
      ('xj2_group','owner_id=?'),('xj2_thread','owner_id=?'),
      ('xj2_batch','owner_id=?'),('xj2_session_reset','owner_id=?'),('xj2_audit','owner_id=?')]
    for table,where in operations:
        counts[table]=tx.execute('SELECT COUNT(*) AS n FROM '+table+' WHERE '+where,(owner,)).fetchone()['n']
    # Existing archived and control fields are the execution boundary. Audit
    # records identify projections retired by a workspace switch, separately
    # from ordinary user archives which remain visible in their workspace.
    for row in tx.execute("SELECT group_id FROM xj2_group g WHERE owner_id=? AND NOT EXISTS (SELECT 1 FROM xj2_audit a WHERE a.owner_id=g.owner_id AND a.entity_id=g.group_id AND a.kind='workspace_group_retired')",(owner,)).fetchall():
        tx.execute('INSERT INTO xj2_audit(audit_id,owner_id,kind,entity_id,created_at,data_json) VALUES(?,?,?,?,?,?)',
            (new_id(),owner,'workspace_group_retired',row['group_id'],now,encoded({'reason':'WORKSPACE_RETIRED'})))
    for row in tx.execute('SELECT thread_id,ingest_revision FROM xj2_thread WHERE owner_id=?',(owner,)).fetchall():
        tx.execute('INSERT INTO xj2_audit(audit_id,owner_id,kind,entity_id,created_at,data_json) VALUES(?,?,?,?,?,?)',
            (new_id(),owner,'workspace_thread_retired',row['thread_id'],now,encoded({'through_revision':row['ingest_revision']})))
    binding=tx.execute('SELECT version FROM xj2_binding WHERE owner_id=?',(owner,)).fetchone()
    tx.execute('INSERT INTO xj2_audit(audit_id,owner_id,kind,entity_id,created_at,data_json) VALUES(?,?,?,?,?,?)',
        (new_id(),owner,'workspace_retired',owner,now,encoded({'retained_counts':counts,'binding_version':binding['version'] if binding else 0})))
    tx.execute('UPDATE xj2_group SET archived=1,send_allowed=0,version=version+1,control_version=control_version+1 WHERE owner_id=?',(owner,))
    tx.execute('UPDATE xj2_peer SET send_allowed=0,version=version+1,control_version=control_version+1 WHERE group_id IN ('+groups+')',(owner,))
    tx.execute("UPDATE xj2_command SET state='CANCELLED',result_json=? WHERE thread_id IN ("+threads+") AND state='QUEUED'",
        (encoded({'reason':'WORKSPACE_RETIRED_BEFORE_SUBMISSION'}),owner))
    tx.execute("UPDATE xj2_command SET cancel_requested=1 WHERE thread_id IN ("+threads+") AND state IN ('PENDING','UNKNOWN')",(owner,))
    tx.execute("UPDATE xj2_analysis SET state='CANCELLED',lease_until=0 WHERE thread_id IN ("+threads+") AND state IN ('RUNNING','RETRY')",(owner,))
    tx.execute("UPDATE xj2_reply_job SET state='CANCELLED',lease_until=0 WHERE member_id IN ("+members+") AND state IN ('RUNNING','RETRY')",(owner,))
    tx.execute('UPDATE xj2_thread SET analyzed_revision=ingest_revision,pending_since=NULL WHERE owner_id=?',(owner,))
    tx.execute('UPDATE xj2_owner SET send_allowed=0,control_version=control_version+1 WHERE owner_id=?',(owner,))
    return counts
