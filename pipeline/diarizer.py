"""
Diarizer — Speaker Diarization via pyannote.audio

Risponde alla domanda "chi parla quando?" su tutto il file audio.
Usa pyannote/speaker-diarization-3.1 (MIT license, gratuito, locale).

Requisiti:
  - Token Hugging Face gratuito (vedi README — serve una volta sola per
    accettare i termini d'uso; poi il modello gira offline)
  - pyannote-audio >= 3.0 (già nel venv whisperx_env: 4.0.7)
  - PyTorch con MPS (già disponibile: torch 2.8.0 su M1 Pro)

Output per segmento:
    {"speaker": "SPEAKER_00", "start": 12.4, "end": 18.7}

Strategia per file lunghi (18-20 ore):
  pyannote lavora sull'intero file in batch interni; su M1 Pro con MPS
  processa ~5-8x realtime. Su 10 ore di parlato effettivo: ~75-120 min.
  Per rientrare nel budget notturno, la diarizzazione gira in parallelo
  con la fase di prosodia (dopo l'ASR), non con l'ASR stesso.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class Diarizer:
    """
    Wrapper attorno a pyannote.audio per la diarizzazione speaker.

    Uso:
        diarizer = Diarizer(config.diarization)
        segments = diarizer.diarize(wav_path)
        # segments: [{"speaker": "SPEAKER_00", "start": 0.5, "end": 4.2}, ...]
    """

    def __init__(self, diarization_config) -> None:
        self.cfg = diarization_config
        self._pipeline = None  # lazy load

    # ------------------------------------------------------------------
    # Caricamento pipeline pyannote (lazy)
    # ------------------------------------------------------------------

    def _load_pipeline(self) -> None:
        if self._pipeline is not None:
            return

        if not self.cfg.hf_token:
            raise RuntimeError(
                "Token Hugging Face non trovato.\n"
                "Segui le istruzioni nel README per ottenerlo gratuitamente:\n"
                "  python setup_env.sh  →  sezione 'Hugging Face token'"
            )

        try:
            from pyannote.audio import Pipeline
            import torch
        except ImportError as exc:
            raise ImportError(
                f"pyannote.audio non disponibile: {exc}\n"
                "Assicurati di usare il venv whisperx_env."
            ) from exc

        logger.info(
            "Caricamento pipeline pyannote: %s ...", self.cfg.model_id
        )
        t0 = time.time()

        # pyannote-audio 4.x con speaker-diarization-3.1:
        # from_pretrained risolve automaticamente le dipendenze del config.yaml.
        # Se il caricamento fallisce per repo gated non necessari, ricade su
        # un'istanza manuale con i modelli esplicitamente specificati.
        try:
            self._pipeline = Pipeline.from_pretrained(
                self.cfg.model_id,
                token=self.cfg.hf_token,
            )
        except Exception as first_err:
            logger.warning(
                "Caricamento diretto fallito (%s), provo caricamento esplicito...",
                type(first_err).__name__,
            )
            self._pipeline = self._load_pipeline_explicit(torch)

        # Sposta su MPS (GPU Apple Silicon) se disponibile
        device = self._resolve_device()
        self._pipeline.to(torch.device(device))

        logger.info(
            "Pipeline pyannote caricata in %.1fs [device=%s]",
            time.time() - t0, device,
        )

    def _load_pipeline_explicit(self, torch):
        """
        Carica speaker-diarization-3.1 costruendo la pipeline manualmente
        con i modelli esplicitamente specificati nel config.yaml,
        evitando dipendenze da repo non autorizzati (es. community-1).
        """
        from pyannote.audio import Pipeline
        from pyannote.audio.pipelines import SpeakerDiarization
        from pyannote.audio.pipelines.utils.getter import get_model

        token = self.cfg.hf_token

        logger.info("Caricamento esplicito: segmentation + embedding separati")

        pipeline = SpeakerDiarization(
            segmentation="pyannote/segmentation-3.0",
            embedding="pyannote/wespeaker-voxceleb-resnet34-LM",
            clustering="AgglomerativeClustering",
            segmentation_batch_size=32,
            embedding_batch_size=32,
            embedding_exclude_overlap=True,
        )

        # Carica i pesi dei sotto-modelli
        pipeline._segmentation.model_ = get_model(
            "pyannote/segmentation-3.0", token=token
        )
        pipeline._embedding = get_model(
            "pyannote/wespeaker-voxceleb-resnet34-LM", token=token
        )

        # Imposta i parametri di clustering da 3.1
        pipeline.instantiate({
            "segmentation": {"min_duration_off": 0.0},
            "clustering": {
                "method": "centroid",
                "min_cluster_size": 12,
                "threshold": 0.7045654963945799,
            },
        })

        return pipeline

    def _resolve_device(self) -> str:
        """Sceglie il device: mps → cpu come fallback."""
        try:
            import torch
            if self.cfg.device == "mps" and torch.backends.mps.is_available():
                return "mps"
        except Exception:
            pass
        if self.cfg.device != "cpu":
            logger.warning(
                "Device '%s' non disponibile, fallback a CPU", self.cfg.device
            )
        return "cpu"

    # ------------------------------------------------------------------
    # Diarizzazione
    # ------------------------------------------------------------------

    def diarize(self, wav_path: Path) -> list[dict[str, Any]]:
        """
        Esegue la diarizzazione sull'intero file WAV.

        Args:
            wav_path: percorso al WAV 16kHz mono prodotto dal VAD

        Returns:
            Lista di segmenti ordinata per start:
            [{"speaker": "SPEAKER_00", "start": 0.5, "end": 4.2}, ...]
        """
        self._load_pipeline()
        wav_path = Path(wav_path)

        logger.info("Diarizzazione: %s ...", wav_path.name)
        t0 = time.time()

        # Parametri speaker
        kwargs: dict[str, Any] = {}
        if self.cfg.num_speakers is not None:
            kwargs["num_speakers"] = self.cfg.num_speakers
        else:
            kwargs["min_speakers"] = self.cfg.min_speakers
            kwargs["max_speakers"] = self.cfg.max_speakers

        diarization = self._pipeline(str(wav_path), **kwargs)

        elapsed = time.time() - t0
        segments = self._to_segments(diarization)

        speakers = {s["speaker"] for s in segments}
        logger.info(
            "Diarizzazione completata in %.1fs | %d segmenti | %d speaker: %s",
            elapsed,
            len(segments),
            len(speakers),
            sorted(speakers),
        )

        return segments

    @staticmethod
    def _to_segments(diarization) -> list[dict[str, Any]]:
        """Converte l'oggetto Annotation di pyannote in lista di dict."""
        segments = []
        for turn, _, speaker in diarization.itertracks(yield_label=True):
            segments.append({
                "speaker": speaker,
                "start":   round(turn.start, 3),
                "end":     round(turn.end,   3),
            })
        # Ordina per start (pyannote li restituisce già ordinati, ma è sicuro)
        segments.sort(key=lambda s: s["start"])
        return segments

    # ------------------------------------------------------------------
    # Assegnazione speaker ai chunk ASR
    # ------------------------------------------------------------------

    @staticmethod
    def assign_speakers(
        asr_chunks: list[dict[str, Any]],
        diarization_segments: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """
        Arricchisce ogni chunk ASR con il label dello speaker dominante
        nell'intervallo temporale del chunk.

        Per ogni chunk calcola quale speaker copre più tempo nell'intervallo
        [chunk.start, chunk.end] — strategia "majority vote" temporale.

        Restituisce i chunk con chiave aggiuntiva "speaker".
        """
        enriched = []
        for chunk in asr_chunks:
            speaker = _dominant_speaker(
                chunk["start"], chunk["end"], diarization_segments
            )
            enriched.append({**chunk, "speaker": speaker})
        return enriched

    @staticmethod
    def assign_speakers_word_level(
        asr_chunks: list[dict[str, Any]],
        diarization_segments: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """
        Versione più precisa: assegna lo speaker a ogni singola parola
        (usa i word-level timestamps prodotti dal Transcriber).

        Utile per transcript con speaker change dentro lo stesso chunk.
        """
        enriched_chunks = []
        for chunk in asr_chunks:
            enriched_words = []
            for word in chunk.get("words", []):
                w_mid = (word["start"] + word["end"]) / 2.0
                speaker = _speaker_at(w_mid, diarization_segments)
                enriched_words.append({**word, "speaker": speaker})

            # Speaker dominante nel chunk = speaker più frequente nelle parole
            if enriched_words:
                from collections import Counter
                chunk_speaker = Counter(
                    w["speaker"] for w in enriched_words
                ).most_common(1)[0][0]
            else:
                chunk_speaker = _dominant_speaker(
                    chunk["start"], chunk["end"], diarization_segments
                )

            enriched_chunks.append({
                **chunk,
                "speaker": chunk_speaker,
                "words": enriched_words,
            })

        return enriched_chunks


# ---------------------------------------------------------------------------
# Utility interne
# ---------------------------------------------------------------------------

def _dominant_speaker(
    start: float,
    end: float,
    segments: list[dict[str, Any]],
) -> str:
    """
    Trova lo speaker che copre più tempo nell'intervallo [start, end].
    Restituisce "UNKNOWN" se nessun segmento si sovrappone.
    """
    overlap: dict[str, float] = {}
    for seg in segments:
        # Intersezione tra [start,end] e [seg.start, seg.end]
        ov_start = max(start, seg["start"])
        ov_end   = min(end,   seg["end"])
        ov = ov_end - ov_start
        if ov > 0:
            overlap[seg["speaker"]] = overlap.get(seg["speaker"], 0.0) + ov

    if not overlap:
        return "UNKNOWN"
    return max(overlap, key=overlap.__getitem__)


def _speaker_at(time_point: float, segments: list[dict[str, Any]]) -> str:
    """
    Trova lo speaker attivo a un dato istante temporale.
    In caso di ambiguità (overlap pyannote) prende il primo match.
    """
    for seg in segments:
        if seg["start"] <= time_point <= seg["end"]:
            return seg["speaker"]
    return "UNKNOWN"
