#!/usr/bin/env python3
"""
Importa un export dei dati di Zepp (braccialetto Amazfit Helio Strap).

    python importa_helio.py ~/Downloads/7086234041_1794054432703
    python importa_helio.py ~/Downloads/7086234041_1794054432703 --pubblica
    python importa_helio.py --stato

Come si ottiene l'export (circa una volta al mese): app Zepp -> Profilo ->
Impostazioni -> «Esercizio dei diritti dell'utente» -> «Esporta dati»
(NON «Cancella dati» o «Elimina account», che stanno accanto), tutti i tipi,
tutto il periodo. Arriva uno zip con password: la password la mostra l'app
al momento della richiesta. Si estrae con il Finder e si passa la cartella
a questo comando. Un .zip senza password si puo' passare direttamente.

Cosa fa:
1. controlla che la cartella sia un export leggibile (`core/helio.py`);
2. la copia in `data/helio/export/<nome>/` (fuori da git: sono dati di
   salute). Un export gia' copiato non si ricopia;
3. stampa cosa contiene e cosa aggiunge rispetto agli export precedenti,
   e gli avvisi sulle stranezze dell'export (date spostate, fusi);
4. con `--pubblica` lancia `publish_corpus.py push`, che scrive
   `helio/AAAA-MM-GG.json` e il campo `corpo` delle metriche nella repo
   privata del corpus; il push fa ricostruire il Taccuino.
   Senza `--pubblica` i dati entrano comunque alla prossima pubblicazione
   (anche quella della notte).

Ogni export contiene tutta la storia: non serve tenere quelli vecchi, ma
tenerli non fa danno (vince sempre il piu' recente).
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from core.helio import EXPORT_DIR, carica, giorni, importa, leggi_export  # noqa: E402


def _riepilogo(titolo: str, h) -> None:
    gg = giorni(h)
    print(f"{titolo}: {len(gg)} giorni" + (f", dal {gg[0]} al {gg[-1]}" if gg else ""))
    if h is None:
        return
    print(f"  battito: {len(h.battito)} minuti · notti: {len(h.notti)} · "
          f"minuti di fasi del sonno: {len(h.fasi)} · attivita': {len(h.sport)}")
    per_giorno = {}
    for t in h.battito:
        per_giorno[t.date().isoformat()] = per_giorno.get(t.date().isoformat(), 0) + 1
    scarsi = [f"{g} ({n} min)" for g, n in sorted(per_giorno.items()) if n < 1368]
    if scarsi:
        print("  giorni con meno del 95% dei minuti di battito: " + ", ".join(scarsi))
    senza_notte = [g for g in gg if not any(b.date().isoformat() == g for _, b in h.notti)]
    if senza_notte:
        print("  giorni senza una notte in SLEEP: " + ", ".join(senza_notte))
    for a in h.avvisi:
        print(f"  avviso: {a}")


def _da_zip(z: Path) -> Path:
    dest = Path(tempfile.mkdtemp(prefix="helio_"))
    try:
        with zipfile.ZipFile(z) as f:
            f.extractall(dest)
    except RuntimeError as e:  # zip cifrato
        raise SystemExit(f"{z}: lo zip ha la password ({e}). Estrailo con il Finder "
                         "e passa la cartella.")
    figli = [p for p in dest.iterdir() if p.is_dir()]
    radice = figli[0] if len(figli) == 1 and not (dest / "HEARTRATE_AUTO").exists() else dest
    finale = dest.parent / z.stem
    if not finale.exists():
        radice.rename(finale)
        return finale
    return radice


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("sorgente", nargs="?", help="cartella (o .zip senza password) dell'export Zepp")
    ap.add_argument("--pubblica", action="store_true", help="poi lancia publish_corpus.py push")
    ap.add_argument("--stato", action="store_true", help="mostra solo cosa c'e' gia'")
    args = ap.parse_args()

    prima = carica()
    if args.stato or not args.sorgente:
        esportati = sorted(p.name for p in EXPORT_DIR.iterdir()) if EXPORT_DIR.is_dir() else []
        print(f"Export in {EXPORT_DIR}: {', '.join(esportati) or 'nessuno'}")
        _riepilogo("Totale", prima)
        return 0

    sorgente = Path(args.sorgente).expanduser()
    if sorgente.suffix.lower() == ".zip":
        sorgente = _da_zip(sorgente)
    nuovo = leggi_export(sorgente)
    _riepilogo(f"Export {sorgente.name}", nuovo)
    dest = importa(sorgente)
    print(f"Copiato in {dest}")
    dopo = carica()
    aggiunti = sorted(set(giorni(dopo)) - set(giorni(prima)))
    print(f"Giorni nuovi rispetto a prima: {len(aggiunti)}"
          + (f" ({aggiunti[0]} … {aggiunti[-1]})" if aggiunti else ""))
    if args.pubblica:
        return subprocess.call([sys.executable, str(ROOT / "publish_corpus.py"), "push"], cwd=ROOT)
    print("I dati entreranno nel Taccuino alla prossima pubblicazione "
          "(python publish_corpus.py push, o il giro della notte).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
