-- TraceLog offline schema. The service also creates missing tables on startup.
-- Default administrator: admin / admin
-- Anonymous account: anonymous. It has no permissions and signs in only from the login page.
-- Permissions for anonymous are changed in user management.
-- Per-project summary tables are created by the service and are not included here.

CREATE DATABASE IF NOT EXISTS TraceLog DEFAULT CHARACTER SET utf8mb4;
USE TraceLog;

CREATE TABLE IF NOT EXISTS accounts (
  id BIGINT PRIMARY KEY AUTO_INCREMENT,
  username VARCHAR(64) NOT NULL UNIQUE,
  password_hash VARCHAR(160) NOT NULL,
  created_at DATETIME NOT NULL,
  is_admin TINYINT NOT NULL DEFAULT 0,
  can_create_project TINYINT NOT NULL DEFAULT 0,
  can_use_datasource TINYINT NOT NULL DEFAULT 0,
  can_use_report TINYINT NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS sessions (
  token VARCHAR(64) PRIMARY KEY,
  username VARCHAR(64) NOT NULL,
  expires_at DATETIME NOT NULL,
  INDEX idx_expires (expires_at)
);

CREATE TABLE IF NOT EXISTS log_sources (
  id BIGINT PRIMARY KEY AUTO_INCREMENT,
  name VARCHAR(80) NOT NULL,
  base_url VARCHAR(300) NOT NULL,
  account_id VARCHAR(20) NOT NULL DEFAULT '1',
  project_id VARCHAR(20) NOT NULL DEFAULT '1',
  created_at DATETIME NOT NULL
);

CREATE TABLE IF NOT EXISTS projects (
  id BIGINT PRIMARY KEY AUTO_INCREMENT,
  name VARCHAR(80) NOT NULL,
  source_id BIGINT NOT NULL,
  namespace_name TEXT NULL,
  remark VARCHAR(200) NOT NULL DEFAULT '',
  containers TEXT NULL,
  deploy_mode VARCHAR(20) NOT NULL DEFAULT 'kubernetes',
  slow_ms INT NOT NULL DEFAULT 500,
  created_at DATETIME NOT NULL
);

CREATE TABLE IF NOT EXISTS project_sources (
  project_id BIGINT NOT NULL,
  source_id BIGINT NOT NULL,
  PRIMARY KEY (project_id, source_id)
);

CREATE TABLE IF NOT EXISTS user_projects (
  username VARCHAR(64) NOT NULL,
  project_id BIGINT NOT NULL,
  PRIMARY KEY (username, project_id)
);

CREATE TABLE IF NOT EXISTS user_data_sources (
  username VARCHAR(64) NOT NULL,
  source_id BIGINT NOT NULL,
  PRIMARY KEY (username, source_id)
);

CREATE TABLE IF NOT EXISTS search_fields (
  id BIGINT PRIMARY KEY AUTO_INCREMENT,
  name VARCHAR(40) NOT NULL,
  kind VARCHAR(20) NOT NULL,
  pattern TEXT NULL,
  enabled TINYINT NOT NULL DEFAULT 1,
  sort_order INT NOT NULL DEFAULT 0,
  created_at DATETIME NOT NULL
);

CREATE TABLE IF NOT EXISTS saved_links (
  id BIGINT PRIMARY KEY AUTO_INCREMENT,
  name VARCHAR(80) NOT NULL,
  project_id BIGINT NOT NULL,
  conditions_json TEXT NOT NULL,
  time_window VARCHAR(16) NOT NULL,
  time_from VARCHAR(40) NOT NULL DEFAULT '',
  time_to VARCHAR(40) NOT NULL DEFAULT '',
  updated_at DATETIME NOT NULL,
  last_checked_at DATETIME NULL,
  last_event_count INT NOT NULL DEFAULT 0,
  last_error_count INT NOT NULL DEFAULT 0,
  last_slowest VARCHAR(300) NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS saved_stats (
  id BIGINT PRIMARY KEY AUTO_INCREMENT,
  name VARCHAR(80) NOT NULL,
  project_id BIGINT NOT NULL,
  time_window VARCHAR(16) NOT NULL,
  time_from VARCHAR(40) NOT NULL DEFAULT '',
  time_to VARCHAR(40) NOT NULL DEFAULT '',
  container_name VARCHAR(128) NOT NULL DEFAULT '',
  realtime TINYINT NOT NULL DEFAULT 0,
  updated_at DATETIME NOT NULL
);

CREATE TABLE IF NOT EXISTS searches (
  id BIGINT PRIMARY KEY AUTO_INCREMENT,
  project_id BIGINT NOT NULL DEFAULT 0,
  user_id VARCHAR(64) NOT NULL,
  time_window VARCHAR(16) NOT NULL,
  created_at DATETIME NOT NULL,
  hit_count INT NOT NULL DEFAULT 0,
  request_count INT NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS events (
  id BIGINT PRIMARY KEY AUTO_INCREMENT,
  search_id BIGINT NOT NULL,
  event_time DATETIME(3) NOT NULL,
  container_name VARCHAR(128) NOT NULL,
  request_key VARCHAR(128) NOT NULL,
  user_id VARCHAR(64) NOT NULL,
  uri VARCHAR(512) NULL,
  http_method VARCHAR(16) NULL,
  status_code VARCHAR(16) NULL,
  duration VARCHAR(32) NULL,
  summary VARCHAR(500) NOT NULL,
  span_id VARCHAR(64) NOT NULL DEFAULT '',
  parent_span_id VARCHAR(64) NOT NULL DEFAULT '',
  message TEXT NULL,
  INDEX idx_search_time (search_id, event_time),
  INDEX idx_search_req (search_id, request_key)
);

CREATE TABLE IF NOT EXISTS data_sources (
  id BIGINT PRIMARY KEY AUTO_INCREMENT,
  name VARCHAR(80) NOT NULL,
  host VARCHAR(191) NOT NULL,
  port INT NOT NULL DEFAULT 3306,
  username VARCHAR(80) NOT NULL,
  password VARCHAR(512) NOT NULL,
  database_name VARCHAR(80) NOT NULL,
  created_at DATETIME NOT NULL
);

CREATE TABLE IF NOT EXISTS saved_reports (
  id BIGINT PRIMARY KEY AUTO_INCREMENT,
  name VARCHAR(80) NOT NULL,
  source_id BIGINT NOT NULL,
  table_name VARCHAR(80) NOT NULL DEFAULT '',
  chart_kind VARCHAR(16) NOT NULL DEFAULT 'bar',
  axis_x VARCHAR(80) NOT NULL DEFAULT '',
  axis_y VARCHAR(80) NOT NULL DEFAULT '',
  sql_text MEDIUMTEXT NOT NULL,
  created_by VARCHAR(80) NOT NULL,
  updated_at DATETIME NOT NULL,
  KEY idx_saved_reports_source (source_id)
);

CREATE TABLE IF NOT EXISTS feishu_slow_report (
  id TINYINT PRIMARY KEY,
  webhook VARCHAR(500) NOT NULL DEFAULT '',
  enabled TINYINT NOT NULL DEFAULT 0,
  send_hour TINYINT NOT NULL DEFAULT 9,
  send_minute TINYINT NOT NULL DEFAULT 0,
  last_sent_on DATE NULL,
  last_attempt DATETIME NULL
);

CREATE TABLE IF NOT EXISTS project_feishu (
  project_id BIGINT PRIMARY KEY,
  webhook VARCHAR(500) NOT NULL DEFAULT '',
  enabled TINYINT NOT NULL DEFAULT 0,
  send_hour TINYINT NOT NULL DEFAULT 9,
  send_minute TINYINT NOT NULL DEFAULT 0,
  time_window VARCHAR(16) NOT NULL DEFAULT '15m',
  time_from VARCHAR(40) NOT NULL DEFAULT '',
  time_to VARCHAR(40) NOT NULL DEFAULT '',
  container_name VARCHAR(128) NOT NULL DEFAULT '',
  realtime TINYINT NOT NULL DEFAULT 0,
  last_sent_on DATE NULL,
  last_attempt DATETIME NULL
);

CREATE TABLE IF NOT EXISTS stats_tables (
  project_id BIGINT PRIMARY KEY,
  project_name VARCHAR(80) NOT NULL,
  table_name VARCHAR(64) NOT NULL,
  namespaces TEXT NULL,
  updated_at DATETIME NOT NULL
);

INSERT INTO accounts (username, password_hash, created_at, is_admin, can_create_project, can_use_datasource, can_use_report)
SELECT 'admin', 'a11d04e7c3b84f0a9e6d25c81b7f4302$6a4e65170bcdaa3991d64a636f10f80d474b7543e70c3c7609674fa6e52eafe8', NOW(), 1, 0, 0, 0
FROM DUAL
WHERE NOT EXISTS (SELECT 1 FROM accounts WHERE username = 'admin');

INSERT INTO accounts (username, password_hash, created_at, is_admin, can_create_project, can_use_datasource, can_use_report)
SELECT 'anonymous', 'b7e21c90d4a65f1830c9ab44e2d70815$d6c177df44546ac32e7cfc86e8d00672868a156a2e13d3ee4832c48b76b025f8', NOW(), 0, 0, 0, 0
FROM DUAL
WHERE NOT EXISTS (SELECT 1 FROM accounts WHERE username = 'anonymous');

INSERT INTO search_fields (name, kind, pattern, enabled, sort_order, created_at)
SELECT '用户 ID', 'user_id', '', 1, 1, NOW()
FROM DUAL
WHERE NOT EXISTS (SELECT 1 FROM search_fields WHERE kind = 'user_id');

INSERT INTO search_fields (name, kind, pattern, enabled, sort_order, created_at)
SELECT 'Trace ID', 'trace_id', '', 1, 2, NOW()
FROM DUAL
WHERE NOT EXISTS (SELECT 1 FROM search_fields WHERE kind = 'trace_id');

INSERT INTO search_fields (name, kind, pattern, enabled, sort_order, created_at)
SELECT '日志 ID', 'log_id', '', 1, 3, NOW()
FROM DUAL
WHERE NOT EXISTS (SELECT 1 FROM search_fields WHERE kind = 'log_id');

INSERT IGNORE INTO feishu_slow_report (id, webhook, enabled, send_hour, send_minute)
VALUES (1, '', 0, 9, 0);

CREATE TABLE IF NOT EXISTS audit_logs (
  id BIGINT PRIMARY KEY AUTO_INCREMENT,
  username VARCHAR(64) NOT NULL,
  action VARCHAR(80) NOT NULL,
  method VARCHAR(8) NOT NULL,
  path VARCHAR(191) NOT NULL,
  detail VARCHAR(1000) NOT NULL DEFAULT '',
  success TINYINT NOT NULL DEFAULT 1,
  ip VARCHAR(64) NOT NULL DEFAULT '',
  created_at DATETIME NOT NULL,
  KEY idx_audit_time (created_at),
  KEY idx_audit_user (username)
);
