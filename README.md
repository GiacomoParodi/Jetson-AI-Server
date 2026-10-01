# Jetson AI Server

Server locale per **NVIDIA Jetson Orin Nano** che esegue modelli di intelligenza artificiale
su file caricati da remoto, tutto in locale (nessun dato lascia il dispositivo).

- **Rilevamento oggetti (YOLO)** su video e immagini, con i tuoi modelli `.pt` addestrati:
  video annotato, conteggio per classe (anche oggetti unici con tracking) e CSV delle detection.
  Sul Jetson i modelli vengono ottimizzati con **TensorRT** al primo utilizzo.
- **Domande su PDF** con un LLM locale (Ollama, es. Qwen 2.5 3B), anche su **PDF scansionati**
  grazie all'OCR. Le risposte citano le pagine.
- **Interfaccia web** utilizzabile da PC e telefono, **più utenti** con login.
- **Coda dei lavori**: un lavoro pesante alla volta, con avanzamento e annullamento.
- **Avvio automatico** all'accensione e **accesso da qualsiasi rete** tramite Tailscale.
- **Plugin**: per aggiungere un nuovo compito basta un file Python.

## Installazione sul Jetson

Requisiti: Jetson Orin Nano con **JetPack 6** (Ubuntu 22.04), connessione a internet per l'installazione.

```bash
git clone https://github.com/giacomoparodi/prova.git jetson-ai-server
cd jetson-ai-server
bash deploy/install.sh
```

Lo script:
1. installa i pacchetti di sistema (ffmpeg, Tesseract OCR italiano/inglese, …);
2. crea l'ambiente Python con **PyTorch per Jetson** (GPU) e le dipendenze;
3. installa **Ollama** e scarica il modello LLM e quello per la ricerca nel testo;
4. chiede nome e password del primo **amministratore**;
5. registra il servizio **systemd**, che parte a ogni accensione e si riavvia se si blocca;
6. installa **Tailscale** e attiva l'accesso HTTPS.

Alla fine stampa gli indirizzi da aprire nel browser.

> Consiglio: per le prestazioni massime imposta la modalità di alimentazione più alta
> (`sudo nvpmodel -m 0` o "MAXN SUPER" dal menu in alto a destra) e usa un SSD NVMe per i dati.

### Accesso da fuori casa / ufficio (Tailscale)

Tailscale crea una rete privata tra i tuoi dispositivi senza aprire porte sul router.

1. Sui dispositivi da cui vuoi usare il server (PC, telefono) installa l'app **Tailscale**
   e accedi con lo stesso account usato sul Jetson.
2. Apri `https://<nome-del-jetson>.<tua-tailnet>.ts.net` (l'indirizzo esatto lo stampa lo script
   e lo trovi nella console di Tailscale).

Per far usare il server ad altre persone, invitale nella tua tailnet (o condividi solo il
Jetson dalla console Tailscale) e crea loro un utente dalla pagina **Utenti**.

## Uso

1. Accedi con il tuo utente.
2. **Nuovo lavoro** → scegli il compito, carica il file e imposta i parametri.
3. Segui l'avanzamento; al termine trovi risultati, anteprime e file da scaricare.
4. Dal risultato puoi **rieseguire** sullo stesso file con altri parametri
   (per i PDF: "Fai un'altra domanda", senza ricaricare né rileggere il documento).

### Modelli YOLO

Dalla pagina **Modelli** gli amministratori caricano i file `.pt` addestrati con Ultralytics
(YOLOv8, YOLO11, …). Il nome del file diventa il nome del modello e le classi vengono lette
automaticamente. Nel campo "Classi da cercare" puoi limitare la ricerca (es. `desk`).

Solo gli amministratori possono caricare modelli: un file `.pt` può contenere codice che viene
eseguito all'apertura, quindi carica solo modelli di cui ti fidi.

### Scegliere un altro LLM

Nel file `.env` cambia `JAS_LLM_MODEL` (es. `llama3.2:3b`, `gemma3:4b`, oppure `qwen2.5:1.5b`
per risposte più veloci), poi:

```bash
ollama pull llama3.2:3b
sudo systemctl restart jetson-ai-server
```

Con 8 GB di memoria condivisa tra CPU e GPU conviene restare sui modelli fino a ~4 miliardi di
parametri. Prima di ogni lavoro YOLO il server scarica l'LLM dalla memoria per lasciare spazio.

## Aggiungere un nuovo compito (plugin)

Crea un file in `plugins/` (es. `plugins/mio_compito.py`) e riavvia il servizio: il compito compare
da solo nell'interfaccia, con il modulo generato dai parametri. Esempio completo in
[`plugins/_esempio.py`](plugins/_esempio.py).

```python
from app.tasks import JobContext, Param, Task, TaskError

class MioCompito(Task):
    id = "mio_compito"
    title = "Il mio compito"
    description = "Cosa fa, in una frase."
    accept = [".jpg", ".png"]                     # file accettati
    params = [Param("soglia", "Soglia", "number", default=0.5, min=0, max=1, step=0.1)]

    def run(self, ctx: JobContext) -> dict:
        # ctx.input_path: file caricato   ctx.params: parametri validati
        # ctx.output_dir: dove salvare i risultati
        ctx.progress(0.5, "A metà…")    # avanzamento mostrato all'utente
        ctx.check_cancelled()           # rispetta il pulsante Annulla
        return {
            "summary": "Testo del risultato (supporta **markdown** semplice)",
            "table": {"columns": ["A", "B"], "rows": [[1, 2]]},
            "outputs": [{"file": "risultato.png", "kind": "image", "label": "Risultato"}],
        }
```

Tipi di parametro: `text`, `textarea`, `number`, `bool`, `select` (con `choices`), `yolo_model`.
Tipi di output: `video`, `image` (mostrati in anteprima), `file` (solo download).
Gli errori da mostrare all'utente si segnalano con `raise TaskError("messaggio")`.

## API

Tutto ciò che fa l'interfaccia è disponibile via HTTP (documentazione interattiva su `/docs`):

```bash
TOKEN=$(curl -s https://jetson.tailnet.ts.net/api/login -H 'content-type: application/json' \
  -d '{"username":"giacomo","password":"..."}' | jq -r .token)

curl -H "Authorization: Bearer $TOKEN" \
  -F task=yolo_detect -F 'params={"model":"scrivanie.pt","classes":"desk"}' \
  -F file=@video.mp4 https://jetson.tailnet.ts.net/api/jobs
```

## Gestione

```bash
journalctl -u jetson-ai-server -f          # log in tempo reale
sudo systemctl restart jetson-ai-server    # riavvio (dopo modifiche a .env o plugin)
.venv/bin/python -m app.cli list-users     # utenti
.venv/bin/python -m app.cli reset-password NOME
git pull && sudo systemctl restart jetson-ai-server   # aggiornamento
```

I dati (database, file caricati, risultati, modelli) sono in `data/`.

## Sviluppo su PC

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python -m app.cli create-user admin --admin
.venv/bin/python -m app            # http://localhost:8000
.venv/bin/python -m pytest
```

Senza GPU YOLO gira su CPU (più lento); per i PDF serve Ollama installato.
