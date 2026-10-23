#!/usr/bin/env python3
"""
Costruisce la pagina del pannello dalle metriche del corpus.

    python pannello/costruisci.py <cartella metriche> <file html di uscita> [<cartella giorni>] [--senza-conversazioni]

Con la cartella `giorni/` del corpus costruisce anche le pagine delle
conversazioni, una per giorno, in `<cartella dell'uscita>/giorni/`
(`pannello/conversazioni.py`), e il pannello le collega.

Lo lancia GitHub Actions nella repo privata del corpus (vedi
`pannello/pannello.yml`), poi StatiCrypt cifra la pagina e la pubblica.
Puo' girare anche a mano sul Mac per vedere la pagina in chiaro.

Regole (documento `analisi-corpus.md`, sezione 8):

- **Una pagina sola, con i dati dentro.** StatiCrypt cifra la pagina e
  non i file accanto: un JSON separato resterebbe in chiaro. Percio' i
  dati sono incorporati, e la pagina non carica niente da fuori.
- **Solo metriche aggregate.** Nessun testo delle conversazioni entra
  nella pagina: le metriche non ne contengono.
- Nessun titolo o nome di file che dica cosa c'e' dentro: «Taccuino».
"""

from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from core.metriche import LIMITI_FISSI, ORE_MINIME_VALIDO, SOGGETTO  # noqa: E402

# Minimi per mostrare una scala (sezione 4 del documento).
SOGLIE = {
    "ore_valido": ORE_MINIME_VALIDO,
    "settimana": 4,   # giorni validi
    "mese": 12,       # giorni validi
    "stagione": 6,    # settimane con dati
    "anno": 9,        # mesi con dati
}

# Campi che la pagina non usa: restano nel corpus privato, non escono.
_TOGLI = ("elenco",)


def _pulisci(g: dict) -> dict:
    g = json.loads(json.dumps(g))
    for k in _TOGLI:
        g.get("relazioni", {}).pop(k, None)
    return g


def costruisci(cartella_metriche: Path, uscita: Path,
               cartella_giorni: Path | None = None,
               conversazioni: bool = True) -> Path:
    """Il pannello; con `cartella_giorni` anche le pagine dei giorni.

    I nomi delle persone escono con le iniziali, tranne quelli in
    `pannello/nomi_in_chiaro.txt` del corpus (vedi `pannello/iniziali.py`).
    Senza `cartella_giorni` l'elenco non si conosce: in chiaro resta solo
    il nome di Pietro.
    """
    from pannello.iniziali import Iniziali, da_corpus, nomi_delle_voci
    if cartella_giorni is not None:
        iniziali = da_corpus(Path(cartella_giorni), nomi_delle_voci(Path(cartella_giorni)))
    else:
        iniziali = Iniziali(["Pietro"], set())
    giorni = []
    for p in sorted(Path(cartella_metriche).glob("????-??-??.json")):
        g = _pulisci(json.loads(p.read_text(encoding="utf-8")))
        for v in g.get("voci", []):
            v["nome"] = iniziali.nome(v.get("nome"))
        giorni.append(g)
    limiti = dict(LIMITI_FISSI)
    if giorni and not all(g["qualita"]["testo_corretto"] for g in giorni):
        limiti["testo"] = ("in parte o del tutto non corretto: lessico e "
                           "intercalari risentono degli errori di trascrizione")
    dati = {
        "generato": datetime.now(ZoneInfo("Europe/Rome")).strftime("%d/%m/%Y %H:%M"),
        "soggetto": SOGGETTO,
        "soglie": SOGLIE,
        "limiti": limiti,
        "giorni": giorni,
        "conversazioni": [],
    }
    if cartella_giorni is not None and conversazioni:
        from pannello.conversazioni import costruisci as pagine_giorni
        scritte = pagine_giorni(Path(cartella_giorni), Path(uscita).parent / "giorni",
                                iniziali)
        dati["conversazioni"] = sorted(p.stem for p in scritte)
    blob = json.dumps(dati, ensure_ascii=False, separators=(",", ":"))
    # Dentro <script type="application/json"> l'unica sequenza pericolosa
    # e' la chiusura del tag.
    blob = blob.replace("</", "<\\/")
    html = (HERE / "modello.html").read_text(encoding="utf-8").replace("__DATI__", blob)
    uscita = Path(uscita)
    uscita.parent.mkdir(parents=True, exist_ok=True)
    uscita.write_text(html, encoding="utf-8")
    return uscita


if __name__ == "__main__":
    argomenti = [a for a in sys.argv[1:] if a != "--senza-conversazioni"]
    if len(argomenti) not in (2, 3):
        raise SystemExit(__doc__)
    print(costruisci(Path(argomenti[0]), Path(argomenti[1]),
                     Path(argomenti[2]) if len(argomenti) == 3 else None,
                     conversazioni="--senza-conversazioni" not in sys.argv))
