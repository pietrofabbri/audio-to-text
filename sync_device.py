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
import subprocess
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
    # Prima di tutto, tutti i volumi montati e i motivi di eventuali
    # esclusioni. Un "non trovato" senza questo contesto e' un vicolo
    # cieco: non distingue "il device non e' collegato" da "e' collegato
    # ma in sola lettura" da "e' un .app", e quelle tre cose si
    # risolvono in modo completamente diverso.
    print(f"Volumi montati in {args.mounts}:")
    for entry in sorted(Path(args.mounts).iterdir()) if Path(args.mounts).is_dir() else []:
        if entry.name.startswith("."):
            continue
        marker = " (symlink, ignorato)" if entry.is_symlink() else ""
        try:
            free = shutil.disk_usage(str(entry)).free / 1e9
        except OSError:
            free = 0.0
        print(f"  {entry}{marker}  {free:.0f} GB liberi")
    print()

    volumes = discover(args.mounts)
    if not volumes:
        print("Nessun volume con file audio.")
        print("\nCosa controllare, in quest'ordine:")
        print("  1. il registratore e' collegato? (in /Volumes non c'e)")
        print("     diskutil list external | grep -i apple  <- deve comparire")
        print("  2. e' collegato ma macOS non lo monta?")
        print("     - Android via MTP: macOS NON lo monta, serve OpenMTP")
        print("       oppure la cartella del device via rete/Finder")
        print("     - alcuni registratori si caricano solo tramite app")
        print("  3. e' montato ma i file hanno un'estensione non prevista?")
        print("     ls <volume>  ->  le estensioni supportate sono")
        print(f"       {', '.join(AUDIO_EXTENSIONS)}")
        print("\nCon un percorso esplicito si bypassa il rilevamento:")
        print("  python sync_device.py detect --source /percorso/del/device")
        return 1

    print(f"Volumi con file audio:\n")
    for v in volumes:
        print(v.summary())
        if v.looks_like_recorder:
            print("    candidato: SI  <== REGISTRATORE\n")
        else:
            print(f"    candidato: no — {v.rejection_reason() or 'non scelto'}\n")

    chosen = pick_recorder(volumes)
    if not chosen:
        print("Nessun registratore identificato con certezza.")
        print("Se piu di un volume e' plausibile, indicane uno esplicitamente:")
        for v in volumes:
            print(f"  python sync_device.py detect --source '{v.path}'")
        return 1

    print(f"Registratore: {chosen.path}\n")
    info = describe_filenames(chosen.audio_files)
    print("Come sono fatti i nomi dei file:")
    print(f"  file totali   : {info['total']}")
    print(f"  data ricavata : {info['parsed']} ({100*info['parsed']//max(1,info['total'])}%)")
    print(f"  pattern       : {info['patterns']}")
    if info.get("est_hours"):
        print(f"  durata totale : {info['est_hours']:.1f} ore")
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


