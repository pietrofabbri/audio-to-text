#!/usr/bin/env python3
"""Quanto sbaglia la trascrizione, misurato su minuti corretti a mano.

    python misura_trascrizione.py prepara          # sceglie e taglia gli estratti
    python misura_trascrizione.py misura           # WER e attribuzione, oggi
    python misura_trascrizione.py misura --asr large-v3   # e un altro modello

Gli estratti stanno in data/riferimento/ (fuori dal corpus: e' la voce di
persone vere). Per ogni estratto:

    estratto_01.mp3            l'audio, un minuto
    estratto_01.txt            DA CORREGGERE: parte dal testo attuale
    estratto_01.ipotesi.txt    il testo com'era il giorno del taglio, non toccare

`misura` confronta il riferimento con il testo che la pipeline produce
adesso (rileggendo output/), e con quello del giorno del taglio: dopo una
modifica alla trascrizione si vede di quanto e' cambiato. Un estratto il
cui .txt e' ancora uguale all'ipotesi non e' stato corretto e non conta.
Vedi core/riferimento.py.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from core.riferimento import (confronta, leggi_testo_con_voci,  # noqa: E402
                              parole_sessione, scegli_finestra, testo_con_voci)

CARTELLA = ROOT / "data" / "riferimento"
DURATA = 60.0

# Situazioni diverse, una sessione ciascuna: scuola, riunioni, il focus
# group, il resto della giornata. Scelte dall'archivio del 29/10
# (l'audio originale resta 7 giorni).
SESSIONI = [
    ("mattina", "2026-10-21_10-21-39"),
    ("prima mattina", "2026-10-22_07-30-56"),
    ("lezione di informatica", "2026-10-26_13-36-49"),
    ("in classe", "2026-10-27_13-28-35"),
    ("mattina", "2026-10-22_09-35-48"),
    ("riunione sull'IA", "2026-10-26_15-39-36"),
    ("pomeriggio", "2026-10-26_16-48-59"),
    ("focus group", "2026-10-27_15-32-38"),
    ("focus group", "2026-10-27_16-34-38"),
    ("dopo il focus group", "2026-10-27_17-35-37"),
    ("sera", "2026-10-27_18-41-09"),
    ("pomeriggio", "2026-10-21_15-39-13"),
]

ISTRUZIONI = """\
# Estratto {n}: {situazione}, {sessione}, dal minuto {da} al {a} della registrazione.
# Ascolta l'audio (estratto_{n}.mp3) e correggi il testo QUI SOTTO perche'
# sia esattamente quello che si sente: parole, e chi le dice.
# - Una riga per turno: [Nome] testo. Cambia il nome se e' sbagliato.
# - Se due parlano insieme, scrivi entrambi i turni, uno dopo l'altro.
# - Le parole che non si capiscono neanche ascoltando: [?] al loro posto.
# - Punteggiatura e maiuscole non contano.
# - Non leggere e basta: il testo sbagliato ma plausibile si vede solo ascoltando.
"""


def _nomi() -> dict[str, str]:
    from core.speaker_db import SpeakerDB
    db = SpeakerDB()
    return {g: db.get_name(g) for g in db._data["speakers"] if db.get_name(g)}


def _finestra(stem: str, inizio: float) -> list:
    return [p for p in parole_sessione(ROOT / "output" / stem)
            if inizio <= p.inizio < inizio + DURATA]


def cmd_prepara(args) -> int:
    from core.voice_review import trova_audio
    CARTELLA.mkdir(parents=True, exist_ok=True)
    manifesto_path = CARTELLA / "estratti.json"
    manifesto = json.loads(manifesto_path.read_text()) if manifesto_path.exists() else []
    gia = {e["sessione"] for e in manifesto}
    nomi = _nomi()
    for situazione, stem in SESSIONI:
        if stem in gia:
            continue
        audio = trova_audio(stem)
        parole = parole_sessione(ROOT / "output" / stem)
        inizio = scegli_finestra(parole, DURATA)
        if audio is None or inizio is None:
            print(f"  {stem}: niente audio o niente parole, salto")
            continue
        n = f"{len(manifesto) + 1:02d}"
        mp3 = CARTELLA / f"estratto_{n}.mp3"
        subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-ss", f"{inizio:.2f}",
                        "-t", f"{DURATA:.0f}", "-i", str(audio), "-ac", "1",
                        "-b:a", "96k", str(mp3)], check=True)
        dentro = [p for p in parole if inizio <= p.inizio < inizio + DURATA]
        ipotesi = testo_con_voci(dentro, nomi)
        da = f"{int(inizio // 60):02d}:{int(inizio % 60):02d}"
        a = f"{int((inizio + DURATA) // 60):02d}:{int((inizio + DURATA) % 60):02d}"
        intest = ISTRUZIONI.format(n=n, situazione=situazione, sessione=stem, da=da, a=a)
        (CARTELLA / f"estratto_{n}.ipotesi.txt").write_text(ipotesi, encoding="utf-8")
        (CARTELLA / f"estratto_{n}.txt").write_text(intest + "\n" + ipotesi, encoding="utf-8")
        manifesto.append({"n": n, "situazione": situazione, "sessione": stem,
                          "inizio": round(inizio, 2), "durata": DURATA,
                          "parole": len(dentro)})
        print(f"  estratto_{n}: {situazione}, {stem} da {da}, {len(dentro)} parole")
    manifesto_path.write_text(json.dumps(manifesto, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n{len(manifesto)} estratti in {CARTELLA}")
    return 0


def _asr(modello: str, mp3: Path) -> list[tuple[str, str]]:
    from faster_whisper import WhisperModel
    from core.config import config
    m = WhisperModel(modello, device="cpu", compute_type=config.asr.compute_type)
    segs, _ = m.transcribe(str(mp3), language="it", initial_prompt=config.asr.style_prompt,
                           condition_on_previous_text=False)
    return leggi_testo_con_voci("[?] " + " ".join(s.text for s in segs))


def cmd_misura(args) -> int:
    manifesto = json.loads((CARTELLA / "estratti.json").read_text())
    nomi = _nomi()
    tot = {k: [0, 0, 0, 0, 0, 0] for k in ("taglio", "oggi", "asr")}
    righe = []
    for e in manifesto:
        n = e["n"]
        rif_txt = (CARTELLA / f"estratto_{n}.txt").read_text(encoding="utf-8")
        ipo_txt = (CARTELLA / f"estratto_{n}.ipotesi.txt").read_text(encoding="utf-8")
        rif = leggi_testo_con_voci(rif_txt)
        if rif == leggi_testo_con_voci(ipo_txt):
            righe.append(f"  {n} {e['situazione']:<20} non ancora corretto")
            continue
        casi = {"taglio": leggi_testo_con_voci(ipo_txt),
                "oggi": leggi_testo_con_voci(testo_con_voci(_finestra(e["sessione"], e["inizio"]), nomi))}
        if args.asr:
            casi["asr"] = _asr(args.asr, CARTELLA / f"estratto_{n}.mp3")
        parti = []
        for k, ipo in casi.items():
            c = confronta(rif, ipo)
            t = tot[k]
            t[0] += c["parole_riferimento"]; t[1] += c["sostituzioni"] + c["cancellazioni"] + c["inserimenti"]
            t[2] += c["parole_giuste"]
            t[3] += round((c["attribuzione"] or 0) * c["parole_giuste"])
            parti.append(f"{k} WER {c['wer']:.0%}" + (f" voci {c['attribuzione']:.0%}" if k != "asr" and c["attribuzione"] is not None else ""))
        righe.append(f"  {n} {e['situazione']:<20} {len(rif):4d} parole | " + " | ".join(parti))
    print("\n".join(righe))
    print()
    for k, (n, err, giuste, voci, *_ ) in tot.items():
        if n:
            print(f"  {k:<7} WER {err / n:.1%} su {n} parole"
                  + (f", voci giuste {voci / giuste:.1%}" if k != "asr" and giuste else ""))
    if not any(t[0] for t in tot.values()):
        print("Nessun estratto corretto finora.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("prepara")
    m = sub.add_parser("misura")
    m.add_argument("--asr", default=None, help="modello faster-whisper da confrontare, es. large-v3")
    args = ap.parse_args()
    return {"prepara": cmd_prepara, "misura": cmd_misura}[args.cmd](args)


if __name__ == "__main__":
    raise SystemExit(main())
