#!/usr/bin/env bash
# Aggiorna il server all'ultima versione. Uso:  bash deploy/update.sh
# Database, documenti, modelli e .env non vengono toccati.
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PIP="$DIR/.venv/bin/pip"
cd "$DIR"

[[ -x "$PIP" ]] || { echo "Ambiente Python mancante: lancia prima deploy/install.sh"; exit 1; }

echo "==> Scarico gli aggiornamenti"
git pull --ff-only

echo "==> Aggiorno le dipendenze"
if [[ -f /etc/nv_tegra_release ]]; then
  CONSTRAINTS="$(mktemp)"
  "$PIP" freeze | grep -iE '^(torch|torchvision)==' > "$CONSTRAINTS" || true
  "$PIP" install -r requirements.txt "numpy<2" --constraint "$CONSTRAINTS"
  rm -f "$CONSTRAINTS"
else
  "$PIP" install -r requirements.txt
fi

echo "==> Riavvio il server"
sudo systemctl restart jetson-ai-server
PORT="$(grep -E '^JAS_PORT=' "$DIR/.env" 2>/dev/null | cut -d= -f2)"
for _ in $(seq 1 60); do curl -fs "http://127.0.0.1:${PORT:-8000}/api/health" >/dev/null 2>&1 && break; sleep 1; done
bash "$DIR/deploy/check.sh"
