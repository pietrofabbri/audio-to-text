"""
Rivedi le identità vocali: vedi chi è chi, metti i nomi, correggi gli errori.

    python review_speakers.py                 # elenco voci e somiglianze
    python review_speakers.py name GLOBAL_001 Pietro
    python review_speakers.py merge GLOBAL_003 GLOBAL_004
    python review_speakers.py threshold 0.70
    python review_speakers.py sync            # allinea i nomi a tutto il materiale già scritto

Perché esiste. Il riconoscimento automatico lavora su una soglia di
coseno, e su quattro registrazioni reali i numeri sono ambigui in una
fascia precisa: persone certe stanno a 0,82 (stessa persona), persone
diverse a 0,13-0,29 (già ben separate), e in mezzo una banda 0,53-0,67
dove la macchina non sa. Non è un difetto da nascondere: è il punto in
cui serve un giudizio umano.

E il giudizio umano costa una volta sola. Un merge sbagliato si vede
subito e si separa con `split`; un nome sbagliato si cambia. Quello che
non si può fare è fingere che la soglia risolva da sola un caso che non
è decidibile dagli embedding.

Nomi e merge sono salvati subito e valgono per tutte le sessioni future.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from core.config import config  # noqa: E402
from core.speaker_db import SpeakerDB, cosine_similarity, to_vector  # noqa: E402


def _fmt_min(sec: float) -> str:
    if sec < 90:
        return f"{sec:.0f}s"
    if sec < 5400:
        return f"{sec/60:.0f}m"
    return f"{sec/3600:.1f}h"


def cmd_list(db: SpeakerDB, args) -> int:
    prof = db.profiles()
    if not prof:
        print("Nessuna voce registrata. Elabora qualche sessione prima.")
        return 0

    ids = sorted(prof)
    print(f"\n{len(ids)} voci riconosciute "
          f"(soglia di match: {db.threshold:.2f})\n")
    for gid in ids:
        p = prof[gid]
        name = p["name"] or "(senza nome)"
        print(f"  {gid}  {name:<16s} {p['total_seconds']/60:6.1f} min  "
              f"{p['sessions_count']} sessioni")
        print(f"      {' · '.join(p['sessions'])}")

    # --- matrice di somiglianza ----------------------------------------
    cents = {}
    for gid in ids:
        rec = db._data["speakers"].get(gid, {})
        v = to_vector(rec.get("centroid"))
        if v is not None:
            cents[gid] = v
    if len(cents) > 1:
        print("\nSomiglianza fra le voci (coseno):")
        short = {g: g.replace("GLOBAL_", "#") for g in ids}
        print("        " + " ".join(f"{short[g]:>5s}" for g in ids))
        for a in ids:
            if a not in cents:
                continue
            row = []
            for b in ids:
                if b not in cents:
                    row.append("    -")
                    continue
                s = cosine_similarity(cents[a], cents[b])
                # Il colore non esiste in un terminale: si marca il
                # numero, e il commento dice cosa significa.
                mark = " <" if s >= db.threshold else ""
                row.append(f"{s:5.2f}{mark}")
            print(f"  {short[a]:>5s} " + " ".join(row))
        print("\n  < = sopra la soglia, ritenuta la stessa persona")
        print("  una coppia fra 0,5 e la soglia è il caso in cui la macchina")
        print("  non sa: decidi tu con 'merge' (stessa persona) o lascia così")
    return 0


def cmd_name(db: SpeakerDB, args) -> int:
    if args.gid not in db._data["speakers"]:
        print(f"Voce sconosciuta: {args.gid}", file=sys.stderr)
        return 1
    db.set_name(args.gid, args.name or None)
    db.save()
    print(f"{args.gid} -> {args.name or '(senza nome)'}")

    # Rinominare non finisce qui. Il nome era finito anche dentro
    # transcript.json, session.json e corpus.db di ogni sessione già
    # scritta, e quelle copie non si aggiornano da sole: senza questo
    # passaggio il rename vale solo da oggi in poi, e il corpus continua
    # a parlare di GLOBAL_001 per settimane.
    return cmd_sync(db, args, quiet=False)


def cmd_sync(db: SpeakerDB, args, quiet: bool = False) -> int:
    """Riallinea i nomi a tutto il materiale già scritto.

    Il DB delle voci è l'unica fonte: questo comando copia il nome dove
    serve, senza rileggere l'audio e senza rielaborare nulla. Costa
    secondi, e dopo un rename o un merge è il passo che rende il cambio
    visibile nelle sessioni passate.
    """
    from core.speaker_sync import sync_speaker_names

    try:
        report = sync_speaker_names(db=db, dry_run=getattr(args, "dry_run", False))
    except Exception as exc:  # noqa: BLE001
        # L'allineamento è manutenzione, non lavoro notturno: se fallisce
        # il nome è già salvato nel DB delle voci e si può ritentare.
        print(f"Allineamento fallito: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    if quiet:
        return 0

    if report["dry_run"]:
        print(f"[dry-run] {report['named_speakers']} voci nominate; "
              f"aggiornerei {report['db_rows']} righe in corpus.db e "
              f"{report['sessions_updated']} sessioni")
        return 0

    print(f"Allineati {report['named_speakers']} voci nominate: "
          f"{report['db_rows']} righe in corpus.db, "
          f"{report['sessions_updated']} sessioni aggiornate "
          f"({report['files_updated']} file), "
          f"{report['sessions_unchanged']} già allineate")
    if report["sessions_updated"]:
        # Le sessioni aggiornate vanno ripubblicate, o la repo del corpus
        # continua a mostrare i vecchi pseudonimi: il sync corregge il
        # materiale locale, non quello già pushato.
        print("Se hai gia' pubblicato il corpus, rilancia: "
              "python publish_corpus.py push")
    return 0


def cmd_merge(db: SpeakerDB, args) -> int:
    """Unisce due identità: i contributi passano sotto il primo ID.

    L'idità conservata è il primo dei due, così i riferimenti già scritti
    nelle sessioni passate restano validi per la maggior parte del
    materiale.
    """
    keep, drop = args.gid, args.into
    if keep not in db._data["speakers"] or drop not in db._data["speakers"]:
        print("Una delle due voci non esiste", file=sys.stderr)
        return 1
    if keep == drop:
        print("Stessa voce", file=sys.stderr)
        return 1

    src = db._data["speakers"].pop(drop)
    dst = db._data["speakers"][keep]
    for k, v in src.get("sessions", {}).items():
        dst.setdefault("sessions", {}).setdefault(k, v)
        dst["sessions"][k]["seconds"] = (
            dst["sessions"][k].get("seconds", 0) + v.get("seconds", 0)
        )
    dst["total_seconds"] = sum(
        s.get("seconds", 0) for s in dst.get("sessions", {}).values()
    )
    dst["sessions_count"] = len({s.get("stem") for s in dst.get("sessions", {}).values()})
    if dst.get("first_seen") and src.get("first_seen"):
        dst["first_seen"] = min(dst["first_seen"], src["first_seen"])
    if dst.get("last_seen") and src.get("last_seen"):
        dst["last_seen"] = max(dst["last_seen"], src["last_seen"])
    if not dst.get("name") and src.get("name"):
        dst["name"] = src["name"]

    db.save()
    print(f"{drop} unita in {keep} "
          f"({dst['total_seconds']/60:.1f} min, {dst['sessions_count']} sessioni)")
    # Le sessioni che citavano il vecchio ID continuano a citarlo: sono
    # dati già scritti e non si rietichettano da sole. Si dice, e si
    # lascia il comando a chi lo vuole.
    print("Attenzione: le sessioni gia' scritte che citano "
          f"{drop} non vengono rietichettate.")
    return cmd_sync(db, args, quiet=False)


def cmd_split(db: SpeakerDB, args) -> int:
    """Stacca una voce dal DB: tornerà a essere riconosciuta come nuova.

    Serve quando due voci sono state unite per errore: si separa e si
    lascia che il prossimo file la ricostruisca. Non è una cancellazione:
    il materiale audio non tocca qui, si riapplica solo alle sessioni
    future.
    """
    if args.gid not in db._data["speakers"]:
        print(f"Voce sconosciuta: {args.gid}", file=sys.stderr)
        return 1
    rec = db._data["speakers"].pop(args.gid)
    db.save()
    print(f"{args.gid} staccata ({len(rec.get('sessions', {}))} contributi). "
          f"Riapparirà come voce nuova alla prossima sessione.")
    return 0


def cmd_threshold(db: SpeakerDB, args) -> int:
    if args.value <= 0 or args.value >= 1:
        print("La soglia deve stare fra 0 e 1", file=sys.stderr)
        return 1
    db.threshold = args.value
    db.save()
    print(f"Soglia di match: {args.value:.2f}")
    print("Vale per le sessioni future. Le identità già assegnate restano.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd")

    sub.add_parser("list", help="elenco voci e matrice di somiglianza")

    n = sub.add_parser("name", help="assegna un nome a una voce")
    n.add_argument("gid")
    n.add_argument("name", nargs="?", help="vuoto per rimuovere il nome")
    n.add_argument("--no-sync", action="store_true",
                   help="salva il nome senza riallineare le sessioni già scritte")

    m = sub.add_parser("merge", help="unisce due voci: sono la stessa persona")
    m.add_argument("gid")
    m.add_argument("into")

    s = sub.add_parser("split", help="stacca una voce erroneamente unita")
    s.add_argument("gid")

    t = sub.add_parser("threshold", help="cambia la soglia di somiglianza")
    t.add_argument("value", type=float)

    y = sub.add_parser(
        "sync",
        help="allinea i nomi a corpus.db e alle sessioni già scritte",
    )
    y.add_argument("--dry-run", action="store_true",
                   help="mostra cosa cambierebbe senza scrivere")

    args = ap.parse_args()
    cmd = args.cmd or "list"

    db = SpeakerDB()

    if cmd == "name" and getattr(args, "no_sync", False):
        if args.gid not in db._data["speakers"]:
            print(f"Voce sconosciuta: {args.gid}", file=sys.stderr)
            return 1
        db.set_name(args.gid, args.name or None)
        db.save()
        print(f"{args.gid} -> {args.name or '(senza nome)'} (nessun allineamento)")
        return 0

    return {
        "list": cmd_list, "name": cmd_name, "merge": cmd_merge,
        "split": cmd_split, "threshold": cmd_threshold, "sync": cmd_sync,
    }[cmd](db, args)


if __name__ == "__main__":
    raise SystemExit(main())