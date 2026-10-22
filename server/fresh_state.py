"""Explicit destructive workspace reset. Native IM history is never changed."""
from __future__ import annotations
import json
from .negotiation.store import encoded

def clear_workspace(tx, owner):
    """Delete old business content in the caller's owner transaction.

    Authorization/quote/source caches outside this workspace are not touched.
    Receiver context fences are transport metadata, not migrated business tasks.
    """
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
        tx.execute('DELETE FROM '+table+' WHERE '+where,(owner,))
    tx.execute('UPDATE xj2_owner SET send_allowed=0,control_version=control_version+1 WHERE owner_id=?',(owner,))
    return counts
