"""
Rivedi le identità vocali: vedi chi è chi, metti i nomi, correggi gli errori.

    python review_speakers.py                 # elenco voci e somiglianze
    python review_speakers.py name GLOBAL_001 Pietro
    python review_speakers.py merge GLOBAL_003 GLOBAL_004
    python review_speakers.py threshold 0.70
    python review_speakers.py sync            # allinea i nomi a tutto il materiale già scritto
    python review_speakers.py voices          # coppie di voci da decidere (stessa persona?)
    python review_speakers.py nuove           # voci che non hai ancora guardato
    python review_speakers.py ascolta GLOBAL_035 --play   # sentila prima di nominarla
    python review_speakers.py ignora GLOBAL_051           # vista, resta senza nome

Il giro tipico dopo una notte: `nuove` dice chi e' comparso, `ascolta`
fa sentire tre o quattro frasi di ciascuno, e poi `name`, `merge` o
`ignora`. Il giro notturno prepara gia' gli estratti e lo stesso elenco
in output/voci_da_rivedere.md.

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
import shutil
import subprocess
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
    if not db.merge_ids(keep, drop,
                        dry_run=getattr(args, "dry_run", False)):
        if keep == drop:
            print("Stessa voce", file=sys.stderr)
        else:
            print("Una delle due voci non esiste", file=sys.stderr)
        return 1

    dst = db._data["speakers"][keep]
    print(f"{drop} unita in {keep} "
          f"({dst['total_seconds']/60:.1f} min, {dst['sessions_count']} sessioni)")

    # Il merge non è finito finché le sessioni già scritte non lo
    # sanno. Unire gli embedding senza rietichettare i segmenti lascia
    # due ID per la stessa persona nel corpus, con statistiche che non
    # si sommano — cioè esattamente il problema che il merge doveva
    # risolvere.
    _relabel({drop: keep}, args)
    return cmd_sync(db, args, quiet=False)


def _relabel(renames: dict[str, str], args) -> None:
    from core.corpus_db import CorpusDB
    from core.speaker_sync import relabel_sessions

    dry = getattr(args, "dry_run", False)
    try:
        with CorpusDB() as cdb:
            rep = relabel_sessions(renames, corpus_db=cdb, dry_run=dry)
    except Exception as exc:  # noqa: BLE001
        # Il merge è già salvato nel DB delle voci: fallire qui non lo
        # annulla, e segnalarlo è più utile che far fallire il comando.
        print(f"Rietichettamento non riuscito ({type(exc).__name__}: {exc}). "
              "Le sessioni vecchie citano ancora il vecchio ID: "
              "python review_speakers.py sync --dry-run per vedere cosa resta",
              file=sys.stderr)
        return

    if rep["substitutions"] or rep["db_segments"]:
        print(f"Rietichettate {rep['sessions']} sessioni "
              f"({rep['substitutions']} riferimenti) e {rep['db_segments']} "
              f"righe in corpus.db")
        if not dry and rep["sessions"]:
            print("Se hai gia' pubblicato il corpus, rilancia: "
                  "python publish_corpus.py push")
    else:
        print("Nessuna sessione citava l'ID vecchio: niente da rietichettare")


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
    rec = db._data["speakers"][args.gid]
    contributi = len(rec.get("sessions", {}))
    if getattr(args, "dry_run", False):
        # Il flag esiste fin dall'inizio e non era mai stato letto: il
        # comando prometteva di non toccare niente e cancellava la voce.
        # `split` e' il comando che si usa per non perdere una voce,
        # quindi e' il posto peggiore in cui sbagliare.
        print(f"[dry-run] staccherei {args.gid} ({contributi} contributi). "
              "Niente scritto.")
        return 0
    db._data["speakers"].pop(args.gid)
    db.save()
    print(f"{args.gid} staccata ({contributi} contributi). "
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


def cmd_consolidate(db: SpeakerDB, args) -> int:
    """Rifonde le voci dei cluster deboli nelle sessioni gia' scritte.

    La fusione e' gia' dentro la pipeline, e questa e' la same
    operazione per il materiale che e' passato da prima: senza questo
    comando le sessioni vecchie resterebbero con 21 voci per sempre e
    il corpus avrebbe due linguaggi, uno pulito e uno no, senza che
    niente lo dichiarasse.

    Non rilegge l'audio e non rielabora niente: prende i segmenti e
    gli embedding gia' salvati nel checkpoint, li rifonde, e poi
    riscrive quello che da quei segmenti dipende — i turni di voce
    dentro le parole, la mappa delle identita', il checkpoint.

    Le identita' globali gia' assegnate vengono ricalcolate da capo
    dopo che la sessione e' stata rimossa dal DB delle voci: altrimenti
    il frammento troverebbe subito l'identita' che si era creato la
    prima volta e la fusione non cambierebbe nulla.
    """
    from core.checkpoint import Checkpoint
    from core.config import OUTPUT_DIR
    from core.speakers_merge import MergePolicy, merge_weak_clusters

    dry = getattr(args, "dry_run", False)
    # I default vengono dalla configurazione e non dai numeri scritti qui:
    # due copie degli stessi valori in due posti divergono appena uno
    # dei due viene ritoccato, e il comando che rifonde rifonderebbe
    # secondo regole diverse da quelle della pipeline.
    policy = MergePolicy(
        min_seconds=(args.min_seconds if args.min_seconds is not None
                     else config.speaker_id.merge_min_seconds),
        threshold=(args.threshold if args.threshold is not None
                   else config.speaker_id.merge_threshold),
    )

    sessioni = sorted(
        d for d in OUTPUT_DIR.iterdir()
        if d.is_dir() and list(d.glob("*.checkpoint.json"))
    ) if OUTPUT_DIR.is_dir() else []
    if not sessioni:
        print("Nessuna sessione con checkpoint in output/.")
        return 1

    # Tutte le sessioni con embedding vengono ricalcolate, anche quelle
    # senza fusioni da fare. Il motivo e' che questo comando non corregge
    # solo i frammenti: ricostruisce anche le identita' globali, e quelle
    # vanno ricalcolate per tutte. Una sessione senza frammenti che
    # salta il giro terrebbe la mappa che le avevano dato le identita'
    # sbagliate, e il DB continuerebbe a contare voci che nessuna
    # sessione genera piu'.
    piani = []
    for d in sessioni:
        ck_file = next(d.glob("*.checkpoint.json"))
        dati = json.loads(ck_file.read_text(encoding="utf-8"))
        segmenti = dati.get("diarization_segments") or []
        emb = dati.get("speaker_embeddings") or {}
        if not segmenti or not emb:
            continue
        nuovi, nuovi_emb, rep = merge_weak_clusters(segmenti, emb, policy)
        piani.append((d, ck_file, dati, nuovi, nuovi_emb, rep))

    da_fondere = [p for p in piani if p[5].changed]
    if not da_fondere:
        print("Nessun cluster da fondere: i segmenti sono gia' buoni.")
        print("Le identita' globali verranno comunque ricalcolate.\n")

    if dry:
        print("[dry-run] rifonderei:\n")
        for d, _, _, _, _, rep in piani:
            if not rep.changed:
                print(f"  {d.name}: {rep.clusters_after} voci, nessuna fusione")
                continue
            print(f"  {d.name}: {rep.clusters_before} -> {rep.clusters_after} voci")
            for da, a in sorted(rep.merged.items()):
                print(f"      {da} ({rep.seconds_before.get(da, 0):.0f}s) -> {a}")
        print("\nNessuna scrittura eseguita.")
        return 0

    rinessi = []
    for d, ck_file, dati, nuovi, nuovi_emb, rep in piani:
        # `dati` serve per il percorso del file audio e per la mappa
        # globale vecchia, che serve a conservare le identita'.
        stem = d.name
        audio = Path(dati.get("file") or (d / f"{stem}.wav"))
        ck = Checkpoint(audio, OUTPUT_DIR, stem=stem)

        vecchia = dati.get("speaker_global_map") or {}

        # Un cluster che conserva la sua etichetta locale e' la stessa
        # voce di prima, e la sua identita' globale va conservata.
        # Ricalcolarla da zero non darebbe un risultato diverso ma un
        # ID diverso, e un ID diverso rietichetta l'intero corpus a ogni
        # esecuzione del comando: `consolidate` rieseguito due volte deve
        # dare lo stesso identico risultato, altrimenti non e' una
        # riparazione ma un rumore che cambia da solo.
        #
        # La somiglianza fra l'embedding nuovo e il centroide salvato
        # dice se sono la stessa voce, che e' l'unica cosa che conta.
        riusata: dict[str, str] = {}
        for locale, emb in nuovi_emb.items():
            gid = vecchia.get(locale)
            if not gid or gid not in db._data["speakers"]:
                continue
            sim = cosine_similarity(
                emb, db._data["speakers"][gid].get("centroid"))
            if sim >= db.threshold:
                riusata[locale] = gid

        if riusata and len(riusata) == len(nuovi_emb):
            # Tutte le voci si riappartiscono a quelle che esistono
            # gia': non c'e' niente da ricalcolare, si aggiornano solo
            # i secondi, che la fusione puo' aver cambiato.
            sec = _speaking_seconds(nuovi)
            for locale, gid in riusata.items():
                db._register(gid, stem, locale, to_vector(nuovi_emb[locale]),
                             sec.get(locale, 0.0))
            db.save()
            if riusata != vecchia:
                ck.save_diarization(nuovi, nuovi_emb, riusata)
            rinessi.append(stem)
            if rep.changed:
                _write_merge_report(d, rep)
            print(f"  {stem}: {rep.clusters_after} voci, identita' gia' corrette")
            continue

        # Nessuna fusione e identita' complete: i cluster sono gli stessi
        # di prima, quindi le identita' sono le stesse. Il confronto con
        # il centroide puo' stare sotto soglia anche quando non e' cambiato
        # niente — accade quando il centroide si e' spostato rispetto a
        # una sessione vecchia. Ricalcolarle da zero in quel caso non
        # ripara niente: cambia gli ID e divide una persona in due, perche'
        # le altre sessioni che la hanno registrata continuano a chiamarla
        # con l'identita' vecchia.
        if not rep.changed and len(vecchia) == len(nuovi_emb):
            sec = _speaking_seconds(nuovi)
            for locale, gid in vecchia.items():
                db._register(gid, stem, locale, to_vector(nuovi_emb[locale]),
                             sec.get(locale, 0.0))
            db.save()
            rinessi.append(stem)
            print(f"  {stem}: {rep.clusters_after} voci, nessuna fusione, "
                  "identita' conservate")
            continue

        # Qualcosa non si riappiglia: si parte da zero per quella
        # sessione. E' il caso in cui la fusione ha cambiato una voce
        # cosi' tanto che non e' piu' la stessa, ed e' corretto che
        # debba cercarsi un'identita' nuova.
        db.forget_session(stem)

        ck.save_diarization(nuovi, nuovi_emb, None)
        # La mappa globale la rifa' il DB, non questo comando: qui si
        # cancella quella vecchia cosi' `resolve` parte da zero e non
        # riusa per sbaglio una corrispondenza con le voci già sciolte.
        ck._data["speaker_global_map"] = {}
        ck.save()

        from run import _resolve_global_speakers, _write_merge_report

        class _Fake:            # solo i campi che _resolve_global_speakers usa
            embeddings = nuovi_emb
            speaker_seconds = _speaking_seconds(nuovi)

        nuova_mappa, _ = _resolve_global_speakers(config, stem, _Fake(), db=db)
        ck.save_diarization(nuovi, nuovi_emb, nuova_mappa)

        # I turni di voce dentro le parole vanno ricalcolati: le parole
        # hanno il speaker vecchio scritto dentro, e senza questo passaggio
        # il testo continuerebbe a citare voci che non esistono piu'.
        from pipeline.diarizer import Diarizer
        ck._data["chunks"] = Diarizer.assign_speakers_word_level(
            ck.get_all_chunks(), nuovi,
        )
        # Solo l'assemblaggio va rifatto: la prosodia e' gia' calcolata e
        # non cambia, e rifarla costerebbe minuti di CPU per ottenere lo
        # stesso identico risultato.
        ck.reset_stage("assembly")
        ck.save()

        _write_merge_report(d, rep)
        rinessi.append(stem)
        print(f"  {stem}: {rep.clusters_before} -> {rep.clusters_after} voci "
              f"({len(rep.merged)} fusioni)")

    # Gli ID globali che non hanno piu' nessun contributo sono gia' stati
    # rimossi da `forget_session`, che si fa carico del caso anche
    # quando le sessioni sono piu' di una: una voce che aveva
    # contributi in due file li perde entrambi alla prima passata e la
    # seconda non trova piu' niente da togliere.

    print(f"\nRifuse {len(rinessi)} sessioni. "
          f"Il DB delle voci conta ora {len(db._data['speakers'])} identita'.")
    print("Ora lancia la pipeline per riscrivere gli output:")
    print("  python run.py input/ --all")
    print("e poi: python publish_corpus.py reindex")
    return 0


def _speaking_seconds(segmenti):
    from core.speakers_merge import speaking_seconds
    return speaking_seconds(segmenti)


def cmd_voices(db: SpeakerDB, args) -> int:
    """Matrice di somiglianza fra le voci, voce per voce.

    Il DB delle voci tiene un solo numero per persona: il centroide, la
    media di tutte le sessioni. E' il numero giusto per riconoscere e il
    numero sbagliato per capire, perche' una media non e' una voce. Qui
    invece si confrontano le voci come sono state udite in ciascuna
    sessione, quindi si vede di quanto una persona cambia da un giorno
    all'altro — e soprattutto quali coppie la soglia non riesce a
    decidere.
    """
    import json

    from core.config import OUTPUT_DIR
    from core.voice_matrix import build_matrix, format_report, load_samples

    campioni = load_samples(OUTPUT_DIR)
    if not campioni:
        print(f"Nessun campione vocale in {OUTPUT_DIR}. Serve almeno una "
              "sessione con diarizzazione e identita' globali.")
        return 1

    rep = build_matrix(campioni, soglia=db.threshold, centroidi=db.centroids())
    print(format_report(rep, mostra_tutto=getattr(args, "tutto", False)))

    out = getattr(args, "json", None)
    if out:
        Path(out).write_text(
            json.dumps(rep.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(f"\nScritto: {out}")

    da_decidere = rep.coppie_da_decidere()
    if da_decidere:
        print(f"\n{len(da_decidere)} coppie di voci aspettano una decisione tua. "
              "Prima di unire, ascoltale: review_speakers.py ascolta <voce>.")
    return 0


def cmd_ascolta(db: SpeakerDB, args) -> int:
    """Estratti brevi di una voce, da sentire prima di darle un nome.

    Gli estratti finiscono in data/ascolto/ (fuori dal repo e dal corpus)
    e si riusano: tagliati una volta, restano anche quando l'archivio ha
    cancellato l'audio originale.
    """
    from core.voice_review import CLIP_DIR, prepara_ascolto

    gid = args.gid
    if gid not in db._data["speakers"]:
        print(f"Voce sconosciuta: {gid}", file=sys.stderr)
        return 1
    pronti, senza_audio = prepara_ascolto(gid, n=args.n, rifai=args.rifai)
    nome = db._data["speakers"][gid].get("name")
    print(f"\n{gid}" + (f" ({nome})" if nome else " (senza nome)")
          + f" — {len(pronti)} estratti in {CLIP_DIR}\n")
    if not pronti:
        print("Nessun estratto: " + (
            f"l'audio originale di {senza_audio} candidati non c'e' piu' "
            "(archivio oltre 7 giorni?)." if senza_audio else
            "nessuna corsa di parole abbastanza lunga e pulita di questa voce."))
        return 1
    for i, a in enumerate(pronti, 1):
        e = a.estratto
        print(f"  {i}. {e.sessione}  {e.inizio/60:5.1f} min  {e.durata:4.1f}s  {a.file.name}")
        print(f"     «{e.testo}»")
    if senza_audio:
        print(f"\n  ({senza_audio} candidati saltati: audio originale non piu' disponibile)")

    if args.play:
        player = shutil.which("afplay")
        if not player:
            print("\n--play funziona solo sul Mac (afplay). Apri i file a mano.")
            return 0
        for i, a in enumerate(pronti, 1):
            print(f"\n> {i}/{len(pronti)} «{a.estratto.testo}»")
            subprocess.run([player, str(a.file)], check=False)
    print(f"\nSe la riconosci: python review_speakers.py name {gid} <Nome>")
    return 0


def _sessioni(n: int) -> str:
    return f"{n} sessione " if n == 1 else f"{n} sessioni"


def cmd_nuove(db: SpeakerDB, args) -> int:
    """Le voci che non hai ancora guardato, dalla piu' presente."""
    from core.voice_review import MIN_SECONDI_DA_RIVEDERE, voci_da_rivedere

    minimo = args.min_minuti * 60 if args.min_minuti is not None \
        else MIN_SECONDI_DA_RIVEDERE
    voci = voci_da_rivedere(db, min_secondi=minimo)
    if not voci:
        print(f"Nessuna voce da rivedere (senza nome, mai vista, almeno "
              f"{minimo/60:.0f} min di parlato).")
        return 0
    print(f"\n{len(voci)} voci da rivedere (senza nome, mai viste, almeno "
          f"{minimo/60:.0f} min di parlato):\n")
    for v in voci:
        giorni = sorted({x[:10] for x in v["sessioni"]})
        riga = (f"  {v['gid']}  {v['secondi']/60:6.1f} min  "
                f"{_sessioni(len(v['sessioni']))}  {', '.join(giorni)}")
        if v["vicina"]:
            chi = v["vicina_nome"] or v["vicina"]
            riga += f"   piu' simile: {chi} {v['somiglianza']:.2f}"
            if v["somiglianza"] >= db.threshold - 0.06:
                riga += "  <- vicina alla soglia"
        print(riga)
    print("\n  ascolta:  python review_speakers.py ascolta <voce> --play")
    print("  nome:     python review_speakers.py name <voce> <Nome>")
    print("  unisci:   python review_speakers.py merge <tenere> <unire>")
    print("  lascia:   python review_speakers.py ignora <voce>")
    return 0


