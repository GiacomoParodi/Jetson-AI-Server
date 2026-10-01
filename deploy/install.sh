#!/usr/bin/env bash
# Installazione del Jetson AI Server su NVIDIA Jetson (JetPack 6, Ubuntu 22.04).
#
# Uso (dalla cartella del progetto, con l'utente normale, NON con sudo):
#   bash deploy/install.sh
#
# Lo script si può rilanciare: salta i passaggi già fatti.
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
USER_NAME="$(id -un)"
VENV="$DIR/.venv"
PY="$VENV/bin/python"
PIP="$VENV/bin/pip"

# Ruote PyTorch per JetPack 6 (Python 3.10). Se cambiano, aggiornale seguendo
# https://docs.ultralytics.com/guides/nvidia-jetson/ (sezione "Install PyTorch and Torchvision").
TORCH_WHEEL="https://github.com/ultralytics/assets/releases/download/v0.0.0/torch-2.5.0a0+872d972e41.nv24.08-cp310-cp310-linux_aarch64.whl"
TORCHVISION_WHEEL="https://github.com/ultralytics/assets/releases/download/v0.0.0/torchvision-0.20.0a0+afc54f7-cp310-cp310-linux_aarch64.whl"

LLM_MODEL="$(grep -E '^JAS_LLM_MODEL=' "$DIR/.env" 2>/dev/null | cut -d= -f2 || true)"
LLM_MODEL="${LLM_MODEL:-qwen2.5:3b}"
EMBED_MODEL="$(grep -E '^JAS_EMBED_MODEL=' "$DIR/.env" 2>/dev/null | cut -d= -f2 || true)"
EMBED_MODEL="${EMBED_MODEL:-nomic-embed-text}"

step() { printf '\n\033[1;32m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m!! %s\033[0m\n' "$*"; }

if [[ $EUID -eq 0 ]]; then
  echo "Lancia lo script con il tuo utente normale (userà sudo quando serve)."
  exit 1
fi

IS_JETSON=0
if [[ -f /etc/nv_tegra_release ]]; then
  IS_JETSON=1
  echo "Jetson rilevato: $(head -n1 /etc/nv_tegra_release)"
  grep -q "R36" /etc/nv_tegra_release || warn "Questo script è pensato per JetPack 6 (L4T R36): potrebbero servire ruote PyTorch diverse."
else
  warn "Non sembra un Jetson: installo la versione standard (CPU o GPU desktop)."
fi

step "Pacchetti di sistema"
sudo apt-get update
sudo apt-get install -y python3-venv python3-pip python3-dev curl ffmpeg \
  tesseract-ocr tesseract-ocr-ita tesseract-ocr-eng libopenblas-dev
if [[ $IS_JETSON -eq 1 ]]; then
  # cuSPARSELt serve al PyTorch per JetPack 6.
  if ! dpkg -s libcusparselt0 >/dev/null 2>&1; then
    tmp="$(mktemp -d)"
    curl -fsSL -o "$tmp/cuda-keyring.deb" https://developer.download.nvidia.com/compute/cuda/repos/ubuntu2204/arm64/cuda-keyring_1.1-1_all.deb
    sudo dpkg -i "$tmp/cuda-keyring.deb"
    sudo apt-get update
    sudo apt-get install -y libcusparselt0 libcusparselt-dev
    rm -rf "$tmp"
  fi
fi

step "Ambiente Python"
if [[ ! -x "$PY" ]]; then
  # --system-site-packages: così si vedono i binding TensorRT installati da JetPack.
  python3 -m venv --system-site-packages "$VENV"
fi
"$PIP" install --upgrade pip wheel

if [[ $IS_JETSON -eq 1 ]]; then
  if ! "$PY" -c "import torch, sys; sys.exit(0 if torch.cuda.is_available() else 1)" 2>/dev/null; then
    step "PyTorch per Jetson (con GPU)"
    "$PIP" install "numpy<2" "$TORCH_WHEEL" "$TORCHVISION_WHEEL"
  fi
  "$PIP" install "numpy<2" onnx onnxslim
fi

step "Dipendenze del server"
if [[ $IS_JETSON -eq 1 ]]; then
  # Evita che pip sostituisca il PyTorch per Jetson con quello generico.
  "$PIP" install -r "$DIR/requirements.txt" "numpy<2" \
    --constraint <("$PIP" freeze | grep -iE '^(torch|torchvision)==' || true)
