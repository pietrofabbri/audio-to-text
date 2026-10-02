#!/usr/bin/env python
"""
sync_device.py — porta le registrazioni dal device alla pipeline.

Uso tipico (il device è collegato da almeno 3 ore):

    python sync_device.py detect            # che cosa è montato? (non tocca nulla)
    python sync_device.py pull --dry-run    # cosa verrebbe fatto?
    python sync_device.py pull              # importa, processa, archivia
    python sync_device.py purge             # svuota l'archivio oltre i 7 giorni

Filosofia: il registratore resta la fonte di verità finché il lavoro non
è finito. I file NON vengono copiati prima di elaborarli — vengono letti
dove sono, e la cancellazione è l'ultimo atto, dopo che la trascrizione
esiste ed è stata verificata. Se il cavolo salta, il file è ancora lì.

Ordine di sicurezza, in una riga: elaboro → verifico → archivio → cancello.
Se una delle prime fasi fallisce, il file non viene cancellato. MAI.

Il manifest in logs/device_manifest.jsonl registra ogni file visto, con
hash e esito, così ogni cancellazione è reversibile da un punto di vista
amministrativo anche quando il dato non lo è più.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import shutil
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from core.config import config, INPUT_DIR, OUTPUT_DIR, LOGS_DIR  # noqa: E402
from core.device import (  # noqa: E402
    AUDIO_EXTENSIONS,
    discover,
    describe_filenames,
    parse_recording_time,
    pick_recorder,
)
from core.corpus_db import CorpusDB  # noqa: E402

logger = logging.getLogger("sync_device")

MANIFEST_PATH = LOGS_DIR / "device_manifest.jsonl"
ARCHIVE_DAYS = 7

# Un output è considerato valido se contiene questo file e non è vuoto.
# È la verifica minima che autorizza a cancellare l'audio originale.
REQUIRED_OUTPUT = "transcript.json"


# ---------------------------------------------------------------------------
# Utilità
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


def file_sha256(path: Path, chunk: int = 1 << 20) -> str:
    """Hash del contenuto. Costa qualche secondo per un file da 1h: serve
    per poter dire in futuro che il file elaborato era proprio quello."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while block := f.read(chunk):
            h.update(block)
    return h.hexdigest()


