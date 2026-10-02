"""
Assembler — Costruisce l'output finale della pipeline

Prende i risultati dei tre stadi precedenti e li assembla in:

  Formati esistenti:
  - transcript.json      (struttura completa: testo + speaker + prosodia)
  - transcript.txt       (testo leggibile con etichette speaker)
  - transcript.srt       (sottotitoli standard SRT)
  - prosody.csv          (metadati prosodici in formato tabulare)

  Nuovi formati arricchiti:
  - session.json         (metadata sessione: data, ora inizio, speaker map, statistiche)
  - segments.jsonl       (un segmento per riga, tutto dentro — ottimale per AI/LLM ingest)
  - tokens.jsonl         (una parola per riga con timestamp — per analisi linguistica, KWIC)
  - wordfreq.csv         (frequenze parole per speaker — pronto per corpus analysis)
  - analysis_ready.md    (testo strutturato chunked, ottimizzato per LLM)

Il JSON è il formato canonico — gli altri si ricavano da quello.
"""

from __future__ import annotations

import csv
import json
import logging
import re
from collections import Counter
from datetime import datetime, timezone
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
        speaker_global_map: dict[str, str] | None = None,
        speaker_names: dict[str, str] | None = None,
    ) -> dict[str, Path]:
        """
        Assembla tutti i dati e scrive i file di output.

        Args:
            speaker_global_map: mapping speaker locale → ID globale cross-file,
                                 es. {"SPEAKER_00": "GLOBAL_001", ...}
                                 Se None, usa direttamente i label locali.
            speaker_names: ID globale → nome umano, es. {"GLOBAL_001": "Pietro"}.
                           Viene risolto in fase di output, così i formati
                           sono leggibili anche senza aprire il DB.

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
        final_segments = self._build_segments(
            asr_chunks, prosody_by_idx, speaker_global_map
        )

        # Rimuovi chunk vuoti (silenzio residuo post-VAD)
        final_segments = [s for s in final_segments if s["text"].strip()]

        # Metadati globali
        doc = self._build_document(
            stem=stem,
            audio_path=audio_path,
            final_segments=final_segments,
            diar_segments=diar_segments,
            vad_stats=vad_stats,
            speaker_global_map=speaker_global_map,
            speaker_names=speaker_names,
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

        # ------------------------------------------------------------------
        # Nuovi formati arricchiti
        # ------------------------------------------------------------------

        if self.cfg.write_session_json:
            p = output_dir / "session.json"
            _write_session_json(doc["meta"], p, indent=self.cfg.json_indent)
            written["session_json"] = p
            logger.info("Scritto: %s", p)

        if self.cfg.write_segments_jsonl:
            p = output_dir / "segments.jsonl"
            _write_segments_jsonl(final_segments, p)
            written["segments_jsonl"] = p
            logger.info("Scritto: %s", p)

        if self.cfg.write_tokens_jsonl:
            p = output_dir / "tokens.jsonl"
            _write_tokens_jsonl(final_segments, p)
            written["tokens_jsonl"] = p
            logger.info("Scritto: %s", p)

        if self.cfg.write_wordfreq_csv:
            p = output_dir / "wordfreq.csv"
            _write_wordfreq_csv(final_segments, p)
            written["wordfreq_csv"] = p
            logger.info("Scritto: %s", p)

        if self.cfg.write_analysis_md:
            p = output_dir / "analysis_ready.md"
            _write_analysis_md(doc["meta"], final_segments, p)
            written["analysis_md"] = p
            logger.info("Scritto: %s", p)

        return written

    # ------------------------------------------------------------------
    # Costruzione struttura dati
    # ------------------------------------------------------------------

    def _build_segments(
        self,
        asr_chunks: list[dict[str, Any]],
        prosody_by_idx: dict[int, dict[str, Any]],
        speaker_global_map: dict[str, str] | None = None,
    ) -> list[dict[str, Any]]:
        """Unisce ogni chunk ASR con i suoi metadati prosodici e speaker globale."""
        segments = []
        for chunk in asr_chunks:
            idx = chunk["idx"]
            pros = prosody_by_idx.get(idx, {})

            local_speaker = chunk.get("speaker", "UNKNOWN")
            global_speaker = (
                speaker_global_map.get(local_speaker, local_speaker)
                if speaker_global_map
                else local_speaker
            )

            seg: dict[str, Any] = {
                "idx":              idx,
                "start":            chunk["start"],
                "end":              chunk["end"],
                "duration_sec":     round(chunk["end"] - chunk["start"], 3),
                "text":             chunk["text"],
                "speaker":          global_speaker,
                "speaker_local":    local_speaker,
                "language":         chunk.get("language", "it"),
                "no_speech_prob":   chunk.get("no_speech_prob", 0.0),
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
        speaker_global_map: dict[str, str] | None = None,
        speaker_names: dict[str, str] | None = None,
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
                "segments_count":    len(sp_segs),
                "total_duration_sec": round(sp_duration, 2),
                "total_words":        sp_words,
                "fraction":           round(sp_duration / total_speech, 3) if total_speech > 0 else 0,
            }

        return {
            "meta": {
                "file":                audio_path.name,
                "stem":                stem,
                "processed_at":        datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S"),
                "session_start_wall":  None,   # popolato da sync_bio o manualmente
                "total_duration_sec":  round(vad_stats.get("total_duration_sec", 0), 2),
                "speech_duration_sec": round(vad_stats.get("speech_duration_sec", 0), 2),
                "speech_ratio":        round(vad_stats.get("speech_ratio", 0), 3),
                "segments_count":      len(final_segments),
                "total_words":         total_words,
                "speakers":            speakers,
                "speaker_names":       speaker_names or {},
                "speaker_stats":       speaker_stats,
                "speaker_global_map":  speaker_global_map or {},
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

    Quando lo speaker cambia, apre un nuovo blocco con intestazione.
    Quando lo speaker continua, aggiunge il testo allo stesso blocco
    e aggiorna il timestamp di fine nell'intestazione.

    [00:01:23 → 00:01:31] SPEAKER_00
    Buongiorno a tutti, oggi parliamo di...
    Continuo dello stesso speaker...

    [00:01:31 → 00:01:45] SPEAKER_01
    Grazie per l'introduzione...
    """
    # Struttura: lista di blocchi {"speaker", "start", "end", "lines": []}
    blocks: list[dict] = []

    for seg in segments:
        speaker  = seg.get("speaker", "UNKNOWN")
        text     = seg["text"].strip()
        if not text:
            continue

        # Nuovo blocco se speaker cambia o è il primo
        if not blocks or blocks[-1]["speaker"] != speaker:
            blocks.append({
                "speaker": speaker,
                "start":   seg["start"],
                "end":     seg["end"],
                "lines":   [text],
            })
        else:
            # Stesso speaker — aggiunge testo e aggiorna timestamp fine
            blocks[-1]["end"] = seg["end"]
            blocks[-1]["lines"].append(text)

    # Serializza
    output_lines: list[str] = []
    for block in blocks:
        start_ts = _fmt_timestamp(block["start"])
        end_ts   = _fmt_timestamp(block["end"])
        output_lines.append(f"[{start_ts} → {end_ts}] {block['speaker']}")
        output_lines.extend(block["lines"])
        output_lines.append("")  # riga vuota tra blocchi

    path.write_text("\n".join(output_lines).rstrip() + "\n", encoding="utf-8")


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
# Nuovi writer arricchiti
# ---------------------------------------------------------------------------

