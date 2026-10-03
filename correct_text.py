#!/usr/bin/env python3
"""
Correzione del testo delle sessioni con un LLM.

    python correct_text.py                    # cosa verrebbe fatto
    python correct_text.py --dry --limit 5     # cinque correzioni, niente scrittura
    python correct_text.py --consent           # scrive davvero
    python correct_text.py --consent --session 19-42-33
    python correct_text.py --session 19-42-33 --riscorri

Perche' `--consent` e non un interruttore silenzioso. Ogni segmento
mandato a un'API esterna porta fuori dal portatile il testo di una
conversazione personale. Il testo di questa pipeline e' gia' pensato
per non uscire mai — i nomi reali non arrivano al corpus pubblico, e
ci si e' messi cura. Mandare il testo a un servizio esterno e' una
decisione diversa, e non la prende uno script per abitudine: quindi
qui la porta e' chiusa per default e si apre solo quando lo chiedi
esplicitamente. Senza `--consent` il comando mostra cosa farebbe e si
ferma.

Perche' `--dry` esiste anche con `--consent`. Centoventisei segmenti per
sessione, quattro sessioni: sono oltre cinquecento correzioni, e il
giudizio su una correzione e' tuo, non mio. `--dry --limit 5` mostra
cinque segmenti originali e corretti uno sotto l'altro, e da li' si
decide se vale la pena spendere il resto. Costa cinque chiamate.

Il file che ne esce e' affiancato, non sostitutivo. Ogni parola
conserva originale, correzione e se e' cambiata. Questo permette di
misurare quanto il modello sbaglia e quanto sbagliava Whisper: due
errori insieme sono la misura vera della qualita' della trascrizione.
Senza il confronto uno dei due sparisce e non si sa quale.

Ogni segmento scarta e' dichiarato nel file con il motivo. Un segmento
non corretto non e' un errore: e' un segmento di cui non ci si fida, e
la differenza conta perche' il testo che resta e' il testo detto.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from core.config import OUTPUT_DIR  # noqa: E402
from core.text_correction import (  # noqa: E402
    MODELLO, Correttore, scrivi_varianti,
)

logger = logging.getLogger("correct_text")

NOME_FILE = "text_correction.json"


def _firma(testo: str) -> str:
    """L'impronta di un testo, per capire se e' gia' stato corretto.

    Una notte interrotta e un tentativo rifatto non devono costare due
    volte, e non devono neppure correggere due volte lo stesso testo:
    la seconda passata su un testo gia' corretto non lo migliora, lo
    peggiora potenzialmente. L'impronta e' il testo intero perche' e'
    l'unica cosa che conta: se il segmento non e' cambiato, la risposta
    vecchia resta valida.
    """
    return hashlib.sha1(testo.encode("utf-8")).hexdigest()[:16]


def _leggi_sessione(d: Path) -> list[dict]:
    """I segmenti di una sessione, in ordine."""
    path = d / "segments.jsonl"
    if not path.exists():
        return []
    out = []
    for riga in path.read_text(encoding="utf-8").splitlines():
        if riga.strip():
            out.append(json.loads(riga))
    out.sort(key=lambda s: s.get("idx", 0))
    return out


def _carica_precedente(d: Path) -> dict[int, dict]:
    """Le correzioni gia' fatte in un giro precedente."""
    path = d / NOME_FILE
    if not path.exists():
        return {}
    try:
        dati = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        logger.warning("%s: illeggibile, lo rifaccio da capo", path.name)
        return {}
    return {int(r["idx"]): r for r in dati.get("segments", [])}


