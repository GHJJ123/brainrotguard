#!/bin/bash
# Deploy BrainRotGuard via Docker
# Usage: ./deploy.sh [TARGET] [REMOTE_PATH]

set -e

CYAN='\033[0;36m'
YELLOW='\033[1;33m'
GREEN='\033[0;32m'
NC='\033[0m'

TARGET="${1:?Usage: $0 <user@host> [remote_path]}"
REMOTE_PATH="${2:-/opt/brainrotguard}"

if [[ "$1" == "--help" ]] || [[ "$1" == "-h" ]]; then
    echo "Usage: $0 [TARGET] [REMOTE_PATH]"
    echo ""
    echo "Arguments:"
    echo "  TARGET       SSH target (e.g. user@myserver)"
    echo "  REMOTE_PATH  Deployment path (default: /opt/brainrotguard)"
    exit 0
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TEMP_TAR="/tmp/brainrotguard.tar"

echo -e "${CYAN}=== Deploying BrainRotGuard to $TARGET ===${NC}"

echo -e "${YELLOW}Creating archive...${NC}"
cd "$SCRIPT_DIR"
tar -cf "$TEMP_TAR" \
    --exclude='__pycache__' \
    --exclude='*.pyc' \
    --exclude='.env' \
    --exclude='*.log' \
    --exclude='deploy*.sh' \
    --exclude='config.yaml' \
    .

echo -e "${YELLOW}Uploading and deploying...${NC}"
scp "$TEMP_TAR" "${TARGET}:/tmp/brainrotguard.tar"

ssh "$TARGET" <<EOF
set -e
mkdir -p $REMOTE_PATH
cd $REMOTE_PATH
tar -xf /tmp/brainrotguard.tar
rm /tmp/brainrotguard.tar
[ ! -f config.yaml ] && cp config.example.yaml config.yaml && echo 'Created config.yaml - edit with your tokens!'
# Auto-detect host LAN IP for base_url (container can't see it)
HOST_IP=\$(hostname -I | awk '{print \$1}')
PORT=\$(grep -oP '"\K[0-9]+(?=:)' docker-compose.yml | head -1)
PORT=\${PORT:-8080}
grep -q '^BRG_BASE_URL=' .env 2>/dev/null && sed -i "s|^BRG_BASE_URL=.*|BRG_BASE_URL=http://\${HOST_IP}:\${PORT}|" .env || echo "BRG_BASE_URL=http://\${HOST_IP}:\${PORT}" >> .env
echo "Auto-detected base_url: http://\${HOST_IP}:\${PORT}"
docker compose down 2>/dev/null || true
docker compose build
docker compose up -d
docker image prune -f
docker compose ps
EOF

rm -f "$TEMP_TAR"

echo ""
echo -e "${GREEN}=== Deployment complete! ===${NC}"
echo ""
echo "Next: ssh $TARGET 'nano $REMOTE_PATH/config.yaml'"
echo "Logs: ssh $TARGET 'docker logs -f brainrotguard'"