def _write_session_json(meta: dict, path: Path, indent: int = 2) -> None:
    """
    session.json — metadata della sessione di registrazione.

    Contiene tutto il necessario per identificare la sessione, correlare
    con dati biometrici e costruire il profilo cross-file degli speaker.

    Struttura:
    {
      "file": "registrazione.mp3",
      "stem": "registrazione",
      "processed_at": "2026-10-01T03:14:22",
      "session_start_wall": null,          ← popolato da sync_bio.py
      "duration": {"total_sec": 72000, "speech_sec": 36000, "speech_ratio": 0.5},        "speakers": {
        "GLOBAL_001": {"segments": 42, "duration_sec": 1800, "words": 3200, "fraction": 0.5},
        ...
      },
      "speaker_names":       {"GLOBAL_001": "Pietro", ...},
      "speaker_global_map": {"SPEAKER_00": "GLOBAL_001", ...},
      "stats": {"total_words": 6400, "segments_count": 84}
    }
    """
    speaker_names = meta.get("speaker_names", {})
    session = {
        "file":               meta["file"],
        "stem":               meta["stem"],
        "processed_at":       meta["processed_at"],
        "session_start_wall": meta.get("session_start_wall"),
        "duration": {
            "total_sec":    meta["total_duration_sec"],
            "speech_sec":   meta["speech_duration_sec"],
            "speech_ratio": meta["speech_ratio"],
        },
        "speakers":           meta["speaker_stats"],
        "speaker_names":      speaker_names,
        "speaker_global_map": meta.get("speaker_global_map", {}),
        "stats": {
            "total_words":    meta["total_words"],
            "segments_count": meta["segments_count"],
        },
    }
    path.write_text(
        json.dumps(session, ensure_ascii=False, indent=indent),
        encoding="utf-8",
    )


