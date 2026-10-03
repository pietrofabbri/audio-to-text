"""
Configurazione centralizzata della pipeline audio-to-text.
Modifica qui i parametri senza toccare il codice della pipeline.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

# ---------------------------------------------------------------------------
# Percorsi base
# ---------------------------------------------------------------------------

# Tutto ciò che la pipeline scrive (input, output, log, database, archivio)
# sta sotto questa radice. A2T_ROOT_DIR la sposta altrove: è ciò che
# permette a un test end-to-end di girare sulla catena vera senza
# mescolare le sue sessioni finte a quelle vere. Il default è la directory
# del progetto, quindi il comportamento normale non cambia.
ROOT_DIR   = Path(os.environ.get("A2T_ROOT_DIR") or Path(__file__).resolve().parent.parent)
INPUT_DIR  = ROOT_DIR / "input"
OUTPUT_DIR = ROOT_DIR / "output"
LOGS_DIR   = ROOT_DIR / "logs"

# venv con faster-whisper / whisperx / pyannote già installati
VENV_PYTHON = Path.home() / "Desktop" / "Titoli Fabbri" / "whisperx_env" / "bin" / "python"

# ---------------------------------------------------------------------------
# Pianificazione — l'unico posto dove la finestra notturna è scritta
# ---------------------------------------------------------------------------
# Prima questi numeri vivevano in tre file (setup_launchd.py, nightly.py,
# README) e due non concordavano: lanciando nightly.py a mano la finestra
# era più corta di quella del job launchd. Il numero che conta è "quanto
# tempo ha la notte", quindi sta qui, e nightly.py e setup_launchd.py lo
# leggono. Cambiarlo qui cambia ovunque, che è il punto.

# Notte: pieno regime. La macchina è libera e serve.
NIGHT_START_HOUR   = 2
NIGHT_START_MINUTE = 0
NIGHT_WINDOW_SEC   = 4 * 3600      # 02:00 → 06:00
NIGHT_NICE         = 10            # priorità sotto la normale
NIGHT_THREADS      = 4             # i core performance: vedi core/cost.py
NIGHT_COOLDOWN_SEC = 90            # pausa di respiro fra un file e il successivo

# Passate diurne: processo leggero in background, tre volte al giorno.
# Non sono un secondo ciclo completo: sono lo stesso ciclo con un budget
# piccolo, pensato per non lasciare la coda ferma otto ore senza pero'
# rubare la macchina a chi la sta usando.
DAYTIME_HOURS       = (9, 15, 21)
DAYTIME_BUDGET_SEC  = 40 * 60
DAYTIME_THREADS     = 3
DAYTIME_NICE        = 15
DAYTIME_COOLDOWN_SEC = 30

# I thread non sono una scelta di compromesso fra velocità e calore:
# sono una misura. Sulla M1 Pro, 4 thread danno un ASR più VELOCE di 8
# (3,86x realtime contro 3,11x sul parlato reale), perché gli altri 4
# core sono efficiency: non aggiungono lavoro, aggiungono contesa e
# consumo termico. Sotto 4 il tempo peggiora davvero.
#
# Perciò il tetto è dichiarato qui e verificato da un test: nessuno
# deve "mettere più thread perché tanto la macchina è libera" senza
# sapere che sta rendendo più lento il lavoro e più caldo il portatile.
MAX_THREADS = 4


# ---------------------------------------------------------------------------
# ASR — backend e modello
# ---------------------------------------------------------------------------

@dataclass
class ASRConfig:
    # Backend: "faster" (CTranslate2, CPU INT8, stabile) oppure "mlx" (GPU nativa M1)
    # NOTA: mlx e PyTorch/faster-whisper non coesistono nello stesso processo su macOS
    # (conflitto MPS → segfault). Usa "faster" per stabilità, "mlx" solo se usi
    # un processo separato (futuro: modalità subprocess isolato).
    backend: str = "faster"

    # Modello Whisper
    # faster:  "large-v3-turbo"  (~1.5 GB, consigliato — ottimo italiano, CPU INT8)
    #          "large-v3"        (~3 GB, massima qualità)
    #          "small"           (~244 MB, test rapido)
    # mlx (solo processo isolato):
    #          "mlx-community/whisper-large-v3-turbo"       (~1.6 GB, fp16)
    #          "mlx-community/whisper-large-v3-turbo-4bit"  (~500 MB)
    #          "mlx-community/whisper-large-v3-mlx"         (~3 GB)
    model_id: str = field(
        default_factory=lambda: os.environ.get("A2T_ASR_MODEL", "large-v3-turbo")
    )

    # Lingua forzata (None = auto-detect)
    language: str = "it"

    # Dimensione batch per mlx-whisper (chunk processati insieme)
    # Su M1 Pro 16 GB: 4–8 è sicuro, 12 rischia OOM con large-v3
    batch_size: int = 6

    # Lunghezza massima di ogni chunk audio inviato all'ASR (secondi)
    # Whisper lavora su finestre di 30s; chunk più lunghi = VAD li spezza prima
    chunk_max_sec: float = 29.0

    # Soglia no-speech per scartare chunk silenzioso residuo dopo VAD
    # (0.0–1.0; 0.6 è conservativo, abbassa se perdi parlato)
    no_speech_threshold: float = 0.6

    # Disabilita la condizionatura sul testo precedente per evitare
    # hallucination loop su audio lungo
    condition_on_previous_text: bool = False

    # Vietta la ripetizione esatta di 6 parole consecutive. Su una
    # registrazione reale (misurato: 40% delle parole erano un loop
    # "ma tu non vado a fare il bambino" ripetuto 22 volte) porta i
    # segmenti in loop da 40% a 0%.
    #
    # La finestra di 6 parole e' volutamente larga: l'italiano ripete
    # volentieri ("va va", "no no", "sì sì") e una finestra stretta
    # taglierebbe la voce vera insieme alla spazzatura. Con 6 si
    # prende il loop e si lascia parlare.
    # 0 = disattivato.
    no_repeat_ngram_size: int = 6

    # Priorita' CPU per faster-whisper (None = scelta della libreria).
    # Le passate diurne la abbassano per non saturare la macchina.
    cpu_threads: int | None = field(
        default_factory=lambda: (
            int(os.environ["A2T_ASR_THREADS"])
            if os.environ.get("A2T_ASR_THREADS") else None
        )
    )

    # Quantizzazione per faster-whisper
    compute_type: str = "int8"

    # Device per faster-whisper: "cpu" è stabile su M1 Pro con CTranslate2
    # (MPS ha supporto parziale in CTranslate2 e può causare errori)
    device: str = "cpu"


# ---------------------------------------------------------------------------
# VAD — Voice Activity Detection
# ---------------------------------------------------------------------------

@dataclass
class VADConfig:
    # Soglia di confidenza Silero VAD (0.0–1.0)
    # 0.5 = default; abbassare per parlato sommesso, alzare per meno falsi positivi
    threshold: float = 0.5

    # Silenzio minimo (ms) per separare due segmenti di parlato
    min_silence_duration_ms: int = 500

    # Durata minima di un segmento parlato da tenere (ms)
    min_speech_duration_ms: int = 250

    # Padding aggiunto prima e dopo ogni segmento (ms) per non tagliare
    speech_pad_ms: int = 200

    # Sample rate atteso dal VAD
    sample_rate: int = 16_000


# ---------------------------------------------------------------------------
# Diarizzazione — chi parla quando
# ---------------------------------------------------------------------------

@dataclass
class DiarizationConfig:
    # Modello pyannote su HuggingFace
    # Richiede HF_TOKEN (vedi README per come ottenerlo)
    model_id: str = "pyannote/speaker-diarization-3.1"

    # Numero di speaker attesi (None = auto-detect)
    num_speakers: int | None = None

    # Range di speaker possibili (usato se num_speakers è None)
    min_speakers: int = 1
    max_speakers: int = 10

    # Device: "mps" per GPU Apple Silicon, "cpu" come fallback
    device: str = "mps"

    # Token Hugging Face — viene letto dalla variabile d'ambiente HF_TOKEN
    # oppure dal file ~/.huggingface/token (impostato da `huggingface-cli login`)
    hf_token: str = field(
        default_factory=lambda: (
            os.environ.get("HF_TOKEN") or _read_hf_token_from_file()
        )
    )


def _read_hf_token_from_file() -> str:
    """Legge il token HF dai percorsi standard di huggingface-hub / hf CLI."""
    candidates = [
        Path.home() / ".cache" / "huggingface" / "token",  # hf CLI (hub 2.x)
        Path.home() / ".huggingface" / "token",             # vecchio huggingface-cli
    ]
    for p in candidates:
        if p.exists():
            t = p.read_text().strip()
            if t:
                return t
    return ""


# ---------------------------------------------------------------------------
# Prosodia — analisi acustica per segmento
# ---------------------------------------------------------------------------

@dataclass
class ProsodyConfig:
    # Numero di processi CPU paralleli per l'estrazione prosodica.
    # La prosodia gira insieme all'ASR, quindi insieme a lui scalda:
    # con l'ASR già limitato a N thread, la prosodia ne prende un paio.
    # Il default è 2 perché il riscaldamento non è un dettaglio estetico: il
    # portatile sotto un carico prolungato al 100% diventa scomodo da
    # usare di giorno, e la notte e' l'unico momento in cui il lavoro
    # viene fatto.
    num_workers: int = field(
        default_factory=lambda: (
            int(os.environ["A2T_PROSODY_WORKERS"])
            if os.environ.get("A2T_PROSODY_WORKERS") else 2
        )
    )

    # Parametri Praat/Parselmouth per F0 (pitch)
    f0_min_hz: float = 75.0    # Hz — limite inferiore pitch voce umana
    f0_max_hz: float = 500.0   # Hz — limite superiore (soprano ~1000, ma 500 copre tutto il parlato)
    f0_time_step: float = 0.01 # secondi tra campioni F0

    # Feature da estrarre per segmento
    extract_f0: bool = True          # pitch contour (F0 mean/std/min/max/range)
    extract_energy: bool = True      # intensità RMS
    extract_speech_rate: bool = True # sillabe/secondo stimato
    extract_jitter: bool = True      # variabilità ciclo-per-ciclo pitch (qualità voce)
    extract_shimmer: bool = True     # variabilità ampiezza (qualità voce)
    extract_voiced_fraction: bool = True  # % audio vocalizzato nel segmento

    # Durata minima segmento (sec) per estrarre prosodia significativa
    min_segment_duration: float = 0.5


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

@dataclass
class OutputConfig:
    # Formati di output da generare
    write_json: bool = True   # trascrizione + speaker + prosodia completa
    write_txt: bool = True    # testo semplice con speaker label
    write_srt: bool = True    # sottotitoli SRT
    write_csv: bool = True    # metadati prosodici in formato tabulare

    # Formati arricchiti (corpus analysis / ingest LLM)
    write_session_json: bool = True     # session.json: metadata sessione + statistiche speaker
    write_segments_jsonl: bool = True   # segments.jsonl: un segmento per riga
    write_tokens_jsonl: bool = True     # tokens.jsonl: una parola per riga con timestamp
    write_wordfreq_csv: bool = True     # wordfreq.csv: frequenze parole per speaker
    write_analysis_md: bool = True      # analysis_ready.md: testo chunked per LLM

    # Indentazione JSON (None = compatto, 2 = leggibile)
    json_indent: int = 2

    # Includi nel JSON i token-level timestamps (aumenta dimensione file)
    include_word_timestamps: bool = True


# ---------------------------------------------------------------------------
# Speaker ID — identità vocali persistenti cross-file
# ---------------------------------------------------------------------------
# Denoise — pulizia del fruscio e scelta automatica della variante
# ---------------------------------------------------------------------------

@dataclass
class DenoiseConfig:
    # Se False, si trascrive l'audio originale e basta
    enabled: bool = True

    # Confronto automatico originale vs ripulito. Se False si usa sempre
    # la variante ripulita senza misurare.
    compare: bool = True

    # Quanto audio usare per il confronto. Il confronto avviene su un
    # campione di centro, non su tutto il file: con 18 ore a notte una
    # seconda passata ASR completa costerebbe oltre un'ora e farebbe
    # sballare il budget da solo. Su un file da un'ora la qualità
    # dell'audio non cambia di minuto in minuto, quindi la decisione
    # presa su 3 minuti vale per tutto il file.
    sample_sec: float = 180.0

    # afftdn: riduzione del rumore in dB (0-97) e soglia di soppressione.
    # 12/-25 è aggressivo ma tiene la voce; 6/-20 se la voce risulta ovattata.
    nr: int = 12
    nf: int = -25

    # Scrive denoise_decision.json con i numeri che hanno prodotto la scelta,
    # così la decisione è verificabile e le soglie si possono ritoccare
    # vedendo i dati reali invece di indovinarli
    write_decision_json: bool = True


# ---------------------------------------------------------------------------

@dataclass
class SpeakerIDConfig:
    # Se False, i label restano locali alla sessione (SPEAKER_00, ...)
    # e nessun DB viene scritto
    enabled: bool = True

    # DB degli embedding vocali. È un dato biometrico: non va nel repo.
    db_path: Path = ROOT_DIR / "data" / "speakers_db.json"

    # Soglia di similarità coseno per considerare due voci la stessa
    # persona. Più alta = più conservativo (più voci nuove, meno
    # rischi di fondere persone diverse). pyannote clusterizza a 0.7045
    # dentro un singolo file; qui si confrontano sessioni diverse, dove
    # variano voce, rumore e distanza dal microfono, quindi si sale.
    match_threshold: float = 0.78

    # Aggiorna il centroide di ogni voce con i nuovi contributi
    # (media pesata per durata). False = il centroide resta quello della
    # prima sessione in cui la voce è comparsa.
    update_centroid: bool = True

    # Scrivi speaker_profiles.json (profilo aggregato senza vettori)
    # accanto agli output della sessione
    write_profiles_json: bool = True


# ---------------------------------------------------------------------------
# Pipeline globale
# ---------------------------------------------------------------------------

@dataclass
class PipelineConfig:
    asr: ASRConfig = field(default_factory=ASRConfig)
    vad: VADConfig = field(default_factory=VADConfig)
    diarization: DiarizationConfig = field(default_factory=DiarizationConfig)
    prosody: ProsodyConfig = field(default_factory=ProsodyConfig)
    output: OutputConfig = field(default_factory=OutputConfig)
    denoise: DenoiseConfig = field(default_factory=DenoiseConfig)
    speaker_id: SpeakerIDConfig = field(default_factory=SpeakerIDConfig)

    # Estensioni audio/video accettate come input
    accepted_extensions: tuple[str, ...] = (
        ".mp3", ".mp4", ".wav", ".m4a", ".aac",
        ".ogg", ".flac", ".mkv", ".mov", ".avi",
        ".webm", ".wma", ".opus",
    )

    # Salva un checkpoint ogni N chunk ASR processati
    # (permette di riprendere da dove si era rimasti)
    checkpoint_every_n_chunks: int = 10

    # Massimo tempo di esecuzione in secondi (0 = nessun limite)
    # Utile per il job notturno: 7200 = 2 ore, poi si ferma ordinatamente
    max_runtime_sec: int = 0

    # Se True, salta i file che hanno già un output completo in output/
    skip_completed: bool = True


# Istanza di default — importa questa nei moduli della pipeline
config = PipelineConfig()
