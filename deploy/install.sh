#!/usr/bin/env bash
# Installazione del Jetson AI Server su NVIDIA Jetson (JetPack 6, Ubuntu 22.04).
#
# Uso (dalla cartella del progetto, con l'utente normale, NON con sudo):
#   bash deploy/install.sh
#
# Lo script si può rilanciare: salta i passaggi già fatti. Alla fine esegue un controllo
# completo (deploy/check.sh) e riassume eventuali avvisi.
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
USER_NAME="$(id -un)"
VENV="$DIR/.venv"
PY="$VENV/bin/python"
PIP="$VENV/bin/pip"

# Ruote PyTorch per JetPack 6 (Python 3.10). Se cambiano, passale da fuori:
#   TORCH_WHEEL=<url> TORCHVISION_WHEEL=<url> bash deploy/install.sh
# (vedi https://docs.ultralytics.com/guides/nvidia-jetson/ → "Install PyTorch and Torchvision").
TORCH_WHEEL="${TORCH_WHEEL:-https://github.com/ultralytics/assets/releases/download/v0.0.0/torch-2.5.0a0+872d972e41.nv24.08-cp310-cp310-linux_aarch64.whl}"
TORCHVISION_WHEEL="${TORCHVISION_WHEEL:-https://github.com/ultralytics/assets/releases/download/v0.0.0/torchvision-0.20.0a0+afc54f7-cp310-cp310-linux_aarch64.whl}"

# LLM da scaricare e impostare in uso (Qwen3-4B-Instruct-2507, quantizzato Q4_K_M).
LLM_CANDIDATES=(
  "qwen3:4b-instruct-2507-q4_K_M"
  "qwen3:4b-instruct"
  "hf.co/unsloth/Qwen3-4B-Instruct-2507-GGUF:Q4_K_M"
  "hf.co/Qwen/Qwen3-4B-Instruct-2507-GGUF:Q4_K_M"
)

WARNINGS=()
step() { printf '\n\033[1;32m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m!! %s\033[0m\n' "$*"; WARNINGS+=("$*"); }
die()  { printf '\033[1;31mERRORE: %s\033[0m\n' "$*" >&2; exit 1; }

if [[ $EUID -eq 0 ]]; then
  die "lancia lo script con il tuo utente normale (userà sudo quando serve)."
fi
command -v sudo >/dev/null 2>&1 || die "sudo non trovato."

step "Controlli preliminari"
IS_JETSON=0
if [[ -f /etc/nv_tegra_release ]]; then
  IS_JETSON=1
  echo "Jetson rilevato: $(head -n1 /etc/nv_tegra_release)"
  grep -q "R36" /etc/nv_tegra_release || warn "Pensato per JetPack 6 (L4T R36): con un'altra versione potrebbero servire ruote PyTorch diverse."
else
  warn "Non sembra un Jetson: installo la versione standard (CPU o GPU desktop)."
fi
command -v python3 >/dev/null 2>&1 || die "python3 non trovato."
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' || die "serve Python 3.10 o superiore."
if ! curl -fsS --max-time 15 -o /dev/null https://pypi.org/simple/pip/; then
  die "nessun accesso a internet (pypi.org non raggiungibile): controlla la connessione e rilancia."
fi
free_gb="$(df -BG --output=avail "$DIR" | tail -n1 | tr -dc '0-9')"
if [[ -n "$free_gb" && "$free_gb" -lt 15 ]]; then
  warn "Spazio libero ${free_gb} GB: ne servono almeno 15 (PyTorch, modelli, LLM). Meglio avere un SSD NVMe."
fi

FSTYPE="$(findmnt -no FSTYPE -T "$DIR" 2>/dev/null || true)"
case "$FSTYPE" in
  vfat|exfat|ntfs|fuseblk) die "la cartella è su un'unità $FSTYPE: non supporta i permessi di Linux. Formatta l'unità in ext4 (vedi README) e rifai il clone lì." ;;
esac