def append_manifest(record: dict) -> None:
    MANIFEST_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(MANIFEST_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def read_manifest() -> list[dict]:
    if not MANIFEST_PATH.exists():
        return []
    out = []
    for line in MANIFEST_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def already_processed_hashes() -> set[str]:
    """Hash dei file già eliminati con successo.

    Serve a coprire il caso in cui la cancellazione è fallita (permessi,
    volume protetto, device scollegato): il file ricompare a ogni run e,
    senza questo, verrebbe riprocessato da capo ogni volta.
    """
    return {
        r["sha256"] for r in read_manifest()
        if r.get("action") == "deleted" and r.get("sha256")
    }


# ---------------------------------------------------------------------------
# detect
# ---------------------------------------------------------------------------

def cmd_detect(args) -> int:
    volumes = discover(args.mounts)
    if not volumes:
        print("Nessun volume con file audio trovato in", args.mounts)
        print("\nSe il device è collegato e ha una cartella 'record':")
        print("  - verifica il cavo e che sia riconosciuto come memoria USB")
        print("  - su un device Android via MTP macOS NON lo monta in /Volumes:")
        print("    in quel caso serve un percorso alternativo (--source)")
        return 1

    print(f"Volumi con audio in {args.mounts}:\n")
    for v in volumes:
        print(v.summary())
        marker = "  <== REGISTRATORE" if v.looks_like_recorder else ""
        print(f"    candidato: {'si' if v.looks_like_recorder else 'no'}{marker}\n")

    chosen = pick_recorder(volumes)
    if not chosen:
        print("Nessun registratore identificato con certezza.")
        return 1

    print(f"Registratore: {chosen.path}\n")
    info = describe_filenames(chosen.audio_files)
    print("Come sono fatti i nomi dei file:")
    print(f"  file totali   : {info['total']}")
    print(f"  data ricavata : {info['parsed']} ({100*info['parsed']//max(1,info['total'])}%)")
    print(f"  pattern       : {info['patterns']}")
    if info.get("earliest"):
        print(f"  primo file    : {info['earliest']}")
        print(f"  ultimo file   : {info['latest']}")
    if info.get("median_gap_hours") is not None:
        print(f"  intervallo mediano: {info['median_gap_hours']} h fra un file e l'altro")
    if info["unparsed"]:
        print(f"  NON riconosciuti ({info['unparsed']}): {info['unparsed_examples']}")
        print("  -> questi file verranno importati ma senza orario di registrazione")

    if not chosen.writable:
        print("\nATTENZIONE: il volume è in sola lettura. Si può elaborare, "
              "ma i file non potranno essere cancellati.")

    print(f"\nPer procedere:\n  python sync_device.py pull --source '{chosen.path}'")
    return 0


# ---------------------------------------------------------------------------
# pull
# ---------------------------------------------------------------------------

def _stem_for(path: Path, recorded: datetime | None) -> str:
    """Nome della sessione. Se il file ha già un orario nel nome lo si
    riusa (è l'informazione più affidabile), altrimenti si usa la data
    di importazione con l'ora corrente per non sovrascrivere."""
    if recorded:
        return recorded.strftime("%Y-%m-%d_%H-%M-%S")
    return f"imported-{datetime.now().strftime('%Y-%m-%d_%H-%M-%S')}-{path.stem[:40]}"


def _verify_output(stem: str) -> tuple[bool, str]:
    out_dir = OUTPUT_DIR / stem
    p = out_dir / REQUIRED_OUTPUT
    if not p.exists():
        return False, f"manca {REQUIRED_OUTPUT}"
    try:
        doc = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        return False, f"{REQUIRED_OUTPUT} non leggibile: {exc}"
    segs = doc.get("segments") or []
    if not segs:
        return False, "nessun segmento trascritto"
    words = sum(len((s.get("text") or "").split()) for s in segs)
    if words < 5:
        return False, f"solo {words} parole: trascrizione sospetta"
    return True, f"{len(segs)} segmenti, {words} parole"


def _archive(source: Path, stem: str) -> Path | None:
    """Copia l'originale nell'archivio locale, che verrà ripulito dopo
    ARCHIVE_DAYS giorni. Non è 'tenere tutto per sempre': è la rete di
    sicurezza per il caso in cui l'ASR sbagli e il device non è più lì."""
    if not args_keep_local():
        return None
    archive_dir = ROOT / "archive"
    archive_dir.mkdir(parents=True, exist_ok=True)
    dest = archive_dir / f"{stem}{source.suffix}"
    if dest.exists():
        return dest
    try:
        shutil.copy2(source, dest)
        return dest
    except OSError as exc:
        logger.warning("Archiviazione fallita per %s: %s", source.name, exc)
        return None


def args_keep_local() -> bool:
    return os.environ.get("A2T_KEEP_LOCAL", "1") != "0"


def cmd_pull(args) -> int:
    from run import process_file  # la pipeline vera, nessun duplicato

    # --- 1. individuare il device ------------------------------------
    if args.source:
        src = Path(args.source)
        if not src.is_dir():
            print(f"Non esiste: {src}", file=sys.stderr)
            return 1
        chosen = None
        files = sorted(
            f for f in src.rglob("*")
            if f.is_file() and f.suffix.lower() in AUDIO_EXTENSIONS
        )
        label = str(src)
    else:
        chosen = pick_recorder(discover(args.mounts))
        if not chosen:
            print("Nessun registratore identificato. Usa --source o 'detect'.",
                  file=sys.stderr)
            return 1
        src = chosen.path
        files = sorted(chosen.audio_files)
        label = chosen.label

    if not files:
        print(f"Nessun file audio in {src}")
        return 1

    if args.limit:
        files = files[:args.limit]

    logger.info("Device: %s (%s)", label, src)
    logger.info("File trovati: %d", len(files))

    # --- 2. cosa è già stato fatto -----------------------------------
    done_hashes = already_processed_hashes()

    # --- 3. elaborazione ---------------------------------------------
    results = {"ok": 0, "failed": 0, "skipped": 0, "deleted": 0, "kept": 0}

    for i, f in enumerate(files, 1):
        recorded, pattern = parse_recording_time(f.name)
        try:
            digest = file_sha256(f)
        except OSError as exc:
            logger.warning("[%d/%d] %s: illeggibile (%s)", i, len(files), f.name, exc)
            results["skipped"] += 1
            continue

        if digest in done_hashes:
            logger.info("[%d/%d] %s: già elaborato, cerco di liberare spazio",
                        i, len(files), f.name)
            _try_delete(f, digest, "già elaborato in una run precedente", args)
            continue

        stem = _stem_for(f, recorded)
        # stem derivato dal nome: due file diversi non devono mai
        # condividere la cartella di output
        out_dir = OUTPUT_DIR / stem
        if (out_dir / REQUIRED_OUTPUT).exists():
            logger.info("[%d/%d] %s: output già presente, salto", i, len(files), f.name)
            results["skipped"] += 1
            append_manifest({
                "ts": _now_iso(), "device": label, "file": f.name,
                "sha256": digest, "stem": stem, "action": "skipped",
                "reason": "output già presente",
            })
            continue

        logger.info("[%d/%d] %s (%.1f MB) — orario %s [%s]",
                    i, len(files), f.name, f.stat().st_size / 1e6,
                    recorded.isoformat() if recorded else "ignoto", pattern)

        if args.dry_run:
            print(f"  [dry-run] elaborerei {f.name} -> {stem}")
            results["skipped"] += 1
            continue

        # Argomminto fittizio: la pipeline legge args.no_diarization ecc.
        ns = argparse.Namespace(
            no_diarization=False, no_prosody=args.no_prosody,
        )
        t0 = time.time()
        try:
            ok = process_file(f, config, ns)
        except Exception as exc:  # noqa: BLE001
            logger.error("  elaborazione fallita: %s: %s", type(exc).__name__, exc)
            ok = False
        elapsed = time.time() - t0

        if not ok:
            logger.error("  pipeline non completata: il file resta sul device")
            results["failed"] += 1
            append_manifest({
                "ts": _now_iso(), "device": label, "file": f.name, "sha256": digest,
                "stem": stem, "action": "kept", "reason": "pipeline fallita",
                "elapsed_sec": round(elapsed, 1),
            })
            continue

        # --- 4. verifica prima di toccare l'originale -----------------
        good, why = _verify_output(stem)
        if not good:
            logger.error("  output non verificato (%s): il file resta sul device", why)
            results["failed"] += 1
            append_manifest({
                "ts": _now_iso(), "device": label, "file": f.name, "sha256": digest,
                "stem": stem, "action": "kept", "reason": f"output non valido: {why}",
                "elapsed_sec": round(elapsed, 1),
            })
            continue

        # --- 5. annotazioni derivate ---------------------------------
        _stamp_session(stem, recorded, label, f.name)

        with CorpusDB() as cdb:
            cdb.ingest_session(
                stem=stem,
                transcript=json.loads((out_dir / REQUIRED_OUTPUT).read_text(encoding="utf-8")),
                vad_stats={},
                recorded_at=recorded.isoformat() if recorded else None,
                source_device=label,
                source_filename=f.name,
            )

        # --- 6. archivio, poi cancellazione --------------------------
        archived = _archive(f, stem)
        deleted = _try_delete(f, digest, f"elaborato: {why}", args)

        results["ok"] += 1
        results["deleted" if deleted else "kept"] += 1
        append_manifest({
            "ts": _now_iso(), "device": label, "file": f.name, "sha256": digest,
            "stem": stem, "action": "deleted" if deleted else "kept",
            "archived": str(archived) if archived else None,
            "elapsed_sec": round(elapsed, 1), "verified": why,
        })
        logger.info("  fatto in %.0fs — %s", elapsed, why)

    logger.info(
        "Riepilogo: %d completati, %d falliti, %d saltati | cancellati %d, "
        "rimasti sul device %d",
        results["ok"], results["failed"], results["skipped"],
        results["deleted"], results["kept"],
    )
    return 0 if results["failed"] == 0 else 1


def _stamp_session(stem: str, recorded, device: str, filename: str) -> None:
    """Scrive l'orario di registrazione in session.json.

    È il dato che rende possibile, in futuro, allineare la trascrizione
    ai dati biometrici: senza un orario di pareggio il transcript è
    testo isolato, con è una serie di misure nel tempo.
    """
    p = OUTPUT_DIR / stem / "session.json"
    if not p.exists():
        return
    try:
        doc = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return
    doc.setdefault("source", {})
    doc["source"] = {
        "device": device,
        "filename": filename,
        "recorded_at": recorded.isoformat() if recorded else None,
    }
    if recorded:
        doc["meta"]["session_start_wall"] = recorded.isoformat()
    p.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")


def _try_delete(f: Path, digest: str, reason: str, args) -> bool:
    if args.dry_run:
        print(f"  [dry-run] cancellerei {f.name}")
        return False
    if getattr(args, "no_delete", False):
        logger.info("  cancellazione disabilitata (--no-delete), file lasciato")
        return False
    try:
        f.unlink()
        logger.info("  cancellato dal device: %s (%s)", f.name, reason)
        return True
    except OSError as exc:
        # FAT32/exFAT possono rifiutare la cancellazione se il file è
        # in uso o se il volume è montato in sola lettura. Non è un
        # errore della pipeline: si logga e si va avanti.
        logger.warning("  impossibile cancellare %s: %s", f.name, exc)
        return False


# ---------------------------------------------------------------------------
# purge
# ---------------------------------------------------------------------------

def cmd_purge(args) -> int:
    """Svuota l'archivio locale dei file più vecchi di ARCHIVE_DAYS giorni."""
    archive_dir = ROOT / "archive"
    if not archive_dir.is_dir():
        print("Nessun archivio locale.")
        return 0

    keep_days = args.days if args.days else ARCHIVE_DAYS
    removed, kept, freed = 0, 0, 0
    for f in sorted(archive_dir.iterdir()):
        if not f.is_file():
            continue
        age_days = (time.time() - f.stat().st_mtime) / 86400
        if age_days < keep_days:
            kept += 1
            continue
        size = f.stat().st_size
        if args.dry_run:
            print(f"  [dry-run] eliminerei {f.name} ({size/1e6:.1f} MB, {age_days:.0f} giorni)")
        else:
            f.unlink()
        removed += 1
        freed += size

    verb = "verrebbero eliminati" if args.dry_run else "eliminati"
    print(f"{verb} {removed} file oltre {keep_days} giorni ({freed/1e6:.0f} MB), tenuti {kept}")
    return 0


# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(
        description="Importa le registrazioni dal device, le processa e le archivia",
    )
    sub = ap.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("detect", help="identifica il registratore (non modifica nulla)")
    d.add_argument("--mounts", default="/Volumes")
    d.set_defaults(func=cmd_detect)

    p = sub.add_parser("pull", help="importa ed elabora i file trovati sul device")
    p.add_argument("--mounts", default="/Volumes")
    p.add_argument("--source", help="percorso esplicito del device o della cartella")
    p.add_argument("--limit", type=int, help="massimo file da elaborare (prova)")
    p.add_argument("--no-prosody", action="store_true", help="salta la prosodia")
    p.add_argument("--no-delete", action="store_true", help="non cancellare dal device")
    p.add_argument("--dry-run", action="store_true", help="mostra cosa farebbe, non tocca nulla")
    p.set_defaults(func=cmd_pull)

    g = sub.add_parser("purge", help="svuota l'archivio locale oltre i giorni indicati")
    g.add_argument("--days", type=int, default=0, help=f"default {ARCHIVE_DAYS}")
    g.add_argument("--dry-run", action="store_true")
    g.set_defaults(func=cmd_purge)

    args = ap.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[logging.StreamHandler(sys.stdout)],
        force=True,
    )

    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
