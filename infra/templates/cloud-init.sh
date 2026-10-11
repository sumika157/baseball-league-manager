#!/bin/bash
# Lightsail の起動スクリプト（root で1回だけ動く）。秘密は入れない（コンソールから見える）。
# .env と cron は Terraform が SSH で書き込む（bootstrap.tf）。
set -euxo pipefail
exec > >(tee -a /var/log/baseball-init.log) 2>&1

export DEBIAN_FRONTEND=noninteractive
APP_DIR=/home/ubuntu/baseball-league-manager

# タイムゾーン（既定の UTC だと cron の時刻が9時間ずれる）。cron は起動時のタイムゾーンで動き続けるので再起動する
timedatectl set-timezone Asia/Tokyo
systemctl restart cron

# スワップ 2GB（1GB の VM で Docker のビルドが通りやすくなる）
if [ ! -f /swapfile ]; then
  fallocate -l 2G /swapfile
  chmod 600 /swapfile
  mkswap /swapfile
  swapon /swapfile
  echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi

apt-get update
apt-get install -y curl git unzip ca-certificates

# Docker
curl -fsSL https://get.docker.com | sh
usermod -aG docker ubuntu

# AWS CLI v2（R2 への転送に使う。Ubuntu 24.04 の apt には awscli が無い）
ARCH="$(uname -m)"
curl -fsSL "https://awscli.amazonaws.com/awscli-exe-linux-${ARCH}.zip" -o /tmp/awscliv2.zip
unzip -q /tmp/awscliv2.zip -d /tmp
/tmp/aws/install
rm -rf /tmp/aws /tmp/awscliv2.zip

# 作業ディレクトリと backups/（コンテナの利用者 uid 10001 が書けるようにする）
mkdir -p "${APP_DIR}/backups"
chown ubuntu:ubuntu "${APP_DIR}"
chown 10001 "${APP_DIR}/backups"

# 目印（Terraform はこれが現れるまで待つ）
touch /var/lib/baseball-init-done