step "Pacchetti di sistema"
# Alcuni repository di terze parti già presenti sul sistema (RealSense, ROS, ...) possono dare errore:
# non è un problema nostro, si prosegue con gli indici già scaricati.
sudo apt-get update || warn "apt-get update ha dato errori su repository di terze parti (es. RealSense/ROS): ignorati, si prosegue."
sudo apt-get install -y python3-venv python3-pip python3-dev curl ffmpeg \
  tesseract-ocr tesseract-ocr-ita tesseract-ocr-eng libopenblas-dev libgl1 libglib2.0-0
if [[ $IS_JETSON -eq 1 ]]; then
  # cuSPARSELt serve al PyTorch per JetPack 6.
  if ! dpkg -s libcusparselt0 >/dev/null 2>&1; then
    tmp="$(mktemp -d)"
    if curl -fsSL -o "$tmp/cuda-keyring.deb" https://developer.download.nvidia.com/compute/cuda/repos/ubuntu2204/arm64/cuda-keyring_1.1-1_all.deb \
       && sudo dpkg -i "$tmp/cuda-keyring.deb" && { sudo apt-get update || true; } \
       && sudo apt-get install -y libcusparselt0 libcusparselt-dev; then
      :
    else
      warn "cuSPARSELt non installato: PyTorch potrebbe non partire con la GPU."
    fi
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
    if ! "$PIP" install "numpy<2" "$TORCH_WHEEL" "$TORCHVISION_WHEEL"; then
      warn "Ruote PyTorch non scaricate: YOLO userà la CPU (lento). Trova gli indirizzi aggiornati su https://docs.ultralytics.com/guides/nvidia-jetson/ e rilancia con TORCH_WHEEL=… TORCHVISION_WHEEL=… bash deploy/install.sh"
    fi
  fi
  "$PIP" install "numpy<2" onnx onnxslim || warn "onnx/onnxslim non installati: l'esportazione TensorRT potrebbe fallire."
fi

step "Dipendenze del server"
if [[ $IS_JETSON -eq 1 ]]; then
  # Evita che pip sostituisca il PyTorch per Jetson con quello generico.
  CONSTRAINTS="$(mktemp)"
  "$PIP" freeze | grep -iE '^(torch|torchvision)==' > "$CONSTRAINTS" || true
  "$PIP" install -r "$DIR/requirements.txt" "numpy<2" --constraint "$CONSTRAINTS"
  rm -f "$CONSTRAINTS"
else
  "$PIP" install -r "$DIR/requirements.txt"
fi

if [[ $IS_JETSON -eq 1 ]]; then
  if "$PY" -c "import torch, sys; sys.exit(0 if torch.cuda.is_available() else 1)" 2>/dev/null; then
    echo "PyTorch vede la GPU: ok"
  else
    warn "PyTorch NON vede la GPU: YOLO funzionerà ma lentamente (solo CPU)."
  fi
  if "$PY" -c "import tensorrt" 2>/dev/null; then
    echo "TensorRT disponibile: ok"
  else
    warn "TensorRT non trovato in Python (sudo apt install nvidia-jetpack): niente ottimizzazione TensorRT."
  fi
fi

step "Configurazione"
[[ -f "$DIR/.env" ]] || cp "$DIR/.env.example" "$DIR/.env"
mkdir -p "$DIR/data/models/yolo" "$DIR/plugins"
# Nessun modello YOLO viene scaricato o aggiunto in automatico: li carichi tu
# da Analisi video → Modelli di visione (o con: .venv/bin/python -m app.cli add-model file.pt).

step "Ollama (motore per gli LLM locali)"
if ! command -v ollama >/dev/null 2>&1; then
  curl -fsSL https://ollama.com/install.sh | sh || warn "Installazione di Ollama non riuscita: senza non funziona la chat con i documenti."
fi
if command -v ollama >/dev/null 2>&1; then
  # Poca memoria sul Jetson: un solo modello alla volta, una sola richiesta, scaricato da solo dopo l'uso.
  sudo mkdir -p /etc/systemd/system/ollama.service.d
  sudo tee /etc/systemd/system/ollama.service.d/jetson.conf >/dev/null <<'EOF'
