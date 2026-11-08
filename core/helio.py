"""
Le misure del braccialetto Amazfit Helio Strap, dall'export dei dati di Zepp.

Da dove vengono
---------------
Zepp non ha un'API pubblica. L'unico canale completo e' l'esportazione dei
dati personali (GDPR): app Zepp -> Profilo -> Impostazioni -> «Esercizio dei
diritti dell'utente» -> «Esporta dati», oppure il portale indicato in
https://www.zepp.com/privacy-support. Arriva uno zip con password; una volta
estratto e' una cartella `<userId>_<millisecondi>` con una sottocartella per
tipo, ognuna con un CSV `<TIPO>_<millisecondi>.csv` (UTF-8 con BOM).

Pietro fa un export circa al mese. Ogni export contiene tutta la storia
dall'inizio, quindi gli export si sovrappongono: `carica()` li legge tutti e
per ogni chiave (minuto, giorno, notte) vince l'export piu' recente.

Cosa c'e' e cosa no (export del 7/11/2026, dati dal 20/10)
----------------------------------------------------------
| Cartella          | Contenuto                                   | Orario |
|-------------------|---------------------------------------------|--------|
| HEARTRATE_AUTO    | battito, un valore al minuto                | locale |
| ACTIVITY_MINUTE   | passi al minuto, solo i minuti con passi    | locale |
| ACTIVITY          | passi, distanza, calorie per giorno         | data   |
| ACTIVITY_STAGE    | tratti di cammino (inizio, fine, passi)     | locale |
| SLEEP             | una riga per notte: inizio, fine, minuti di  | UTC    |
|                   | sonno profondo, leggero, REM, sveglio       |        |
| SLEEP_MINUTE      | fase del sonno al minuto                    | vedi * |
| SPORT             | allenamenti: tipo, inizio, durata, calorie  | UTC    |
| USER, BODY        | profilo, peso, altezza                      | —      |
| HEARTRATE,        | vuoti in questo export                      | —      |
| HEALTH_DATA       |                                             |        |

Non ci sono HRV, stress, SpO2, battito a riposo, temperatura: Zepp li mostra
nell'app ma non li esporta. Al loro posto, due stime dichiarate come tali e
calcolate qui dal battito al minuto: il battito a riposo (media piu' bassa su
30 minuti consecutivi nella notte) e l'«attivazione da fermo» (quota dei
minuti svegli e senza movimento con il battito sopra la soglia personale;
costanti ATTIV_* e funzione `attivazione`). Non sono lo stress di Zepp, che
viene dall'HRV. USER e BODY non si leggono: non servono al
pannello e sono dati anagrafici.

Le trappole dell'export, misurate sui dati veri
-----------------------------------------------
1. **Fusi diversi nello stesso export.** HEARTRATE_AUTO e ACTIVITY_MINUTE sono
   in ora locale (Europe/Rome); SLEEP e SPORT in UTC (`+0000`). Verificato
   due modi: il battito durante i 41 allenamenti di almeno 10 minuti e'
   massimo con uno sfasamento di +1 h rispetto all'UTC (il periodo e' quasi
   tutto in ora solare); durante le notti di SLEEP portate in ora locale il
   battito e' basso e i passi quasi zero.
2. **SLEEP_MINUTE ha la data spostata di un giorno in avanti.** Le ore sono
   locali, ma ogni notte compare con la data del giorno dopo: la notte che
   SLEEP colloca fra le 01:05 e le 08:32 del 21/10 (ora locale) in
   SLEEP_MINUTE sta fra le 01:05 e le 08:31 del 22/10, e l'export del 7/11
   alle 13:27 contiene minuti di sonno «dell'8/11». La sua colonna `hr` e'
   presa da HEARTRATE_AUTO alla data sbagliata, quindi non si usa.
   Lo spostamento non e' scritto qui come costante: `leggi_export()` lo
   misura a ogni export (quale spostamento mette piu' minuti di SLEEP_MINUTE
   dentro le notti di SLEEP) e lo dichiara negli avvisi, cosi' se Zepp
   corregge l'export il codice se ne accorge da solo.
3. **Orari «24:xx».** Nel giorno del cambio d'ora (25/10) HEARTRATE_AUTO ha
   minuti 24:00-24:06: si leggono come 00:00-00:06 del giorno dopo, e se quel
   minuto c'e' gia' si tiene il valore gia' letto.
4. **Righe vuote in SLEEP.** Le notti senza dati hanno inizio uguale alla
   fine e tutti i minuti a zero: si scartano.
5. **ACTIVITY e ACTIVITY_MINUTE non coincidono** in alcuni giorni (per il
   24/10: 8481 passi contro 7751): il totale del giorno viene da ACTIVITY,
   la distribuzione per ora da ACTIVITY_MINUTE.

A chi si attribuisce una notte
------------------------------
Al giorno in cui finisce (la sveglia): e' la regola «notte -> giorno dopo»
del documento `analisi-corpus.md` (sezione 5, punto 7).

Uso
---
    from core.helio import carica, calcola_giorno, giorni
    h = carica()                      # data/helio/export/*
    for g in giorni(h):
        blocco = calcola_giorno(h, g)

`importa_helio.py` copia un export nuovo in `data/helio/export/`.
"""

