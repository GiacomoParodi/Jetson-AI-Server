# Jetson AI Server

Server locale per **NVIDIA Jetson Orin Nano** che esegue modelli di intelligenza artificiale
su file caricati da remoto, tutto in locale (nessun dato lascia il dispositivo).

L'interfaccia ha due aree distinte:

- **Lettore documenti**: carichi i PDF (anche scansionati, con OCR) e ci dialoghi come in un
  chatbot, in stile NotebookLM. Le risposte arrivano in streaming, indicano le pagine con
  riferimenti cliccabili e il documento si legge accanto alla chat. Per ogni documento puoi
  tenere più conversazioni e ritrovarle in qualsiasi momento.
- **Analisi video**: pipeline ad albero di modelli di visione (rilevamento, segmentazione,
  classificazione) su video e immagini, con editor visuale. Ogni modello caricato viene
  ottimizzato automaticamente con **TensorRT FP16** per il Jetson.

Inoltre: interfaccia web per PC e telefono, **più utenti** con login, avvio automatico
all'accensione, **accesso da qualsiasi rete** tramite Tailscale, **plugin** per aggiungere nuovi
strumenti con un file Python. Tutto gira in locale: nessun dato lascia il dispositivo.

## Demo senza Jetson

`demo/demo.html` è l'interfaccia vera con un finto server dentro il browser e dati di esempio
(documenti con conversazioni, pipeline, modelli, risultati di una vera esecuzione). Si apre con un
doppio clic, anche senza connessione, e niente viene salvato né inviato. Si può cliccare
dappertutto: chattare con i documenti, caricare un PDF, creare e modificare pipeline, avviare
un'analisi, scaricare modelli, uscire ed entrare come utente senza permessi di amministratore
(nome utente `mario`).

Dopo modifiche a `app/static/` si rigenera con `python demo/build.py`.

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
3. installa **Ollama**, scarica **Qwen3-4B-Instruct-2507** (Q4_K_M) e lo imposta come LLM in uso;
4. chiede nome e password del primo **amministratore**;
5. registra il servizio **systemd**, che parte a ogni accensione e si riavvia se si blocca;
6. installa **Tailscale** e attiva l'accesso HTTPS.

Alla fine stampa gli indirizzi da aprire nel browser.

> Consiglio: per le prestazioni massime imposta la modalità di alimentazione più alta
> (`sudo nvpmodel -m 0` o "MAXN SUPER" dal menu in alto a destra) e usa un SSD NVMe per i dati.

## Accesso da remoto e sicurezza

### Come funziona

Il server **non è esposto su internet**. Per usarlo da fuori casa si usa **Tailscale**, una VPN
privata (basata su WireGuard) che collega i tuoi dispositivi come se fossero sulla stessa rete,
ovunque siano: Wi-Fi di casa, ufficio, 4G.

- Non serve aprire porte sul router né avere un IP pubblico.
- Il server ascolta **solo sul Jetson stesso** (`127.0.0.1`). `tailscale serve` lo rende
  disponibile all'indirizzo `https://<nome-jetson>.<tua-rete>.ts.net` **solo ai dispositivi della
  tua rete Tailscale**, con un certificato HTTPS valido.
- Chi non è nella tua rete Tailscale non può nemmeno raggiungere il server.

Per usarlo: sul Jetson ci pensa `deploy/install.sh`; su PC e telefono installi l'app **Tailscale**,
accedi con lo stesso account e apri l'indirizzo che lo script stampa alla fine. Per HTTPS va attivato
una volta nella console Tailscale (*DNS → HTTPS Certificates*).

### Livelli di protezione