[Service]
Environment="OLLAMA_MAX_LOADED_MODELS=1"
Environment="OLLAMA_NUM_PARALLEL=1"
EOF
  # Modelli LLM su un'altra unità (es. la SD/SSD esterna):
  #   OLLAMA_MODELS_DIR=/mnt/dati/ollama bash deploy/install.sh
  if [[ -n "${OLLAMA_MODELS_DIR:-}" ]]; then
    sudo mkdir -p "$OLLAMA_MODELS_DIR"
    sudo chown -R ollama:ollama "$OLLAMA_MODELS_DIR" 2>/dev/null || true
    printf 'Environment="OLLAMA_MODELS=%s"\n' "$OLLAMA_MODELS_DIR" | sudo tee -a /etc/systemd/system/ollama.service.d/jetson.conf >/dev/null
    echo "Modelli LLM salvati in: $OLLAMA_MODELS_DIR"
  fi
  sudo systemctl daemon-reload
  sudo systemctl enable ollama || true
  sudo systemctl restart ollama || true
  for _ in $(seq 1 30); do curl -fs http://127.0.0.1:11434/api/tags >/dev/null && break; sleep 1; done
  if ! curl -fs http://127.0.0.1:11434/api/tags >/dev/null; then
    warn "Ollama non risponde: controlla con  journalctl -u ollama -n 50"
  fi
fi

step "LLM scelto: Qwen3-4B-Instruct-2507"
# Il modello è pubblicato con nomi diversi (libreria Ollama o GGUF su Hugging Face):
# si prova in ordine e si usa il primo che si scarica. Altri modelli si aggiungono
# e si scelgono da Lettore documenti → Modelli linguistici.
LLM_SCELTO=""
if command -v ollama >/dev/null 2>&1 && curl -fs http://127.0.0.1:11434/api/tags >/dev/null; then
  for candidate in "${LLM_CANDIDATES[@]}"; do
    echo "Provo: $candidate"
    if ollama pull "$candidate"; then LLM_SCELTO="$candidate"; break; fi
  done
fi
if [[ -n "$LLM_SCELTO" ]]; then
  # --if-unset: se in un'installazione precedente hai già scelto un altro modello, non lo tocca.
  (cd "$DIR" && "$PY" -m app.cli set-llm "$LLM_SCELTO" --if-unset)
else
  warn "Qwen3-4B-Instruct-2507 non scaricato: scaricalo da Lettore documenti → Modelli linguistici (nome su ollama.com o hf.co/…)."
fi

step "Primo utente amministratore"
if [[ "$(cd "$DIR" && "$PY" -m app.cli count-users)" == "0" ]]; then
  if [[ -t 0 ]]; then
    read -rp "Nome dell'amministratore (3-32 caratteri: lettere, numeri, . _ -): " ADMIN
    (cd "$DIR" && "$PY" -m app.cli create-user "$ADMIN" --admin) \
      || warn "Amministratore non creato: riprova con  cd $DIR && .venv/bin/python -m app.cli create-user NOME --admin"
  else
    warn "Terminale non interattivo: crea l'amministratore con  cd $DIR && .venv/bin/python -m app.cli create-user NOME --admin"
  fi
else
  echo "Utenti già presenti: salto."
fi

step "Avvio automatico all'accensione (systemd)"
sed -e "s|__USER__|$USER_NAME|g" -e "s|__DIR__|$DIR|g" "$DIR/deploy/jetson-ai-server.service" \
  | sudo tee /etc/systemd/system/jetson-ai-server.service >/dev/null
sudo systemctl daemon-reload
sudo systemctl enable jetson-ai-server
sudo systemctl restart jetson-ai-server