from __future__ import annotations

import csv
import json
import os
import shutil
import statistics
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo

VERSIONE = 2

ROOT = Path(__file__).resolve().parent.parent
# `A2T_HELIO_DIR` sposta l'archivio (i test lo puntano a una cartella vuota,
# cosi' non leggono i dati veri).
EXPORT_DIR = Path(os.environ.get("A2T_HELIO_DIR") or ROOT / "data" / "helio" / "export")

FUSO = ZoneInfo("Europe/Rome")

# Le cartelle che si leggono. Le altre (USER, BODY, HEALTH_DATA,
# HEARTRATE) si copiano con l'export ma non entrano nel corpus.
TIPI = ("HEARTRATE_AUTO", "ACTIVITY_MINUTE", "ACTIVITY", "SLEEP",
        "SLEEP_MINUTE", "SPORT")

# Un «altro tratto di sonno» e' un tratto di SLEEP_MINUTE fuori dalle notti
# di SLEEP, lungo almeno questo; dentro, buchi fino a GAP_PISOLINO_MIN non
# lo spezzano.
PISOLINO_MIN = 10
GAP_PISOLINO_MIN = 5

# Per il confronto battito/parlato: un minuto e' «fermo» se non ci sono
# passi in quel minuto e nei FERMO_INTORNO minuti prima e dopo. Serve a
# separare l'effetto del parlare da quello del camminare (si parla spesso
# camminando, e il battito sale per il cammino).
FERMO_INTORNO = 2
# Minimo di secondi registrati perche' un minuto conti come registrato.
MIN_REGISTRATO_SEC = 30.0
# Minimo di minuti per dare una media di classe.
MIN_MINUTI_CLASSE = 5

FASI = ("DEEP", "LIGHT", "REM", "WAKE")

# Battito a riposo stimato: la media piu' bassa su RIPOSO_FINESTRA_MIN
# minuti consecutivi dentro la notte, con almeno RIPOSO_MIN_VALORI valori.
# Zepp calcola il suo «battito a riposo» ma non lo esporta: questo e' una
# stima indipendente, dichiarata come tale.
RIPOSO_FINESTRA_MIN = 30
RIPOSO_MIN_VALORI = 20

