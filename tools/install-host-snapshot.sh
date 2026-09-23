#!/bin/bash
# Install the live network snapshot on an Ubuntu host.
# Writes /var/lib/simba-network/live.json every 15 minutes and after boot.
set -euo pipefail

if [[ "$(id -u)" -ne 0 ]]; then
  echo "Run as root: sudo bash $0" >&2
  exit 1
fi

HERE="$(cd "$(dirname "$0")" && pwd)"
install -d /usr/local/lib/simba-network /var/lib/simba-network
install -m 0755 "$HERE/host_snapshot.py" /usr/local/lib/simba-network/host_snapshot.py

cat > /etc/systemd/system/simba-network-snapshot.service <<'EOF'
[Unit]
Description=Write Simba live nginx/ufw/listener snapshot
After=network-online.target nginx.service

[Service]
Type=oneshot
Environment=SIMBA_NETWORK_LIVE=/var/lib/simba-network/live.json
ExecStart=/usr/bin/python3 /usr/local/lib/simba-network/host_snapshot.py
EOF

cat > /etc/systemd/system/simba-network-snapshot.timer <<'EOF'
[Unit]
Description=Refresh Simba live network snapshot every 15 minutes

[Timer]
OnBootSec=2min
OnUnitActiveSec=15min
Persistent=true

[Install]
WantedBy=timers.target
EOF

systemctl daemon-reload
systemctl enable --now simba-network-snapshot.timer
systemctl start simba-network-snapshot.service
echo "Snapshot timer enabled. File: /var/lib/simba-network/live.json"
