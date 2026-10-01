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

ROOT_DIR   = Path(__file__).resolve().parent.parent
INPUT_DIR  = ROOT_DIR / "input"
OUTPUT_DIR = ROOT_DIR / "output"
LOGS_DIR   = ROOT_DIR / "logs"

# venv con faster-whisper / whisperx / pyannote già installati
VENV_PYTHON = Path.home() / "Desktop" / "Titoli Fabbri" / "whisperx_env" / "bin" / "python"


# ---------------------------------------------------------------------------
# ASR — backend e modello
# ---------------------------------------------------------------------------

@dataclass
class ASRConfig:
    # Backend: "mlx" (nativo Apple Silicon, più veloce) oppure "faster" (fallback)
    backend: str = "mlx"

    # Modello Whisper
    # mlx:     "mlx-community/whisper-large-v3-turbo-8bit"  (~900 MB, consigliato)
    #          "mlx-community/whisper-large-v3-turbo"        (~1.6 GB, fp16)
    #          "mlx-community/whisper-large-v3-mlx"          (~3 GB, massima qualità)
    #          "mlx-community/whisper-large-v3-turbo-4bit"   (~500 MB, più veloce)
    # faster:  "large-v3-turbo"  /  "large-v3"  /  "small"
    model_id: str = "mlx-community/whisper-large-v3-turbo-8bit"

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

    # Quantizzazione per faster-whisper (ignorato da mlx)
    compute_type: str = "int8"

    # Device per faster-whisper: "cpu" | "cuda" | "mps"  (ignorato da mlx)
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
    """Legge il token HF dal path standard di huggingface-cli."""
    candidates = [
        Path.home() / ".huggingface" / "token",
        Path.home() / ".cache" / "huggingface" / "token",
    ]
    for p in candidates:
        if p.exists():
            return p.read_text().strip()
    return ""


# ---------------------------------------------------------------------------
# Prosodia — analisi acustica per segmento
# ---------------------------------------------------------------------------

@dataclass
class ProsodyConfig:
    # Numero di processi CPU paralleli per l'estrazione prosodica
    # Su M1 Pro (6 perf core): 4 è sicuro mentre GPU lavora sull'ASR
    num_workers: int = 4

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

    # Indentazione JSON (None = compatto, 2 = leggibile)
    json_indent: int = 2

    # Includi nel JSON i token-level timestamps (aumenta dimensione file)
    include_word_timestamps: bool = True


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
