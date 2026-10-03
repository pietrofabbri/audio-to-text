"""
ProsodyAnalyzer — Analisi prosodia per segmento

Estrae metadati acustici per ogni segmento di parlato usando:
  - Parselmouth (Praat in Python): F0/pitch, intensità, jitter, shimmer
  - Librosa: speech rate stimato, frazione vocalizzata, ZCR

I worker girano in parallelo su CPU (multiprocessing.Pool) mentre
il Transcriber usa la GPU — sovrapposizione che ottimizza i tempi.

Metadati estratti per segmento:
  f0_mean_hz, f0_std_hz, f0_min_hz, f0_max_hz, f0_range_hz
  intensity_mean_db, intensity_max_db
  voiced_fraction          % di frame con F0 rilevato
  jitter_local             variabilità ciclo-per-ciclo pitch
  shimmer_local            variabilità ampiezza
  speech_rate_syl_per_sec  sillabe/secondo (stima da onset energia)
  pause_ratio              % silenzio nel segmento
  duration_sec

Tutti i valori float sono arrotondati a 4 cifre.
Campi impostati a null se il segmento è troppo breve o Parselmouth
non riesce a estrarre il parametro (es. voce su rumore forte).
"""

from __future__ import annotations

import logging
import multiprocessing as mp
from pathlib import Path
from typing import Any

import numpy as np

logger = logging.getLogger(__name__)

# Campionamento WAV atteso
_SR = 16_000


# ---------------------------------------------------------------------------
# Worker (eseguito in processo separato)
# ---------------------------------------------------------------------------

_WORKER_WAV: dict[str, Any] = {}


def _init_prosody_worker(wav_path_str: str, sr: int, cfg_dict: dict) -> None:
    """Carica l'audio una volta per worker, all'avvio della pool.

    E' tutto qui il punto. Prima l'audio finiva dentro ogni singolo task
    (pool.map con una tupla per segmento), e con lo start method "spawn"
    ogni task viene serializzato e spedito attraverso una pipe: 230 MB
    di WAV moltiplicati per 126 segmenti fanno 29 GB di trasferimento
    per una sola ora di registrazione. La prosodia si fermava e non
    ripartiva, senza errori nel log: semplicemente non finiva piu'.

    Il commento che c'era prima diceva «condiviso tramite copy-on-write»,
    ma con spawn non si eredita la memoria del padre: il copy-on-write e'
    una proprieta' di fork, e fork qui non si puo' usare (nel padre e' gia'
    caricato PyTorch, e fork dopo altri thread e' il modo classico di
    prendere un deadlock). La condivisione va fatta per conto nostro:
    ogni worker legge il file una volta e basta.
    """
    import soundfile as sf
    audio, file_sr = sf.read(wav_path_str, dtype="float32", always_2d=False)
    if file_sr != sr:
        raise ValueError(f"WAV deve essere {sr}Hz, trovato {file_sr}Hz")
    _WORKER_WAV["wav"] = audio
    _WORKER_WAV["sr"] = sr
    _WORKER_WAV["cfg"] = cfg_dict


def _analyze_segment_shared(seg: dict[str, Any]) -> dict[str, Any]:
    """Analizza un segmento con l'audio gia' caricato dal worker."""
    return _analyze_segment_worker(
        (seg, _WORKER_WAV["wav"], _WORKER_WAV["sr"], _WORKER_WAV["cfg"])
    )


def _analyze_segment_worker(args: tuple) -> dict[str, Any]:
    """
    Funzione top-level necessaria per multiprocessing (non può essere un metodo).
    args = (segment_dict, wav_array_shared, sr, prosody_config_dict)
    """
    seg, wav_array, sr, cfg_dict = args

    start = seg["start"]
    end   = seg["end"]
    idx   = seg.get("idx", -1)

    duration = end - start
    result: dict[str, Any] = {
        "idx":          idx,
        "start":        start,
        "end":          end,
        "duration_sec": round(duration, 3),
        # Feature inizializzate a None
        "f0_mean_hz":           None,
        "f0_std_hz":            None,
        "f0_min_hz":            None,
        "f0_max_hz":            None,
        "f0_range_hz":          None,
        "intensity_mean_db":    None,
        "intensity_max_db":     None,
        "voiced_fraction":      None,
        "jitter_local":         None,
        "shimmer_local":        None,
        "speech_rate_syl_per_sec": None,
        "pause_ratio":          None,
    }

    if duration < cfg_dict.get("min_segment_duration", 0.5):
        return result

    # Estrai fetta audio
    i_start = max(0, int(start * sr))
    i_end   = min(len(wav_array), int(end * sr))
    audio   = wav_array[i_start:i_end]

    if len(audio) < sr * 0.1:  # meno di 100ms, saltiamo
        return result

    # --- Parselmouth / Praat ---
    if cfg_dict.get("extract_f0") or cfg_dict.get("extract_jitter") or cfg_dict.get("extract_shimmer"):
        try:
            result.update(_extract_praat(audio, sr, cfg_dict))
        except Exception as exc:
            logger.debug("Parselmouth fallito per segmento %d: %s", idx, exc)

    # --- Librosa ---
    if cfg_dict.get("extract_speech_rate") or cfg_dict.get("extract_voiced_fraction"):
        try:
            result.update(_extract_librosa(audio, sr))
        except Exception as exc:
            logger.debug("Librosa fallito per segmento %d: %s", idx, exc)

    return result


