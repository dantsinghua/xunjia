"""Small inquiry schema, separate from customer reference-data tables."""


def statements(mysql: bool = False) -> list[str]:
    text = "LONGTEXT" if mysql else "TEXT"
    ddl = [
        f"""CREATE TABLE IF NOT EXISTS xj_turn (
          turn_id VARCHAR(64) PRIMARY KEY, message_id VARCHAR(64) NOT NULL UNIQUE,
          owner_user_id VARCHAR(160) NOT NULL, conversation_id VARCHAR(64) NOT NULL,
          turn_ref VARCHAR(96) NOT NULL UNIQUE, tool_budget_ref VARCHAR(96) NOT NULL UNIQUE, state VARCHAR(32) NOT NULL,
          control_version BIGINT NOT NULL, memory_revision BIGINT NOT NULL,
          binding_version BIGINT NOT NULL, snapshot_json {text} NOT NULL,
          data_json {text} NOT NULL, created_at DOUBLE NOT NULL, expires_at DOUBLE NOT NULL)""",
        f"""CREATE TABLE IF NOT EXISTS xj_binding (
          owner_user_id VARCHAR(160) PRIMARY KEY, account_uid VARCHAR(80) NOT NULL UNIQUE,
          workbench_id VARCHAR(160) NOT NULL, transport VARCHAR(16) NOT NULL,
          version BIGINT NOT NULL, bot_enabled INTEGER NOT NULL, config_json {text} NOT NULL)""",
        f"""CREATE TABLE IF NOT EXISTS xj_batch (
          batch_id VARCHAR(64) PRIMARY KEY, owner_user_id VARCHAR(160) NOT NULL,
          request_id VARCHAR(160) NOT NULL, content_hash VARCHAR(64) NOT NULL,
          version BIGINT NOT NULL, status VARCHAR(32) NOT NULL, binding_version BIGINT NOT NULL,
          data_json {text} NOT NULL, created_at DOUBLE NOT NULL, approved_at DOUBLE,
          confirm_request_id VARCHAR(160), UNIQUE(owner_user_id, request_id))""",
        f"""CREATE TABLE IF NOT EXISTS xj_conversation (
          conversation_id VARCHAR(64) PRIMARY KEY, owner_user_id VARCHAR(160) NOT NULL,
          account_uid VARCHAR(80) NOT NULL, peer_id VARCHAR(160) NOT NULL,
          kind VARCHAR(16) NOT NULL, mode VARCHAR(32) NOT NULL, control_version BIGINT NOT NULL,
          human_lock INTEGER NOT NULL DEFAULT 0, receiver_fence_id VARCHAR(160), receiver_fence_version BIGINT NOT NULL DEFAULT 0,
          resume_seq BIGINT NOT NULL, social_authorized INTEGER NOT NULL,
          memory_revision BIGINT NOT NULL, memory_json {text} NOT NULL,
          ambiguity_json {text} NOT NULL, UNIQUE(account_uid,kind,peer_id))""",
        f"""CREATE TABLE IF NOT EXISTS xj_session (
          session_id VARCHAR(64) PRIMARY KEY, owner_user_id VARCHAR(160) NOT NULL,
          batch_id VARCHAR(64) NOT NULL, conversation_id VARCHAR(64) NOT NULL,
          bond_code VARCHAR(32) NOT NULL, state VARCHAR(32) NOT NULL, revision BIGINT NOT NULL,
          policy_json {text} NOT NULL, peer_quote_json {text} NOT NULL,
          our_quote_json {text} NOT NULL, pending_command_id VARCHAR(64),
          archived INTEGER NOT NULL, created_at DOUBLE NOT NULL)""",
        f"""CREATE TABLE IF NOT EXISTS xj_command (
          command_id VARCHAR(64) PRIMARY KEY, owner_user_id VARCHAR(160) NOT NULL, batch_id VARCHAR(64),
          account_uid VARCHAR(80) NOT NULL, peer_id VARCHAR(160) NOT NULL,
          conversation_id VARCHAR(64) NOT NULL, kind VARCHAR(32) NOT NULL,
          request_id VARCHAR(160) NOT NULL, content_hash VARCHAR(64) NOT NULL,
          body_json {text} NOT NULL, status VARCHAR(32) NOT NULL,
          control_version BIGINT NOT NULL, next_check_at DOUBLE NOT NULL DEFAULT 0, created_at DOUBLE NOT NULL, expires_at DOUBLE NOT NULL,
          result_json {text} NOT NULL, UNIQUE(owner_user_id,request_id))""",
        f"""CREATE TABLE IF NOT EXISTS xj_message (
          message_id VARCHAR(64) PRIMARY KEY, owner_user_id VARCHAR(160) NOT NULL,
          conversation_id VARCHAR(64) NOT NULL, account_uid VARCHAR(80) NOT NULL,
          source_event_id VARCHAR(160) NOT NULL, source_seq BIGINT NOT NULL,
          kind VARCHAR(32) NOT NULL, direction VARCHAR(16) NOT NULL, native_message_id VARCHAR(160),
          text {text} NOT NULL, occurred_at DOUBLE NOT NULL, processing VARCHAR(32) NOT NULL,
          data_json {text} NOT NULL, UNIQUE(account_uid,source_event_id))""",
        f"""CREATE TABLE IF NOT EXISTS xj_audit (
          audit_id VARCHAR(64) PRIMARY KEY, owner_user_id VARCHAR(160) NOT NULL,
          kind VARCHAR(64) NOT NULL, entity_id VARCHAR(64) NOT NULL,
          created_at DOUBLE NOT NULL, data_json {text} NOT NULL)""",
        f"""CREATE TABLE IF NOT EXISTS xj_cursor (
          cursor_id VARCHAR(160) PRIMARY KEY, revision BIGINT NOT NULL,
          data_json {text} NOT NULL)""",
    ]

    indexes = [
        ('xj_batch','idx_batch_owner','owner_user_id,status,created_at'),
        ('xj_conversation','idx_conversation_owner','owner_user_id,peer_id'),
        ('xj_session','idx_session_owner','owner_user_id,archived,bond_code'),
        ('xj_session','idx_session_conversation','conversation_id,state'),
        ('xj_command','idx_command_status','status,next_check_at,created_at'),
        ('xj_command','idx_command_conversation','conversation_id,status'),
        ('xj_command','idx_command_owner','owner_user_id,created_at'),
        ('xj_command','idx_command_batch','owner_user_id,batch_id'),
        ('xj_message','idx_message_history','owner_user_id,conversation_id,occurred_at,message_id'),
        ('xj_message','idx_message_pending','processing,occurred_at'),
        ('xj_message','idx_message_watermark','conversation_id,source_seq'),
        ('xj_message','idx_message_native','account_uid,conversation_id,native_message_id'),
        ('xj_turn','idx_turn_conversation','conversation_id,state'),
        ('xj_turn','idx_turn_expiry','state,expires_at'),
        ('xj_audit','idx_audit_owner','owner_user_id,created_at'),
    ]
    if mysql:
        output=[]
        for sql in ddl:
            table=sql.split()[5]
            keys=[f"KEY {name} ({columns})" for target,name,columns in indexes if target==table]
            if keys:sql=sql[:-1]+", "+", ".join(keys)+")"
            output.append(sql+" ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin")
        return output
    return ddl+[f"CREATE INDEX IF NOT EXISTS {name} ON {table} ({columns})" for table,name,columns in indexes]
