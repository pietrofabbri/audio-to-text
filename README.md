# audio-to-text

Pipeline locale per trascrizione, diarizzazione speaker e analisi prosodia di file audio/video lunghi (fino a 20+ ore), ottimizzata per Apple Silicon M1 Pro.

**Tutto gira in locale. Nessun dato inviato a servizi cloud. Gratuito.**

---

## Cosa fa

1. **VAD** — rileva i segmenti con voce (Silero VAD), scarta silenzi e rumori → dimezza il carico ASR
2. **ASR** — trascrive con word-level timestamps (mlx-whisper `large-v3-turbo` su GPU M1)
3. **Diarizzazione** — assegna ogni parola a uno speaker (`SPEAKER_00`, `SPEAKER_01`, ...)
4. **Prosodia** — estrae F0, intensità, jitter, shimmer, velocità del parlato per ogni segmento
5. **Output** — `transcript.json`, `transcript.txt`, `transcript.srt`, `prosody.csv`

**Tempi stimati su M1 Pro (16 GB, 20 ore di audio con 50% silenzio):**
- VAD: ~10 minuti
- ASR: ~30 minuti
- Diarizzazione: ~40 minuti (in parallelo con prosodia)
- Prosodia: ~15 minuti (4 worker CPU)
- **Totale: ~60–90 minuti**

---

## Requisiti di sistema