# «Attivazione da fermo»: una stima dal solo battito al minuto, al posto
# dello stress di Zepp, che non si esporta (lo stress di Zepp viene
# dall'HRV, che nell'export non c'e'). Un minuto e' «fermo e sveglio» se:
# non si dorme (notti e altri tratti di sonno), non e' dentro un
# allenamento rilevato, non ci sono passi in quel minuto ne' nei
# ATTIV_FERMO_PRIMA minuti prima (il battito resta alto per qualche minuto
# dopo il cammino). E' «attivato» se il battito supera la soglia personale:
# il percentile ATTIV_PERCENTILE dei minuti fermi e svegli negli
# ATTIV_RIF_GIORNI giorni che finiscono con quello misurato. Per
# costruzione, in un giorno tipico la quota e' intorno o sotto il 10% (sotto, perche' il battito e' intero e conta
# solo chi supera la soglia, non chi la eguaglia): conta lo
# scarto da li', non il valore assoluto. Confonde: caffe', caldo,
# digestione, stare in piedi, muovere le braccia senza camminare (la fascia
# e' sul braccio), parlare (vedi `battito_e_parlato`).
ATTIV_FERMO_PRIMA = 5
ATTIV_PERCENTILE = 0.90
ATTIV_RIF_GIORNI = 28
# Minimi: minuti fermi e svegli per dare la quota di un giorno; giorni con
# quel minimo per avere una soglia; minuti fermi per la quota di un'ora.
ATTIV_MIN_FERMI = 60
ATTIV_MIN_GIORNI_RIF = 3
ATTIV_MIN_ORA = 10

LIMITI = {
    "fonte": "export dei dati personali di Zepp (GDPR), circa uno al mese",
    "assenti": ("HRV, stress, SpO2, battito a riposo e temperatura non sono "
                "nell'export di Zepp; battito a riposo e attivazione da fermo "
                "sono stime calcolate qui dal battito al minuto"),
    "attivazione": ("quota dei minuti svegli, senza passi e fuori dagli "
                    "allenamenti, con il battito sopra il 90esimo percentile "
                    "personale degli ultimi 28 giorni: una stima dal battito, "
                    "non lo stress di Zepp (che viene dall'HRV)"),
    "battito": "un valore al minuto (HEARTRATE_AUTO), media per 5 minuti nel grafico",
    "notte": "attribuita al giorno della sveglia",
    "orologi": ("battito e registrazioni sono allineati con l'ora del "
                "registratore, la cui deriva non e' ancora misurata"),
}


@dataclass
class Helio:
    """Le misure lette da uno o piu' export, gia' in ora locale."""
    battito: dict[datetime, int] = field(default_factory=dict)
    passi_minuto: dict[datetime, int] = field(default_factory=dict)
    attivita: dict[str, dict[str, Any]] = field(default_factory=dict)
    notti: dict[tuple[datetime, datetime], dict[str, Any]] = field(default_factory=dict)
    fasi: dict[datetime, str] = field(default_factory=dict)
    sport: dict[datetime, dict[str, Any]] = field(default_factory=dict)
    esportazioni: list[str] = field(default_factory=list)
    avvisi: list[str] = field(default_factory=list)
    _cache_fermi: dict[str, dict[datetime, int]] = field(
        default_factory=dict, repr=False, compare=False)

    def aggiorna(self, altro: "Helio") -> None:
        """Aggiunge un export piu' recente: a parita' di chiave vince lui."""
        self.battito.update(altro.battito)
        self.passi_minuto.update(altro.passi_minuto)
        self.attivita.update(altro.attivita)
        # Una notte rivista da un export successivo puo' cambiare inizio
        # o fine: si tolgono le notti vecchie che si sovrappongono.
        for chiave in altro.notti:
            a, b = chiave
            for vecchia in [k for k in self.notti if k[0] < b and a < k[1]]:
                del self.notti[vecchia]
        self.notti.update(altro.notti)
        self.fasi.update(altro.fasi)
        self.sport.update(altro.sport)
        self.esportazioni += altro.esportazioni
        self._cache_fermi.clear()
        self.avvisi += altro.avvisi


# ----------------------------------------------------------------------
# Lettura di un export
# ----------------------------------------------------------------------

def _righe(cartella: Path, tipo: str) -> list[dict[str, str]]:
    """Le righe del CSV di un tipo; [] se il tipo manca."""
    file = sorted(cartella.glob(f"{tipo}/{tipo}_*.csv")) or sorted(
        cartella.glob(f"{tipo}_*.csv"))
    out: list[dict[str, str]] = []
    for f in file:
        with f.open(encoding="utf-8-sig", newline="") as fh:
            out.extend(csv.DictReader(fh))
    return out


