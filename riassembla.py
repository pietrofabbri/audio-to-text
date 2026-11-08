#!/usr/bin/env python3
"""Rifa' solo l'assemblaggio degli output di tutte le sessioni, dai checkpoint.

    python riassembla.py            # tutte le sessioni complete
    python riassembla.py 2026-10-27 # solo quelle il cui nome comincia cosi'

Serve dopo un cambiamento che riguarda come si scrivono gli output e non
l'audio: una regola di qualita' nuova (core/quality.py, 29/10: frasi
inventate e prompt nel testo), un formato. Non rilegge l'audio e non
rifa' trascrizione, diarizzazione o prosodia: legge il checkpoint e
riscrive output/<sessione>/ e corpus.db. Dopo: python publish_corpus.py push
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import run  # noqa: E402
from core.checkpoint import Checkpoint  # noqa: E402
from core.config import OUTPUT_DIR, config as cfg  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("prefisso", nargs="?", default="")
    a = ap.parse_args()
    ns = argparse.Namespace(no_prosody=False, no_diarization=False, retranscribe=False)
    fatte = saltate = 0
    for d in sorted(p for p in OUTPUT_DIR.iterdir() if p.is_dir() and p.name.startswith(a.prefisso)):
        f = d / f"{d.name}.checkpoint.json"
        if not f.exists():
            continue
        dati = json.loads(f.read_text(encoding="utf-8"))
        stadi = dati.get("stages") or {}
        if not all((stadi.get(s) or {}).get("done") for s in ("transcription", "diarization", "prosody", "assembly")):
            saltate += 1
            print(f"  {d.name}: incompleta, la lascio alla pipeline")
            continue
        audio = Path(dati.get("file") or d / f"{d.name}.wav")
        ck = Checkpoint(audio, OUTPUT_DIR, stem=d.name)
        ck.reset_stage("assembly")
        ck.save()
        run.process_file(audio, cfg, ns, stem=d.name)
        fatte += 1
        print(f"  {d.name}: riscritta")
    print(f"\nRiscritte {fatte} sessioni, {saltate} lasciate alla pipeline.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