def _extract_praat(audio: np.ndarray, sr: int, cfg: dict) -> dict[str, Any]:
    """Estrai F0, intensità, jitter, shimmer via Parselmouth (Praat)."""
    import parselmouth
    from parselmouth.praat import call

    # Converti in oggetto Sound di Praat
    snd = parselmouth.Sound(audio, sampling_frequency=sr)

    out: dict[str, Any] = {}

    # F0 (pitch)
    if cfg.get("extract_f0", True):
        pitch = call(snd, "To Pitch", cfg.get("f0_time_step", 0.01),
                     cfg.get("f0_min_hz", 75.0),
                     cfg.get("f0_max_hz", 500.0))

        f0_values = pitch.selected_array["frequency"]
        f0_voiced = f0_values[f0_values > 0]

        if len(f0_voiced) > 0:
            out["f0_mean_hz"]  = round(float(np.mean(f0_voiced)), 4)
            out["f0_std_hz"]   = round(float(np.std(f0_voiced)),  4)
            out["f0_min_hz"]   = round(float(np.min(f0_voiced)),  4)
            out["f0_max_hz"]   = round(float(np.max(f0_voiced)),  4)
            out["f0_range_hz"] = round(float(np.max(f0_voiced) - np.min(f0_voiced)), 4)
            out["voiced_fraction"] = round(len(f0_voiced) / max(len(f0_values), 1), 4)
        else:
            out["voiced_fraction"] = 0.0

    # Intensità
    if cfg.get("extract_energy", True):
        intensity = call(snd, "To Intensity", 100.0, 0.0, "yes")
        int_values = intensity.values.flatten()
        int_values = int_values[np.isfinite(int_values)]
        if len(int_values) > 0:
            out["intensity_mean_db"] = round(float(np.mean(int_values)), 4)
            out["intensity_max_db"]  = round(float(np.max(int_values)),  4)

    # Jitter (variabilità ciclo-per-ciclo pitch — stress vocale)
    if cfg.get("extract_jitter", True):
        try:
            point_process = call(snd, "To PointProcess (periodic, cc)...",
                                 cfg.get("f0_min_hz", 75.0),
                                 cfg.get("f0_max_hz", 500.0))
            jitter = call(point_process, "Get jitter (local)", 0, 0, 0.0001, 0.02, 1.3)
            if jitter is not None and np.isfinite(jitter):
                out["jitter_local"] = round(float(jitter), 6)
        except Exception:
            pass  # jitter fallisce su segmenti molto brevi

    # Shimmer (variabilità ampiezza — affaticamento vocale)
    if cfg.get("extract_shimmer", True):
        try:
            point_process = call(snd, "To PointProcess (periodic, cc)...",
                                 cfg.get("f0_min_hz", 75.0),
                                 cfg.get("f0_max_hz", 500.0))
            shimmer = call([snd, point_process],
                           "Get shimmer (local)", 0, 0, 0.0001, 0.02, 1.3, 1.6)
            if shimmer is not None and np.isfinite(shimmer):
                out["shimmer_local"] = round(float(shimmer), 6)
        except Exception:
            pass

    return out


def _extract_librosa(audio: np.ndarray, sr: int) -> dict[str, Any]:
    """Estrai speech rate e pause ratio via librosa."""
    try:
        import librosa
    except ImportError:
        return {}

    out: dict[str, Any] = {}
    duration = len(audio) / sr

    # Speech rate stimato: conta gli onset di energia (approssimazione sillabe)
    if len(audio) > 0:
        onset_frames = librosa.onset.onset_detect(
            y=audio, sr=sr, units="time",
            pre_max=1, post_max=1, pre_avg=3, post_avg=3,
            delta=0.07, wait=0.1,
        )
        if duration > 0:
            out["speech_rate_syl_per_sec"] = round(len(onset_frames) / duration, 4)

    # Pause ratio: frazione di frame sotto soglia RMS
    frame_length = int(0.025 * sr)  # 25ms frame
    hop_length   = int(0.010 * sr)  # 10ms hop
    rms = librosa.feature.rms(y=audio, frame_length=frame_length, hop_length=hop_length)[0]
    if len(rms) > 0:
        threshold = 0.01  # ~-40 dBFS
        pause_frames = np.sum(rms < threshold)
        out["pause_ratio"] = round(float(pause_frames) / len(rms), 4)

    return out


# ---------------------------------------------------------------------------
# Classe principale
# ---------------------------------------------------------------------------

