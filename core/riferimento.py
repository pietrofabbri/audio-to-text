"""
Un set di riferimento per misurare quanto sbaglia la trascrizione.

Senza un riferimento ogni modifica alla trascrizione e' un'opinione: «mi
sembra meglio». Qui si prende una manciata di minuti veri, di situazioni
diverse (lezione, riunione, focus group, strada), Pietro li corregge a
mano, e da li' in poi ogni cambiamento si misura con due numeri:

- **WER** (word error rate): parole sostituite + cancellate + inserite,
  diviso le parole del riferimento. Si calcola sul testo normalizzato
  (minuscole, senza punteggiatura), che e' la convenzione usuale.
- **Attribuzione**: delle parole riconosciute giuste, quante sono date
  alla persona giusta.

Il riferimento parte dal testo attuale (si corregge, non si riscrive): e'
dieci volte piu' veloce, con un rischio noto, che chi corregge lasci
passare un errore plausibile. Per questo il file dice di ascoltare
l'audio, non di leggere il testo.

Il file di riferimento e' testo semplice, una riga per turno:

    [Pietro] allora ragazzi cominciamo
    [Carola] posso dire una cosa?

Le righe che cominciano con # sono commenti. Una riga senza [Nome] e'
della stessa persona della riga prima.
"""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


@dataclass
class Parola:
    inizio: float
    fine: float
    testo: str
    voce: str            # ID globale, o UNKNOWN


def _json(p: Path):
    try:
        return json.loads(Path(p).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def parole_sessione(cartella: Path) -> list[Parola]:
    """Le parole di una sessione, con la voce di ciascuna (non del segmento)."""
    cartella = Path(cartella)
    tr = _json(cartella / "transcript.json") or {}
    sj = _json(cartella / "session.json") or {}
    mappa = sj.get("speaker_global_map") or {}
    fusioni = (_json(cartella / "speaker_merge.json") or {}).get("merged") or {}

    def globale(locale, ripiego):
        visti = set()
        while locale in fusioni and locale not in visti:
            visti.add(locale)
            locale = fusioni[locale]
        return mappa.get(locale) or ripiego

    out = []
    for seg in tr.get("segments") or []:
        if seg.get("quality") == "unreliable":
            continue
        for w in seg.get("words") or []:
            t = (w.get("word") or "").strip()
            if not t or w.get("start") is None:
                continue
            out.append(Parola(float(w["start"]), float(w.get("end") or w["start"]), t,
                              globale(w.get("speaker"), seg.get("speaker") or "UNKNOWN")))
    out.sort(key=lambda p: p.inizio)
    return out


def scegli_finestra(parole: list[Parola], durata: float = 60.0,
                    passo: float = 15.0) -> float | None:
    """L'inizio della finestra con piu' parole e almeno due voci."""
    if not parole:
        return None
    migliore, inizio = (-1, None)
    t = parole[0].inizio
    fine_sessione = parole[-1].fine
    while t + durata <= fine_sessione + passo:
        dentro = [p for p in parole if t <= p.inizio < t + durata]
        voci = {p.voce for p in dentro} - {"UNKNOWN"}
        punteggio = len(dentro) if len(voci) >= 2 else len(dentro) // 3
        if punteggio > migliore:
            migliore, inizio = punteggio, t
        t += passo
    return inizio


def turni(parole: Iterable[Parola]) -> list[tuple[str, list[Parola]]]:
    out: list[tuple[str, list[Parola]]] = []
    for p in parole:
        if out and out[-1][0] == p.voce:
            out[-1][1].append(p)
        else:
            out.append((p.voce, [p]))
    return out


def testo_con_voci(parole: Iterable[Parola], nomi: dict[str, str]) -> str:
    righe = []
    for voce, pp in turni(parole):
        testo = re.sub(r" (?=['’])", "", " ".join(p.testo for p in pp))
        righe.append(f"[{nomi.get(voce) or voce}] " + testo)
    return "\n".join(righe) + "\n"


def leggi_testo_con_voci(testo: str) -> list[tuple[str, str]]:
    """[(voce, parola normalizzata)] da un file nel formato del riferimento."""
    out: list[tuple[str, str]] = []
    voce = "?"
    for riga in testo.splitlines():
        riga = riga.strip()
        if not riga or riga.startswith("#"):
            continue
        m = re.match(r"^\[([^\]]+)\]\s*(.*)$", riga)
        if m:
            voce, riga = m.group(1).strip(), m.group(2)
        for w in normalizza(riga):
            out.append((voce, w))
    return out


def normalizza(testo: str) -> list[str]:
    """Minuscole, senza punteggiatura; l'apostrofo divide («c'è» -> c è)."""
    testo = unicodedata.normalize("NFC", testo.lower())
    testo = re.sub(r"[’'`]", " ", testo)
    return re.findall(r"[^\W_]+", testo)


def allinea(rif: list[str], ipo: list[str]) -> list[tuple[int | None, int | None]]:
    """Allineamento di Levenshtein sulle parole: coppie (i_rif, i_ipo)."""
    n, m = len(rif), len(ipo)
    d = [[0] * (m + 1) for _ in range(n + 1)]
    for i in range(n + 1):
        d[i][0] = i
    for j in range(m + 1):
        d[0][j] = j
    for i in range(1, n + 1):
        ri = rif[i - 1]
        riga, prec = d[i], d[i - 1]
        for j in range(1, m + 1):
            riga[j] = min(prec[j] + 1, riga[j - 1] + 1,
                          prec[j - 1] + (0 if ri == ipo[j - 1] else 1))
    coppie = []
    i, j = n, m
    while i > 0 or j > 0:
        if i > 0 and j > 0 and d[i][j] == d[i - 1][j - 1] + (0 if rif[i - 1] == ipo[j - 1] else 1):
            coppie.append((i - 1, j - 1)); i -= 1; j -= 1
        elif i > 0 and d[i][j] == d[i - 1][j] + 1:
            coppie.append((i - 1, None)); i -= 1
        else:
            coppie.append((None, j - 1)); j -= 1
    return coppie[::-1]


def confronta(rif: list[tuple[str, str]], ipo: list[tuple[str, str]]) -> dict:
    """WER e attribuzione fra riferimento e ipotesi, entrambi [(voce, parola)]."""
    r = [w for _, w in rif]
    h = [w for _, w in ipo]
    s = dl = ins = giuste = voce_ok = 0
    for i, j in allinea(r, h):
        if i is None:
            ins += 1
        elif j is None:
            dl += 1
        elif r[i] != h[j]:
            s += 1
        else:
            giuste += 1
            voce_ok += rif[i][0].casefold() == ipo[j][0].casefold()
    n = len(r)
    return {
        "parole_riferimento": n,
        "sostituzioni": s, "cancellazioni": dl, "inserimenti": ins,
        "wer": round((s + dl + ins) / n, 4) if n else None,
        "parole_giuste": giuste,
        "attribuzione": round(voce_ok / giuste, 4) if giuste else None,
    }