def _write_segments_jsonl(segments: list[dict], path: Path) -> None:
    """
    segments.jsonl — un segmento per riga in formato JSON Lines.

    Ogni riga è un JSON completo e auto-contenuto: può essere processato
    in streaming da qualsiasi tool, LLM o script Python senza caricare
    l'intero transcript.json in memoria.

    Campi per riga:
    {
      "idx": 0,
      "start": 12.4,             ← secondi dall'inizio audio
      "end": 18.7,
      "duration_sec": 6.3,
      "speaker": "GLOBAL_001",   ← ID globale cross-file
      "speaker_local": "SPEAKER_00",
      "text": "buongiorno a tutti",
      "language": "it",
      "no_speech_prob": 0.02,
      "prosody": {               ← solo se disponibile
        "f0_mean": 142.3,
        "f0_std": 18.1,
        "energy_rms": 0.04,
        "speech_rate_syl_sec": 4.2,
        ...
      }
    }
    Note: i word-level timestamps vengono omessi per contenere le dimensioni.
    Usa tokens.jsonl per analisi a livello di parola.
    """
    lines = []
    for seg in segments:
        row = {k: v for k, v in seg.items() if k != "words"}
        lines.append(json.dumps(row, ensure_ascii=False))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write_tokens_jsonl(segments: list[dict], path: Path) -> None:
    """
    tokens.jsonl — una parola per riga con tutti i suoi metadati.

    Formato pensato per:
    - Analisi linguistica (POS, frequenze, n-grammi, KWIC)
    - Allineamento preciso con dati biometrici al secondo
    - Studio conversazionale (turni, overlap, velocità)

    Campi per riga:
    {
      "segment_idx": 0,
      "token_idx": 3,            ← posizione nella sessione
      "word": "buongiorno",
      "start": 12.4,             ← secondi assoluti nell'audio
      "end": 12.9,
      "duration_ms": 500,
      "speaker": "GLOBAL_001",
      "segment_start": 12.4,    ← per risalire al segmento padre
      "f0_mean": 142.3           ← prosodia del segmento padre (se disponibile)
    }

    Se i word-level timestamps non sono disponibili (include_word_timestamps=False),
    distribuisce le parole equamente nell'intervallo del segmento.
    """
    lines = []
    token_idx = 0

    for seg in segments:
        speaker  = seg.get("speaker", "UNKNOWN")
        seg_idx  = seg["idx"]
        seg_start = seg["start"]
        pros     = seg.get("prosody", {})
        # Include solo feature scalari di prosodia (non array/None)
        pros_flat = {k: v for k, v in pros.items() if isinstance(v, (int, float))}

        words = seg.get("words", [])
        if words:
            # Word-level timestamps disponibili
            for w in words:
                start = w.get("start", seg["start"])
                end   = w.get("end",   seg["end"])
                row   = {
                    "segment_idx":  seg_idx,
                    "token_idx":    token_idx,
                    "word":         w.get("word", "").strip(),
                    "start":        round(start, 3),
                    "end":          round(end, 3),
                    "duration_ms":  round((end - start) * 1000),
                    "speaker":      w.get("speaker", speaker),
                    "segment_start": seg_start,
                    **pros_flat,
                }
                if row["word"]:  # salta word vuote
                    lines.append(json.dumps(row, ensure_ascii=False))
                    token_idx += 1
        else:
            # Stima posizione delle parole distribuendole nell'intervallo
            text_words = [w for w in seg["text"].split() if w.strip()]
            n = len(text_words)
            if n == 0:
                continue
            seg_dur = seg["end"] - seg["start"]
            step = seg_dur / n
            for i, word in enumerate(text_words):
                start = seg["start"] + i * step
                end   = seg["start"] + (i + 1) * step
                row   = {
                    "segment_idx":  seg_idx,
                    "token_idx":    token_idx,
                    "word":         word,
                    "start":        round(start, 3),
                    "end":          round(end, 3),
                    "duration_ms":  round(step * 1000),
                    "speaker":      speaker,
                    "segment_start": seg_start,
                    "estimated":    True,  # timestamp stimato, non preciso
                    **pros_flat,
                }
                lines.append(json.dumps(row, ensure_ascii=False))
                token_idx += 1

    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write_wordfreq_csv(segments: list[dict], path: Path) -> None:
    """
    wordfreq.csv — frequenze parole per speaker e globale.

    Compatibile con Excel, pandas, R, AntConc, Voyant Tools.
    Normalizza le parole (lowercase, rimuove punteggiatura).

    Colonne: word, freq_global, freq_SPEAKER_X, freq_SPEAKER_Y, ...
    """
    # Collect speakers
    speakers = sorted({seg.get("speaker", "UNKNOWN") for seg in segments})

    # Count frequencies
    global_counts: Counter = Counter()
    speaker_counts: dict[str, Counter] = {sp: Counter() for sp in speakers}

    for seg in segments:
        sp = seg.get("speaker", "UNKNOWN")
        # Tokenizzazione semplice: lowercase + rimuovi punteggiatura
        words = re.findall(r"\b[a-zA-ZàèéìíîòóùúÀÈÉÌÍÎÒÓÙÚ']+\b", seg["text"].lower())
        for w in words:
            global_counts[w] += 1
            speaker_counts[sp][w] += 1

    if not global_counts:
        path.write_text("word,freq_global\n", encoding="utf-8")
        return

    fieldnames = ["word", "freq_global"] + [f"freq_{sp}" for sp in speakers]

    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for word, freq in global_counts.most_common():
            row: dict[str, Any] = {"word": word, "freq_global": freq}
            for sp in speakers:
                row[f"freq_{sp}"] = speaker_counts[sp].get(word, 0)
            writer.writerow(row)