def _minuto(giorno: str, ora: str) -> datetime:
    """`2026-10-20`, `18:22` -> datetime locale; `24:05` e' il giorno dopo."""
    h, m = (int(x) for x in ora.strip().split(":")[:2])
    return datetime.fromisoformat(giorno.strip()) + timedelta(hours=h, minutes=m)


def _utc_locale(testo: str) -> datetime:
    """`2026-10-20 23:05:00+0000` -> ora locale senza fuso."""
    t = datetime.strptime(testo.strip()[:19], "%Y-%m-%d %H:%M:%S")
    return t.replace(tzinfo=timezone.utc).astimezone(FUSO).replace(tzinfo=None)


def _intero(v: str | None) -> int | None:
    try:
        return int(float(v)) if v not in (None, "", "null") else None
    except ValueError:
        return None


def _dentro(t: datetime, finestre: Iterable[tuple[datetime, datetime]]) -> bool:
    return any(a <= t < b for a, b in finestre)


def leggi_export(cartella: Path) -> Helio:
    """Legge una cartella di export Zepp gia' estratta."""
    cartella = Path(cartella)
    if not cartella.is_dir():
        raise FileNotFoundError(f"{cartella} non e' una cartella")
    h = Helio(esportazioni=[cartella.name])

    for r in _righe(cartella, "HEARTRATE_AUTO"):
        v = _intero(r.get("heartRate"))
        if v and r.get("date") and r.get("time"):
            # setdefault: un «24:05» non sovrascrive un «00:05» gia' letto.
            h.battito.setdefault(_minuto(r["date"], r["time"]), v)

    for r in _righe(cartella, "ACTIVITY_MINUTE"):
        v = _intero(r.get("steps"))
        if v is not None and r.get("date") and r.get("time"):
            h.passi_minuto[_minuto(r["date"], r["time"])] = v

    for r in _righe(cartella, "ACTIVITY"):
        if r.get("date"):
            h.attivita[r["date"].strip()] = {
                "passi": _intero(r.get("steps")),
                "distanza_m": _intero(r.get("distance")),
                "calorie": _intero(r.get("calories")),
            }

    for r in _righe(cartella, "SLEEP"):
        if not (r.get("start") and r.get("stop")):
            continue
        a, b = _utc_locale(r["start"]), _utc_locale(r["stop"])
        profondo = _intero(r.get("deepSleepTime")) or 0
        leggero = _intero(r.get("shallowSleepTime")) or 0
        rem = _intero(r.get("REMTime")) or 0
        if b <= a or profondo + leggero + rem == 0:
            continue
        h.notti[(a, b)] = {
            "profondo_min": profondo, "leggero_min": leggero, "rem_min": rem,
            "sveglio_min": _intero(r.get("wakeTime")) or 0,
        }

    # SLEEP_MINUTE: si misura lo spostamento di data (trappola 2).
    grezze = [(_minuto(r["date"], r["time"]), (r.get("stage") or "").strip().upper())
              for r in _righe(cartella, "SLEEP_MINUTE")
              if r.get("date") and r.get("time")]
    if grezze:
        finestre = list(h.notti)
        migliore, punti = 0, -1
        for giorni_sposta in (0, -1, 1):
            d = timedelta(days=giorni_sposta)
            n = sum(_dentro(t + d, finestre) for t, _ in grezze)
            if n > punti:
                migliore, punti = giorni_sposta, n
        if finestre and punti == 0:
            h.avvisi.append(f"{cartella.name}: SLEEP_MINUTE non combacia con "
                            "SLEEP con nessuno spostamento: fasi scartate")
        else:
            if migliore:
                h.avvisi.append(f"{cartella.name}: SLEEP_MINUTE spostato di "
                                f"{migliore:+d} giorni per combaciare con SLEEP "
                                f"({punti} minuti su {len(grezze)} dentro le notti)")
            d = timedelta(days=migliore)
            for t, fase in grezze:
                if fase in FASI:
                    h.fasi[t + d] = fase

    for r in _righe(cartella, "SPORT"):
        if not r.get("startTime"):
            continue
        inizio = _utc_locale(r["startTime"])
        h.sport[inizio] = {
            "tipo": _intero(r.get("type")),
            "secondi": _intero(r.get("sportTime(s)")) or 0,
            "kcal": _intero(r.get("calories(kcal)")),
            "distanza_m": _intero(r.get("distance(m)")),
        }
    return h


