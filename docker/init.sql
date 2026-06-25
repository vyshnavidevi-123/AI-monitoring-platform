-- Run automatically by MySQL Docker on first start
CREATE DATABASE IF NOT EXISTS monitoring;
CREATE USER IF NOT EXISTS 'monitor'@'%' IDENTIFIED BY 'monitor123';
GRANT ALL PRIVILEGES ON monitoring.* TO 'monitor'@'%';
FLUSH PRIVILEGES;