def cmd_doctor(args) -> int:
    """
    Controllo dell'ambiente, una voce alla volta, con un verdetto per ognuna.

    Serve a una cosa precisa: distinguere "l'ambiente non è pronto" da
    "manca solo il device". Sono due problemi che si risolvono con
    azioni opposte, e confonderli fa perdere un'ora a cercare il
    problema nel posto sbagliato.
    """
    print("=" * 64)
    print("  Controllo ambiente — audio-to-text")
    print("=" * 64)

    ok = True

    def check(label: str, good: bool, detail: str = "", fix: str = "") -> None:
        nonlocal ok
        segno = "OK  " if good else "KO  "
        print(f"[{segno}] {label}")
        if detail:
            print(f"        {detail}")
        if not good and fix:
            print(f"        → {fix}")
        if not good:
            ok = False

    # --- strumenti di sistema -----------------------------------------
    for tool in ("ffmpeg", "ffprobe"):
        path = shutil.which(tool)
        check(f"{tool} disponibile", bool(path), path or "non nel PATH",
              "brew install ffmpeg")

    # --- spazio disco ---------------------------------------------------
    usage = shutil.disk_usage(str(ROOT))
    free_gb = usage.free / 1e9
    # 18 ore di registrazione: WAV 16kHz mono ~2 GB, più gli output
    need_gb = 3.0
    check("spazio disco", free_gb > need_gb,
          f"{free_gb:.0f} GB liberi (servono ~{need_gb:.0f} GB per le cache WAV)",
          "libera spazio: le cache WAV sono in input/.wav_cache/")

    # --- modelli -------------------------------------------------------
    # Si verifica il modello che la pipeline usera DAVVERO, non "un
    # faster-whisper qualsiasi": nella cache c'è anche il small (usato
    # da whisperx per default) e un controllo che dicesse "OK" per quello
    # darebbe una risposta falsa sul modello da 1,5 GB.
    hub = Path.home() / ".cache" / "huggingface" / "hub"
    sys.path.insert(0, str(ROOT))
    try:
        from core.config import config as _cfg
        wanted = _cfg.asr.model_id
    except Exception:  # noqa: BLE001
        wanted = "large-v3-turbo"

    asr_models = sorted(
        d.name.split("models--")[-1].replace("--", "/")
        for d in hub.glob("models--*--faster-whisper-*")
    ) if hub.is_dir() else []
    asr_ok = any(wanted.split("/")[-1] in m for m in asr_models)
    check(f"modello ASR in uso ({wanted})", asr_ok,
          "in cache" if asr_ok else f"in cache: {asr_models or 'nessuno'}",
          "si scarica alla prima esecuzione, serve rete")

    for label, pat in (
        ("modello pyannote", "models--pyannote--segmentation*"),
        ("modello mlx (opzionale)", "models--mlx-community--whisper*"),
    ):
        found = list(hub.glob(pat)) if hub.is_dir() else []
        check(label, bool(found),
              found[0].name if found else "non in cache",
              "si scarica alla prima esecuzione, serve rete")

    # --- token HF ------------------------------------------------------
    token = None
    for p in (Path.home() / ".cache/huggingface/token",
              Path.home() / ".huggingface/token"):
        if p.exists() and p.read_text().strip():
            token = p
            break
    check("token Hugging Face", bool(token),
          str(token) if token else "assente",
          "huggingface-cli login  (serve per la diarizzazione)")

    # --- import python --------------------------------------------------
    try:
        import faster_whisper  # noqa: F401
        check("faster-whisper", True, getattr(faster_whisper, "__version__", "installato"))
    except ImportError as exc:
        check("faster-whisper", False, str(exc), "pip install faster-whisper")
    try:
        import pyannote.audio  # noqa: F401
        check("pyannote.audio", True, "installato")
    except ImportError as exc:
        check("pyannote.audio", False, str(exc), "pip install pyannote.audio")

    # --- device ---------------------------------------------------------
    print()
    vol = pick_recorder(discover())
    if vol:
        check("registratore", True, f"{vol.path} — {vol.audio_count} file audio",
              "")
    else:
        print("[KO  ] registratore non rilevato")
        print("        È l'unica voce attesa in KO se l'ambiente è a posto.")
        print("        Collegalo e riapeti: python sync_device.py detect")

    # --- job launchd ----------------------------------------------------
    print()
    for label in ("it.pietrofabbri.audio-to-text",
                  "it.pietrofabbri.audio-to-text-daytime"):
        res = subprocess.run(["launchctl", "list", label], capture_output=True)
        check(f"job launchd {label.split('.')[-1]}", res.returncode == 0,
              "attivo" if res.returncode == 0 else "non caricato",
              "python setup_launchd.py install")

    print()
    print("=" * 64)
    print("  Ambiente pronto." if ok else "  Ci sono voci in KO: vedi le righe sopra.")
    print("=" * 64)
    return 0 if ok else 1


# ---------------------------------------------------------------------------
# pull
# ---------------------------------------------------------------------------