def _ordine_export(p: Path) -> tuple[int, str]:
    """Gli export si chiamano `<userId>_<millisecondi>`: si ordinano per data."""
    coda = p.name.rsplit("_", 1)[-1]
    return (int(coda) if coda.isdigit() else 0, p.name)


def carica(cartella: Path = EXPORT_DIR) -> Helio | None:
    """Tutti gli export in `cartella`, dal piu' vecchio al piu' recente."""
    cartella = Path(cartella)
    if not cartella.is_dir():
        return None
    export = sorted((p for p in cartella.iterdir() if p.is_dir()), key=_ordine_export)
    if not export:
        return None
    h = Helio()
    for p in export:
        h.aggiorna(leggi_export(p))
    return h


def importa(sorgente: Path, cartella: Path = EXPORT_DIR) -> Path:
    """Copia un export estratto in `data/helio/export/<nome>`.

    Si copia tutto, anche i tipi che non si leggono: e' l'archivio
    originale, e un giorno puo' servire. Se c'e' gia' una copia con lo
    stesso nome non si tocca.
    """
    sorgente = Path(sorgente).expanduser()
    leggi_export(sorgente)  # fallisce qui se non e' un export leggibile
    if not any((sorgente / t).is_dir() or list(sorgente.glob(f"{t}_*.csv"))
               for t in TIPI):
        raise ValueError(f"{sorgente}: nessuna delle cartelle {', '.join(TIPI)}")
    dest = Path(cartella) / sorgente.name
    if dest.exists():
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(sorgente, dest)
    return dest


# ----------------------------------------------------------------------
# Una giornata
# ----------------------------------------------------------------------

def giorni(h: Helio | None) -> list[str]:
    """I giorni con almeno un minuto di battito o una notte."""
    if h is None:
        return []
    out = {t.date().isoformat() for t in h.battito}
    out |= {b.date().isoformat() for _, b in h.notti}
    return sorted(out)


def _media(v: list[int | float]) -> float | None:
    return round(statistics.mean(v), 1) if v else None


def _notte_del_giorno(h: Helio, giorno: date) -> tuple[datetime, datetime, dict] | None:
    """La notte piu' lunga che finisce in quel giorno."""
    candidate = [(a, b, info) for (a, b), info in h.notti.items() if b.date() == giorno]
    if not candidate:
        return None
    return max(candidate, key=lambda x: x[1] - x[0])


def _tratti(minuti: list[datetime], gap: int) -> list[tuple[datetime, datetime]]:
    """Minuti ordinati -> tratti continui (fine esclusa), con buchi <= gap."""
    out: list[list[datetime]] = []
    for t in sorted(minuti):
        if out and t - out[-1][1] <= timedelta(minutes=gap + 1):
            out[-1][1] = t + timedelta(minutes=1)
        else:
            out.append([t, t + timedelta(minutes=1)])
    return [(a, b) for a, b in out]


def _riposo(h: Helio, a: datetime, b: datetime) -> float | None:
    """Media piu' bassa su RIPOSO_FINESTRA_MIN minuti consecutivi in [a, b)."""
    migliore = None
    t = a
    while t + timedelta(minutes=RIPOSO_FINESTRA_MIN) <= b:
        v = [h.battito[t + timedelta(minutes=k)] for k in range(RIPOSO_FINESTRA_MIN)
             if t + timedelta(minutes=k) in h.battito]
        if len(v) >= RIPOSO_MIN_VALORI:
            m = statistics.mean(v)
            if migliore is None or m < migliore:
                migliore = m
        t += timedelta(minutes=5)
    return round(migliore, 1) if migliore is not None else None


