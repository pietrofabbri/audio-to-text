"""
Nomi di persona ridotti a iniziali nelle pagine del taccuino.

Decisione di Pietro del 21 ottobre 2026: nel taccuino si tiene tutto, anche
le ore di lezione con studenti minorenni, ma **i nomi dei minorenni solo
con le iniziali**. Il corpus privato resta integrale; le iniziali valgono
solo per le pagine pubblicate (cifrate) del taccuino.

Il codice non puo' sapere chi e' minorenne. Percio' la regola e' la piu'
prudente: **ogni nome di persona diventa iniziali, tranne i nomi
nell'elenco «in chiaro»** (adulti, luoghi), che Pietro tiene nella repo
privata del corpus in `pannello/nomi_in_chiaro.txt`, un nome per riga,
`#` per i commenti. Il nome di Pietro e' sempre in chiaro. Un nome che
manca dall'elenco esce abbreviato: l'errore possibile e' un adulto
abbreviato, mai un minorenne in chiaro.

Che cosa e' «un nome di persona» nel testo, senza un elenco di nomi:

- una parola con l'iniziale maiuscola e il resto minuscolo («Yousef»);
- che nel corpus **non compare mai scritta minuscola**: «Allora» a inizio
  frase e' una parola comune perche' altrove si trova «allora»; «Gabriele»
  no. E' la stessa idea della protezione dei nomi nel correttore;
- e che non e' nell'elenco in chiaro.

Le parti dei nomi delle voci non in chiaro (es. «Zappaterra») sono
abbreviate sempre, anche se per caso compaiono minuscole.

Effetto collaterale accettato: anche luoghi o parole rare con la maiuscola
(«Excalibur») diventano iniziali finche' non si aggiungono all'elenco.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Iterable

FILE_ELENCO = Path("pannello") / "nomi_in_chiaro.txt"

_PAROLA = re.compile(r"[A-Za-zÀ-ÖØ-öø-ÿ]+")
# Dopo un apostrofo il nome conta («dell'Anna»): solo le lettere lo escludono.
_NOME = re.compile(r"(?<![A-Za-zÀ-ÖØ-öø-ÿ])([A-ZÀ-ÖØ-Þ][a-zà-öø-ÿ]+)(?![A-Za-zÀ-ÖØ-öø-ÿ])")


def leggi_elenco(path: Path) -> list[str]:
    """I nomi in chiaro del file, uno per riga; vuoto se il file non c'e'."""
    try:
        righe = Path(path).read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    return [r.split("#", 1)[0].strip() for r in righe if r.split("#", 1)[0].strip()]


def vocabolario_minuscolo(cartella_giorni: Path) -> set[str]:
    """Le parole che nel corpus compaiono almeno una volta tutte minuscole."""
    vocab: set[str] = set()
    for d in sorted(Path(cartella_giorni).iterdir()):
        for nome in ("segments.jsonl", "segments.corrected.jsonl"):
            p = d / nome
            if not p.exists():
                continue
            with p.open(encoding="utf-8") as f:
                for riga in f:
                    if not riga.strip():
                        continue
                    for w in _PAROLA.findall(json.loads(riga).get("text") or ""):
                        if w.islower():
                            vocab.add(w)
    return vocab


def _iniziale(parola: str) -> str:
    return parola[0].upper() + "."


class Iniziali:
    def __init__(self, in_chiaro: Iterable[str], vocabolario: set[str],
                 nomi_voci: Iterable[str] = ()) -> None:
        self.chiaro = {t.casefold() for n in in_chiaro for t in _PAROLA.findall(n)}
        # Le parole grammaticali e gli intercalari sono sempre comuni, anche
        # se nel corpus compaiono solo a inizio frase («Noi veniamo.»).
        from core.text_correction import PAROLE_PROTETTE
        self.vocabolario = set(vocabolario) | set(PAROLE_PROTETTE)
        self.forzati = {t.casefold() for n in nomi_voci if not self._in_chiaro(n)
                        for t in _PAROLA.findall(n)} - self.chiaro

    def _in_chiaro(self, nome: str) -> bool:
        parti = _PAROLA.findall(nome or "")
        return bool(parti) and all(t.casefold() in getattr(self, "chiaro", set()) for t in parti)

    def nome(self, nome: str | None) -> str | None:
        """Il nome di una voce: intero se in chiaro, altrimenti «N. C.»."""
        if not nome or nome.startswith("GLOBAL_") or self._in_chiaro(nome):
            return nome
        parti = _PAROLA.findall(nome)
        return " ".join(_iniziale(p) for p in parti) if parti else nome

    def testo(self, testo: str) -> str:
        def sostituisci(m: re.Match) -> str:
            w = m.group(1)
            k = w.casefold()
            if k in self.chiaro:
                return w
            if k in self.forzati or k not in self.vocabolario:
                return _iniziale(w)
            return w
        return _NOME.sub(sostituisci, testo or "")


def da_corpus(cartella_giorni: Path, nomi_voci: Iterable[str],
              sempre_in_chiaro: Iterable[str] = ("Pietro",)) -> Iniziali:
    """L'anonimizzatore per un corpus: elenco dal file + vocabolario."""
    cartella_giorni = Path(cartella_giorni)
    elenco = leggi_elenco(cartella_giorni.parent / FILE_ELENCO)
    return Iniziali(list(sempre_in_chiaro) + elenco,
                    vocabolario_minuscolo(cartella_giorni), nomi_voci)


def nomi_delle_voci(cartella_giorni: Path) -> set[str]:
    nomi: set[str] = set()
    for d in Path(cartella_giorni).iterdir():
        g = d / "giorno.json"
        if g.exists():
            nomi.update(v for v in (json.loads(g.read_text(encoding="utf-8"))
                                    .get("speaker_names") or {}).values() if v)
    return nomi
