"""
Un segmento diviso nei turni di chi parla.

Un segmento e' un pezzo di audio trascritto da Whisper in una volta sola,
fino a 29 secondi, e porta una voce sola. Dentro pero' si parla spesso in
due o tre: il 28/10 il 17,6% delle parole stava nel segmento di un'altra
voce. La diarizzazione sa chi dice ogni parola; qui quella informazione
torna nel testo che si legge: il segmento diventa uno o piu' turni,
ciascuno con la sua voce.

Il testo puo' essere quello corretto dal modello di lingua, che non ha le
parole di Whisper una per una: le sue parole si allineano a quelle di
Whisper (`difflib`), e ognuna prende la voce della parola a cui
corrisponde. Le correzioni sono poche e vicine al suono, quindi
l'allineamento regge; una parola senza corrispondenza prende la voce di
quella prima.

Un turno di una parola sola fra due turni della stessa voce e' quasi
sempre un errore della diarizzazione al confine, non una persona che
dice «si'» in mezzo: si riassorbe. Le interiezioni vere restano quando
sono lunghe almeno `MIN_PAROLE_TURNO` parole.
"""

from __future__ import annotations

import re
import unicodedata
from difflib import SequenceMatcher
from typing import Any, Sequence

MIN_PAROLE_TURNO = 2


def _chiave(parola: str) -> str:
    p = unicodedata.normalize("NFC", (parola or "").lower())
    p = re.sub(r"[’'`]", " ", p)
    return " ".join(re.findall(r"[^\W_]+", p))


def dividi(testo: str, parole: Sequence[tuple[str, str, float | None]],
           voce_segmento: str, inizio: float, fine: float,
           min_parole: int = MIN_PAROLE_TURNO) -> list[dict[str, Any]]:
    """I turni di un segmento.

    Args:
        testo: il testo del segmento (grezzo o corretto).
        parole: le parole di Whisper del segmento, in ordine:
            (parola, voce, inizio in secondi o None).
        voce_segmento: la voce del segmento, usata se manca tutto il resto.
        inizio, fine: i tempi del segmento.

    Returns:
        [{"speaker", "text", "start", "end"}], almeno uno.
    """
    toks = (testo or "").split()
    intero = [{"speaker": voce_segmento, "text": (testo or "").strip(),
               "start": inizio, "end": fine}]
    if not toks or not parole or len({v for _, v, _ in parole}) < 2:
        if parole and toks:
            intero[0]["speaker"] = parole[0][1] or voce_segmento
        return intero

    kt = [_chiave(t) for t in toks]
    kp = [_chiave(w) for w, _, _ in parole]
    voce: list[str | None] = [None] * len(toks)
    tempo: list[float | None] = [None] * len(toks)
    for tag, i1, i2, j1, j2 in SequenceMatcher(None, kt, kp, autojunk=False).get_opcodes():
        if tag in ("equal", "replace") and j2 > j1:
            for k in range(i2 - i1):
                j = j1 + min(j2 - j1 - 1, (k * (j2 - j1)) // max(1, i2 - i1))
                voce[i1 + k], tempo[i1 + k] = parole[j][1], parole[j][2]
    ultimo = next((v for v in voce if v), voce_segmento)
    for k in range(len(voce)):
        voce[k] = voce[k] or ultimo
        ultimo = voce[k]

    # Corse di parole della stessa voce.
    corse: list[list[int]] = []
    for k in range(len(toks)):
        if corse and voce[corse[-1][0]] == voce[k]:
            corse[-1].append(k)
        else:
            corse.append([k])
    # Le corse troppo corte si riassorbono nella precedente (o nella
    # seguente, se sono la prima), finche' non ce ne sono piu'.
    cambiato = True
    while cambiato and len(corse) > 1:
        cambiato = False
        for c in range(len(corse)):
            if len(corse[c]) >= min_parole:
                continue
            dove = c - 1 if c > 0 else c + 1
            nuova = voce[corse[dove][0]]
            for k in corse[c]:
                voce[k] = nuova
            unite = sorted(corse[c] + corse[dove])
            lo, hi = min(c, dove), max(c, dove)
            corse[lo:hi + 1] = [unite]
            cambiato = True
            break
        # corse adiacenti della stessa voce, dopo un riassorbimento
        fuse: list[list[int]] = []
        for c in corse:
            if fuse and voce[fuse[-1][0]] == voce[c[0]]:
                fuse[-1].extend(c)
            else:
                fuse.append(c)
        corse = fuse

    out = []
    for n, c in enumerate(corse):
        a = next((tempo[k] for k in c if tempo[k] is not None), None)
        out.append({"speaker": voce[c[0]], "text": " ".join(toks[k] for k in c),
                    "start": inizio if n == 0 or a is None else a, "end": fine})
    for n in range(len(out) - 1):
        out[n]["end"] = out[n + 1]["start"]
    return out