def _write_analysis_md(
    meta: dict,
    segments: list[dict],
    path: Path,
    chunk_words: int = 800,
) -> None:
    """
    analysis_ready.md — testo strutturato ottimizzato per input LLM.

    Design goals:
    - Auto-contenuto: metadata in testa, poi trascritto
    - Chunked: sezioni di ~800 parole per rispettare context window piccole
    - Speaker label chiari: visibili all'LLM per analisi di turni e dialogo
    - Timestamp come anchor: permettono di tornare al punto nell'audio
    - Nessuna formattazione pesante: niente tabelle, solo testo + markdown base

    Struttura:
    # Sessione: <stem>

    ## Metadata
    - File: ...
    - Durata: ...
    - Speaker: ...

    ## Trascrizione

    ### Parte 1 (00:00:00 – 00:13:20)

    **[GLOBAL_001]** [00:00:12] buongiorno a tutti...
    **[GLOBAL_002]** [00:00:31] grazie per l'introduzione...
    ...
    """
    lines: list[str] = []
    stem = meta.get("stem", "sessione")

    # Header
    lines += [
        f"# Sessione: {stem}",
        "",
        "## Metadata",
        "",
        f"- **File:** {meta.get('file', stem)}",
        f"- **Processato il:** {meta.get('processed_at', '')}",
        f"- **Durata totale:** {_fmt_timestamp(meta.get('total_duration_sec', 0))}",
        f"- **Parlato effettivo:** {_fmt_timestamp(meta.get('speech_duration_sec', 0))} "
        f"({meta.get('speech_ratio', 0):.0%})",
        f"- **Parole totali:** {meta.get('total_words', 0):,}",
        f"- **Segmenti:** {meta.get('segments_count', 0)}",
        "",
        "### Speaker",
        "",
    ]

    for sp, stats in meta.get("speaker_stats", {}).items():
        dur   = _fmt_timestamp(stats.get("total_duration_sec", 0))
        words = stats.get("total_words", 0)
        pct   = stats.get("fraction", 0)
        lines.append(
            f"- **{sp}** — {dur} di parlato, {words:,} parole ({pct:.0%})"
        )

    lines += ["", "---", "", "## Trascrizione", ""]

    # Chunking in parti da ~chunk_words parole
    part_num   = 0
    word_count = 0
    part_start: float | None = None
    part_last:  float | None = None
    buffer: list[str] = []

    def flush_part(buf: list[str], p_start: float, p_end: float, pnum: int) -> list[str]:
        header = f"### Parte {pnum} ({_fmt_timestamp(p_start)} – {_fmt_timestamp(p_end)})"
        return [header, ""] + buf + [""]

    for seg in segments:
        text = seg["text"].strip()
        if not text:
            continue

        speaker = seg.get("speaker", "UNKNOWN")
        ts      = _fmt_timestamp(seg["start"])
        n_words = len(text.split())

        if part_start is None:
            part_start = seg["start"]
            part_num  += 1

        # Inizio nuovo chunk se superato il limite
        if word_count > 0 and word_count + n_words > chunk_words:
            lines += flush_part(buffer, part_start, part_last or seg["start"], part_num)
            buffer     = []
            word_count = 0
            part_start = seg["start"]
            part_num  += 1

        buffer.append(f"**[{speaker}]** [{ts}] {text}")
        word_count += n_words
        part_last   = seg["end"]

    # Flush ultimo chunk
    if buffer and part_start is not None:
        lines += flush_part(buffer, part_start, part_last or part_start, part_num)

    path.write_text("\n".join(lines), encoding="utf-8")


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
