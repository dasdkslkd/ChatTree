from __future__ import annotations


CURRENT_SCHEMA_VERSION = 2


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS server_metadata (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL,
  created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS blobs (
  id TEXT PRIMARY KEY,
  path TEXT NOT NULL,
  mime_type TEXT NOT NULL DEFAULT 'text/plain; charset=utf-8',
  compression TEXT NOT NULL DEFAULT 'zstd',
  byte_size INTEGER NOT NULL,
  stored_size INTEGER NOT NULL,
  char_count INTEGER,
  ref_count INTEGER NOT NULL DEFAULT 0,
  created_at INTEGER NOT NULL,
  last_accessed_at INTEGER
);

CREATE TABLE IF NOT EXISTS conversations (
  id TEXT PRIMARY KEY,
  title TEXT NOT NULL,
  root_node_id TEXT,
  current_node_id TEXT,
  project_id TEXT,
  provider_id TEXT,
  model_id TEXT,
  reasoning_effort TEXT,
  thinking_enabled INTEGER,
  multi_agent_mode TEXT NOT NULL DEFAULT 'explicit_request_only',
  workspace_json TEXT,
  settings_json TEXT,
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS nodes (
  id TEXT PRIMARY KEY,
  conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
  parent_id TEXT REFERENCES nodes(id) ON DELETE CASCADE,
  child_order INTEGER NOT NULL DEFAULT 0,
  depth INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL DEFAULT 'complete',
  model_id TEXT,
  provider_id TEXT,
  tool_permission_mode TEXT,
  task_context_mode TEXT NOT NULL DEFAULT 'attached'
    CHECK (task_context_mode IN ('attached', 'detached')),
  turn_usage_json TEXT,
  branch_usage_json TEXT,
  active_context_usage_json TEXT,
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL,
  UNIQUE(conversation_id, id),
  FOREIGN KEY (conversation_id, parent_id) REFERENCES nodes(conversation_id, id)
);

CREATE INDEX IF NOT EXISTS idx_nodes_conversation_parent
  ON nodes(conversation_id, parent_id, child_order);
CREATE UNIQUE INDEX IF NOT EXISTS idx_nodes_single_root_per_conversation
  ON nodes(conversation_id)
  WHERE parent_id IS NULL;
CREATE INDEX IF NOT EXISTS idx_nodes_conversation_depth
  ON nodes(conversation_id, depth);
CREATE INDEX IF NOT EXISTS idx_nodes_conversation_updated
  ON nodes(conversation_id, updated_at);

CREATE TABLE IF NOT EXISTS messages (
  id TEXT PRIMARY KEY,
  conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
  node_id TEXT REFERENCES nodes(id) ON DELETE CASCADE,
  role TEXT NOT NULL,
  subtype TEXT,
  name TEXT,
  content_inline TEXT,
  content_blob_id TEXT REFERENCES blobs(id),
  preview TEXT NOT NULL DEFAULT '',
  hidden INTEGER NOT NULL DEFAULT 0,
  transcript_only INTEGER NOT NULL DEFAULT 0,
  metadata_json TEXT,
  usage_json TEXT,
  created_at INTEGER NOT NULL,
  UNIQUE(conversation_id, id),
  FOREIGN KEY (conversation_id, node_id) REFERENCES nodes(conversation_id, id)
);

CREATE INDEX IF NOT EXISTS idx_messages_node_role
  ON messages(node_id, role, created_at);
CREATE INDEX IF NOT EXISTS idx_messages_conversation_created
  ON messages(conversation_id, created_at);
CREATE INDEX IF NOT EXISTS idx_messages_blob
  ON messages(content_blob_id);

CREATE TABLE IF NOT EXISTS tool_calls (
  id TEXT NOT NULL,
  conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
  node_id TEXT REFERENCES nodes(id) ON DELETE CASCADE,
  run_id TEXT REFERENCES runs(id) ON DELETE SET NULL,
  assistant_message_id TEXT REFERENCES messages(id) ON DELETE SET NULL,
  call_index INTEGER NOT NULL,
  name TEXT NOT NULL,
  args_inline TEXT,
  args_blob_id TEXT REFERENCES blobs(id),
  args_preview TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'running',
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL,
  PRIMARY KEY (conversation_id, id),
  FOREIGN KEY (conversation_id, node_id) REFERENCES nodes(conversation_id, id),
  FOREIGN KEY (conversation_id, run_id) REFERENCES runs(conversation_id, id),
  FOREIGN KEY (conversation_id, assistant_message_id)
    REFERENCES messages(conversation_id, id)
);

CREATE TABLE IF NOT EXISTS tool_results (
  id TEXT PRIMARY KEY,
  conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
  node_id TEXT REFERENCES nodes(id) ON DELETE CASCADE,
  run_id TEXT REFERENCES runs(id) ON DELETE SET NULL,
  tool_call_id TEXT,
  status TEXT NOT NULL,
  output_preview TEXT NOT NULL DEFAULT '',
  output_blob_id TEXT REFERENCES blobs(id),
  output_size INTEGER NOT NULL DEFAULT 0,
  truncated INTEGER NOT NULL DEFAULT 0,
  metadata_json TEXT,
  created_at INTEGER NOT NULL,
  UNIQUE(conversation_id, id),
  UNIQUE(conversation_id, tool_call_id),
  FOREIGN KEY (conversation_id, node_id) REFERENCES nodes(conversation_id, id),
  FOREIGN KEY (conversation_id, run_id) REFERENCES runs(conversation_id, id),
  FOREIGN KEY (conversation_id, tool_call_id)
    REFERENCES tool_calls(conversation_id, id)
    ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_tool_calls_node
  ON tool_calls(node_id, call_index);
CREATE INDEX IF NOT EXISTS idx_tool_results_call
  ON tool_results(conversation_id, tool_call_id);
CREATE INDEX IF NOT EXISTS idx_tool_results_blob
  ON tool_results(output_blob_id);

CREATE TABLE IF NOT EXISTS runs (
  id TEXT PRIMARY KEY,
  conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
  kind TEXT NOT NULL,
  status TEXT NOT NULL,
  created_by_run_id TEXT REFERENCES runs(id) ON DELETE SET NULL,
  cancellation_parent_run_id TEXT REFERENCES runs(id) ON DELETE SET NULL,
  anchor_node_id TEXT REFERENCES nodes(id) ON DELETE SET NULL,
  target_node_id TEXT REFERENCES nodes(id) ON DELETE SET NULL,
  summary TEXT NOT NULL DEFAULT '',
  metadata_json TEXT,
  event_count INTEGER NOT NULL DEFAULT 0,
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL,
  finished_at INTEGER,
  idempotency_key TEXT,
  request_fingerprint TEXT,
  UNIQUE(conversation_id, id),
  FOREIGN KEY (conversation_id, created_by_run_id)
    REFERENCES runs(conversation_id, id),
  FOREIGN KEY (conversation_id, cancellation_parent_run_id)
    REFERENCES runs(conversation_id, id),
  FOREIGN KEY (conversation_id, anchor_node_id)
    REFERENCES nodes(conversation_id, id),
  FOREIGN KEY (conversation_id, target_node_id)
    REFERENCES nodes(conversation_id, id),
  CHECK (
    (idempotency_key IS NULL AND request_fingerprint IS NULL)
    OR
    (idempotency_key IS NOT NULL AND request_fingerprint IS NOT NULL)
  )
);

CREATE TABLE IF NOT EXISTS run_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
  conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
  event_index INTEGER NOT NULL,
  event_type TEXT NOT NULL,
  payload_inline TEXT,
  payload_blob_id TEXT REFERENCES blobs(id),
  created_at INTEGER NOT NULL,
  UNIQUE(run_id, event_index),
  FOREIGN KEY (conversation_id, run_id) REFERENCES runs(conversation_id, id)
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_runs_idempotency_key
  ON runs(idempotency_key)
  WHERE idempotency_key IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_runs_conversation_status
  ON runs(conversation_id, status, updated_at);
CREATE INDEX IF NOT EXISTS idx_runs_target_node
  ON runs(target_node_id);
CREATE INDEX IF NOT EXISTS idx_runs_anchor_node
  ON runs(anchor_node_id);
CREATE INDEX IF NOT EXISTS idx_runs_created_by
  ON runs(created_by_run_id);
CREATE INDEX IF NOT EXISTS idx_runs_cancellation_parent
  ON runs(cancellation_parent_run_id);
CREATE INDEX IF NOT EXISTS idx_run_events_run_index
  ON run_events(run_id, event_index);

CREATE TABLE IF NOT EXISTS plans (
  id TEXT PRIMARY KEY,
  conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
  status TEXT NOT NULL,
  entered_node_id TEXT REFERENCES nodes(id) ON DELETE SET NULL,
  entered_run_id TEXT REFERENCES runs(id) ON DELETE SET NULL,
  approved_run_id TEXT REFERENCES runs(id) ON DELETE SET NULL,
  question_tool_call_id TEXT,
  exit_tool_call_id TEXT,
  blocking_node_id TEXT REFERENCES nodes(id) ON DELETE SET NULL,
  blocking_run_id TEXT REFERENCES runs(id) ON DELETE SET NULL,
  previous_permission_mode TEXT NOT NULL DEFAULT 'modify_only',
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL,
  approved_at INTEGER,
  rejected_at INTEGER,
  UNIQUE(conversation_id, id),
  FOREIGN KEY (conversation_id, entered_node_id)
    REFERENCES nodes(conversation_id, id),
  FOREIGN KEY (conversation_id, entered_run_id)
    REFERENCES runs(conversation_id, id),
  FOREIGN KEY (conversation_id, approved_run_id)
    REFERENCES runs(conversation_id, id),
  FOREIGN KEY (conversation_id, blocking_node_id)
    REFERENCES nodes(conversation_id, id),
  FOREIGN KEY (conversation_id, blocking_run_id)
    REFERENCES runs(conversation_id, id)
);

CREATE INDEX IF NOT EXISTS idx_plans_conversation_status
  ON plans(conversation_id, status, updated_at);

CREATE TABLE IF NOT EXISTS active_tasks (
  conversation_id TEXT PRIMARY KEY REFERENCES conversations(id) ON DELETE CASCADE,
  generation_id TEXT NOT NULL UNIQUE,
  revision INTEGER NOT NULL DEFAULT 0,
  title TEXT NOT NULL,
  detail_inline TEXT,
  detail_blob_id TEXT REFERENCES blobs(id),
  created_by_run_id TEXT REFERENCES runs(id) ON DELETE SET NULL,
  created_by_tool_call_id TEXT,
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL,
  UNIQUE(conversation_id, generation_id),
  FOREIGN KEY (conversation_id, created_by_run_id)
    REFERENCES runs(conversation_id, id)
);

CREATE TABLE IF NOT EXISTS active_task_steps (
  conversation_id TEXT NOT NULL REFERENCES active_tasks(conversation_id) ON DELETE CASCADE,
  position INTEGER NOT NULL CHECK (position > 0),
  title TEXT NOT NULL,
  detail_inline TEXT,
  detail_blob_id TEXT REFERENCES blobs(id),
  status TEXT NOT NULL CHECK (status IN ('pending', 'blocked', 'completed')),
  evidence_run_id TEXT REFERENCES runs(id) ON DELETE SET NULL,
  evidence_summary TEXT NOT NULL DEFAULT '',
  updated_at INTEGER NOT NULL,
  PRIMARY KEY (conversation_id, position),
  FOREIGN KEY (conversation_id, evidence_run_id)
    REFERENCES runs(conversation_id, id)
);

CREATE TABLE IF NOT EXISTS task_run_bindings (
  run_id TEXT PRIMARY KEY REFERENCES runs(id) ON DELETE CASCADE,
  conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
  task_generation_id TEXT NOT NULL,
  step_position INTEGER NOT NULL CHECK (step_position > 0),
  base_revision INTEGER NOT NULL,
  created_at INTEGER NOT NULL,
  FOREIGN KEY (conversation_id, run_id) REFERENCES runs(conversation_id, id),
  FOREIGN KEY (conversation_id, task_generation_id)
    REFERENCES active_tasks(conversation_id, generation_id) ON DELETE CASCADE
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_task_run_bindings_conversation
  ON task_run_bindings(conversation_id);

CREATE TABLE IF NOT EXISTS task_notifications (
  id TEXT PRIMARY KEY,
  conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
  source_run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
  source_run_kind TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL CHECK (
    status IN (
      'unbound',
      'bound',
      'delivering',
      'delivered',
      'delivery_failed',
      'delivery_cancelled'
    )
  ),
  summary TEXT NOT NULL DEFAULT '',
  content TEXT NOT NULL DEFAULT '',
  delivery_node_id TEXT REFERENCES nodes(id) ON DELETE SET NULL,
  delivery_run_id TEXT REFERENCES runs(id) ON DELETE SET NULL,
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL,
  UNIQUE(conversation_id, id),
  FOREIGN KEY (conversation_id, source_run_id)
    REFERENCES runs(conversation_id, id),
  FOREIGN KEY (conversation_id, delivery_node_id)
    REFERENCES nodes(conversation_id, id),
  FOREIGN KEY (conversation_id, delivery_run_id)
    REFERENCES runs(conversation_id, id)
);

CREATE INDEX IF NOT EXISTS idx_task_notifications_conversation_status
  ON task_notifications(conversation_id, status, updated_at);
CREATE INDEX IF NOT EXISTS idx_task_notifications_delivery_node
  ON task_notifications(delivery_node_id, updated_at);

"""