def _scrivi(d: Path, risultati: list[dict], modello: str) -> None:
    """Il file di correzione della sessione, con il riepilogo.

    Il riepilogo conta le parole e quante sono cambiate, perche' e' la
    domanda che ci si pone subito: «quanto ha lavorato il modello?» Ma
    la risposta interessante e' l'altra, e sta anche nel file: quante
    parole ha cambiato piu' di una volta, e quante sono state scartate.
    """
    parole = sum(r["n_words"] for r in risultati)
    cambiate = sum(r["n_changed"] for r in risultati)
    scartati = sum(1 for r in risultati if r.get("discarded"))
    riepilogo = {
        "segments": len(risultati),
        "words": parole,
        "words_changed": cambiate,
        "changed_share": round(cambiate / parole, 3) if parole else 0.0,
        "discarded": scartati,
        "model": modello,
    }
    out = {"summary": riepilogo, "segments": risultati}
    (d / NOME_FILE).write_text(
        json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")


def _mostra_confronto(righe: list[tuple[dict, dict]]) -> None:
    """Originale e correzione uno sotto l'altro, per segmenti.

    Leggere le due colonne in fila e' difficile: servono due passaggi
    con l'occhio e si perde il nesso. Uno sopra l'altro si legge come
    una correzione fatta a mano, che e' esattamente quello che si vuole
    giudicare.
    """
    for seg, res in righe:
        print(f"\n  [{seg.get('start', 0):7.1f}s] {seg.get('speaker', '?')}")
        print(f"    prima : {res['original_text']}")
        if res.get("discarded"):
            print(f"    scartato: {res.get('discard_reason', '')}")
            continue
        print(f"    dopo  : {res['corrected_text']}")
        if res["n_changed"]:
            parole = [f"{w['raw']}->{w['fixed']}" for w in res["words"]
                      if w["changed"]]
            print(f"    {res['n_changed']} parole: {', '.join(parole)}")
        else:
            print("    nessuna correzione")


def _sessioni_da_elaborare(args, out_dir: Path) -> list[Path]:
    """Le cartelle da guardare, con `--session` che accetta l'ora sola.

    Le cartelle si chiamano `2026-10-02_19-42-33`, ma l'ora e' l'unica
    parte che distingue una sessione dall'altra e l'unica che si ricorda.
    Accettare anche il nome intero evita che `--session 19-42-33` finisca
    col messaggio «sessione non trovata» su una sessione che c'e'.
    """
    if args.session:
        scelto = args.session.strip()
        trovate = [p for p in sorted(out_dir.glob("*")) if p.is_dir()
                   and (p == scelto or p.name.endswith(f"_{scelto}")
                        or p.name == scelto)]
        if not trovate:
            raise SystemExit(f"sessione non trovata: {scelto} in {out_dir}")
        return [p for p in trovate if (p / "segments.jsonl").exists()] or trovate
    return sorted(p for p in out_dir.glob("*") if p.is_dir()
                  and (p / "segments.jsonl").exists())


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Correggi con un LLM il testo delle trascrizioni")
    ap.add_argument("--session", help="una sessione sola (es. 19-42-33)")
    ap.add_argument("--consent", action="store_true",
                    help="autorizza a mandare il testo a un'API esterna")
    ap.add_argument("--dry", action="store_true",
                    help="stampa le correzioni senza scrivere nulla")
    ap.add_argument("--riscorri", action="store_true",
                    help="ricalcola anche i segmenti gia' corretti")
    ap.add_argument("--limit", type=int, default=0,
                    help="al massimo N segmenti per sessione")
    ap.add_argument("--model", default=MODELLO, help=f"default {MODELLO}")
    ap.add_argument("--pausa", type=float, default=0.5,
                    help="secondi fra una chiamata e l'altra")
    ap.add_argument("--out-dir", default=str(OUTPUT_DIR),
                    help="cartella delle sessioni")
    args, avanzi = ap.parse_known_args()

    # Le righe con un commento in coda si copiano dal README, e in zsh
    # il `#` non e' un commento interattivo: il finale arriva qui come
    # argomento e argparse risponde «unrecognized arguments», che non
    # dice nulla di utile. Meglio nominarlo.
    if avanzi:
        if avanzi[0].startswith("#"):
            # Il comando suggerito ricostruito **senza** il commento: se
            # contenesse ancora il `#`, chi lo copia per provare
            # riceverebbe lo stesso errore, e il suggerimento sarebbe
            # un modo per non uscirne.
            pulito = [a for a in sys.argv[1:] if not a.startswith("#")]
            raise SystemExit(
                f"\n  Il '#' finale e' arrivato come argomento.\n"
                f"  In zsh, nei comandi interattivi, '#' non e' un commento\n"
                f"  se INTERACTIVE_COMMENTS non e' impostato.\n\n"
                f"  Questo funziona:\n"
                f"    {Path(sys.argv[0]).name} {' '.join(pulito)}\n\n"
                f"  Oppure, una volta sola in ~/.zshrc:\n"
                f"    setopt interactive_comments\n"
                f"  e da li' in avanti i commenti in coda si potranno copiare\n"
                f"  dalle righe del README.\n"
            )
        ap.error("argomenti sconosciuti: " + " ".join(avanzi))

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    out_dir = Path(args.out_dir)
    sessioni = _sessioni_da_elaborare(args, out_dir)
    if not sessioni:
        raise SystemExit(f"nessuna sessione in {out_dir}")

    # Unocchiata a cosa ci sarebbe da correggere, prima di qualsiasi
    # decisione sulla chiave API.
    print(f"{len(sessioni)} sessioni in {out_dir}")
    totale = 0
    for d in sessioni:
        n = len(_leggi_sessione(d))
        fatte = len(_carica_precedente(d))
        da_fare = 0 if args.riscorri else n - fatte
        totale += max(0, da_fare)
        print(f"  {d.name}: {n} segmenti, {fatte} gia' corretti, "
              f"{max(0, da_fare)} da correggere")
    print(f"\n{totale} segmenti da correggere in tutto")

    if not args.consent:
        print("\nNessuna chiamata: manca --consent.")
        print("Il testo delle conversazioni resterebbe sul portatile.")
        return 0

    if args.dry:
        print(f"\n[dry-run] fino a {args.limit or 'tutti'} segmenti per "
              f"sessione, niente scritto\n")

    correttore = Correttore(modello=args.model, consentito=True,
                            pausa=args.pausa)
    pronto, motivo = correttore.pronto()
    if not pronto:
        raise SystemExit(f"non posso procedere: {motivo}")

    for d in sessioni:
        segmenti = _leggi_sessione(d)
        if not segmenti:
            continue
        if not args.riscorri:
            gia = _carica_precedente(d)
            segmenti = [s for s in segmenti
                        if int(s.get("idx", 0)) not in gia]
        if args.limit:
            segmenti = segmenti[:args.limit]
        if not segmenti:
            print(f"{d.name}: niente da fare")
            continue

        print(f"\n=== {d.name}: {len(segmenti)} segmenti ===")
        da_inviare = [(int(s.get("idx", 0)), s.get("text", ""))
                      for s in segmenti]
        risultati = correttore.correggi(da_inviare)

        righe = []
        out_segmenti = []
        for seg, res in zip(segmenti, risultati):
            r = res.to_dict()
            r["start"] = seg.get("start")
            r["end"] = seg.get("end")
            r["speaker"] = seg.get("speaker")
            r["firma"] = _firma(seg.get("text", ""))
            out_segmenti.append(r)
            righe.append((seg, r))
            if not args.dry:
                print(f"  [{seg.get('idx'):>4}] {r['n_changed']:>3} correzioni"
                      + ("  SCARTATO" if r["discarded"] else ""))

        _mostra_confronto(righe)

        if args.dry:
            continue

        # I segmenti scartati restano nel file: sapere che quel testo e'
        # rimasto grezzo e' informazione, non rumore. Saltarli perderebbe
        # la traccia di dove il modello ha fallito.
        #
        # E i giri precedenti vanno tenuti: se il file si riscrive solo
        # coi segmenti di adesso, un girointerrotto a meta' perderebbe
        # tutto il lavoro di ieri. Si unisce per indice, e un segmento
        # rifatto in questa passata sostituisce il suo vecchio.
        uniti = {r["idx"]: r for r in _carica_precedente(d).values()}
        uniti.update({r["idx"]: r for r in out_segmenti})
        finali = sorted(uniti.values(), key=lambda r: r["idx"])
        _scrivi(d, finali, args.model)

        # Le varianti pubblicabili, cosi' che su GitHub si legga il
        # testo corretto e non quello grezzo. `transcript.txt` resta
        # intatto: i due errori devono restare entrambi visibili.
        scritti = scrivi_varianti(d, _leggi_sessione(d), {
            r["idx"]: r for r in finali})
        print(f"  scritto {d / NOME_FILE}")
        for p in scritti:
            print(f"  scritto {p.name}")

    if args.dry:
        print("\n[dry-run] niente scritto. Con --consent si scrive.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
