"""
Checkpoint: salva e riprende lo stato di avanzamento della pipeline.

Per ogni file audio processato viene creato un file JSON in output/<stem>/
che traccia quali stadi sono completati e quali chunk ASR sono già stati
trascritti. Se il processo viene interrotto (crash, timeout notturno, ecc.)
la prossima esecuzione riprende dall'ultimo chunk salvato.

Struttura del checkpoint:
{
  "file": "/path/to/audio.mp3",
  "stem": "audio",
  "stages": {
    "ffmpeg":        {"done": true,  "wav_path": "...", "duration_sec": 72000.0},
    "vad":           {"done": true,  "segments_count": 1842, "speech_ratio": 0.51},
    "transcription": {"done": false, "chunks_done": 47, "chunks_total": 184},
    "diarization":   {"done": false},
    "prosody":       {"done": false},
    "assembly":      {"done": false}
  },
  "chunks": [
    {"idx": 0, "start": 0.0, "end": 28.4, "text": "...", "language": "it"},
    ...
  ],
  "created_at": "2026-10-01T03:00:00",
  "updated_at": "2026-10-01T03:14:22"
}
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class Checkpoint:
    """
    Gestisce la persistenza dello stato di avanzamento per un singolo file audio.

    Uso:
        ck = Checkpoint(audio_path, output_dir)
        if not ck.stage_done("ffmpeg"):
            wav = convert_to_wav(audio_path)
            ck.complete_stage("ffmpeg", wav_path=str(wav), duration_sec=3600.0)

        for chunk in ck.pending_chunks(all_chunks):
            result = transcribe(chunk)
            ck.add_chunk_result(chunk.idx, result)
    """

    STAGES = ("ffmpeg", "vad", "transcription", "diarization", "prosody", "assembly")

    def __init__(self, audio_path: Path, output_dir: Path) -> None:
        self.audio_path = Path(audio_path)
        self.stem = self.audio_path.stem
        self.job_dir = output_dir / self.stem
        self.job_dir.mkdir(parents=True, exist_ok=True)
        self._path = self.job_dir / f"{self.stem}.checkpoint.json"
        self._data = self._load()

    # ------------------------------------------------------------------
    # Persistenza
    # ------------------------------------------------------------------

    def _load(self) -> dict[str, Any]:
        if self._path.exists():
            try:
                data = json.loads(self._path.read_text(encoding="utf-8"))
                logger.info("Checkpoint esistente caricato: %s", self._path)
                return data
            except (json.JSONDecodeError, OSError) as exc:
                logger.warning("Checkpoint corrotto, ricominciamo da zero: %s", exc)

        return {
            "file": str(self.audio_path),
            "stem": self.stem,
            "stages": {s: {"done": False} for s in self.STAGES},
            "chunks": [],
            "created_at": _now_iso(),
            "updated_at": _now_iso(),
        }

    def save(self) -> None:
        self._data["updated_at"] = _now_iso()
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(self._data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        tmp.replace(self._path)  # scrittura atomica

    # ------------------------------------------------------------------
    # Stadi della pipeline
    # ------------------------------------------------------------------

    def stage_done(self, stage: str) -> bool:
        """Restituisce True se lo stadio è già completato."""
        return self._data["stages"].get(stage, {}).get("done", False)

    def complete_stage(self, stage: str, **metadata: Any) -> None:
        """Marca uno stadio come completato e salva i metadati opzionali."""
        self._data["stages"][stage] = {"done": True, **metadata}
        self.save()
        logger.info("Stadio completato: %s %s", stage, metadata or "")

    def get_stage_data(self, stage: str) -> dict[str, Any]:
        """Restituisce i metadati salvati per uno stadio."""
        return self._data["stages"].get(stage, {})

    def all_done(self) -> bool:
        """True se tutti gli stadi sono completati."""
        return all(
            self._data["stages"].get(s, {}).get("done", False)
            for s in self.STAGES
        )

    # ------------------------------------------------------------------
    # Chunk ASR
    # ------------------------------------------------------------------

    def chunks_done_count(self) -> int:
        return len(self._data["chunks"])

    def add_chunk_result(self, chunk_result: dict[str, Any]) -> None:
        """
        Aggiunge il risultato di un chunk ASR al checkpoint.
        chunk_result deve avere almeno: idx, start, end, text.
        """
        self._data["chunks"].append(chunk_result)

    def get_all_chunks(self) -> list[dict[str, Any]]:
        return list(self._data["chunks"])

    def get_done_chunk_indices(self) -> set[int]:
        return {c["idx"] for c in self._data["chunks"]}

    # ------------------------------------------------------------------
    # Diarizzazione e prosodia (salvate come blob nel checkpoint)
    # ------------------------------------------------------------------

    def save_diarization(self, segments: list[dict[str, Any]]) -> None:
        self._data["diarization_segments"] = segments
        self.complete_stage("diarization", segments_count=len(segments))

    def get_diarization(self) -> list[dict[str, Any]]:
        return self._data.get("diarization_segments", [])

    def save_prosody(self, prosody_data: list[dict[str, Any]]) -> None:
        self._data["prosody_segments"] = prosody_data
        self.complete_stage("prosody", segments_count=len(prosody_data))

    def get_prosody(self) -> list[dict[str, Any]]:
        return self._data.get("prosody_segments", [])

    # ------------------------------------------------------------------
    # Utility
    # ------------------------------------------------------------------

    def progress_summary(self) -> str:
        stages_status = ", ".join(
            f"{s}={'✓' if self._data['stages'].get(s, {}).get('done') else '…'}"
            for s in self.STAGES
        )
        chunks = self.chunks_done_count()
        return f"[{self.stem}] {stages_status} | chunks ASR: {chunks}"

    def __repr__(self) -> str:
        return f"Checkpoint({self.stem!r}, done={self.all_done()})"


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
