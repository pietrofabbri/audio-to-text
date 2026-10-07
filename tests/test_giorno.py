"""
Test della giornata: le sessioni di un giorno in un file per tipo.

    python tests/test_giorno.py

Niente modelli e niente audio: sessioni finte scritte come le scrive la
pipeline (session.json, segments.jsonl, tokens.jsonl, wordfreq.csv).

Cosa si protegge.

  - **Il giorno giusto.** Un blocco continuo appartiene al giorno in cui
    comincia: una conversazione che passa la mezzanotte non si spezza.
  - **L'ora vera.** Ogni segmento e ogni parola portano l'ora
    dell'orologio, ma anche `session` + `start`/`end` del suo file: chi
    deve tornare all'audio sa ancora dove.
  - **Nessuna parola persa o contata due volte** sommando le frequenze.
  - **La prosodia non si ripete per ogni parola**: sta nel segmento.
"""

from __future__ import annotations

import csv
import io
import json
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from core.giorno import (  # noqa: E402
    FILE_GIORNO, SOGLIA_CONTINUITA_SEC, giornate, inizio_sessione,
)


class Failure(Exception):
    pass


def require(cond: bool, msg: str) -> None:
    if not cond:
        raise Failure(msg)


def _sessione(root: Path, stem: str, durata: float = 3600.0,
              segmenti: list[tuple] | None = None, wall: bool = True,
              parole_freq: dict | None = None, corretta: bool = False) -> Path:
    """Una sessione finta. `segmenti`: (start, end, voce, testo)."""
    d = root / stem
    d.mkdir(parents=True)
    segmenti = segmenti or [(0.0, 10.0, "GLOBAL_001", "ciao a tutti"),
                            (12.0, 20.0, "GLOBAL_002", "buongiorno")]
    segs = []
    tokens = []
    for i, (a, b, voce, testo) in enumerate(segmenti):
        segs.append({"idx": i, "start": a, "end": b, "duration_sec": b - a,
                     "text": testo, "speaker": voce, "speaker_local": f"SPEAKER_0{i}",
                     "quality": "ok", "quality_reasons": [], "no_speech_prob": 0.01,
                     "prosody": {"duration_sec": b - a, "f0_mean_hz": 120.0 + i}})
        for k, w in enumerate(testo.split()):
            tokens.append({"segment_idx": i, "token_idx": k, "word": w,
                           "start": a + k, "end": a + k + 0.5, "asr_prob": 0.9,
                           "speaker": f"SPEAKER_0{i}", "duration_sec": b - a,
                           "f0_mean_hz": 120.0 + i})
    sj = {"stem": stem, "duration": {"total_sec": durata, "speech_sec": durata / 2}}
    if wall:
        sj["session_start_wall"] = (stem[:10] + "T" + stem[11:19].replace("-", ":"))
    (d / "session.json").write_text(json.dumps(sj), encoding="utf-8")
    (d / "transcript.json").write_text(json.dumps(
        {"meta": {"stem": stem, "file": f"{stem}.MP3"}, "segments": segs}),
        encoding="utf-8")
    (d / "segments.jsonl").write_text(
        "".join(json.dumps(s) + "\n" for s in segs), encoding="utf-8")
    (d / "tokens.jsonl").write_text(
        "".join(json.dumps(t) + "\n" for t in tokens), encoding="utf-8")
    (d / "speaker_merge.json").write_text(json.dumps(
        {"merged": {"SPEAKER_04": "SPEAKER_00"}}), encoding="utf-8")
    if parole_freq is not None:
        voci = sorted({v for r in parole_freq.values() for v in r})
        righe = ["word,freq_global," + ",".join(f"freq_{v}" for v in voci)]
        for w, r in parole_freq.items():
            righe.append(f"{w},{sum(r.values())}," + ",".join(str(r.get(v, 0)) for v in voci))
        (d / "wordfreq.csv").write_text("\n".join(righe) + "\n", encoding="utf-8")
    if corretta:
        segs_c = [dict(s, text=s["text"].upper(), text_raw=s["text"]) for s in segs]
        (d / "segments.corrected.jsonl").write_text(
            "".join(json.dumps(s) + "\n" for s in segs_c), encoding="utf-8")
    return d


# ----------------------------------------------------------------------