- macOS con Apple Silicon (M1/M2/M3/M4)
- [Homebrew](https://brew.sh) con `ffmpeg` già installato
- venv `~/Desktop/Titoli Fabbri/whisperx_env` già presente

Verifica ffmpeg:
```bash
ffmpeg -version
```

---

## Setup iniziale

### 1. Installa le dipendenze mancanti

```bash
cd ~/Progetti/audio-to-text
./setup_env.sh
```

Installa: `mlx`, `mlx-whisper`, `praat-parselmouth`, `librosa`, `soundfile`.  
Non tocca: `torch`, `pyannote`, `faster-whisper`, `whisperx` (già presenti).

---

### 2. Token Hugging Face (necessario per la diarizzazione)

Il modello `pyannote/speaker-diarization-3.1` è open source (MIT) ma richiede
di accettare i termini d'uso su Hugging Face. **È completamente gratuito.**

#### Passo 1 — Crea un account HF (se non ce l'hai)
Vai su: https://huggingface.co/join

#### Passo 2 — Crea un token di accesso Read
Vai su: https://huggingface.co/settings/tokens  
→ clicca **New token**  
→ tipo: **Read**  
→ nome: `audio-to-text` (qualsiasi)  
→ clicca **Generate token**  
→ copia il token (inizia con `hf_...`)

#### Passo 3 — Accetta i termini dei modelli pyannote

Vai su queste due pagine e clicca **"Accept"** (richiede login HF):
- https://huggingface.co/pyannote/speaker-diarization-3.1
- https://huggingface.co/pyannote/segmentation-3.0

#### Passo 4 — Salva il token in locale

```bash
~/Desktop/Titoli\ Fabbri/whisperx_env/bin/hf auth login
```

Incolla il token quando richiesto. Viene salvato in `~/.huggingface/token`
e usato automaticamente dalla pipeline.

In alternativa, puoi usare una variabile d'ambiente:
```bash
export HF_TOKEN="hf_tuotoken"
```

---

### 3. Scarica il modello ASR (prima esecuzione)

Il modello `mlx-community/whisper-large-v3-turbo-q8` (~1.5 GB) viene scaricato
automaticamente alla prima esecuzione dalla cache Hugging Face.

Per scaricarlo manualmente prima:
```bash
~/Desktop/Titoli\ Fabbri/whisperx_env/bin/python -c "
import mlx_whisper
mlx_whisper.transcribe('', path_or_hf_repo='mlx-community/whisper-large-v3-turbo-q8')
"
```

---

## Uso

### Processare un file

```bash
~/Desktop/Titoli\ Fabbri/whisperx_env/bin/python run.py input/registrazione.mp3
```

Oppure attiva il venv prima:
```bash
source ~/Desktop/Titoli\ Fabbri/whisperx_env/bin/activate
python run.py input/registrazione.mp3
```

### Processare tutti i file in input/

```bash
python run.py --all
```

### Senza diarizzazione (non serve token HF)

```bash
python run.py input/file.mp3 --no-diarization
```

### Usare faster-whisper come fallback (solo CPU, più lento)

```bash
python run.py input/file.mp3 --backend faster --model large-v3-turbo
```

### Verificare lo stato dei job

```bash
python run.py --status
```

---

## Scheduling notturno (03:00 ogni notte)

### Installa il job launchd

```bash
python setup_launchd.py install
```

Ogni notte alle 03:00 la pipeline si avvia, processa tutti i file in `input/`
che non hanno ancora un output completo, e si ferma dopo 2.5 ore.

### Controlla lo stato

```bash
python setup_launchd.py status
```

### Test manuale (esegui subito)

```bash
python setup_launchd.py run-now
```

### Rimuovi il job

```bash
python setup_launchd.py uninstall
```

---

## Tempi reali misurati (le stime precedenti erano ottimistiche ~3x)

Su questo Mac, misurati sul campione reale da 97,8 s (non stimati):

| Stadio | RTF misurato | 18 file da 1h |
|---|---|---|
| **ASR (faster-whisper large-v3-turbo, CPU INT8)** | **3,3x realtime** | **~5,5 h** |
| Diarizzazione (pyannote, MPS) | 14x realtime | ~77 min |
| VAD + ffmpeg | 33x realtime | ~33 min |
| Denoise (confronto su campione) | — | ~20 min |
| Prosodia + output | 49x realtime | ~22 min |
| **Totale** | | **~8 h** |

Nella finestra notturna di 3 ore entrano quindi **3 file da un'ora**, e
gli altri restano sul device: la coda avanza di 3 file a notte, dal più
vecchio al più nuovo. Non è un limite aggirabile con l'attesa.

Il parallelismo non aiuta: `num_workers` di faster-whisper agisce solo
se si passano più segmenti in una singola chiamata, mentre la pipeline
chiama `transcribe()` un chunk alla volta — il guadagno misurato è 12%.

### La GPU non è la soluzione che sembra

Misurato sugli stessi 11,4 minuti di audio reale, modello in cache:

| Backend | RTF | 18 ore di audio |
|---|---|---|
| faster-whisper CPU INT8 | 3,15x | 5,7 ore |
| mlx-whisper GPU (`mlx-community/whisper-large-v3-turbo`) | 4,77x | 3,8 ore |

La GPU è **1,5x**, non 5-10x come suggerito da alcune stime. Con 18 ore
a notte si passerebbe da 3 a ~6 file per notte, al costo di un
sottoprocesso separato con IPC (mlx e PyTorch non convivono) e della
sua manutenzione. Il modello è gia in cache, quindi il confronto e
stato fatto davvero e non e' un calcolo teorico.

La conclusione netta: **18 ore di audio non si elaborano in una notte
su questa macchina, in nessuna configurazione misurata.** Il sistema e
costruito per che la coda avanzi di qualche file a notte, in ordine,
senza perdere nulla.

### Capacità misurata (18 file da 1h che arrivano ogni giorno)

Finestra notturna 02:00–06:00 (4h a pieno regime) più tre passate
diurne brevi (09:30, 15:30, 21:30, da 40 min con 3 thread e priorità
bassa).

La riga che conta è il **rapporto di parlato**: il VAD scarta il
silenzio prima dell'ASR, quindi l'ASR paga solo le parole, non i minuti.

| Parlato | Costo per file da 1h | File per notte | + diurno | Esito vs 18/giorno |
|---|---|---|---|---|
| 92% (campione) | 0,70 h | 4 | 1 | scopre 13 |
| 70% | 0,53 h | 6 | 2 | scopre 10 |
| 50% | 0,38 h | 9 | 3 | scopre 6 |
| 36% (**misurato su registrazione vera**) | 0,27 h | 13 | 4 | scopre 1 |

**La coda cresce con qualsiasi rapporto di parlato realistico.** Il
rapporto vero si misura da solo: `nightly.py` lo legge dalle sessioni
gia elaborate e lo usa per la stima, quindi dopo la prima notte il piano
smette di essere una supposizione.

La leva che chiude il divario è il modello ASR, ed è una riga:

```bash
export A2T_ASR_MODEL=medium    # ~2x piu veloce, un po' meno accurato
```

`core/config.py` legge `A2T_ASR_MODEL` (e `A2T_ASR_THREADS` per i
thread): cambiare modello non richiede toccare il codice, e il checkpoint
riconosce cio che e gia fatto e non ricomincia.

## Flusso col registratore (import → elaborazione → archiviazione)

Quando il registratore è collegato, tutto il ciclo è in un comando:

```bash
python nightly.py --dry-run           # piano della notte: quanti file entrano
python nightly.py                     # ciclo completo: importa, elabora, pubblica
```

Sotto, i singoli passi:

```bash
python sync_device.py detect          # che cosa è montato? non tocca nulla
python sync_device.py pull --dry-run  # cosa verrebbe fatto?
python sync_device.py pull            # importa, processa, archivia, cancella
python sync_device.py purge           # svuota l'archivio oltre 7 giorni
python publish_corpus.py push         # pubblica sulla repo privata
```

Il ciclo notturno si ferma **fra un file e l'altro** quando il budget
di tempo (`--max-seconds`, 3h di default) è esaurito: iniziare un file
che non finisce dentro la finestra costerebbe il suo tempo senza
produrre nulla. Quello che non entra resta sul device e riparte dalla
stessa condizione la notte dopo.

I file **non** vengono copiati prima di essere elaborati: vengono letti
dove sono. Il registratore resta la fonte di verità finche il lavoro non
è finito, e la cancellazione è l'ultimo atto:

```
elboro → verifico l'output → archivio in locale → cancello dal device
```

Se una passaggio fallisce, **il file resta sul device**. Non esiste un
percorso in cui un file viene cancellato senza che la trascrizione esista
e sia stata verificata (`transcript.json` presente, con segmenti e almeno
poche parole). Ogni file toccato finisce in `logs/device_manifest.jsonl`
con hash ed esito.

`detect` riconosce i formati di nome più comuni dei registratori
(`REC_20261003_220415.mp3`, `2026-10-03 22-04-15.m4a`, e così via) e
ricava l'ora di registrazione, che finisce in `session.json` come
`session_start_wall`: è il dato che rende poi possibile agganciare la
trascrizione ai dati biometrici. I nomi di cui non si riconosce la data
vengono importati ma senza orario, e il tool te lo dice.

L'archivio locale (`archive/`) tiene l'originale per 7 giorni, poi `purge`
lo cancella: abbastanza per rifare un ASR migliore, non abbastanza per
far crescere il disco per sempre. Con `A2T_KEEP_LOCAL=0` si disattiva.

### Riduzione dei fruscii: decide il software

Non c'è nessuno sveglio la notte ad ascoltare, quindi la scelta è
automatica e **misurata** (`core/config.py → DenoiseConfig`).

Il confronto usa le **stesse finestre temporali** per originale e
variante ripulita, altrimenti ogni differenza sarebbe inseparabile da
dove sono stati tagliati i chunk. Su entrambe si calcolano:

| Segnale | Cosa cattura |
|---|---|
| confidenza ASR media | quanto l'ASR è sicuro di quello che sente |
| rapporto di parlato (VAD) | quanto parlato è sopravvissuto alla pulizia |
| parole/secondo | allineamento rotto, dettatura assurda |
| quota di segmenti ripetuti | l'ASR che "si arrende" e ripete un nucleo |

Regola: si cambia variante solo se una delle due è sana e l'altra no, o
se il guadagno supera un margine. A parità si tiene l'originale, che è
il minormale. La scelta e i numeri che l'hanno prodotta finiscono in
`denoise_decision.json`: le soglie si possono ritoccare vedendo i dati
reali invece di indovinarli.

`afftdn` è già in ffmpeg, quindi nessuna dipendenza nuova. Se il fruscio
diventa cattivo (ventola, traffico) il passo successivo è DeepFilterNet,
non un modello dentro ffmpeg.

## Il corpus: dove finisce il materiale testuale

Due destinazioni, con due ruoli diversi.

**`publish_corpus.py`** pubblica su una repo GitHub **privata** il
materiale che un LLM deve poter leggere: transcript, segmenti, token,
frequenze, markdown di analisi, con `INDEX.md` come punto d'ingresso.

```bash
python publish_corpus.py init     # clona la repo privata in locale
python publish_corpus.py push     # pubblica le sessioni nuove
python publish_corpus.py status   # cosa c'è e cosa manca
```

Sulla repo **non** finiscono mai: audio, embedding vocali, il mapping
`GLOBAL_00x → nome reale`, i database locali, i checkpoint. Non è una
scelta di comodità: testo, prosodia e statistiche parlarie insieme
ricostruiscono un profilo che nessun file rivela da solo. Tenendo i
nomi fuori, un accesso alla repo non dà l'identità.

**`core/corpus_db.py`** tiene un SQLite **locale** che fa ciò che git
non sa fare: aggregare mesi di dati in una query. Lo schema è pensato
per le analisi che farai dopo, non per quelle di oggi.

```python
from core.corpus_db import CorpusDB
with CorpusDB() as db:
    db.query("SELECT word, SUM(freq) c FROM wordfreq GROUP BY word ORDER BY c DESC LIMIT 20")
    db.query("SELECT date, kind, summary FROM analyses ORDER BY date DESC")
```

Tabelle: `sessions`, `segments` (con le feature prosodiche come colonne
proprie, non dentro un JSON), `tokens` (forma originale **e** normalizzata:
in italiano la maiuscola dopo il punto è informazione), `wordfreq`,
`bigrams`, `corpora` (testi di riferimento), `analyses` (una riga per
data e tipo di analisi, con la sintesi in una riga: è il punto in cui
le analisi future diventano confrontabili nel tempo).

L'ingestione è idempotente: rielaborare una sessione sostituisce i dati
invece di duplicarli.

## Struttura output

Per ogni file `input/registrazione.mp3` viene creata la cartella `output/registrazione/`:

```
output/registrazione/
├── transcript.json      # struttura completa (testo + speaker + prosodia)
├── transcript.txt       # testo leggibile con etichette speaker
├── transcript.srt       # sottotitoli SRT
├── prosody.csv          # metadati prosodici in formato tabulare
├── session.json         # metadata sessione, durate, statistiche per speaker
├── segments.jsonl       # un segmento per riga (JSONL) — ingest LLM/analisi
├── tokens.jsonl         # una parola per riga con timestamp — KWIC, n-grammi
├── wordfreq.csv         # frequenze parole per speaker
├── analysis_ready.md    # testo chunked pronto per un LLM
├── speaker_profiles.json# profilo aggregato delle voci globali (senza vettori)
└── registrazione.checkpoint.json  # stato avanzamento (ripresa automatica)
```

### Identità vocali cross-file (speaker ID)

`pyannote` etichetta gli speaker con ID locali (`SPEAKER_00`) che valgono
solo per una sessione: la stessa persona può avere label diversi in due
file diversi. La pipeline risolve il problema con gli **embedding vocali**
che pyannote calcola già per il clustering (nessun costo aggiuntivo):
ogni voce viene confrontata per similarità coseno con un database
persistente e associata a un ID globale stabile (`GLOBAL_001`).

```
data/speakers_db.json   # database delle voci (dato biometrico, gitignored)
```

```python
from core.speaker_db import SpeakerDB
db = SpeakerDB()
db.set_name("GLOBAL_001", "Pietro")   # etichetta manuale
print(db.profiles())                  # ore parlate, sessioni, date
```

Nei file di output i segmenti riportano `speaker` (ID globale),
`speaker_local` (ID della sessione) e `speaker_names` (nome umano se
assegnato). La soglia di match è `match_threshold` in `core/config.py`
(default 0.78): più alta = più conservativo. Sopra la soglia una voce
è considerata nuova persona e nasce un ID nuovo.

> `data/speakers_db.json` contiene embedding vocali, che sono
> identificatori biometrici: resta in locale e non va nel repo. Se lo
> perdi non si perde nulla — si ricostruisce riprocessando i file.### Test

```bash
python tests/test_speaker_db.py        # matching cross-file delle voci
python tests/test_device_pipeline.py   # device, denoise, corpus, archivio
```

Entrambi girano con embedding e file sintetici: nessun modello, nessun
audio, nessuna rete, pochi secondi.

### Esempio transcript.txt

```
[00:00:04 → 00:00:12] SPEAKER_00
Buongiorno a tutti, oggi parleremo del progetto...

[00:00:12 → 00:00:28] SPEAKER_01
Grazie per l'introduzione. Come dicevo ieri...
```

### Metadati prosodici (prosody.csv)

| Colonna | Descrizione |
|---|---|
| `f0_mean_hz` | Pitch medio (Hz) — tono della voce |
| `f0_range_hz` | Range intonativo — espressività |
| `f0_std_hz` | Variabilità del pitch |
| `intensity_mean_db` | Intensità media (dB) |
| `voiced_fraction` | % audio vocalizzato |
| `jitter_local` | Variabilità ciclo-per-ciclo pitch (stress vocale) |
| `shimmer_local` | Variabilità ampiezza (affaticamento vocale) |
| `speech_rate_syl_per_sec` | Sillabe/secondo (velocità del parlato) |
| `pause_ratio` | % silenzio nel segmento |

---

## Configurazione avanzata

Tutti i parametri sono in `core/config.py`:

```python
# Cambia modello ASR
cfg.asr.model_id = "mlx-community/whisper-large-v3-mlx-q8"  # più accurato, 3GB

# Forza numero di speaker
cfg.diarization.num_speakers = 3

# Più worker CPU per prosodia (se hai core liberi)
cfg.prosody.num_workers = 6

# Timeout notturno: 3 ore invece di 2.5
cfg.max_runtime_sec = 10800
```

---

## Test: come provarlo senza il registratore

Un solo comando:

```bash
python tests/run_all.py            # test veloci, ~10 secondi
python tests/run_all.py --full     # anche il ciclo completo, ~2 minuti
```

`--full` è quello che conta quando qualcosa è cambiato: costruisce un
**registratore finto** e ci fa girare la catena vera. L'audio non è un
beep — è voce italiana vera, sintetizzata con `say` e poi sporcata di
rumore, perché un beep non attraversa VAD, diarizzazione e prosodia
come una voce e un test fatto di beep passerebbe senza provare niente.

Cosa viene provato, in sette passi: il device viene riconosciuto, i nomi
sporchi respinti (`untitled.mp3`, `00000001_000000.MP3`, `99999932`),
il piano a secco non tocca nulla, un file attraversa tutta la catena e
viene cancellato **solo dopo** la verifica, l'orario di registrazione
finisce in `session.json`, la spazzatura viene lasciata stare, due file
con lo stesso orario vengono entrambi trascritti, e rilanciare non
rifà il lavoro già fatto.

Tutto avviene in una directory temporanea: `A2T_ROOT_DIR` sposta output,
database e log, quindi i test non toccano la produzione.

Per guardare dentro senza eseguire:

```bash
python tests/make_fake_device.py --out /tmp/rec --count 5 --minutes 1 --edge
python sync_device.py detect --mounts /tmp
python sync_device.py pull --source /tmp/rec --dry-run
```

I file veri del registratore si provano con un estratto breve, così si
vede la qualità della trascrizione senza aspettare un'ora di elaborazione:

```bash
ffmpeg -i input/2026-10-02_22-44-20.MP3 -t 180 /tmp/rec/record/2026-10-02_19-42-33.MP3
python sync_device.py pull --source /tmp/rec
```

---

## Struttura del codice

```
audio-to-text/
├── run.py                  # entrypoint CLI della pipeline
├── nightly.py              # ciclo notturno: importa, elabora, pubblica
├── sync_device.py          # import dal registratore + cancellazione sicura
├── publish_corpus.py       # pubblicazione sulla repo privata del corpus
├── setup_env.sh            # installa dipendenze
├── setup_launchd.py        # scheduling notturno macOS
├── core/
│   ├── config.py           # tutti i parametri
│   ├── checkpoint.py       # persistenza stato per ripresa
│   ├── device.py           # rilevamento registratore e orario nei nomi file
│   ├── speaker_db.py       # identità vocali persistenti cross-file
│   └── corpus_db.py        # indice SQLite locale per le analisi
├── pipeline/
│   ├── vad.py              # Voice Activity Detection
│   ├── transcriber.py      # ASR (mlx-whisper / faster-whisper)
│   ├── diarizer.py         # diarizzazione speaker (pyannote)
│   ├── denoise.py          # pulizia frusci + scelta automatica variante
│   ├── prosody.py          # analisi prosodia (Parselmouth + librosa)
│   └── assembler.py        # assemblaggio output (JSON/TXT/SRT/CSV/JSONL/MD)
├── tests/                  # test + generatore di registratore finto
│   ├── run_all.py          # un comando per eseguire tutto
│   ├── make_fake_device.py # crea un registratore finto, con voce vera
│   ├── test_e2e.py         # ciclo completo su device finto
│   └── test_nightly.py     # piano, budget, coda
├── input/                  # metti qui i file audio/video
├── output/                 # risultati
├── archive/                # originali in attesa di purga (7 giorni)
├── data/                   # database voci e corpus (biometrico, gitignored)
└── logs/                   # log di esecuzione e manifest del device
```

---

## Risoluzione problemi

**"mlx_whisper non trovato"**  
→ Esegui `./setup_env.sh`

**"Token Hugging Face non trovato"**  
→ Vedi sezione "Token Hugging Face" sopra  
→ Oppure usa `--no-diarization` per saltare la diarizzazione

**"ffmpeg non trovato"**  
→ `brew install ffmpeg`

**Il job notturno non si avvia**  
→ `python setup_launchd.py status` per vedere i log  
→ Verifica che il Mac non sia in modalità "Non disturbare" con blocco accesso disco

**Vuoi riprendere da dove si era fermato**  
→ La pipeline riprende automaticamente: riesegui lo stesso comando

---

## Licenze

- [mlx-whisper](https://github.com/ml-explore/mlx-examples) — MIT
- [faster-whisper](https://github.com/SYSTRAN/faster-whisper) — MIT
- [pyannote.audio](https://github.com/pyannote/pyannote-audio) — MIT
- [praat-parselmouth](https://github.com/YannickJadoul/Parselmouth) — GPL-3.0
- [librosa](https://librosa.org) — ISC
- [whisper](https://github.com/openai/whisper) — MIT (modello OpenAI)
