-- Run in a maintenance window. One ALTER rebuilds the table once.
-- InnoDB can do this in place, but a table this size still needs free disk and time.
-- recv_time, client_ip, subscriber_id, app_label, and log_id are already indexed.
-- start_time is the column questions filter on, and it is not indexed yet.

ALTER TABLE ntc_ssl_logs
  ADD INDEX idx_start_time (start_time),
  ADD INDEX idx_subscriber_start (subscriber_id, start_time),
  ADD INDEX idx_client_ip_start (client_ip, start_time),
  ADD INDEX idx_app_label_start (app_label, start_time),
  ADD INDEX idx_app_category_start (app_category, start_time),
  ADD INDEX idx_server_ip_start (server_ip, start_time),
  ADD INDEX idx_requested_server_start (requested_server(80), start_time),
  ALGORITHM=INPLACE, LOCK=NONE;
