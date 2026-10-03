"""
VAD — Voice Activity Detection

Usa il VAD integrato in faster-whisper (Silero VAD via CTranslate2),
che è già installato nel venv whisperx_env senza dipendenze aggiuntive.

Responsabilità:
  1. Convertire il file audio in WAV 16kHz mono via ffmpeg
  2. Eseguire VAD per ottenere la mappa temporale parlato/non-parlato
  3. Restituire una lista di SpeechSegment pronti per l'ASR
  4. Calcolare statistiche (speech_ratio, durata totale, ecc.)

Il VAD è il primo filtro della pipeline: su 20 ore con ~50% silenzio
riduce il carico ASR a ~10 ore effettive, dimezzando i tempi.
"""

from __future__ import annotations

import hashlib
import json
import logging
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.config import ROOT_DIR  # noqa: E402

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Cache WAV: pulizia
# ---------------------------------------------------------------------------
# I WAV derivati (16 kHz mono) sono temporanei per definizione: servono
# solo finche' una sessione e' in corso. Tenuti per sempre occupavano
# ~4 GB al giorno con 18 file da un'ora, e il giorno in cui il disco si
# riempie la notte si ferma a meta' — che e' il modo peggiore in cui puo'
# andare: si scopre solo dai log del mattino dopo.
#
# La regola di cancellazione e' deliberatamente conservativa: si butta
# solo cio' che non serve a nessuna sessione riprendibile. Un checkpoint
# incompleto ha ancora bisogno del suo WAV per riprendere, quindi la
# funzione di pulizia non lo tocca — cancellarlo significherebbe
# ricalcolare da capo il VAD e ripartire dal primo chunk.

def wav_cache_dir() -> Path:
    """Cartella dei WAV derivati. Nessun file sorgente li tocca: sono
    sempre sotto ROOT_DIR, mai accanto all'audio sul registratore."""
    return ROOT_DIR / "data" / "wav_cache"


def _is_wav_16k_mono(path: Path) -> bool:
    """True solo se il file e' gia' un WAV 16 kHz mono a 16 bit.

    Tre condizioni, tutte necessarie, e nessuna viene dedotta dal nome:
    controllare il nome sarebbe indovinare, e il nome del file puo'
    essere quello di una copia vecchia che contiene altro.
    """
    try:
        import soundfile as sf
        info = sf.info(str(path))
    except Exception:  # noqa: BLE001
        # Non si puo' leggere il formato: allora si converte. Meglio una
        # copia inutile che scoprirlo alla fine, quando il VAD ha gia'
        # prodotto segmenti sbagliati.
        return False
    return (
        getattr(info, "subtype", None) == "PCM_16"
        and info.samplerate == 16_000
        and info.channels == 1
    )