def cmd_ignora(db: SpeakerDB, args) -> int:
    """Segna una voce come vista senza darle un nome."""
    if args.gid not in db._data["speakers"]:
        print(f"Voce sconosciuta: {args.gid}", file=sys.stderr)
        return 1
    db.mark_reviewed(args.gid, reviewed=not args.annulla)
    print(f"{args.gid}: " + ("di nuovo fra le voci da rivedere" if args.annulla
                            else "vista, resta senza nome"))
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
    m.add_argument("--dry-run", action="store_true",
                   help="mostra cosa verrebbe rietichettato")

    s = sub.add_parser("split", help="stacca una voce erroneosamente unita")
    s.add_argument("gid")
    s.add_argument("--dry-run", action="store_true")

    t = sub.add_parser("threshold", help="cambia la soglia di somiglianza")
    t.add_argument("value", type=float)

    c = sub.add_parser(
        "consolidate",
        help="rifonde i cluster di voce troppo brevi nelle sessioni gia' "
             "elaborate, senza rileggere l'audio",
    )
    c.add_argument("--dry-run", action="store_true",
                   help="mostra cosa verrebbe rifuso senza scrivere")
    c.add_argument("--min-seconds", type=float, default=None,
                   help="sotto quanti secondi un cluster e' un frammento")
    c.add_argument("--threshold", type=float, default=None,
                   help="somiglianza minima per sciogliere un frammento")

    v = sub.add_parser(
        "voices",
        help="matrice di somiglianza fra le voci, voce per voce e "
             "sessione per sessione",
    )
    v.add_argument("--tutto", action="store_true",
                   help="mostra tutte le coppie, non solo la zona grigia")
    v.add_argument("--json", default=None,
                   help="scrivi la matrice anche in JSON")

    a = sub.add_parser("ascolta", help="estratti audio di una voce, da sentire")
    a.add_argument("gid")
    a.add_argument("--n", type=int, default=4, help="quanti estratti (default 4)")
    a.add_argument("--play", action="store_true", help="riproducili subito (Mac)")
    a.add_argument("--rifai", action="store_true",
                   help="ritaglia anche gli estratti gia' presenti")

    nu = sub.add_parser("nuove", help="le voci che non hai ancora guardato")
    nu.add_argument("--min-minuti", type=float, default=None,
                    help="parlato minimo per proporre una voce (default 1)")

    ig = sub.add_parser("ignora", help="segna una voce come vista, senza nome")
    ig.add_argument("gid")
    ig.add_argument("--annulla", action="store_true",
                    help="rimettila fra le voci da rivedere")

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
        "consolidate": cmd_consolidate, "voices": cmd_voices,
        "ascolta": cmd_ascolta, "nuove": cmd_nuove, "ignora": cmd_ignora,
    }[cmd](db, args)


if __name__ == "__main__":
    raise SystemExit(main())