class ProsodyAnalyzer:
    """
    Analizza la prosodia di tutti i segmenti in parallelo su CPU.

    I segmenti possono essere i chunk ASR (dopo diarizzazione) oppure
    i raw SpeechSegment dal VAD — in entrambi i casi servono start/end/idx.

    Uso:
        analyzer = ProsodyAnalyzer(config.prosody)
        prosody_data = analyzer.analyze(asr_chunks, wav_path)
    """

    def __init__(self, prosody_config) -> None:
        self.cfg = prosody_config

    def analyze(
        self,
        segments: list[dict[str, Any]],
        wav_path: Path,
    ) -> list[dict[str, Any]]:
        """
        Args:
            segments: lista di dict con almeno {idx, start, end}
            wav_path: WAV 16kHz mono

        Returns:
            Lista di dict con metadati prosodici, ordinata per idx.
        """
        if not segments:
            return []

        # Verifica disponibilità Parselmouth
        _check_parselmouth()

        # Nel percorso sequenziale l'audio si carica qui, una volta sola.
        # In quello parallelo si carica in ogni worker, una volta sola
        # ciascuno: vedi _init_prosody_worker.
        wav_array = None

        cfg_dict = {
            "min_segment_duration": self.cfg.min_segment_duration,
            "f0_min_hz":    self.cfg.f0_min_hz,
            "f0_max_hz":    self.cfg.f0_max_hz,
            "f0_time_step": self.cfg.f0_time_step,
            "extract_f0":       self.cfg.extract_f0,
            "extract_energy":   self.cfg.extract_energy,
            "extract_speech_rate": self.cfg.extract_speech_rate,
            "extract_jitter":   self.cfg.extract_jitter,
            "extract_shimmer":  self.cfg.extract_shimmer,
            "extract_voiced_fraction": self.cfg.extract_voiced_fraction,
        }

        num_workers = min(self.cfg.num_workers, len(segments), mp.cpu_count())

        # Usa multiprocessing solo se vale la pena:
        # almeno 20 segmenti, perche' su un file corto il costo di avviare
        # i worker supera quello di analizzare tutto in fila.
        use_mp = num_workers > 1 and len(segments) >= 20

        # L'audio si carica qui solo se serve a questo processo. In
        # parallelo non serve a niente nel padre (non lo vedono, i worker)
        # e caricarlo significherebbe tenerlo in memoria due volte: una
        # copia per worker si fa caricare a loro, nel loro inizializzatore.
        wav_array = None if use_mp else _load_wav(wav_path)

        args_list = [
            (seg, wav_array, _SR, cfg_dict)
            for seg in segments
        ]
        # Quello che viaggia verso i worker: i soli segmenti. L'audio non
        # passa di qui, e questa e' la riga dove il difetto sarebbe
        # stato evidente (vedi tests/test_prosody_workers.py).
        parallel_payload = list(segments)

        if use_mp:
            logger.info(
                "Prosodia: %d segmenti | %d worker CPU paralleli (spawn)",
                len(segments), num_workers,
            )
            ctx = mp.get_context("spawn")
            with ctx.Pool(
                processes=num_workers,
                initializer=_init_prosody_worker,
                initargs=(str(wav_path), _SR, cfg_dict),
            ) as pool:
                try:
                    results = pool.map(
                        _analyze_segment_shared, parallel_payload,
                    )
                except KeyboardInterrupt:
                    logger.warning("Prosodia interrotta — termino worker pool")
                    pool.terminate()
                    pool.join()
                    raise
        else:
            if len(segments) < 20:
                logger.info(
                    "Prosodia: %d segmenti | elaborazione sequenziale (file corto)",
                    len(segments),
                )
            else:
                logger.info(
                    "Prosodia: %d segmenti | elaborazione sequenziale",
                    len(segments),
                )
            results = [_analyze_segment_worker(a) for a in args_list]

        results.sort(key=lambda r: r.get("idx", 0))

        voiced_count = sum(1 for r in results if r.get("f0_mean_hz") is not None)
        logger.info(
            "Prosodia completata: %d/%d segmenti con F0 estratto",
            voiced_count, len(results),
        )

        return results


# ---------------------------------------------------------------------------
# Utility
# ---------------------------------------------------------------------------

def _load_wav(wav_path: Path) -> np.ndarray:
    try:
        import soundfile as sf
    except ImportError as exc:
        raise ImportError("soundfile richiesto: pip install soundfile") from exc
    audio, sr = sf.read(str(wav_path), dtype="float32", always_2d=False)
    if sr != _SR:
        raise ValueError(f"WAV deve essere 16kHz, trovato {sr}Hz")
    return audio


def _check_parselmouth() -> None:
    try:
        import parselmouth  # noqa: F401
    except ImportError as exc:
        raise ImportError(
            "parselmouth non installato.\n"
            "Esegui: pip install praat-parselmouth\n"
            "(oppure esegui setup_env.sh che lo installa automaticamente)"
        ) from exc