def _wav_status() -> tuple[dict[Path, bool], set[str]]:
    """Per ogni WAV citato da un checkpoint: True se la sessione è finita.

    Il caso da distinguere è "sessione completa" (il WAV non serve più, si
    cancella subito) da "sessione a metà" (il WAV serve per riprendere, non
    si tocca). Un checkpoint illeggibile non viene contato come completo:
    non si cancella nulla per un file che non si è riusciti a leggere.
    """
    out_dir = ROOT_DIR / "output"
    status: dict[Path, bool] = {}
    stem_completi: set[str] = set()
    if not out_dir.is_dir():
        return status, stem_completi

    for ck_file in out_dir.glob("*/*.checkpoint.json"):
        try:
            data = json.loads(ck_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        stages = data.get("stages", {}) or {}
        complete = bool(stages) and all((s or {}).get("done") for s in stages.values())
        wav = (stages.get("ffmpeg") or {}).get("wav_path")
        if wav:
            status[Path(wav).resolve()] = complete
        if complete:
            stem_completi.add(ck_file.parent.name)
    return status, stem_completi


def purge_wav_cache(force: bool = False, min_age_sec: float = 6 * 3600) -> tuple[int, int]:
    """Cancella i WAV derivati che non servono più.

    Tre casi, in quest'ordine:

    1. la sessione che li ha prodotti è finita → si cancella subito,
       indipendentemente dall'età. È il caso normale e quello che tiene
       la cache a un file solo;
    2. nessun checkpoint li cita (una run interrotta prima di salvare, un
       test) e sono più vecchi di `min_age_sec` → si cancella: sono
       orfani, e orfani a 115 MB ciascuno sono ciò che riempie un disco;
    3. la sessione è a metà → NON si cancella. Il checkpoint riprende da
       lì, e senza WAV ripartirebbe dal primo chunk.

    Args:
        force: ignora `min_age_sec` e cancella anche gli orfani recenti.
        min_age_sec: età minima per considerare orfano un WAV non citato.

    Returns:
        (n_file_cancellati, byte_liberati)
    """
    cache = wav_cache_dir()
    if not cache.is_dir():
        return 0, 0

    status, stem_completi = _wav_status()
    now = time.time()
    n = 0
    freed = 0

    for p in cache.glob("*.wav"):
        try:
            rp = p.resolve()
            complete = status.get(rp)
            if complete is None and any(
                p.name.startswith(f"{stem}_") for stem in stem_completi
            ):
                # Un derivato della stessa sessione, non citato da nessun
                # checkpoint (la copia che il VAD faceva della variante
                # ripulita, per esempio). Se la sessione e' finita non
                # serve a nessuno, anche se nessun file di metadati lo
                # nomina: aspettare le sei ore dell'orfanato per liberare
                # disco che tanto non tornerera' e' solo una sciagura
                # rimandata.
                complete = True
            if complete is None:
                # Non citato da nessun checkpoint: orfano, ma solo se
                # ha avuto il tempo di diventarlo.
                if not force and (now - p.stat().st_mtime) < min_age_sec:
                    continue
            elif not complete:
                continue                  # sessione a metà: tiene
            size = p.stat().st_size
            p.unlink()
        except OSError as exc:
            # La pulizia non è mai un motivo per fermare la notte: se un
            # file è in uso o il disco è strano, si logga e si va avanti.
            logger.debug("WAV non cancellato (%s): %s", p.name, exc)
            continue
        n += 1
        freed += size

    if n:
        logger.info("Cache WAV: cancellati %d file, %.1f MB liberati", n, freed / 1e6)
    return n, freed


@dataclass
class SpeechSegment:
    """Un segmento di audio contenente parlato, pronto per l'ASR."""
    idx: int
    start: float   # secondi dall'inizio del file originale
    end: float     # secondi dall'inizio del file originale

    @property
    def duration(self) -> float:
        return self.end - self.start

    def __repr__(self) -> str:
        return (
            f"SpeechSegment(idx={self.idx}, "
            f"start={self.start:.2f}s, end={self.end:.2f}s, "
            f"dur={self.duration:.2f}s)"
        )


class VoiceActivityDetector:
    """
    Wrapper attorno al VAD Silero integrato in faster-whisper.

    Usare il VAD di faster-whisper è la scelta a zero-dipendenze aggiuntive:
    lo stesso pacchetto già installato nel venv gestisce internamente il
    modello Silero via ONNX, senza richiedere PyTorch per il VAD.
    """

    def __init__(self, vad_config, asr_config=None) -> None:
        self.cfg = vad_config
        self.asr_cfg = asr_config
        self._ffmpeg = shutil.which("ffmpeg")
        if not self._ffmpeg:
            raise RuntimeError(
                "ffmpeg non trovato nel PATH. "
                "Installalo con: brew install ffmpeg"
            )

    # ------------------------------------------------------------------
    # Interfaccia pubblica
    # ------------------------------------------------------------------

    def process(self, audio_path: Path) -> tuple[Path, list[SpeechSegment], dict]:
        """
        Processa un file audio:
          1. Converte in WAV 16kHz mono (se necessario)
          2. Esegue VAD
          3. Restituisce (wav_path, segmenti, statistiche)

        Il WAV viene scritto in una directory temporanea nella stessa
        cartella di output del job; il chiamante è responsabile della pulizia.
        """
        audio_path = Path(audio_path)
        logger.info("VAD: inizio processing %s", audio_path.name)

        # Step 1: conversione a WAV
        wav_path = self._to_wav(audio_path)

        # Step 2: VAD via faster-whisper
        segments, stats = self._run_vad(wav_path)

        logger.info(
            "VAD completato: %d segmenti parlato | speech_ratio=%.1f%% | "
            "parlato effettivo=%.1f min su %.1f min totali",
            len(segments),
            stats["speech_ratio"] * 100,
            stats["speech_duration_sec"] / 60,
            stats["total_duration_sec"] / 60,
        )

        return wav_path, segments, stats

    # ------------------------------------------------------------------
    # Conversione audio
    # ------------------------------------------------------------------

    def _to_wav(self, audio_path: Path) -> Path:
        """
        Converte qualsiasi formato audio/video in WAV 16kHz mono.
        Se il file è già WAV 16kHz mono, non lo converte: lo riusa.

        Il riuso non è un dettaglio. La variante ripulita prodotta da
        afftdn è già 16kHz mono, quindi il VAD la riconverteva in un
        file identico da 115 MB che nessuno cancellava: ogni ora di
        audio lasciava un orfano in cache, e con diciotto file a notte
        la cache cresceva di oltre due gigabyte a passata.
        """
        # La cache sta sotto la radice della pipeline, MAI accanto al
        # file sorgente. Con il registratore collegato, "accanto al
        # sorgente" significava scrivere i WAV derivati sulla memoria del
        # registratore: si riempiva il device di file che lui non sa
        # che sono temporanei, e la pull successiva li trovava come
        # registrazioni nuove, li elaborava e li cancellava dal device.
        # Su un volume exFAT da 18 ore è anche lentissimo.
        if _is_wav_16k_mono(audio_path):
            logger.info(
                "Audio già 16kHz mono WAV: lo uso così com'è (%s)",
                audio_path.name,
            )
            return audio_path

        wav_dir = wav_cache_dir()
        wav_dir.mkdir(parents=True, exist_ok=True)
        # L'hash del percorso distingue due file omonimi che stanno in
        # cartelle diverse: senza, la cache del secondo riuserebbe quella
        # del primo e la trascrizione sarebbe dell'audio sbagliato.
        tag = hashlib.sha1(str(audio_path.resolve()).encode()).hexdigest()[:8]
        wav_path = wav_dir / f"{audio_path.stem}_{tag}_16k.wav"

        if wav_path.exists():
            logger.debug("WAV già esistente, riuso: %s", wav_path)
            return wav_path

        logger.info("Conversione WAV 16kHz mono: %s → %s", audio_path.name, wav_path.name)

        cmd = [
            self._ffmpeg,
            "-hide_banner", "-loglevel", "error",
            "-i", str(audio_path),
            "-ac", "1",          # mono
            "-ar", "16000",      # 16kHz
            "-sample_fmt", "s16", # 16-bit PCM
            "-y",                # sovrascrive senza chiedere
            str(wav_path),
        ]

        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=7200,  # max 2 ore per conversione (file molto lunghi)
        )

        if result.returncode != 0:
            raise RuntimeError(
                f"ffmpeg fallito per {audio_path.name}:\n{result.stderr}"
            )

        logger.info("Conversione WAV completata: %.1f MB", wav_path.stat().st_size / 1e6)
        return wav_path

    # ------------------------------------------------------------------
    # VAD core
    # ------------------------------------------------------------------

    def _run_vad(self, wav_path: Path) -> tuple[list[SpeechSegment], dict]:
        """
        Esegue Silero VAD tramite faster-whisper e restituisce
        la lista di segmenti parlato e le statistiche.
        """
        # Import locale per non rompere se faster-whisper non è nel PATH
        # (l'import avviene nel venv corretto grazie a setup_env.sh)
        try:
            from faster_whisper.vad import (
                VadOptions,
                get_speech_timestamps,
            )
            import numpy as np
            import soundfile as sf
        except ImportError as exc:
            raise ImportError(
                f"faster-whisper o soundfile non disponibili: {exc}\n"
                "Assicurati di eseguire con il venv corretto (setup_env.sh)."
            ) from exc

        # Carica il WAV come array numpy
        logger.debug("Caricamento WAV in memoria: %s", wav_path)
        audio_array, sr = sf.read(str(wav_path), dtype="float32", always_2d=False)

        if sr != self.cfg.sample_rate:
            raise ValueError(
                f"Sample rate inatteso: {sr} Hz (atteso {self.cfg.sample_rate} Hz). "
                "Ri-esegui la conversione ffmpeg."
            )

        total_duration_sec = len(audio_array) / sr

        # Configurazione VAD
        vad_options = VadOptions(
            threshold=self.cfg.threshold,
            min_silence_duration_ms=self.cfg.min_silence_duration_ms,
            min_speech_duration_ms=self.cfg.min_speech_duration_ms,
            speech_pad_ms=self.cfg.speech_pad_ms,
        )

        logger.debug("Esecuzione Silero VAD su %.1f minuti di audio...", total_duration_sec / 60)

        raw_timestamps = get_speech_timestamps(
            audio_array,
            vad_options=vad_options,
        )

        # Converti i timestamp (in campioni) in secondi e costruisci SpeechSegment
        segments: list[SpeechSegment] = []
        for idx, ts in enumerate(raw_timestamps):
            start_sec = ts["start"] / sr
            end_sec   = ts["end"]   / sr

            # Applica il padding configurato (clamp ai bordi)
            pad = self.cfg.speech_pad_ms / 1000.0
            start_sec = max(0.0, start_sec - pad)
            end_sec   = min(total_duration_sec, end_sec + pad)

            segments.append(SpeechSegment(idx=idx, start=start_sec, end=end_sec))

        speech_duration = sum(s.duration for s in segments)
        speech_ratio    = speech_duration / total_duration_sec if total_duration_sec > 0 else 0.0

        stats = {
            "total_duration_sec": total_duration_sec,
            "speech_duration_sec": speech_duration,
            "silence_duration_sec": total_duration_sec - speech_duration,
            "speech_ratio": speech_ratio,
            "segments_count": len(segments),
            "wav_path": str(wav_path),
        }

        return segments, stats

    # ------------------------------------------------------------------
    # Chunking per ASR
    # ------------------------------------------------------------------

    @staticmethod
    def split_into_chunks(
        segments: list[SpeechSegment],
        max_chunk_sec: float = 29.0,
    ) -> list[dict]:
        """
        Raggruppa i SpeechSegment in chunk da passare all'ASR.
        Ogni chunk ha durata <= max_chunk_sec.

        Whisper lavora su finestre di 30s: chunk più lunghi vengono troncati.
        Chunk più corti sono inefficienti ma più sicuri contro le hallucination.

        Restituisce una lista di dict:
            {"idx": int, "segments": [SpeechSegment, ...],
             "start": float, "end": float, "duration": float}
        """
        chunks: list[dict] = []
        current_segs: list[SpeechSegment] = []
        current_dur: float = 0.0

        for seg in segments:
            seg_dur = seg.duration

            # Se il singolo segmento supera il massimo, spezzalo
            if seg_dur > max_chunk_sec:
                # Prima chiudi il chunk corrente
                if current_segs:
                    chunks.append(_make_chunk(len(chunks), current_segs))
                    current_segs = []
                    current_dur = 0.0

                # Spezza il segmento lungo in sotto-chunk
                sub_start = seg.start
                sub_idx_offset = 0
                while sub_start < seg.end:
                    sub_end = min(sub_start + max_chunk_sec, seg.end)
                    sub_seg = SpeechSegment(
                        idx=seg.idx * 1000 + sub_idx_offset,
                        start=sub_start,
                        end=sub_end,
                    )
                    chunks.append(_make_chunk(len(chunks), [sub_seg]))
                    sub_start = sub_end
                    sub_idx_offset += 1
                continue

            # Il limite si controlla sul TEMPO TRASCORSO, non sulla somma
            # delle durate di parlato.
            #
            # Sommando il solo parlato, venti segmenti da un secondo
            # separati da silenzi entravano in un solo chunk: il chunk
            # risultante copriva 188 secondi di clock con 29 di parole.
            # Sembrava rispettare il limite di 29 secondi, ma non lo
            # rispettava. E il conto torna male due volte: Whisper lavora
            # su finestre di 30 secondi, e la prosodia (F0, jitter,
            # shimmer) finiva calcolata su tre minuti in cui due minuti
            # e mezzo erano silenzio — numeri senza significato, che
            # nel corpus sembrerebbero dati misurati.
            if (current_segs
                    and seg.end - current_segs[0].start > max_chunk_sec):
                chunks.append(_make_chunk(len(chunks), current_segs))
                current_segs = []
                current_dur = 0.0

            current_segs.append(seg)
            current_dur += seg_dur

        # Chunk residuo
        if current_segs:
            chunks.append(_make_chunk(len(chunks), current_segs))

        logger.info(
            "Chunking: %d segmenti VAD → %d chunk ASR (max %.1fs ciascuno)",
            len(segments), len(chunks), max_chunk_sec,
        )
        return chunks


def _make_chunk(idx: int, segs: list[SpeechSegment]) -> dict:
    return {
        "idx": idx,
        "segments": segs,
        "start": segs[0].start,
        "end": segs[-1].end,
        "duration": sum(s.duration for s in segs),
    }