else
  "$PIP" install -r "$DIR/requirements.txt"
fi

if [[ $IS_JETSON -eq 1 ]]; then
  if "$PY" -c "import torch, sys; sys.exit(0 if torch.cuda.is_available() else 1)"; then
    echo "PyTorch vede la GPU: ok"
  else
    warn "PyTorch NON vede la GPU: YOLO funzionerà ma lentamente (solo CPU)."
    warn "Controlla le ruote PyTorch per la tua versione di JetPack: https://docs.ultralytics.com/guides/nvidia-jetson/"
  fi
  "$PY" -c "import tensorrt" 2>/dev/null && echo "TensorRT disponibile: ok" \
    || warn "TensorRT non trovato in Python (sudo apt install nvidia-jetpack): niente ottimizzazione TensorRT."
fi

step "Configurazione"
[[ -f "$DIR/.env" ]] || cp "$DIR/.env.example" "$DIR/.env"
mkdir -p "$DIR/data/models/yolo" "$DIR/plugins"

step "Modello YOLO predefinito"
(cd "$DIR/data/models" && "$PY" -c "from ultralytics import YOLO; YOLO('yolo11n.pt')") \
  || warn "Download di yolo11n.pt non riuscito: verrà riprovato al primo utilizzo."

step "Ollama (LLM locale)"
if ! command -v ollama >/dev/null 2>&1; then
  curl -fsSL https://ollama.com/install.sh | sh
fi
sudo systemctl enable --now ollama || true
for i in $(seq 1 30); do curl -fs http://127.0.0.1:11434/api/tags >/dev/null && break; sleep 1; done
ollama pull "$LLM_MODEL"
ollama pull "$EMBED_MODEL"

step "Primo utente amministratore"
if [[ "$("$PY" -m app.cli count-users)" == "0" ]]; then
  read -rp "Nome dell'amministratore: " ADMIN
  "$PY" -m app.cli create-user "$ADMIN" --admin
else
  echo "Utenti già presenti: salto."
fi

step "Avvio automatico all'accensione (systemd)"
sed -e "s|__USER__|$USER_NAME|g" -e "s|__DIR__|$DIR|g" "$DIR/deploy/jetson-ai-server.service" \
  | sudo tee /etc/systemd/system/jetson-ai-server.service >/dev/null
sudo systemctl daemon-reload
sudo systemctl enable jetson-ai-server
sudo systemctl restart jetson-ai-server

step "Accesso remoto con Tailscale"
if ! command -v tailscale >/dev/null 2>&1; then
  curl -fsSL https://tailscale.com/install.sh | sh
fi
if ! tailscale status >/dev/null 2>&1; then
  echo "Si aprirà un link: aprilo nel browser e accedi al tuo account Tailscale."
  sudo tailscale up
fi
# HTTPS con certificato valido sull'indirizzo https://<nome-jetson>.<tailnet>.ts.net
sudo tailscale serve --bg 8000 || warn "tailscale serve non riuscito: attiva HTTPS nella console Tailscale (DNS → HTTPS Certificates) e rilancia: sudo tailscale serve --bg 8000"

PORT="$(grep -E '^JAS_PORT=' "$DIR/.env" | cut -d= -f2 || echo 8000)"
TS_NAME="$(tailscale status --json 2>/dev/null | "$PY" -c "import json,sys; print(json.load(sys.stdin)['Self']['DNSName'].rstrip('.'))" 2>/dev/null || true)"
TS_IP="$(tailscale ip -4 2>/dev/null | head -n1 || true)"

step "Fatto!"
echo "Il server parte da solo a ogni accensione del Jetson."
echo
echo "Dalla stessa rete locale:   http://$(hostname -I | awk '{print $1}'):${PORT:-8000}"
[[ -n "$TS_NAME" ]] && echo "Da ovunque (Tailscale):     https://$TS_NAME"
[[ -n "$TS_IP" ]] && echo "Da ovunque (Tailscale, IP): http://$TS_IP:${PORT:-8000}"
echo
echo "Sui dispositivi esterni installa l'app Tailscale e accedi con lo stesso account."
echo "Log del server:  journalctl -u jetson-ai-server -f"
