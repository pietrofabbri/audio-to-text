#!/usr/bin/env python3
"""
Le pagine delle conversazioni del taccuino: una per giorno.

    python pannello/conversazioni.py <cartella giorni del corpus> <cartella di uscita>

Scrive `<uscita>/AAAA-MM-GG.html`, una pagina per giornata, con tutte le
frasi del giorno: ora, chi parla, cosa dice. Lo **stile del testo segue la
voce** di chi parla, rispetto alla sua voce abituale:

- **colore → tono** (altezza media della voce, f0): blu = più basso del
  solito, arancio/rosso = più alto;
- **dimensione → volume** (intensita' media in dB): più grande = più forte;
- **spaziatura → velocità** (sillabe al secondo): lettere larghe = più
  lento, strette = più veloce.

«Rispetto al solito» vuol dire: per ogni voce si calcolano mediana e
dispersione (scarto interquartile) di ognuna delle tre misure **su tutte le
giornate** del corpus, e ogni frase riceve un livello da −2 a +2. Cosi'
una voce acuta di natura non e' «sempre arancio»: il colore dice quando
quella persona parla più acuta di come parla di solito. Una voce con meno
di `MIN_CAMPIONI` frasi misurabili, o non attribuita, resta neutra.

Come per il pannello: ogni pagina e' **una sola** con i dati dentro, poi
StatiCrypt la cifra con la stessa frase d'accesso (vedi `pannello.yml`).
Il testo e' quello corretto quando c'e' (`segments.corrected.jsonl`),
altrimenti quello di Whisper; dove sono diversi la pagina permette di
vedere l'originale.

Specifica: `docs/analisi-corpus.md`, sezione 8.
"""

from __future__ import annotations

import json
import statistics
import sys
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from core.metriche import PAUSA_CONVERSAZIONE_SEC, SCONOSCIUTA, SOGGETTO  # noqa: E402

MISURE = {
    "tono": "f0_mean_hz",
    "vol": "intensity_mean_db",
    "vel": "speech_rate_syl_per_sec",
}
MIN_CAMPIONI = 20
DURATA_MINIMA_SEC = 0.8   # frasi più corte: misure troppo instabili


def _jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(r) for r in path.read_text(encoding="utf-8").splitlines() if r.strip()]


def _durata(s: dict) -> float:
    if isinstance(s.get("duration_sec"), (int, float)):
        return float(s["duration_sec"])
    a, b = s.get("day_sec_start"), s.get("day_sec_end")
    return float(b - a) if isinstance(a, (int, float)) and isinstance(b, (int, float)) else 0.0


def _misurabile(s: dict) -> bool:
    return (s.get("quality") != "unreliable"
            and _durata(s) >= DURATA_MINIMA_SEC
            and isinstance(s.get("prosody"), dict))


def basi_per_voce(cartella_giorni: Path) -> dict[str, dict[str, tuple[float, float]]]:
    """Per ogni voce e misura: (mediana, scala). Scala = IQR / 1.349."""
    valori: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    for d in sorted(Path(cartella_giorni).iterdir()):
        for s in _jsonl(d / "segments.jsonl"):
            voce = s.get("speaker") or SCONOSCIUTA
            if voce == SCONOSCIUTA or not _misurabile(s):
                continue
            for chiave, campo in MISURE.items():
                v = s["prosody"].get(campo)
                if isinstance(v, (int, float)) and v > 0:
                    valori[voce][chiave].append(float(v))
    basi: dict[str, dict[str, tuple[float, float]]] = {}
    for voce, per_misura in valori.items():
        for chiave, vv in per_misura.items():
            if len(vv) < MIN_CAMPIONI:
                continue
            q = statistics.quantiles(vv, n=4)
            scala = (q[2] - q[0]) / 1.349
            if scala > 0:
                basi.setdefault(voce, {})[chiave] = (statistics.median(vv), scala)
    return basi


def livello(x, base) -> int:
    """Da −2 a +2: quanto la frase si scosta dalla voce abituale."""
    if base is None or not isinstance(x, (int, float)) or x <= 0:
        return 0
    z = (x - base[0]) / base[1]
    if z < -1.5:
        return -2
    if z < -0.5:
        return -1
    if z <= 0.5:
        return 0
    if z <= 1.5:
        return 1
    return 2


