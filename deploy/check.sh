#!/usr/bin/env bash
# Controllo dello stato dell'installazione. Uso:  bash deploy/check.sh
# Esce con codice 1 se qualcosa di essenziale non va.
set -uo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="$DIR/.venv/bin/python"
FAIL=0

ok()   { printf '  \033[32m[ok]\033[0m   %s\n' "$*"; }
ko()   { printf '  \033[31m[ERR]\033[0m  %s\n' "$*"; FAIL=1; }
note() { printf '  \033[33m[!]\033[0m    %s\n' "$*"; }

PORT="$(grep -E '^JAS_PORT=' "$DIR/.env" 2>/dev/null | cut -d= -f2)"
PORT="${PORT:-8000}"

echo "Server"
if [[ -x "$PY" ]]; then ok "ambiente Python presente"; else ko "ambiente Python mancante (.venv): lancia deploy/install.sh"; fi
if systemctl is-enabled jetson-ai-server >/dev/null 2>&1; then ok "avvio automatico attivo"; else ko "avvio automatico non attivo"; fi
if systemctl is-active jetson-ai-server >/dev/null 2>&1; then ok "servizio in esecuzione"; else ko "servizio fermo: journalctl -u jetson-ai-server -n 50"; fi
if curl -fs "http://127.0.0.1:$PORT/api/health" >/dev/null 2>&1; then ok "risponde su http://127.0.0.1:$PORT"; else ko "non risponde sulla porta $PORT"; fi
if grep -qE '^JAS_HOST=(0\.0\.0\.0|::)' "$DIR/.env" 2>/dev/null; then note "JAS_HOST apre il server alla rete locale (senza cifratura)"; else ok "ascolta solo in locale (accesso remoto solo da Tailscale)"; fi

echo "Intelligenza artificiale"
if [[ -x "$PY" ]]; then
  if [[ -f /etc/nv_tegra_release ]]; then
    if "$PY" -c "import torch,sys; sys.exit(0 if torch.cuda.is_available() else 1)" 2>/dev/null; then ok "PyTorch vede la GPU"; else note "PyTorch non vede la GPU: YOLO sarà lento"; fi
    if "$PY" -c "import tensorrt" 2>/dev/null; then ok "TensorRT disponibile"; else note "TensorRT non disponibile in Python (sudo apt install nvidia-jetpack)"; fi
  fi
  command -v tesseract >/dev/null 2>&1 && ok "OCR (Tesseract) installato" || note "Tesseract mancante: niente OCR per le scansioni"
fi
if curl -fs http://127.0.0.1:11434/api/tags >/dev/null 2>&1; then ok "Ollama risponde"; else ko "Ollama non risponde: sudo systemctl status ollama"; fi
[[ -x "$PY" ]] && (cd "$DIR" && "$PY" -m app.cli status 2>/dev/null | sed 's/^/        /')

echo "Accesso remoto"
if command -v tailscale >/dev/null 2>&1; then
  if tailscale status >/dev/null 2>&1; then
    ok "Tailscale connesso"
    name="$(tailscale status --json 2>/dev/null | python3 -c "import json,sys; print(json.load(sys.stdin)['Self']['DNSName'].rstrip('.'))" 2>/dev/null)"
    if tailscale serve status 2>/dev/null | grep -q "$PORT"; then ok "HTTPS attivo: https://${name:-<nome-jetson>.ts.net}"; else note "HTTPS non attivo: sudo tailscale serve --bg $PORT"; fi
    if tailscale funnel status 2>/dev/null | grep -qi "funnel on"; then ko "Tailscale Funnel è attivo: il server è su internet! Spegni con: sudo tailscale funnel reset"; fi
  else
    note "Tailscale non connesso: sudo tailscale up"
  fi
else
  note "Tailscale non installato"
fi

echo
if [[ $FAIL -eq 0 ]]; then echo "Tutto ok."; else echo "Ci sono problemi (righe ERR)."; fi
exit $FAIL
