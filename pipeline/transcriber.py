"""
Transcriber — motore ASR

Supporta due backend intercambiabili:
  - "mlx"    → mlx-whisper (nativo Apple Silicon, GPU unificata, ~20x RT)
  - "faster" → faster-whisper (CTranslate2, CPU INT8, fallback)

Il backend si seleziona in core/config.py (ASRConfig.backend).
La pipeline di chunking lavora sui SpeechSegment prodotti dal VAD:
ogni chunk viene estratto dal WAV, trascritto, e il risultato salvato
nel checkpoint per garantire la ripresa in caso di interruzione.

Anti-hallucination:
  - condition_on_previous_text=False  (no loop di ripetizione)
  - no_speech_threshold filtra chunk residui senza voce
  - temperature fallback disabilitato (mlx) o limitato (faster)
  - rilevamento e deduplicazione di segmenti ripetuti consecutivi
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any

import numpy as np

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Risultato di un singolo chunk
# ---------------------------------------------------------------------------

def _make_chunk_result(
    idx: int,
    start: float,
    end: float,
    text: str,
    language: str,
    words: list[dict] | None = None,
    no_speech_prob: float = 0.0,
    duration_sec: float = 0.0,
) -> dict[str, Any]:
    return {
        "idx": idx,
        "start": start,
        "end": end,
        "text": text.strip(),
        "language": language,
        "words": words or [],
        "no_speech_prob": no_speech_prob,
        "duration_sec": duration_sec,
    }


# ---------------------------------------------------------------------------
# Classe principale
# ---------------------------------------------------------------------------

class Transcriber:
    """
    Trascrive una lista di chunk audio (prodotti da VoiceActivityDetector)
    usando mlx-whisper o faster-whisper.

    Uso:
        transcriber = Transcriber(config.asr)
        results = transcriber.transcribe_chunks(chunks, wav_path, checkpoint)
    """

    def __init__(self, asr_config) -> None:
        self.cfg = asr_config
        self._model = None  # caricato lazy al primo uso

    # ------------------------------------------------------------------
    # Caricamento modello (lazy)
    # ------------------------------------------------------------------

    def _load_model(self) -> None:
        if self._model is not None:
            return

        backend = self.cfg.backend
        logger.info("Caricamento modello ASR [backend=%s] %s ...", backend, self.cfg.model_id)
        t0 = time.time()

        if backend == "mlx":
            self._load_mlx()
        elif backend == "faster":
            self._load_faster()
        else:
            raise ValueError(f"Backend ASR sconosciuto: {backend!r}. Usa 'mlx' o 'faster'.")

        logger.info("Modello caricato in %.1fs", time.time() - t0)

    def _load_mlx(self) -> None:
        try:
            import mlx_whisper  # noqa: F401  — verifica disponibilità
            # Il modello mlx-whisper viene caricato implicitamente alla prima
            # chiamata a mlx_whisper.transcribe(); qui memorizziamo solo il ref
            self._model = "mlx_loaded"
            self._backend = "mlx"
        except ImportError as exc:
            raise ImportError(
                "mlx-whisper non installato.\n"
                "Esegui: pip install mlx-whisper\n"
                "oppure imposta ASRConfig.backend = 'faster' in core/config.py"
            ) from exc

    def _load_faster(self) -> None:
        try:
            from faster_whisper import WhisperModel
        except ImportError as exc:
            raise ImportError(
                "faster-whisper non disponibile: attiva il venv whisperx_env"
            ) from exc

        # cpu_threads va passato solo se è un intero: faster-whisper lo
        # inoltra a ctranslate2 come intra_threads, che accetta un int e
        # non un None. Passare None (il default di ASRConfig, cioè
        # "decidi tu") fa fallire la costruzione del modello con un
        # TypeError, e la notte muore sul primo file.
        kwargs = {}
        threads = getattr(self.cfg, "cpu_threads", None)
        if isinstance(threads, int) and threads > 0:
            kwargs["cpu_threads"] = threads

        self._model = WhisperModel(
            self.cfg.model_id,
            device=self.cfg.device,
            compute_type=self.cfg.compute_type,
            **kwargs,
        )
        self._backend = "faster"

    # ------------------------------------------------------------------
    # Trascrizione principale
    # ------------------------------------------------------------------

    def transcribe_chunks(
        self,
        chunks: list[dict],
        wav_path: Path,
        checkpoint,
        save_every: int = 10,
    ) -> list[dict[str, Any]]:
        """
        Trascrive tutti i chunk, saltando quelli già nel checkpoint.

        Args:
            chunks:      lista prodotta da VoiceActivityDetector.split_into_chunks()
            wav_path:    percorso WAV 16kHz mono
            checkpoint:  istanza di core.Checkpoint (per ripresa)
            save_every:  salva il checkpoint ogni N chunk

        Returns:
            Lista di chunk_result ordinata per idx.
        """
        self._load_model()

        done_indices = checkpoint.get_done_chunk_indices()
        pending = [c for c in chunks if c["idx"] not in done_indices]
        total   = len(chunks)

        logger.info(
            "Trascrizione: %d chunk totali, %d già fatti, %d da processare",
            total, len(done_indices), len(pending),
        )

        wav_array = _load_wav(wav_path)
        sr = 16_000

        for i, chunk in enumerate(pending):
            idx   = chunk["idx"]
            start = chunk["start"]
            end   = chunk["end"]

            # Estrai la fetta audio corrispondente
            audio_slice = _slice_audio(wav_array, start, end, sr)

            t0 = time.time()
            result = self._transcribe_slice(audio_slice, start, end, idx)
            elapsed = time.time() - t0

            rtf = (end - start) / elapsed if elapsed > 0 else 0
            logger.debug(
                "Chunk %d/%d [%.1f–%.1fs] → %d chars | RTF=%.1fx | "
                "no_speech=%.2f",
                i + 1 + len(done_indices), total,
                start, end,
                len(result["text"]),
                rtf,
                result["no_speech_prob"],
            )

            # Filtra chunk senza voce residua
            if result["no_speech_prob"] >= self.cfg.no_speech_threshold:
                logger.debug("Chunk %d scartato (no_speech_prob=%.2f)", idx, result["no_speech_prob"])
                # Salviamo comunque un record vuoto per non ri-processare
                result["text"] = ""

            checkpoint.add_chunk_result(result)

            if (i + 1) % save_every == 0:
                checkpoint.save()
                logger.debug("Checkpoint salvato dopo %d chunk", i + 1 + len(done_indices))

        # Salvataggio finale
        checkpoint.save()

        # Combina vecchi + nuovi, ordina per idx
        all_results = sorted(checkpoint.get_all_chunks(), key=lambda r: r["idx"])

        total_speech = sum(r["end"] - r["start"] for r in all_results if r["text"])
        logger.info(
            "Trascrizione completata: %d chunk | parlato netto: %.1f min",
            len(all_results), total_speech / 60,
        )

        return all_results

    # ------------------------------------------------------------------
    # Backend mlx
    # ------------------------------------------------------------------

    def _transcribe_slice_mlx(
        self, audio: np.ndarray, start: float, end: float, idx: int
    ) -> dict[str, Any]:
        import mlx_whisper

        # mlx_whisper.transcribe accetta array numpy float32 normalizzato [-1, 1]
        output = mlx_whisper.transcribe(
            audio,
            path_or_hf_repo=self.cfg.model_id,
            language=self.cfg.language,
            word_timestamps=True,
            # Anti-hallucination
            condition_on_previous_text=self.cfg.condition_on_previous_text,
            no_speech_threshold=self.cfg.no_speech_threshold,
            # Temperatura fissa = nessun fallback speculativo
            temperature=0.0,
            verbose=False,
        )

        text = output.get("text", "")
        language = output.get("language", self.cfg.language or "it")

        # Raccoglie word-level timestamps
        words = []
        for seg in output.get("segments", []):
            for w in seg.get("words", []):
                words.append({
                    "word": w.get("word", ""),
                    "start": round(start + w.get("start", 0.0), 3),
                    "end":   round(start + w.get("end",   0.0), 3),
                    "prob":  round(w.get("probability", 0.0), 4),
                })

        # no_speech_prob = media dei segmenti
        segs = output.get("segments", [])
        no_speech_prob = (
            sum(s.get("no_speech_prob", 0.0) for s in segs) / len(segs)
            if segs else 0.0
        )

        # Deduplicazione hallucination (testo ripetuto)
        text = _deduplicate_text(text)

        return _make_chunk_result(
            idx=idx, start=start, end=end,
            text=text, language=language,
            words=words, no_speech_prob=no_speech_prob,
            duration_sec=end - start,
        )

    # ------------------------------------------------------------------
    # Backend faster-whisper
    # ------------------------------------------------------------------

    def _transcribe_slice_faster(
        self, audio: np.ndarray, start: float, end: float, idx: int
    ) -> dict[str, Any]:
        # I timestamp di parola sono ciò che rende il corpus utilizzabile
        # (KWIC, allineamento con la prosodia, sync biometrico), ma sono
        # anche la parte che va in crisi più spesso: su un chunk degenere
        # l'allineamento internally di faster-whisper solleva
        # IndexError e fino a ieri quella eccezione saliva fino a run.py e
        # uccideva l'intero file — quindi, di notte, tutti i file dopo.
        #
        # Il compromesso è: si ritenta senza timestamp di parola e si
        # tiene il testo. Perdere la granularità di un chunk è un danno
        # piccolo e circoscritto; perdere un'ora di registrazione è un
        # danno che nessuno si accorgerebbe di notte.
        try:
            return self._transcribe_slice_faster_impl(
                audio, start, end, idx, word_timestamps=True,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Chunk %d: timestamp di parola falliti (%s: %s), "
                "ritento senza",
                idx, type(exc).__name__, exc,
            )
            return self._transcribe_slice_faster_impl(
                audio, start, end, idx, word_timestamps=False,
            )

    def _transcribe_slice_faster_impl(
        self, audio: np.ndarray, start: float, end: float, idx: int,
        word_timestamps: bool = True,
    ) -> dict[str, Any]:
        segments_iter, info = self._model.transcribe(
            audio,
            language=self.cfg.language,
            word_timestamps=word_timestamps,
            condition_on_previous_text=self.cfg.condition_on_previous_text,
            no_speech_threshold=self.cfg.no_speech_threshold,
            no_repeat_ngram_size=getattr(self.cfg, "no_repeat_ngram_size", 0),
            temperature=0.0,
            vad_filter=False,  # VAD già fatto a monte
        )

        text_parts = []
        words = []
        no_speech_probs = []

        for seg in segments_iter:
            text_parts.append(seg.text)
            no_speech_probs.append(seg.no_speech_prob)
            if seg.words:
                for w in seg.words:
                    words.append({
                        "word": w.word,
                        "start": round(start + w.start, 3),
                        "end":   round(start + w.end,   3),
                        "prob":  round(w.probability, 4),
                    })

        text = _deduplicate_text("".join(text_parts))
        no_speech_prob = (
            sum(no_speech_probs) / len(no_speech_probs) if no_speech_probs else 0.0
        )

        return _make_chunk_result(
            idx=idx, start=start, end=end,
            text=text, language=info.language,
            words=words, no_speech_prob=no_speech_prob,
            duration_sec=end - start,
        )

    def _transcribe_slice(
        self, audio: np.ndarray, start: float, end: float, idx: int
    ) -> dict[str, Any]:
        if self._backend == "mlx":
            return self._transcribe_slice_mlx(audio, start, end, idx)
        else:
            return self._transcribe_slice_faster(audio, start, end, idx)


# ---------------------------------------------------------------------------
# Utility
# ---------------------------------------------------------------------------

def _load_wav(wav_path: Path) -> np.ndarray:
    """Carica WAV 16kHz come array float32 normalizzato [-1, 1]."""
    try:
        import soundfile as sf
    except ImportError as exc:
        raise ImportError("soundfile richiesto: pip install soundfile") from exc

    audio, sr = sf.read(str(wav_path), dtype="float32", always_2d=False)
    if sr != 16_000:
        raise ValueError(f"WAV deve essere 16kHz, trovato {sr}Hz")
    return audio


def _slice_audio(audio: np.ndarray, start: float, end: float, sr: int = 16_000) -> np.ndarray:
    """Ritaglia una fetta di audio tra start e end (secondi)."""
    i_start = max(0, int(start * sr))
    i_end   = min(len(audio), int(end * sr))
    return audio[i_start:i_end]


def _deduplicate_text(text: str) -> str:
    """
    Rimuove ripetizioni consecutive identiche — il classico bug di
    hallucination di Whisper su segmenti silenziosi.

    Esempio:
        "Grazie. Grazie. Grazie. Grazie." → "Grazie."
    """
    if not text:
        return text

    # Spezza per frase (punto, punto esclamativo, punto interrogativo)
    import re
    sentences = re.split(r"(?<=[.!?])\s+", text.strip())

    deduped = []
    prev = None
    repeat_count = 0
    MAX_REPEATS = 2  # tollerata al massimo 1 ripetizione legittima

    for s in sentences:
        s_norm = s.strip().lower()
        if s_norm == prev:
            repeat_count += 1
            if repeat_count < MAX_REPEATS:
                deduped.append(s)
        else:
            deduped.append(s)
            prev = s_norm
            repeat_count = 0

    return " ".join(deduped)