def _stem_for(path: Path, recorded: datetime | None) -> str:
    """Nome della sessione. Se il file ha gia' un orario nel nome lo si
    riusa (e' l'informazione piu' affidabile). Altrimenti si usa la data
    di importazione piu' un hash del nome: senza hash, due file senza
    orario importati nello stesso secondo finirebbero nella stessa
    cartella di output, e il secondo verrebbe scambiato per un output
    gia' presente e non verrebbe mai trascritto."""
    if recorded:
        return recorded.strftime("%Y-%m-%d_%H-%M-%S")
    tag = hashlib.sha1(path.name.encode("utf-8")).hexdigest()[:6]
    return f"imported-{datetime.now().strftime('%Y-%m-%d_%H-%M-%S')}-{tag}-{path.stem[:32]}"


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
        # Gli stessi filtri del rilevamento automatico: senza, un
        # segnaposto da 0 byte verrebbe processato (e cancellato) anche
        # passando per --source, e i due percorsi si comporterebbero in
        # modo diverso.
        from core.device import MIN_AUDIO_BYTES
        files = sorted(
            f for f in src.rglob("*")
            if f.is_file()
            and f.suffix.lower() in AUDIO_EXTENSIONS
            and f.stat().st_size >= MIN_AUDIO_BYTES
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

    # Ordine cronologico: la coda si svuota dal piu vecchio, cosi il
    # ritardo non si accumula sempre sugli stessi file.
    files.sort(key=lambda f: (parse_recording_time(f.name)[0] or datetime.max, f.name))

    # --- 2. cosa è già stato fatto -----------------------------------
    done_hashes = already_processed_hashes()

    # --- 3. elaborazione ---------------------------------------------
    results = {"ok": 0, "failed": 0, "skipped": 0, "deleted": 0, "kept": 0}
    budget = _Budget(args.max_seconds, simulate=args.dry_run)

    for i, f in enumerate(files, 1):
        # La finestra si controlla PRIMA di iniziare un file, non
        # durante: un file iniziato e non finito costerebbe il suo tempo
        # senza produrre niente, e il giorno dopo lo si rifarebbe da capo.
        est = budget.estimate(f)
        if budget.exhausted(est):
            logger.warning(
                "Budget esaurito (%.0fs su %ds): la coda si ferma qui. "
                "%d file aspettano la notte dopo, nessuno verra perso.",
                budget.elapsed(), budget.limit, len(files) - i + 1,
            )
            results["deferred"] = len(files) - i + 1
            break
        budget.started_file(est)
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
            # In dry-run il tempo non passa davvero, ma consumarlo
            # comunque e' quello che rende il dry-run un piano: mostra
            # quanti file entrano davvero nella finestra.
            budget.finished_file(_probe_duration(f) or 0.0, simulated=True)
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
        budget.finished_file(_probe_duration(f) or 0.0)
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


class _Budget:
    """
    Budget di tempo della notte, con stima del file successivo.

    La stima non viene da una tabella: si impara dai file già fatti in
    questa stessa run. Dopo il primo file da un'ora si sa quanto costa
    un file da un'ora, e la decisione di iniziare o no smette di essere
    un'ipotesi. Prima del primo file si usa un RTF medio, che è una
    stima dichiarata come tale.
    """

    # RTF complessivo della pipeline (elaborazione / audio), misurato
    # su questo Mac: 74s per 97,8s di audio = 0,76.
    MEASURED_RTF = 0.76

    # Margine applicato alla stima per la decisione "inizio questo
    # file?". Sopravvalutare il tempo necessario è la direzione giusta
    # in cui sbagliare: meglio iniziare un file in meno che iniziarne
    # uno che non finisce dentro la finestra.
    SAFETY = 1.15

    @property
    def DEFAULT_RTF(self) -> float:  # noqa: N802 - nome da costante
        return self.MEASURED_RTF * self.SAFETY

    def __init__(self, limit_sec: int, simulate: bool = False) -> None:
        self.limit = max(0, int(limit_sec or 0))
        self.t0 = time.time()
        self.simulate = simulate
        self._audio_done = 0.0
        self._time_done = 0.0
        self._current_start: float | None = None
        self._current_est: float = 0.0

    def elapsed(self) -> float:
        """Tempo consumato.

        In simulazione è la somma delle stime: usare l'orologio qui
        farebbe restare il residuo costante e il budget non taglierebbe
        mai, che è il contrario di quello che serve a un piano.
        """
        if self.simulate:
            return self._time_done
        return time.time() - self.t0

    def remaining(self) -> float:
        if self.limit <= 0:
            return float("inf")
        return self.limit - self.elapsed()

    def rtf(self) -> float:
        if self._time_done <= 0 or self._audio_done <= 0:
            return self.DEFAULT_RTF
        return self._time_done / self._audio_done

    def estimate(self, f: Path) -> float:
        """Secondi stimati per il file, dalla durata se disponibile."""
        dur = _probe_duration(f) or 3600.0
        return dur * self.rtf()

    def started_file(self, est: float) -> None:
        self._current_start = time.time()
        self._current_est = est

    def finished_file(self, audio_sec: float, simulated: bool = False) -> None:
        if self._current_start is None:
            return
        if simulated:
            self._time_done += self._current_est
        else:
            self._time_done += time.time() - self._current_start
        self._audio_done += max(audio_sec, 1.0)
        self._current_start = None

    def exhausted(self, est: float) -> bool:
        if self.limit <= 0:
            return False
        return self.remaining() < est

    def finish_all(self) -> None:
        self.finished_file(self._current_est / max(self.rtf(), 0.01))


def _probe_duration(path: Path) -> float | None:
    """Durata del file in secondi, letta dai metadati con ffprobe.

    Non si decodifica l'audio: su 18 file da un'ora la lettura dei
    metadati costa meno di un secondo, il decoding un'ora.
    """
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        return None
    try:
        out = subprocess.run(
            [ffprobe, "-v", "quiet", "-show_entries", "format=duration",
             "-of", "csv=p=0", str(path)],
            capture_output=True, text=True, timeout=60,
        ).stdout.strip()
        value = float(out)
        return value if value > 0 else None
    except (ValueError, OSError, subprocess.SubprocessError):
        return None


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
    p.add_argument(
        "--max-seconds", type=int, default=0,
        help="budget di tempo: la coda si ferma fra un file e l'altro "
             "quando lo esaurisce (0 = nessun limite)",
    )
    p.set_defaults(func=cmd_pull)

    g = sub.add_parser("purge", help="svuota l'archivio locale oltre i giorni indicati")
    g.add_argument("--days", type=int, default=0, help=f"default {ARCHIVE_DAYS}")
    g.add_argument("--dry-run", action="store_true")
    g.set_defaults(func=cmd_purge)

    doc = sub.add_parser(
        "doctor",
        help="controlla che l'ambiente sia pronto, voce per voce",
    )
    doc.set_defaults(func=cmd_doctor)

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
