"""
Glossario dei nomi propri: le parole che il correttore non deve toccare.

Il difetto che chiude. Sulla prova del 2 ottobre il correttore ha letto
«Zia Titti» e ha scritto «Gigi D'Alessio», in entrambe le occorrenze.
Un modello di lingua che incontra un nome che non conosce lo sostituisce
con uno che conosce: il risultato e' plausibile, nessun controllo lo
segnala, e chi legge dopo non ha modo di accorgersene. E' l'errore piu'
pericoloso del correttore, perche' e' l'unico che non sembra un errore.

Due difese, perche' una sola non basta:

1. **Il glossario.** Un elenco di nomi che non si cambiano mai: i nomi
   dati alle voci (`review_speakers.py name`) e una lista curata a mano
   in `data/glossario.txt` — parenti, amici, luoghi, soprannomi. Il
   glossario va anche nel prompt, cosi' il modello sa che quelle parole
   esistono e puo' correggere *verso* di esse una storpiatura.
2. **La maiuscola.** Una parola con l'iniziale maiuscola fuori
   dall'inizio di frase e' quasi sempre un nome proprio che Whisper ha
   riconosciuto come tale. Non si tocca, anche se non e' nel glossario:
   il glossario non sara' mai completo, e un nome nuovo sentito per la
   prima volta e' proprio il caso in cui il modello inventa.

Il file sta in `data/`, che e' fuori dalla repo pubblica (vedi
`.gitignore`): contiene nomi di persone vere.

Formato di `data/glossario.txt`: una voce per riga, anche di piu' parole
(«Zia Titti»); le righe vuote e quelle che iniziano con `#` si ignorano.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path

from core.config import ROOT_DIR

logger = logging.getLogger(__name__)

GLOSSARIO_PATH = ROOT_DIR / "data" / "glossario.txt"
SPEAKERS_DB_PATH = ROOT_DIR / "data" / "speakers_db.json"

# Le parole funzionali che possono stare dentro una voce del glossario
# («Teatro della Pace», «Casa di Riposo») ma che da sole non sono nomi:
# proteggerle bloccherebbe ogni «di» e ogni «della» del testo.
_FUNZIONALI = frozenset({
    "di", "del", "della", "dei", "delle", "degli", "dello", "da", "dal",
    "dalla", "e", "il", "lo", "la", "i", "gli", "le", "un", "una", "in",
    "a", "al", "alla", "con", "per", "su", "de", "d", "l",
})

_SEPARATORI = re.compile(r"[\s'’]+")


def _pulisci(parola: str) -> str:
    """La parola senza punteggiatura ai bordi, in minuscolo."""
    from core.text_correction import _norm
    return _norm(parola)


def nomi_delle_voci(path: Path | None = None) -> list[str]:
    """I nomi dati alle voci nel database locale.

    Si legge il JSON direttamente e non attraverso `SpeakerDB` per non
    caricare numpy per un elenco di nomi; e se il file manca o non si
    legge, l'elenco e' vuoto: il glossario a mano funziona lo stesso.
    """
    path = Path(path or SPEAKERS_DB_PATH)
    if not path.exists():
        return []
    try:
        dati = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Glossario: %s illeggibile (%s), nomi delle voci "
                       "esclusi", path.name, exc)
        return []
    nomi = []
    for rec in (dati.get("speakers") or {}).values():
        nome = (rec or {}).get("name")
        if nome and str(nome).strip():
            nomi.append(str(nome).strip())
    return nomi


def leggi_file(path: Path | None = None) -> list[str]:
    """Le voci del glossario scritto a mano."""
    path = Path(path or GLOSSARIO_PATH)
    if not path.exists():
        return []
    voci = []
    for riga in path.read_text(encoding="utf-8").splitlines():
        riga = riga.strip()
        if riga and not riga.startswith("#"):
            voci.append(riga)
    return voci


class Glossario:
    """L'insieme dei nomi protetti, per voce intera e per parola."""

    def __init__(self, voci: list[str] | None = None) -> None:
        visti: dict[str, str] = {}
        for v in voci or []:
            v = " ".join(v.split())
            if v and v.casefold() not in visti:
                visti[v.casefold()] = v
        self.voci: list[str] = sorted(visti.values(), key=str.casefold)
        parole: set[str] = set()
        for v in self.voci:
            for pezzo in _SEPARATORI.split(v):
                p = _pulisci(pezzo)
                if p and p not in _FUNZIONALI:
                    parole.add(p)
        self.parole: frozenset[str] = frozenset(parole)

    @classmethod
    def carica(cls, path_file: Path | None = None,
               path_voci: Path | None = None) -> "Glossario":
        """Glossario a mano + nomi delle voci."""
        return cls(leggi_file(path_file) + nomi_delle_voci(path_voci))

    def __len__(self) -> int:
        return len(self.voci)

    def __bool__(self) -> bool:
        return bool(self.voci)

    def contiene(self, parola: str) -> bool:
        """Se questa parola del testo e' (parte di) un nome protetto."""
        p = _pulisci(parola)
        if not p:
            return False
        if p in self.parole:
            return True
        # «D'Alessio» nel testo e' una parola sola; nel glossario e'
        # spezzata in «D» e «Alessio». Si controllano anche i pezzi.
        pezzi = [_pulisci(x) for x in _SEPARATORI.split(parola)]
        return any(x in self.parole for x in pezzi
                   if x and x not in _FUNZIONALI)

    def per_prompt(self, massimo: int = 300) -> str:
        """Il glossario come elenco da mettere nelle istruzioni."""
        return ", ".join(self.voci[:massimo])


def e_nome_proprio(parole: list[str], i: int) -> bool:
    """La parola i-esima ha la maiuscola fuori dall'inizio di frase.

    L'inizio di frase e' la prima parola del segmento o quella dopo un
    punto, un punto interrogativo o esclamativo, o dei puntini. «Io» si
    esclude: e' maiuscolo per l'abitudine di qualcuno, non perche' sia
    un nome.
    """
    if not 0 <= i < len(parole):
        return False
    parola = parole[i].lstrip("«\"'([—-")
    if not parola or not parola[0].isupper():
        return False
    if i == 0:
        return False
    precedente = parole[i - 1].rstrip("»\"')]")
    if precedente.endswith((".", "?", "!", "…")):
        return False
    if _pulisci(parola) == "io":
        return False
    return True
