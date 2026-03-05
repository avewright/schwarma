#!/bin/bash
set -euxo pipefail

# ── Install Docker ───────────────────────────────────────────────────
dnf update -y
dnf install -y docker git
systemctl enable docker
systemctl start docker
usermod -aG docker ec2-user

# ── Install Docker Compose v2 ────────────────────────────────────────
DOCKER_CONFIG=/usr/local/lib/docker/cli-plugins
mkdir -p $DOCKER_CONFIG
curl -SL "https://github.com/docker/compose/releases/latest/download/docker-compose-linux-x86_64" \
  -o $DOCKER_CONFIG/docker-compose
chmod +x $DOCKER_CONFIG/docker-compose
ln -sf $DOCKER_CONFIG/docker-compose /usr/local/bin/docker-compose

# ── Clone the repo ───────────────────────────────────────────────────
cd /home/ec2-user
git clone https://github.com/avewright/schwarma.git
chown -R ec2-user:ec2-user schwarma

echo "=== Schwarma EC2 setup complete ==="