| Livello | Cosa fa |
|---|---|
| Rete | Solo i dispositivi autorizzati della tua rete Tailscale arrivano al server; il traffico è cifrato da un capo all'altro |
| Trasporto | HTTPS con certificato valido |
| Accesso | Login per ogni utente; password salvate solo come hash PBKDF2-SHA256 con sale; sessioni con token casuale (nel database resta solo l'hash); cookie `HttpOnly`, `SameSite` e `Secure`; blocco dopo 5 tentativi falliti in 5 minuti per quell'utente |
| Permessi | Ogni utente vede solo i propri documenti, conversazioni e analisi; solo gli amministratori gestiscono utenti, modelli e pipeline |
| Browser | Intestazioni di sicurezza: politica sui contenuti rigorosa (nessuno script esterno), pagina non incorporabile in altri siti, HSTS |
| Servizio | Il servizio di sistema non può acquisire nuovi privilegi e ha accesso limitato al sistema |

### Altre persone

Due modi: **invitarle nella tua rete Tailscale** (vedrebbero anche gli altri tuoi dispositivi, a meno
di limitarli con le regole di accesso) oppure **condividere solo il Jetson** dalla console Tailscale
(*Share*): useranno il loro account Tailscale e vedranno solo quella macchina. In entrambi i casi
crea poi per loro un utente dalla pagina **Utenti**.

### Cosa non fare

- Non aprire porte sul router (*port forwarding*).
- Non usare `tailscale funnel`: pubblica il servizio su internet.
- Non impostare `JAS_HOST=0.0.0.0` nel file `.env`: apre il server a tutta la rete locale, senza cifratura.
- Non attivare `JAS_API_DOCS=on` se non serve: la documentazione dell'API sarebbe visibile senza accesso.

### Lista di controllo consigliata

- Attiva l'**autenticazione a due fattori** sull'account usato per Tailscale (e sul provider di accesso).
- Nella console Tailscale abilita l'**approvazione dei dispositivi** e la scadenza delle chiavi; con le
  *regole di accesso* (ACL) limita chi può raggiungere il Jetson.
- Usa **password lunghe** per gli utenti del server (il minimo richiesto è 8 caratteri; meglio una frase).
- Tieni il sistema aggiornato: `sudo apt install unattended-upgrades`.
- Per SSH usa solo le chiavi (`PasswordAuthentication no` in `/etc/ssh/sshd_config`) oppure Tailscale SSH.
- Facoltativo, un firewall: `sudo ufw default deny incoming && sudo ufw allow in on tailscale0`, più una
  regola per SSH dalla tua rete. Prima di abilitarlo (`sudo ufw enable`) assicurati di avere un modo per rientrare.
- Carica modelli `.pt` solo da fonti fidate: un file `.pt` può contenere codice che viene eseguito all'apertura.
- I dati sul disco **non sono cifrati**: se il Jetson può essere rubato, valuta la cifratura del disco.
- Fai copie di `data/` (documenti, conversazioni, modelli, database).

### Alternative a Tailscale

| Soluzione | Pro | Contro |
|---|---|---|
| **Tailscale** (consigliata) | Nessuna porta aperta, funziona dietro qualsiasi rete, configurazione semplice | Ogni dispositivo deve avere l'app; verifica i limiti del piano gratuito |
| WireGuard o OpenVPN su router/Jetson | Tutto sotto il tuo controllo | Serve una porta aperta sul router e un indirizzo raggiungibile; non funziona dietro CGNAT; più manutenzione |
| Cloudflare Tunnel con Cloudflare Access | Si usa da qualsiasi browser, senza app | I dati (PDF, video) passano dai loro server; di solito c'è un limite alla dimensione di ogni caricamento (100 MB sui piani gratuiti, da verificare), poco adatto ai video |
| Port forwarding diretto | — | Sconsigliato: espone il Jetson a tutta internet |

## Uso

Dopo l'accesso la pagina iniziale propone le due aree. Gli amministratori hanno in più la
pagina **Utenti**.

### Lettore documenti

1. **Documenti** → carica uno o più PDF (trascinali nella pagina). L'elaborazione (lettura del
   testo, OCR delle scansioni, indice per la ricerca) avviene in background con avanzamento. I
   documenti restano in archivio.
2. Apri un documento: a sinistra le **conversazioni** di quel documento, al centro la **chat**, a
   destra il **documento** (pagine con ‹ ›). Scrivi una domanda e premi Invio.
3. La risposta compare mentre viene scritta. I riferimenti **[p. 3]** sono pulsanti: aprono la
   pagina nel visualizzatore. Per i documenti lunghi, «Fonti usate» mostra i passaggi su cui si
   è basata la risposta. **Interrompi** ferma la risposta (quanto scritto resta salvato).
