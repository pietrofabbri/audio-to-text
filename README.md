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
~/Desktop/Titoli\ Fabbri/whisperx_env/bin/huggingface-cli login
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

## Struttura output

Per ogni file `input/registrazione.mp3` viene creata la cartella `output/registrazione/`:

```
output/registrazione/
├── transcript.json     # struttura completa (testo + speaker + prosodia)
├── transcript.txt      # testo leggibile con etichette speaker
├── transcript.srt      # sottotitoli SRT
├── prosody.csv         # metadati prosodici in formato tabulare
└── registrazione.checkpoint.json  # stato avanzamento (ripresa automatica)
```

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

## Struttura del codice

```
audio-to-text/
├── run.py                  # entrypoint CLI
├── setup_env.sh            # installa dipendenze
├── setup_launchd.py        # scheduling notturno macOS
├── core/
│   ├── config.py           # tutti i parametri
│   └── checkpoint.py       # persistenza stato per ripresa
├── pipeline/
│   ├── vad.py              # Voice Activity Detection
│   ├── transcriber.py      # ASR (mlx-whisper / faster-whisper)
│   ├── diarizer.py         # diarizzazione speaker (pyannote)
│   ├── prosody.py          # analisi prosodia (Parselmouth + librosa)
│   └── assembler.py        # assemblaggio output (JSON/TXT/SRT/CSV)
├── input/                  # metti qui i file audio/video
├── output/                 # risultati
└── logs/                   # log di esecuzione
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