def blocchi_e_giorni() -> None:
    """Buco sotto 5 minuti: stesso blocco. Sopra: blocco nuovo."""
    with tempfile.TemporaryDirectory() as tmp:
        r = Path(tmp)
        d = [_sessione(r, "2026-10-05_09-00-00"),
             _sessione(r, "2026-10-05_10-02-00"),          # 120 s dopo
             _sessione(r, "2026-10-05_11-20-00", 600.0)]   # 18 min dopo
        gg = giornate(d)
        require(len(gg) == 1 and gg[0].giorno == "2026-10-05", f"un giorno: {gg}")
        s = gg[0].sessioni
        require([x.blocco for x in s] == [0, 0, 1], f"blocchi: {[x.blocco for x in s]}")
        require(s[0].gap_prima is None and s[1].gap_prima == 120.0
                and s[2].gap_prima == 1080.0,
                f"buchi: {[x.gap_prima for x in s]}")
        m = gg[0].manifesto()
        require(len(m["blocks"]) == 2 and m["blocks"][0]["sessions"] ==
                ["2026-10-05_09-00-00", "2026-10-05_10-02-00"],
                f"blocchi nel manifesto: {m['blocks']}")
        require(m["sessions"][0]["speaker_merge"] == {"SPEAKER_04": "SPEAKER_00"},
                "le fusioni delle voci entrano nel manifesto")
        require(SOGLIA_CONTINUITA_SEC == 300.0, "la soglia e' 5 minuti (ROADMAP D3)")


def la_mezzanotte_non_spezza_una_conversazione() -> None:
    """Un blocco che passa la mezzanotte resta nel giorno in cui comincia."""
    with tempfile.TemporaryDirectory() as tmp:
        r = Path(tmp)
        d = [_sessione(r, "2026-10-05_23-30-00"),           # fino alle 00:30
             _sessione(r, "2026-10-06_00-31-00", 600.0,     # 60 s dopo: continua
                       [(0.0, 5.0, "GLOBAL_001", "buonanotte")]),
             _sessione(r, "2026-10-06_09-00-00")]           # mattina dopo
        gg = {g.giorno: g for g in giornate(d)}
        require(sorted(gg) == ["2026-10-05", "2026-10-06"], f"giorni: {sorted(gg)}")
        require([s.stem for s in gg["2026-10-05"].sessioni] ==
                ["2026-10-05_23-30-00", "2026-10-06_00-31-00"],
                "la sessione dopo mezzanotte continua il blocco del giorno prima")
        segs = [json.loads(x) for x in gg["2026-10-05"].segments_jsonl().splitlines()]
        dopo = [x for x in segs if x["session"] == "2026-10-06_00-31-00"][0]
        require(dopo["clock_start"] == "00:31:00" and dopo["day_sec_start"] == 88260.0,
                f"oltre mezzanotte i secondi del giorno continuano: {dopo}")
        srt = gg["2026-10-05"].transcript_srt()
        require("24:31:00,000 --> 24:31:05,000" in srt,
                "nell'SRT del giorno l'ora oltre mezzanotte continua a contare")


def ora_vera_e_campi_originali() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        r = Path(tmp)
        g = giornate([_sessione(r, "2026-10-05_09-39-09")])[0]
        segs = [json.loads(x) for x in g.segments_jsonl().splitlines()]
        require(segs[1]["start"] == 12.0 and segs[1]["session"] == "2026-10-05_09-39-09",
                "start resta il secondo nel file, session dice quale file")
        require(segs[1]["clock_start"] == "09:39:21" and segs[1]["day_idx"] == 1,
                f"ora vera e indice del giorno: {segs[1]}")
        txt = g.transcript_txt()
        require("[09:39:09 → 09:39:19] GLOBAL_001" in txt, "il testo usa l'ora vera")
        require("inizio della giornata" in txt, "la prima registrazione e' detta")


def parole_senza_prosodia_ripetuta() -> None:
    """Ogni parola porta il suo segmento, non sedici campi copiati."""
    with tempfile.TemporaryDirectory() as tmp:
        r = Path(tmp)
        g = giornate([_sessione(r, "2026-10-05_09-00-00"),
                      _sessione(r, "2026-10-05_10-01-00")])[0]
        tok = [json.loads(x) for x in g.tokens_jsonl().splitlines()]
        require(len(tok) == 8, f"4 parole per sessione, 8 in tutto: {len(tok)}")
        require(all("f0_mean_hz" not in t and "duration_sec" not in t for t in tok),
                "la prosodia del segmento non deve ripetersi nelle parole")
        ultimo = tok[-1]
        require(ultimo["day_segment_idx"] == 3 and ultimo["speaker_global"] == "GLOBAL_002",
                f"la parola deve dire a che segmento e voce appartiene: {ultimo}")
        segs = [json.loads(x) for x in g.segments_jsonl().splitlines()]
        require(segs[3]["prosody"]["f0_mean_hz"] == 121.0,
                "la prosodia si ritrova nel segmento indicato")