PORT="$(grep -E '^JAS_PORT=' "$DIR/.env" | cut -d= -f2 || true)"
PORT="${PORT:-8000}"
HEALTH=0
for _ in $(seq 1 40); do
  if curl -fs "http://127.0.0.1:$PORT/api/health" >/dev/null 2>&1 || curl -fs -o /dev/null "http://127.0.0.1:$PORT/" 2>/dev/null; then HEALTH=1; break; fi
  sleep 1
done
if [[ $HEALTH -eq 1 ]]; then
  echo "Il server risponde su http://127.0.0.1:$PORT: ok"
else
  warn "Il server non risponde: guarda  journalctl -u jetson-ai-server -n 50"
fi

step "Accesso remoto con Tailscale"
if ! command -v tailscale >/dev/null 2>&1; then
  curl -fsSL https://tailscale.com/install.sh | sh || warn "Installazione di Tailscale non riuscita."
fi
SERVE_OK=0
TS_NAME=""
if command -v tailscale >/dev/null 2>&1; then
  if ! tailscale status >/dev/null 2>&1; then
    echo "Si aprirà un link: aprilo nel browser (anche dal telefono) e accedi al tuo account Tailscale."
    sudo tailscale up || warn "Accesso a Tailscale non completato: rilancia  sudo tailscale up"
  fi
  # HTTPS con certificato valido sull'indirizzo https://<nome-jetson>.<tailnet>.ts.net, raggiungibile
  # SOLO dai dispositivi della tua rete Tailscale (non è "funnel": nulla viene esposto su internet).
  if sudo tailscale serve --bg "$PORT"; then
    SERVE_OK=1
  else
    warn "tailscale serve non riuscito: nella console Tailscale (login.tailscale.com → DNS) attiva MagicDNS e HTTPS Certificates, poi: sudo tailscale serve --bg $PORT"
  fi
  TS_NAME="$(tailscale status --json 2>/dev/null | "$PY" -c "import json,sys; print(json.load(sys.stdin)['Self']['DNSName'].rstrip('.'))" 2>/dev/null || true)"
fi

if grep -qE '^JAS_HOST=(0\.0\.0\.0|::)' "$DIR/.env"; then
  warn "Nel file .env JAS_HOST apre il server a tutta la rete locale (senza cifratura). Per chiuderlo: JAS_HOST=127.0.0.1 e poi sudo systemctl restart jetson-ai-server"
fi

step "Controllo finale"
bash "$DIR/deploy/check.sh" || true

step "Fatto!"
echo "Il server parte da solo a ogni accensione del Jetson."
echo
if [[ $SERVE_OK -eq 1 && -n "$TS_NAME" ]]; then
  echo "Indirizzo (dai dispositivi della tua rete Tailscale):  https://$TS_NAME"
else
  echo "Tailscale non è ancora configurato per il server: vedi gli avvisi qui sotto."
fi
echo "Dal Jetson stesso:  http://localhost:$PORT"
echo
echo "Per sicurezza il server NON è raggiungibile dalla rete locale né da internet: solo tramite Tailscale."
echo "Sui dispositivi da cui vuoi usarlo installa l'app Tailscale e accedi con lo stesso account."
echo
echo "Consigli: attiva l'autenticazione a due fattori sull'account Tailscale, abilita l'approvazione"
echo "dei dispositivi nella console (login.tailscale.com) e usa password lunghe per gli utenti."
echo
echo "Log del server:  journalctl -u jetson-ai-server -f"
echo "Controllo:       bash deploy/check.sh        Aggiornamento:  bash deploy/update.sh"
echo "Massime prestazioni:  sudo nvpmodel -m 0 && sudo jetson_clocks"
echo "I modelli YOLO caricati vengono ottimizzati con TensorRT in automatico (qualche minuto ciascuno)."

if [[ ${#WARNINGS[@]} -gt 0 ]]; then
  printf '\n\033[1;33mDa sistemare (%d):\033[0m\n' "${#WARNINGS[@]}"
  for w in "${WARNINGS[@]}"; do printf '  - %s\n' "$w"; done
else
  printf '\n\033[1;32mTutto a posto, nessun avviso.\033[0m\n'
fi