def _sonni_del_giorno(h: Helio, mezzanotte: datetime) -> list[tuple[datetime, datetime]]:
    """Notti di SLEEP e altri tratti di sonno (SLEEP_MINUTE) del giorno."""
    domani = mezzanotte + timedelta(days=1)
    notti = list(h.notti)
    fasi_giorno = [t for t in h.fasi if mezzanotte <= t < domani]
    altri = [(a, b) for a, b in _tratti(
        [t for t in fasi_giorno if not _dentro(t, notti)], GAP_PISOLINO_MIN)
        if (b - a) >= timedelta(minutes=PISOLINO_MIN)]
    return notti + altri


def fermi_svegli(h: Helio, giorno: str) -> dict[datetime, int]:
    """Battito nei minuti svegli, senza passi e fuori dagli allenamenti."""
    if giorno in h._cache_fermi:
        return h._cache_fermi[giorno]
    g = date.fromisoformat(giorno)
    mezzanotte = datetime.combine(g, datetime.min.time())
    domani = mezzanotte + timedelta(days=1)
    escluse = _sonni_del_giorno(h, mezzanotte) + [
        (a, a + timedelta(seconds=s["secondi"])) for a, s in h.sport.items()
        if a < domani and a + timedelta(seconds=s["secondi"]) > mezzanotte]
    fuori = {}
    for t, v in h.battito.items():
        if not (mezzanotte <= t < domani) or _dentro(t, escluse):
            continue
        if any(h.passi_minuto.get(t - timedelta(minutes=k), 0) > 0
               for k in range(ATTIV_FERMO_PRIMA + 1)):
            continue
        fuori[t] = v
    h._cache_fermi[giorno] = fuori
    return fuori


def _percentile(v: list[int], q: float) -> float:
    v = sorted(v)
    i = q * (len(v) - 1)
    lo = int(i)
    hi = min(lo + 1, len(v) - 1)
    return v[lo] + (v[hi] - v[lo]) * (i - lo)


def attivazione(h: Helio, giorno: str, riposo: float | None = None) -> dict[str, Any] | None:
    """Attivazione da fermo del giorno (vedi ATTIV_* in cima al modulo)."""
    g = date.fromisoformat(giorno)
    oggi = fermi_svegli(h, giorno)
    if len(oggi) < ATTIV_MIN_FERMI:
        return None
    rif: list[int] = []
    n_giorni = 0
    for k in range(ATTIV_RIF_GIORNI):
        f = fermi_svegli(h, (g - timedelta(days=k)).isoformat())
        if len(f) >= ATTIV_MIN_FERMI:
            rif.extend(f.values())
            n_giorni += 1
    base = {
        "minuti_fermo": len(oggi),
        "media_fermo": _media(list(oggi.values())),
        "sopra_riposo_bpm": (round(_media(list(oggi.values())) - riposo, 1)
                             if riposo is not None else None),
        "riferimento_giorni": n_giorni,
    }
    if n_giorni < ATTIV_MIN_GIORNI_RIF:
        return {**base, "soglia_bpm": None, "minuti": None, "quota": None,
                "per_ora": [None] * 24}
    soglia = _percentile(rif, ATTIV_PERCENTILE)
    sopra = [t for t, v in oggi.items() if v > soglia]
    per_ora: list[float | None] = []
    for ora in range(24):
        fermi_ora = [t for t in oggi if t.hour == ora]
        if len(fermi_ora) < ATTIV_MIN_ORA:
            per_ora.append(None)
        else:
            per_ora.append(round(sum(1 for t in fermi_ora if oggi[t] > soglia)
                                 / len(fermi_ora), 3))
    return {**base, "soglia_bpm": round(soglia, 1), "minuti": len(sopra),
            "quota": round(len(sopra) / len(oggi), 3), "per_ora": per_ora}


