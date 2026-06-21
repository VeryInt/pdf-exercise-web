#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${PDF_EXERCISE_APP_DIR:-/opt/pdf-exercise-web}"
APP_USER="${PDF_EXERCISE_APP_USER:-ubuntu}"
PUBLIC_PORT="${PDF_EXERCISE_PUBLIC_PORT:-18437}"
APP_PORT="${PDF_EXERCISE_APP_PORT:-8719}"
DOMAIN="${PDF_EXERCISE_DOMAIN:-}"
PUBLIC_BASE_URL="${PDF_EXERCISE_PUBLIC_BASE_URL:-http://127.0.0.1:${PUBLIC_PORT}}"
ORIGIN_CERT_FILE="${PDF_EXERCISE_ORIGIN_CERT_FILE:-}"
ORIGIN_KEY_FILE="${PDF_EXERCISE_ORIGIN_KEY_FILE:-}"
NGINX_CERT_DIR="${PDF_EXERCISE_NGINX_CERT_DIR:-/etc/nginx/cloudflare-origin}"

echo "[1/8] Installing system packages"
sudo apt-get update
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y \
  python3-venv python3-pip nginx sqlite3 \
  libgl1 libglib2.0-0 \
  fonts-noto-cjk \
  texlive-xetex texlive-lang-chinese

echo "[2/8] Creating directories"
sudo mkdir -p "$APP_DIR" "$APP_DIR/data" "$APP_DIR/var"
sudo chown -R "$APP_USER:$APP_USER" "$APP_DIR"

echo "[3/8] Creating Python venv"
cd "$APP_DIR"
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip wheel
.venv/bin/pip install -r requirements.txt

echo "[4/8] Writing .env"
if [ ! -f "$APP_DIR/.env" ]; then
  cp "$APP_DIR/.env.example" "$APP_DIR/.env"
fi
python3 - <<PY
from pathlib import Path
p = Path("$APP_DIR/.env")
text = p.read_text()
updates = {
    "APP_PORT": "$APP_PORT",
    "PUBLIC_BASE_URL": "$PUBLIC_BASE_URL",
    "MAX_UPLOAD_MB": "10",
    "MAX_ACTIVE_JOBS": "2",
    "MAX_ACTIVE_JOBS_PER_IP": "1",
    "MAX_JOBS_PER_IP_PER_HOUR": "5",
}
lines = text.splitlines()
existing = {line.split("=", 1)[0] for line in lines if "=" in line and not line.startswith("#")}
new_lines = []
for line in lines:
    if "=" in line and not line.startswith("#"):
        key = line.split("=", 1)[0]
        if key in updates:
            new_lines.append(f"{key}={updates[key]}")
            continue
    new_lines.append(line)
for key, value in updates.items():
    if key not in existing:
        new_lines.append(f"{key}={value}")
p.write_text("\\n".join(new_lines).rstrip() + "\\n")
PY

echo "[5/8] Initializing database"
.venv/bin/python - <<'PY'
from app.db import init_db
init_db()
print("database initialized")
PY

echo "[6/8] Installing systemd units"
sudo cp deploy/pdf-exercise-api.service /etc/systemd/system/pdf-exercise-api.service
sudo cp deploy/pdf-exercise-worker.service /etc/systemd/system/pdf-exercise-worker.service
sudo cp deploy/pdf-exercise-cleanup.service /etc/systemd/system/pdf-exercise-cleanup.service
sudo cp deploy/pdf-exercise-cleanup.timer /etc/systemd/system/pdf-exercise-cleanup.timer
sudo systemctl daemon-reload
sudo systemctl enable pdf-exercise-api.service pdf-exercise-worker.service pdf-exercise-cleanup.timer

echo "[7/8] Installing nginx config"
sudo mkdir -p "$NGINX_CERT_DIR"
SSL_ENABLED=0
CERT_TARGET=""
KEY_TARGET=""
if [ -n "$DOMAIN" ] && [ -n "$ORIGIN_CERT_FILE" ] && [ -n "$ORIGIN_KEY_FILE" ]; then
  if [ ! -f "$ORIGIN_CERT_FILE" ] || [ ! -f "$ORIGIN_KEY_FILE" ]; then
    echo "ERROR: Cloudflare Origin certificate or key file was not found." >&2
    exit 1
  fi
  CERT_TARGET="$NGINX_CERT_DIR/$DOMAIN.pem"
  KEY_TARGET="$NGINX_CERT_DIR/$DOMAIN.key"
  sudo cp "$ORIGIN_CERT_FILE" "$CERT_TARGET"
  sudo cp "$ORIGIN_KEY_FILE" "$KEY_TARGET"
  sudo chmod 0644 "$CERT_TARGET"
  sudo chmod 0600 "$KEY_TARGET"
  SSL_ENABLED=1
fi

python3 - <<PY
from pathlib import Path

public_port = "$PUBLIC_PORT"
app_port = "$APP_PORT"
domain = "$DOMAIN"
ssl_enabled = "$SSL_ENABLED" == "1"
cert_target = "$CERT_TARGET"
key_target = "$KEY_TARGET"

blocks = [
f"""server {{
    listen {public_port};
    server_name _;

    client_max_body_size 10M;

    location / {{
        proxy_pass http://127.0.0.1:{app_port};
        proxy_http_version 1.1;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_read_timeout 300s;
        proxy_send_timeout 300s;
    }}
}}
"""
]

if domain and ssl_enabled:
    blocks.append(f"""server {{
    listen 80;
    server_name {domain};
    return 301 https://\$host\$request_uri;
}}

server {{
    listen 443 ssl http2;
    server_name {domain};

    ssl_certificate {cert_target};
    ssl_certificate_key {key_target};
    ssl_protocols TLSv1.2 TLSv1.3;

    client_max_body_size 10M;

    location / {{
        proxy_pass http://127.0.0.1:{app_port};
        proxy_http_version 1.1;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto https;
        proxy_read_timeout 300s;
        proxy_send_timeout 300s;
    }}
}}
""")

Path("/tmp/pdf-exercise-web.nginx").write_text("\\n".join(blocks), encoding="utf-8")
PY
sudo cp /tmp/pdf-exercise-web.nginx /etc/nginx/sites-available/pdf-exercise-web
sudo rm -f /etc/nginx/sites-enabled/default
sudo ln -sf /etc/nginx/sites-available/pdf-exercise-web /etc/nginx/sites-enabled/pdf-exercise-web
sudo nginx -t

echo "[8/8] Starting services"
sudo systemctl restart pdf-exercise-api.service
sudo systemctl restart pdf-exercise-worker.service
sudo systemctl restart nginx
sudo systemctl start pdf-exercise-cleanup.timer

echo "Done. Health URL: $PUBLIC_BASE_URL/health"