4. Ogni conversazione ricorda le domande precedenti (puoi chiedere «e il canone?»). Puoi
   crearne di nuove, **rinominarle** ed **eliminarle**; **Cronologia** raccoglie tutte le
   conversazioni, con ricerca.

Per i documenti lunghi la ricerca combina parole chiave e, se hai scelto un modello di embedding,
somiglianza semantica; le richieste di riassunto usano parti distribuite lungo tutto il testo.
Se cambi modello di embedding, «Reindicizza» aggiorna un documento già caricato.

### Analisi video

1. **Nuova analisi** → scegli la pipeline, carica il video o l'immagine, avvia.
2. Segui l'avanzamento; al termine trovi il file annotato, le tabelle e i file da scaricare.
   Dal risultato puoi **rieseguire** sullo stesso file con altri parametri.
3. **Cronologia** raccoglie le analisi; **Pipeline** e **Modelli di visione** si gestiscono come
   descritto sotto.

### Un solo lavoro pesante alla volta

Con 8 GB di memoria condivisa tra CPU e GPU, chat con i documenti, analisi video ed elaborazione
dei documenti si mettono in fila (nell'ordine di arrivo) invece di competere per la memoria.
Se stai chattando mentre gira un'analisi, la chat mostra «Il server sta eseguendo un'analisi
video» e parte appena finisce. Con `JAS_CONCURRENT_AI=on` nel file `.env` la fila si disattiva.

### Modelli di visione

Da **Analisi video → Modelli di visione** gli amministratori caricano i file `.pt` addestrati con Ultralytics
(YOLOv8, YOLO11, …) di tipo detection, segmentazione o classificazione. Tipo, classi e risoluzione
di addestramento vengono letti dal file.

Subito dopo il caricamento il server mette in coda l'**ottimizzazione**: ricompila il modello con
TensorRT in FP16 per la GPU del Jetson (qualche minuto, una volta sola) e misura la velocità
prima e dopo. Lo stato compare nella pagina ("da ottimizzare", "in ottimizzazione…",
"⚡ TensorRT"). Finché non è pronto, il modello funziona comunque, solo più lento.

- **Sostituisci**: carica una nuova versione del file mantenendo il nome; tutte le pipeline che
  lo usano passano alla nuova versione, che viene ri-ottimizzata.
- **Ri-ottimizza**: rifà la compilazione (es. se era fallita).
- Dopo un aggiornamento di JetPack/TensorRT i modelli vengono ri-ottimizzati da soli al riavvio,
  perché un file TensorRT funziona solo con la versione con cui è stato creato.

Lo stesso script si può lanciare da terminale sul Jetson:

```bash
.venv/bin/python -m app.cli add-model ~/Scaricati/scrivanie.pt   # aggiunge un modello
.venv/bin/python -m app.cli optimize scrivanie.pt               # lo ricompila subito con TensorRT
.venv/bin/python -m app.cli list-models                         # stato e velocità
.venv/bin/python -m app.cli set-llm NOME_OLLAMA                  # LLM in uso
```

Solo gli amministratori possono caricare modelli: un file `.pt` può contenere codice che viene
eseguito all'apertura, quindi carica solo modelli di cui ti fidi.

### Pipeline ad albero

Da **Analisi video → Pipeline** gli amministratori creano alberi di modelli (gli altri utenti possono
solo vederli e usarli). Ogni nodo ha un modello e queste regole:

| Dove si trova il nodo | Su cosa lavora |
|---|---|
| Radice | l'intero fotogramma (o l'immagine) |
| Figlio di una detection/segmentazione | il **ritaglio** di ogni oggetto trovato dal padre, solo per le classi del padre scelte, con un margine regolabile |
| Figlio di una classificazione | la stessa immagine del padre, solo se la classe predetta è tra quelle scelte |
| Fratelli | la stessa immagine, in modo indipendente |

Esempio: *Scrivanie* (detection) → *Stato della scrivania* (classificazione sui ritagli delle
scrivanie: ordinata/disordinata) → *Oggetti* (segmentazione, solo se "disordinata").

Nell'editor clicchi un nodo per modificarlo (nome, modello, confidenza, classi da tenere, classi
del padre che lo attivano, margine del ritaglio) e usi **+ Figlio**, **+ Fratello**, **↑ ↓**,
**Duplica** ed **Elimina**. Dal pannello del nodo puoi anche caricare un nuovo modello o
sostituire il file di quello attuale. Le modifiche si applicano con **Salva**; se due persone
modificano la stessa pipeline insieme, la seconda riceve un avviso invece di sovrascrivere.