def dati_giorno(cartella: Path, basi: dict) -> dict:
    """Il contenuto di una pagina: voci, conversazioni, frasi."""
    cartella = Path(cartella)
    manifesto = json.loads((cartella / "giorno.json").read_text(encoding="utf-8"))
    nomi = manifesto.get("speaker_names") or {}
    grezzi = _jsonl(cartella / "segments.jsonl")
    corretti = {s.get("day_idx"): s for s in _jsonl(cartella / "segments.corrected.jsonl")}
    segmenti = [s for s in grezzi if s.get("day_sec_start") is not None]
    segmenti.sort(key=lambda s: (s["day_sec_start"], s.get("day_sec_end", 0)))

    voci: list[str] = []
    indice: dict[str, int] = {}

    def vid(v: str) -> int:
        if v not in indice:
            indice[v] = len(voci)
            voci.append(v)
        return indice[v]

    vid(SOGGETTO)  # Pietro sempre per primo: colore fisso
    frasi = []
    conversazioni: list[dict] = []
    fine_prec = None
    for s in segmenti:
        voce = s.get("speaker") or SCONOSCIUTA
        a, b = s["day_sec_start"], s.get("day_sec_end", s["day_sec_start"])
        if fine_prec is None or a - fine_prec >= PAUSA_CONVERSAZIONE_SEC:
            conversazioni.append({"da": len(frasi), "inizio": round(a), "fine": round(b),
                                  "voci": set()})
        fine_prec = b if fine_prec is None else max(fine_prec, b)
        c = conversazioni[-1]
        c["fine"] = max(c["fine"], round(b))
        if voce != SCONOSCIUTA:
            c["voci"].add(voce)

        testo = (s.get("text") or "").strip()
        corr = corretti.get(s.get("day_idx"))
        testo_c = (corr.get("text") or "").strip() if corr else ""
        pro = s.get("prosody") or {}
        base = basi.get(voce, {})
        misurabile = _misurabile(s)
        riga = [
            round(a), vid(voce), testo_c or testo,
            livello(pro.get(MISURE["tono"]), base.get("tono")) if misurabile else 0,
            livello(pro.get(MISURE["vol"]), base.get("vol")) if misurabile else 0,
            livello(pro.get(MISURE["vel"]), base.get("vel")) if misurabile else 0,
            1 if s.get("quality") == "unreliable" else 0,
        ]
        if testo_c and testo_c != testo:
            riga.append(testo)  # l'originale di Whisper, solo se diverso
        frasi.append(riga)

    for c in conversazioni:
        c["voci"] = sorted(vid(v) for v in c["voci"])

    blocchi = []
    for b in manifesto.get("blocks") or []:
        try:
            blocchi.append([b["start"][11:16], b["end"][11:16]])
        except (KeyError, TypeError):
            pass

    return {
        "giorno": manifesto["day"],
        "voci": [{"id": v, "nome": (nomi.get(v) or ("non attribuita" if v == SCONOSCIUTA else v)),
                  "base": bool(basi.get(v))} for v in voci],
        "conversazioni": conversazioni,
        "frasi": frasi,
        "blocchi": blocchi,
        "corretto": bool(corretti),
    }


def costruisci(cartella_giorni: Path, uscita: Path) -> list[Path]:
    cartella_giorni, uscita = Path(cartella_giorni), Path(uscita)
    basi = basi_per_voce(cartella_giorni)
    giorni = [d for d in sorted(cartella_giorni.iterdir()) if (d / "giorno.json").exists()]
    elenco = [d.name for d in giorni]
    modello = (HERE / "modello_giorno.html").read_text(encoding="utf-8")
    uscita.mkdir(parents=True, exist_ok=True)
    scritte = []
    for d in giorni:
        dati = dati_giorno(d, basi)
        dati["giorni"] = elenco
        blob = json.dumps(dati, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
        p = uscita / f"{d.name}.html"
        p.write_text(modello.replace("__DATI__", blob), encoding="utf-8")
        scritte.append(p)
    return scritte


if __name__ == "__main__":
    if len(sys.argv) != 3:
        raise SystemExit(__doc__)
    for p in costruisci(Path(sys.argv[1]), Path(sys.argv[2])):
        print(p)