def frequenze_sommate() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        r = Path(tmp)
        g = giornate([
            _sessione(r, "2026-10-05_09-00-00",
                      parole_freq={"che": {"GLOBAL_001": 3, "GLOBAL_002": 1}}),
            _sessione(r, "2026-10-05_10-01-00",
                      parole_freq={"che": {"GLOBAL_001": 2},
                                   "pane": {"GLOBAL_003": 4}}),
        ])[0]
        righe = list(csv.DictReader(io.StringIO(g.wordfreq_csv())))
        che = [x for x in righe if x["word"] == "che"][0]
        require(che["freq_global"] == "6" and che["freq_GLOBAL_001"] == "5"
                and che["freq_GLOBAL_002"] == "1" and che["freq_GLOBAL_003"] == "0",
                f"somme sbagliate: {che}")
        require(righe[0]["word"] == "che", "le parole in ordine di frequenza")


def sessione_senza_session_json() -> None:
    """L'ora di inizio si ricava dal nome se manca altrove."""
    with tempfile.TemporaryDirectory() as tmp:
        d = _sessione(Path(tmp), "2026-10-04_12-07-22", wall=False)
        require(inizio_sessione(d).isoformat() == "2026-10-04T12:07:22",
                "l'ora dal nome della sessione")


def corretti_solo_se_ci_sono() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        r = Path(tmp)
        f = giornate([_sessione(r, "2026-10-05_09-00-00")])[0].file()
        require(set(f) == set(FILE_GIORNO), f"senza correzioni, solo i file base: {sorted(f)}")
    with tempfile.TemporaryDirectory() as tmp:
        r = Path(tmp)
        f = giornate([_sessione(r, "2026-10-05_09-00-00", corretta=True),
                      _sessione(r, "2026-10-05_10-01-00")])[0].file()
        require("transcript.corrected.txt" in f, "con una correzione, il testo corretto")
        require("CIAO A TUTTI" in f["transcript.corrected.txt"]
                and "ciao a tutti" in f["transcript.corrected.txt"],
                "il corretto dove c'e', l'originale dove no")
        require("1 sessioni su 2" in f["transcript.corrected.txt"],
                "il file dice quante sessioni sono corrette")


def nomi_solo_se_dati() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        r = Path(tmp)
        d = [_sessione(r, "2026-10-05_09-00-00")]
        senza = giornate(d)[0].file()
        con = giornate(d, nomi={"GLOBAL_001": "Pietro"})[0].file()
        require("Pietro" not in "".join(senza.values()), "senza nomi, nessun nome")
        require("Pietro (GLOBAL_001)" in con["transcript.txt"]
                and json.loads(con["giorno.json"])["speaker_names"] == {"GLOBAL_001": "Pietro"},
                "con i nomi: nome e pseudonimo nel testo, mappa nel manifesto")


CHECKS = [
    ("blocchi e giorni", blocchi_e_giorni),
    ("la mezzanotte non spezza una conversazione",
     la_mezzanotte_non_spezza_una_conversazione),
    ("ora vera e campi originali", ora_vera_e_campi_originali),
    ("parole senza prosodia ripetuta", parole_senza_prosodia_ripetuta),
    ("frequenze sommate sul giorno", frequenze_sommate),
    ("ora di inizio dal nome", sessione_senza_session_json),
    ("file corretti solo se ci sono", corretti_solo_se_ci_sono),
    ("nomi solo se dati", nomi_solo_se_dati),
]


def main() -> int:
    passed = 0
    failed: list[str] = []
    for name, fn in CHECKS:
        try:
            fn()
            passed += 1
            print(f"  ok  {name}")
        except Failure as exc:
            failed.append(f"{name}: {exc}")
            print(f"  KO  {name} - {exc}")
        except Exception as exc:  # noqa: BLE001
            failed.append(f"{name}: {type(exc).__name__}: {exc}")
            print(f"  ERR {name} - {type(exc).__name__}: {exc}")
    print(f"\n{passed}/{len(CHECKS)} superati")
    if failed:
        print("Falliti:")
        for f in failed:
            print(f"  - {f}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