Per analizzare un video: **Analisi video → Nuova analisi**, scegli la pipeline e carica
il file. Ottieni il video annotato (un colore per nodo, uguale a quello dell'editor), una tabella
per nodo e classe e un CSV in cui ogni riga ha `id` e `parent_id`, così sai a quale oggetto del
padre appartiene ogni risultato. Il lavoro usa la pipeline com'era al momento dell'invio.

### Modelli linguistici (Ollama)

L'installazione scarica **Qwen3-4B-Instruct-2507** (quantizzato Q4_K_M) e lo imposta come LLM in
uso; per cambiarlo, modifica l'elenco `LLM_CANDIDATES` in `deploy/install.sh` oppure usa la pagina
**Lettore documenti → Modelli linguistici**. Nessun modello di visione viene scaricato. Lì un
amministratore:

1. scarica i modelli scrivendo il nome esatto dalla libreria di [ollama.com](https://ollama.com/library)
   (es. `famiglia:3b`), con la barra di avanzamento;
2. sceglie l'**LLM in uso**, l'**embedding in uso** (facoltativo) e la dimensione del **contesto**;
3. elimina i modelli che non servono più.

Finché non scegli un LLM, la chat con i documenti resta disattivata.

**Parametri consigliati per il Jetson Orin Nano (8 GB, memoria condivisa con la GPU):**

| | LLM | Embedding (facoltativo) |
|---|---|---|
| Parametri | 1–4 miliardi (`:1.5b`, `:3b`, `:4b`). 7–8B girano ma lenti | sotto ~600 milioni |
| Quantizzazione | `Q4_K_M` (il tag normale). Evita `fp16` e `q8` | quella di serie |
| Peso del file | sotto ~3 GB | sotto ~1,2 GB |
| Lingua | per l'italiano: famiglie Qwen, Gemma, Llama recenti | **multilingue** per documenti in italiano |

Contesto: 4096 token vanno bene quasi sempre; 8192 se vuoi che documenti più lunghi vengano letti
per intero (più memoria, più lento); 2048 è il più veloce. La pagina segnala i modelli scaricati
che non rispettano questi parametri. Prima di ogni analisi video il server libera la memoria
dell'LLM, quindi i due non si contendono la RAM.

## Aggiungere un nuovo compito (plugin)

Crea un file in `plugins/` (es. `plugins/mio_compito.py`) e riavvia il servizio: il compito compare
da solo nella pagina iniziale, sotto «Altri strumenti», con il modulo generato dai parametri. Esempio completo in
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

Tipi di parametro: `text`, `textarea`, `number`, `bool`, `select` (con `choices`).
Tipi di output: `video`, `image` (mostrati in anteprima), `file` (solo download).
Gli errori da mostrare all'utente si segnalano con `raise TaskError("messaggio")`.

## API

Tutto ciò che fa l'interfaccia è disponibile via HTTP (la documentazione interattiva su `/docs` si attiva con `JAS_API_DOCS=on`, ma resta visibile anche senza accesso):

```bash
TOKEN=$(curl -s https://jetson.tailnet.ts.net/api/login -H 'content-type: application/json' \
  -d '{"username":"giacomo","password":"..."}' | jq -r .token)

# analisi video
curl -H "Authorization: Bearer $TOKEN" \
  -F task=yolo_pipeline -F 'params={"pipeline":"1"}' \
  -F file=@video.mp4 https://jetson.tailnet.ts.net/api/jobs

# documenti: carica un PDF, crea una conversazione e fai una domanda (risposta in streaming, SSE)
curl -H "Authorization: Bearer $TOKEN" -F file=@contratto.pdf https://jetson.tailnet.ts.net/api/documents
curl -X POST -H "Authorization: Bearer $TOKEN" https://jetson.tailnet.ts.net/api/documents/1/conversations
curl -N -H "Authorization: Bearer $TOKEN" -H 'content-type: application/json' \
  -d '{"content":"Quando scade il contratto?"}' https://jetson.tailnet.ts.net/api/conversations/1/messages
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
