-- Application login / privilege store (MySQL).
-- Run once against the auth database configured in config.yaml → auth_mysql.database.
--
-- Example:
--   CREATE DATABASE IF NOT EXISTS ipdr_auth CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
--   mysql -u root -p ipdr_auth < scripts/create_auth_users.sql
--
-- The app connects with auth_mysql.user / password and expects SELECT/INSERT/UPDATE
-- (and optionally DELETE) on this table. Bootstrap the admin row from config on startup.

CREATE TABLE IF NOT EXISTS ipdr_users (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  username VARCHAR(64) NOT NULL,
  password_hash VARCHAR(255) NOT NULL,
  is_admin TINYINT(1) NOT NULL DEFAULT 0,
  can_ai_assistant TINYINT(1) NOT NULL DEFAULT 0,
  can_query_builder TINYINT(1) NOT NULL DEFAULT 0,
  is_active TINYINT(1) NOT NULL DEFAULT 1,
  created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  UNIQUE KEY uq_ipdr_users_username (username)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
