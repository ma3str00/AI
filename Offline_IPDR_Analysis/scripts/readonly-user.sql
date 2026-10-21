-- Read-only account. Change the password, then put the same password in config.yaml.
-- Grant the app host. localhost covers a socket on this VM. Add the app server's
-- address here when MySQL moves to another machine.

CREATE USER IF NOT EXISTS 'ipdr_ro'@'localhost' IDENTIFIED BY 'change-me';
CREATE USER IF NOT EXISTS 'ipdr_ro'@'127.0.0.1' IDENTIFIED BY 'change-me';
GRANT SELECT ON ipdr.ntc_ssl_logs TO 'ipdr_ro'@'localhost';
GRANT SELECT ON ipdr.ntc_ssl_logs TO 'ipdr_ro'@'127.0.0.1';
FLUSH PRIVILEGES;
