"""
Assembler — Costruisce l'output finale della pipeline

Prende i risultati dei tre stadi precedenti e li assembla in:
  - transcript.json  (struttura completa: testo + speaker + prosodia)
  - transcript.txt   (testo leggibile con etichette speaker)
  - transcript.srt   (sottotitoli standard SRT)
  - prosody.csv      (metadati prosodici in formato tabulare)

Il JSON è il formato canonico — gli altri si ricavano da quello.
"""

from __future__ import annotations

import csv
import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class Assembler:
    """
    Assembla e scrive i file di output per un file audio processato.

    Uso:
        assembler = Assembler(config.output)
        assembler.assemble(
            audio_path   = Path("input/registrazione.mp3"),
            asr_chunks   = [...],          # da Transcriber (con speaker da Diarizer)
            diar_segments= [...],          # da Diarizer.diarize()
            prosody_data = [...],          # da ProsodyAnalyzer.analyze()
            vad_stats    = {...},          # da VoiceActivityDetector.process()
            output_dir   = Path("output/registrazione/"),
        )
    """

    def __init__(self, output_config) -> None:
        self.cfg = output_config

    # ------------------------------------------------------------------
    # Entrypoint principale
    # ------------------------------------------------------------------

    def assemble(
        self,
        audio_path: Path,
        asr_chunks: list[dict[str, Any]],
        diar_segments: list[dict[str, Any]],
        prosody_data: list[dict[str, Any]],
        vad_stats: dict[str, Any],
        output_dir: Path,
    ) -> dict[str, Path]:
        """
        Assembla tutti i dati e scrive i file di output.

        Returns:
            Dict con i percorsi dei file scritti, es:
            {"json": Path("output/stem/transcript.json"), ...}
        """
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        stem = audio_path.stem

        # Costruisci l'indice prosodia per lookup rapido per idx
        prosody_by_idx = {p["idx"]: p for p in prosody_data}

        # Costruisci i segmenti finali (unisce ASR + speaker + prosodia)
        final_segments = self._build_segments(asr_chunks, prosody_by_idx)

        # Rimuovi chunk vuoti (silenzio residuo post-VAD)
        final_segments = [s for s in final_segments if s["text"].strip()]

        # Metadati globali
        doc = self._build_document(
            stem=stem,
            audio_path=audio_path,
            final_segments=final_segments,
            diar_segments=diar_segments,
            vad_stats=vad_stats,
        )

        written: dict[str, Path] = {}

        if self.cfg.write_json:
            p = output_dir / "transcript.json"
            _write_json(doc, p, indent=self.cfg.json_indent)
            written["json"] = p
            logger.info("Scritto: %s", p)

        if self.cfg.write_txt:
            p = output_dir / "transcript.txt"
            _write_txt(final_segments, p)
            written["txt"] = p
            logger.info("Scritto: %s", p)

        if self.cfg.write_srt:
            p = output_dir / "transcript.srt"
            _write_srt(final_segments, p)
            written["srt"] = p
            logger.info("Scritto: %s", p)

        if self.cfg.write_csv:
            p = output_dir / "prosody.csv"
            _write_csv(final_segments, p)
            written["csv"] = p
            logger.info("Scritto: %s", p)

        return written

    # ------------------------------------------------------------------
    # Costruzione struttura dati
    # ------------------------------------------------------------------

    def _build_segments(
        self,
        asr_chunks: list[dict[str, Any]],
        prosody_by_idx: dict[int, dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Unisce ogni chunk ASR con i suoi metadati prosodici."""
        segments = []
        for chunk in asr_chunks:
            idx = chunk["idx"]
            pros = prosody_by_idx.get(idx, {})

            seg: dict[str, Any] = {
                "idx":     idx,
                "start":   chunk["start"],
                "end":     chunk["end"],
                "text":    chunk["text"],
                "speaker": chunk.get("speaker", "UNKNOWN"),
                "language": chunk.get("language", "it"),
                "no_speech_prob": chunk.get("no_speech_prob", 0.0),
            }

            # Word-level timestamps (opzionale)
            if self.cfg.include_word_timestamps and chunk.get("words"):
                seg["words"] = chunk["words"]

            # Metadati prosodici (solo i campi non-None)
            prosody_fields = {
                k: v for k, v in pros.items()
                if k not in ("idx", "start", "end") and v is not None
            }
            if prosody_fields:
                seg["prosody"] = prosody_fields

            segments.append(seg)

        segments.sort(key=lambda s: s["start"])
        return segments

    @staticmethod
    def _build_document(
        stem: str,
        audio_path: Path,
        final_segments: list[dict[str, Any]],
        diar_segments: list[dict[str, Any]],
        vad_stats: dict[str, Any],
    ) -> dict[str, Any]:
        """Costruisce il documento JSON completo."""
        speakers = sorted({s["speaker"] for s in final_segments})
        total_words = sum(len(s["text"].split()) for s in final_segments)
        total_speech = sum(s["end"] - s["start"] for s in final_segments)

        # Statistiche per speaker
        speaker_stats: dict[str, Any] = {}
        for sp in speakers:
            sp_segs = [s for s in final_segments if s["speaker"] == sp]
            sp_duration = sum(s["end"] - s["start"] for s in sp_segs)
            sp_words = sum(len(s["text"].split()) for s in sp_segs)
            speaker_stats[sp] = {
                "segments_count": len(sp_segs),
                "total_duration_sec": round(sp_duration, 2),
                "total_words": sp_words,
                "fraction": round(sp_duration / total_speech, 3) if total_speech > 0 else 0,
            }

        return {
            "meta": {
                "file": audio_path.name,
                "stem": stem,
                "total_duration_sec": round(vad_stats.get("total_duration_sec", 0), 2),
                "speech_duration_sec": round(vad_stats.get("speech_duration_sec", 0), 2),
                "speech_ratio": round(vad_stats.get("speech_ratio", 0), 3),
                "segments_count": len(final_segments),
                "total_words": total_words,
                "speakers": speakers,
                "speaker_stats": speaker_stats,
            },
            "segments": final_segments,
        }


# ---------------------------------------------------------------------------
# Writer per ogni formato
# ---------------------------------------------------------------------------

def _write_json(doc: dict, path: Path, indent: int = 2) -> None:
    path.write_text(
        json.dumps(doc, ensure_ascii=False, indent=indent),
        encoding="utf-8",
    )


def _write_txt(segments: list[dict], path: Path) -> None:
    """
    Formato testo semplice con etichette speaker e timestamp.

    [00:01:23 → 00:01:31] SPEAKER_00
    Buongiorno a tutti, oggi parliamo di...

    [00:01:31 → 00:01:45] SPEAKER_01
    Grazie per l'introduzione...
    """
    lines = []
    prev_speaker = None
    for seg in segments:
        speaker = seg.get("speaker", "UNKNOWN")
        start_ts = _fmt_timestamp(seg["start"])
        end_ts   = _fmt_timestamp(seg["end"])

        # Stampa intestazione speaker solo quando cambia
        if speaker != prev_speaker:
            if lines:
                lines.append("")
            lines.append(f"[{start_ts} → {end_ts}] {speaker}")
            prev_speaker = speaker
        else:
            # Stessa persona che continua — aggiorna solo il timestamp di fine
            lines[-1] = f"[{start_ts} → {end_ts}] {speaker}"

        lines.append(seg["text"])

    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write_srt(segments: list[dict], path: Path) -> None:
    """
    Formato SRT standard.
    Ogni blocco: indice, timestamps, testo (con speaker label).

    1
    00:01:23,000 --> 00:01:31,000
    [SPEAKER_00] Buongiorno a tutti...
    """
    blocks = []
    for i, seg in enumerate(segments, start=1):
        start_srt = _fmt_srt_time(seg["start"])
        end_srt   = _fmt_srt_time(seg["end"])
        speaker   = seg.get("speaker", "UNKNOWN")
        text      = seg["text"].strip()

        blocks.append(
            f"{i}\n{start_srt} --> {end_srt}\n[{speaker}] {text}"
        )

    path.write_text("\n\n".join(blocks) + "\n", encoding="utf-8")


def _write_csv(segments: list[dict], path: Path) -> None:
    """
    CSV con una riga per segmento, colonne per ogni feature prosodia.
    Compatibile con Excel, pandas, R.
    """
    if not segments:
        path.write_text("", encoding="utf-8")
        return

    # Raccogli tutte le chiavi prosodia presenti
    prosody_keys: list[str] = []
    for seg in segments:
        for k in seg.get("prosody", {}).keys():
            if k not in prosody_keys:
                prosody_keys.append(k)

    fieldnames = [
        "idx", "start", "end", "duration_sec",
        "speaker", "text_preview", "word_count",
        "no_speech_prob",
    ] + prosody_keys

    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for seg in segments:
            pros = seg.get("prosody", {})
            row: dict[str, Any] = {
                "idx":          seg["idx"],
                "start":        seg["start"],
                "end":          seg["end"],
                "duration_sec": round(seg["end"] - seg["start"], 3),
                "speaker":      seg.get("speaker", "UNKNOWN"),
                "text_preview": seg["text"][:60].replace("\n", " "),
                "word_count":   len(seg["text"].split()),
                "no_speech_prob": seg.get("no_speech_prob", 0.0),
            }
            for k in prosody_keys:
                row[k] = pros.get(k, "")
            writer.writerow(row)


# ---------------------------------------------------------------------------
# Utility timestamp
# ---------------------------------------------------------------------------

def _fmt_timestamp(seconds: float) -> str:
    """Formatta secondi come HH:MM:SS."""
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def _fmt_srt_time(seconds: float) -> str:
    """Formatta secondi nel formato SRT: HH:MM:SS,mmm."""
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    ms = int((seconds - int(seconds)) * 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"