def calcola_giorno(h: Helio, giorno: str) -> dict[str, Any]:
    """Il blocco delle misure di un giorno, sulle 24 ore."""
    g = date.fromisoformat(giorno)
    mezzanotte = datetime.combine(g, datetime.min.time())
    domani = mezzanotte + timedelta(days=1)
    notti = list(h.notti)

    # --- battito ----------------------------------------------------
    del_giorno = {t: v for t, v in h.battito.items() if mezzanotte <= t < domani}
    valori = list(del_giorno.values())
    per5: list[float | None] = []
    for i in range(288):
        a = mezzanotte + timedelta(minutes=5 * i)
        v = [del_giorno[a + timedelta(minutes=k)] for k in range(5)
             if a + timedelta(minutes=k) in del_giorno]
        per5.append(round(statistics.mean(v)) if v else None)
    cinque = [x for x in per5 if x is not None]

    # Minuti di sonno del giorno: notti di SLEEP e pisolini.
    fasi_giorno = [t for t in h.fasi if mezzanotte <= t < domani]
    altri_sonni = [(a, b) for a, b in _tratti(
        [t for t in fasi_giorno if not _dentro(t, notti)], GAP_PISOLINO_MIN)
        if (b - a) >= timedelta(minutes=PISOLINO_MIN)]
    dormendo = notti + altri_sonni
    sveglio = [v for t, v in del_giorno.items() if not _dentro(t, dormendo)]

    # --- sonno: la notte che finisce oggi -------------------------------
    sonno = None
    notte = _notte_del_giorno(h, g)
    if notte:
        a, b, info = notte
        nel_sonno = [v for t, v in h.battito.items() if a <= t < b]
        medie5 = []
        t = a
        while t + timedelta(minutes=5) <= b:
            v = [h.battito[t + timedelta(minutes=k)] for k in range(5)
                 if t + timedelta(minutes=k) in h.battito]
            if len(v) >= 3:
                medie5.append(statistics.mean(v))
            t += timedelta(minutes=5)
        fasi: list[list[Any]] = []
        t = a
        while t < b:
            f = h.fasi.get(t)
            rel = int((t - mezzanotte).total_seconds() // 60)
            if f and fasi and fasi[-1][2] == f and fasi[-1][1] == rel:
                fasi[-1][1] = rel + 1
            elif f:
                fasi.append([rel, rel + 1, f])
            t += timedelta(minutes=1)
        dormito = info["profondo_min"] + info["leggero_min"] + info["rem_min"]
        sonno = {
            "inizio": a.isoformat(timespec="minutes"),
            "fine": b.isoformat(timespec="minutes"),
            "a_letto_min": int((b - a).total_seconds() // 60),
            "dormito_min": dormito,
            **info,
            "profondo_rem_quota": (round((info["profondo_min"] + info["rem_min"])
                                         / dormito, 3) if dormito else None),
            "battito_media": _media(nel_sonno),
            "battito_minimo_5min": round(min(medie5), 1) if medie5 else None,
            # Stima del battito a riposo (Zepp non lo esporta).
            "battito_riposo_30min": _riposo(h, a, b),
            "fasi": fasi,
        }

    # --- passi --------------------------------------------------------
    per_ora = [0] * 24
    for t, v in h.passi_minuto.items():
        if mezzanotte <= t < domani:
            per_ora[t.hour] += v
    att = h.attivita.get(giorno) or {}

    allenamenti = []
    for inizio, s in sorted(h.sport.items()):
        if inizio.date() == g:
            allenamenti.append({
                "inizio": inizio.strftime("%H:%M"),
                "minuti": round(s["secondi"] / 60),
                "kcal": s["kcal"],
                "tipo": s["tipo"],
            })

    return {
        "versione": VERSIONE,
        "giorno": giorno,
        "giorno_settimana": g.isoweekday(),
        "battito": {
            "minuti": len(valori),
            # Sotto il 95% il giorno e' incompleto (l'ultimo giorno di un
            # export, o il braccialetto tolto): le medie valgono meno.
            "copertura_quota": round(len(valori) / 1440, 3),
            "media": _media(valori),
            "mediana": round(statistics.median(valori)) if valori else None,
            "minimo_5min": min(cinque) if cinque else None,
            "massimo_5min": max(cinque) if cinque else None,
            "sveglio_media": _media(sveglio),
            "per_5min": per5,
        },
        "passi": {
            "totale": att.get("passi") if att.get("passi") is not None else sum(per_ora),
            "distanza_m": att.get("distanza_m"),
            "calorie": att.get("calorie"),
            "per_ora": per_ora,
        },
        "sonno": sonno,
        # Tratti di SLEEP_MINUTE fuori dalle notti di SLEEP: pisolini, ma
        # anche pezzi di notte che SLEEP non riassume (la notte del cambio
        # d'ora, 24-25/10, in SLEEP e' vuota). Non si chiamano «pisolini»
        # perche' non si sa quali sono.
        "altri_sonni": [{"inizio": a.strftime("%H:%M"), "fine": b.strftime("%H:%M"),
                         "minuti": int((b - a).total_seconds() // 60)}
                        for a, b in altri_sonni],
        "allenamenti": allenamenti,
        "attivazione": attivazione(
            h, giorno, sonno["battito_riposo_30min"] if sonno else None),
    }


# ----------------------------------------------------------------------
# Battito e parlato (scala dei minuti, solo dove c'e' registrazione)
# ----------------------------------------------------------------------

def battito_e_parlato(h: Helio | None, giorno: str,
                      registrato: dict[int, float],
                      parlato: dict[int, set[str]],
                      soggetto: str) -> dict[str, Any] | None:
    """Battito medio nei minuti registrati, per cosa succede in quel minuto.

    `registrato`: minuto del giorno -> secondi registrati.
    `parlato`: minuto del giorno -> voci che parlano almeno un secondo.

    Classi: «parli» (c'e' la voce di Pietro), «altri» (parla qualcuno, non
    Pietro), «silenzio» (registrato senza parlato). Ogni classe ha la media
    su tutti i minuti e sui soli minuti «fermi» (nessun passo nel minuto e
    nei FERMO_INTORNO minuti intorno): il secondo numero e' quello che si
    confronta, perche' toglie il cammino.

    Ipotesi «parlare <-> battito piu' alto» (documento `analisi-corpus.md`,
    sezione 6, scala dei minuti). Non e' una correlazione: e' una
    differenza di medie in un giorno, e il pannello la presenta cosi'.
    """
    if h is None:
        return None
    mezzanotte = datetime.fromisoformat(giorno)
    classi: dict[str, list[int]] = defaultdict(list)
    fermi: dict[str, list[int]] = defaultdict(list)
    registrati = coperti = 0
    for m, sec in registrato.items():
        if sec < MIN_REGISTRATO_SEC:
            continue
        registrati += 1
        t = mezzanotte + timedelta(minutes=m)
        v = h.battito.get(t)
        if v is None:
            continue
        coperti += 1
        voci = parlato.get(m) or set()
        c = "parli" if soggetto in voci else ("altri" if voci else "silenzio")
        classi[c].append(v)
        if all(not h.passi_minuto.get(t + timedelta(minutes=k))
               for k in range(-FERMO_INTORNO, FERMO_INTORNO + 1)):
            fermi[c].append(v)
    if not coperti:
        return None

    def riassunto(vv: list[int]) -> dict[str, Any]:
        return {"minuti": len(vv),
                "media": _media(vv) if len(vv) >= MIN_MINUTI_CLASSE else None}

    out = {
        "minuti_registrati": registrati,
        "minuti_con_battito": coperti,
        "tutti": {c: riassunto(classi[c]) for c in ("parli", "altri", "silenzio")},
        "fermo": {c: riassunto(fermi[c]) for c in ("parli", "altri", "silenzio")},
    }
    p, s = out["fermo"]["parli"]["media"], out["fermo"]["silenzio"]["media"]
    out["parli_meno_silenzio_fermo"] = round(p - s, 1) if p is not None and s is not None else None
    return out


def scrivi(h: Helio | None, cartella: Path) -> list[Path]:
    """`<cartella>/AAAA-MM-GG.json` per ogni giorno, solo se cambiato."""
    scritti = []
    for g in giorni(h):
        testo = json.dumps(calcola_giorno(h, g), ensure_ascii=False,
                           separators=(",", ":")) + "\n"
        dest = Path(cartella) / f"{g}.json"
        if dest.exists() and dest.read_text(encoding="utf-8") == testo:
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(testo, encoding="utf-8")
        scritti.append(dest)
    return scritti
