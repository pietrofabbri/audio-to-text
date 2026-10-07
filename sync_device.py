#!/usr/bin/env python
"""
sync_device.py — porta le registrazioni dal device alla pipeline.

Uso tipico (il device è collegato da almeno 3 ore):

    python sync_device.py detect            # che cosa è montato? (non tocca nulla)
    python sync_device.py pull --dry-run    # cosa verrebbe fatto?
    python sync_device.py pull              # importa, processa, archivia
    python sync_device.py purge             # svuota l'archivio oltre i 7 giorni
    python sync_device.py scarica           # copia in coda, libera ed espelle il registratore

Dal 7 ottobre il percorso normale e' `scarica`, lanciato da solo da
launchd a ogni inserimento (setup_launchd.py): copia verificata in
input/coda/, cancellazione dal registratore, espulsione, notifica «puoi
staccarlo». Poi `pull --source input/coda` (lo fanno la notte e le
passate diurne) trascrive dalla coda. Leggere `pull` direttamente dal
registratore resta possibile, ma lo tiene collegato per ore: e' il motivo
per cui il 5 ottobre una sessione ha perso l'audio quando e' stato
staccato a meta'.

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
from core.cost import estimate_seconds  # noqa: E402
from core.thermal import ThermalGovernor  # noqa: E402
from core.media import probe_duration as _probe_duration  # noqa: E402

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
          "libera spazio: le cache WAV sono in data/wav_cache/")

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


# Dove finisce il pezzo recuperabile di un file che non si lascia leggere
# per intero. Dentro logs/, che è già ignorato: sono lavori temporanei, non
# un archivio (l'archivio è un altro, e vede solo i file sani).
LAVORO_DIR = LOGS_DIR / "lavoro"

# Sotto questa quantità di byte recuperati non si fa nemmeno il tentativo.
# Non è una soglia di qualita' dell'audio: e' la soglia sotto la quale il
# file non e' "una registrazione troncata" ma un file rotto, e su quello
# conviene che una persona guardi invece che la pipeline lo dichiari
# elaborato e cancelli il device.
SALVATAGGIO_MIN_BYTES = 1 << 19

# Sotto questa dimensione di lettura il salvataggio si arrende:shrinkare
# un blocco non recupera niente se il blocco e' gia' piu' piccolo del
# confine, e continuare a provare costerebbe una lettura fallita per
# ogni riga che manca.
SALVATAGGIO_CHUNK_MINIMO = 1 << 16


def _salva_prefisso(source: Path, dest: Path, chunk: int = 1 << 20) -> tuple[int, str]:
    """Copia da `source` finche' il filesystem risponde, e si ferma al primo errore.

    Restituisce (byte copiati, motivo). Serve per i file che il registratore
    lascia a meta': la voce in directory promette un'ora di audio, ma gli
    offset oltre la dimensione allocata non si possono leggere e ogni
    lettura li chiede. Non e' un errore transitorio e rileggere con lo
    stesso blocco non serve: il filesystem ha gia' detto di no.

    Però il confine non e' netto, ed e' per questo che si riprova con
    blocchi piu' piccoli. Il driver rifiuta la lettura che *varca* il
    confine, non quella che arriva esattamente al confine: leggendo a
    1 MiB ci si ferma all'ultimo MiB intero e si buttano via i byte fra
    l'ultimo blocco e la fine reale. Su un file vero misurato sono stati
    3.145.728 byte con blocchi da 1 MiB e 3.538.944 con blocchi da 4 KiB:
    quaranta secondi di registrazione, persi non perche' non ci fossero ma
    perche' non si chiedeva nel modo giusto. Quindi si dimezza e si
    riprova, fino a un minimo sotto il quale il gioco non vale piu'.

    Il motivo torna al chiamante perche' la risposta conta: "ho salvato
    3 MiB" e "non ho salvato niente" portano a due decisioni opposte.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    scritti = 0
    motivo = "file interamente leggibile"
    try:
        with source.open("rb") as src, dest.open("wb") as out:
            while True:
                try:
                    blocco = src.read(chunk)
                except OSError as exc:
                    if chunk <= SALVATAGGIO_CHUNK_MINIMO:
                        motivo = (f"si ferma a {scritti} byte "
                                  f"(blocchi da {chunk}): {exc.strerror or exc}")
                        break
                    chunk = max(SALVATAGGIO_CHUNK_MINIMO, chunk // 4)
                    continue
                if not blocco:
                    break
                out.write(blocco)
                scritti += len(blocco)
    except OSError as exc:
        motivo = f"copia interrotta a {scritti} byte: {exc.strerror or exc}"
    return scritti, motivo


def _lavoro_path(nome: str) -> Path:
    """Il posto dove finisce il pezzo salvato.

    Il nome del file si mantiene **uguale a quello del device**, e la
    separazione la fa la cartella. Il nome non si puo' cambiaare: la
    pipeline scrive `meta.stem` in transcript.json a partire dal nome del
    file che le si passa, e da li il corpus prende il nome della sessione.
    Rinominare la copia di lavoro produceva due nomi per la stessa
    registrazione — cartella `2026-10-04_10-49-40`, corpus
    `2026-10-04_10-49-40-1508d6ee` — e i due non tornavano piu' insieme.
    """
    return LAVORO_DIR / nome


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

    logger.info("Device: %s (%s)", label, src)

    # Ordine cronologico: la coda si svuota dal piu vecchio, cosi il
    # ritardo non si accumula sempre sugli stessi file. I file senza
    # orario leggibile vanno in coda, non davanti.
    files.sort(key=lambda f: (parse_recording_time(f.name)[0] or datetime.max, f.name))

    # Il limite si applica DOPO l'ordinamento. Applicato prima, prende i
    # primi N per nome: "00000001_000000.MP3" verrebbe prima di
    # "REC_20261003_210000.mp3", e --limit 1 (la prova che fa l'utente
    # per fidarsi del sistema) processerebbe spazzatura invece della
    # registrazione piu' vecchia.
    if args.limit:
        files = files[:args.limit]

    logger.info("File trovati: %d", len(files))

    # --- 2. cosa è già stato fatto -----------------------------------
    done_hashes = already_processed_hashes()

    # --- 3. elaborazione ---------------------------------------------
    results = {"ok": 0, "failed": 0, "skipped": 0, "deleted": 0, "kept": 0, "junk": 0}
    budget = _Budget(args.max_seconds, simulate=args.dry_run)

    # La pausa di respiro e' un compromesso, non un trucco. Non la si
    # mette perche' "rallenta": la si mette perche' diciotto file da
    # un'ora in quattro ore tengono il processore acceso al massimo per
    # quattro ore, e quello che ne risente e' la macchina quando la si
    # usa il giorno dopo. Il costo e' esplicito: si allunga la notte
    # necessaria a svuotare la coda, e va detto adesso, non scoperto
    # fra un mese. Chi preferisce la coda rapida passi --cooldown-sec 0.
    cooldown = max(0.0, float(getattr(args, "cooldown_sec", 0.0) or 0.0))
    if cooldown:
        logger.info("Pausa di respiro fra un file e il successo: almeno %.0fs", cooldown)

    # ...e non solo quella: la pausa vera e' proporzionale a quanto si e'
    # appena lavorato, e si allunga da sola se il chip rallenta. Il
    # minimo qui sopra resta il pavimento, quindi --cooldown-sec continua
    # a significare la stessa cosa di prima (core/thermal.py).
    from core.config import thermal_policy
    gov = ThermalGovernor(thermal_policy(cooldown))

    for i, f in enumerate(files, 1):
        # La pausa va PRIMA del controllo di budget: il tempo di respiro
        # è tempo passato, e se non lo si conta il budget si consuma
        # mentre la macchina è ferma a guardare.
        pause = max(cooldown, gov.last_cooldown_sec)
        if pause > 0 and i > 1:
            gov.rest(sleeper=budget.sleep)

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
        nome_originale = f.name
        # L'originale di cui f è, per ora, una copia. Diventa qualcosa
        # solo quando il file si rivela troncato: allora f passa a essere
        # il pezzo recuperato e questo conserva il nome vero, quello che
        # va nel manifest e in session.json.
        originale: Path | None = None
        try:
            digest = file_sha256(f)
        except OSError as exc:
            # Non è un errore della pipeline: è il registratore rimasto
            # senza corrente mentre scriveva. La voce in directory
            # promette un'ora di audio, gli offset oltre la dimensione
            # allocata non si possono leggere. Scartare il file significa
            # lasciarlo sul device per sempre —la coda non si svuota e
            # l'unica parte recuperabile muore con la prossima
            # formattazione della card—, quindi si prova a prendere il
            # pezzo che c'è. Quello che si prende sono quasi sempre i
            # primi minuti, che sono quelli con il contesto.
            if args.dry_run:
                logger.warning("[%d/%d] %s: illeggibile (%s) — in dry-run "
                               "salverei il pezzo leggibile",
                               i, len(files), f.name, exc)
                results["skipped"] += 1
                continue
            pezzo = _lavoro_path(nome_originale)
            scritti, motivo = _salva_prefisso(f, pezzo)
            if scritti < SALVATAGGIO_MIN_BYTES or _is_not_audio(pezzo):
                logger.warning("[%d/%d] %s: illeggibile (%s) e senza audio "
                               "recuperabile, resta sul device",
                               i, len(files), f.name, exc)
                results["skipped"] += 1
                pezzo.unlink(missing_ok=True)
                append_manifest({
                    "ts": _now_iso(), "device": label, "file": nome_originale,
                    "sha256": None, "stem": None, "action": "kept",
                    "reason": f"illeggibile, nulla recuperabile ({motivo})",
                })
                continue
            logger.warning("[%d/%d] %s: illeggibile (%s); %s",
                           i, len(files), f.name, exc, motivo)
            try:
                digest = file_sha256(pezzo)
            except OSError as exc2:  # pragma: no cover - difensivo
                # Un file locale che non si lascia leggere è un problema
                # diverso e non si risolve da solo: si lascia il file sul
                # device invece di far morire l'intera coda.
                logger.error("[%d/%d] %s: anche il pezzo salvato è illeggibile (%s)",
                             i, len(files), nome_originale, exc2)
                results["failed"] += 1
                continue
            originale, f = f, pezzo

        if digest in done_hashes:
            logger.info("[%d/%d] %s: già elaborato, cerco di liberare spazio",
                        i, len(files), nome_originale)
            _try_delete(f, digest, "già elaborato in una run precedente", args)
            continue

        # Spazzatura del registratore: si nota e si lascia stare. Non si
        # cancella, non si processa, non si conta come fallimento.
        if _is_not_audio(f):
            logger.info("[%d/%d] %s: non è audio (spazzatura), lascio sul device",
                        i, len(files), nome_originale)
            results["junk"] += 1
            append_manifest({
                "ts": _now_iso(), "device": label, "file": nome_originale,
                "sha256": digest, "stem": None, "action": "junk",
                "reason": "ffprobe: nessun audio decodificabile",
            })
            continue

        stem = _stem_for(f, recorded)
        # Due file diversi non devono mai condividere la cartella di
        # output. Lo stem viene dall'orario nel nome, e l'orario non
        # distingue due registrazioni fatte nella stessa notte con lo
        # stesso minuto — o, peggio, due notti diverse se l'orologio del
        # registratore non è mai stato impostato.
        #
        # Saltare il secondo file sarebbe silenziosamente irreversibile:
        # resterebbe sul device per sempre, ogni notte, senza mai essere
        # trascritto e senza mai poter essere cancellato. Meglio una
        # cartella in più che una coda bloccata.
        #
        # Il suffisso viene dal NOME, non dal contenuto: due copie
        # identiche hanno la stessa impronta, e con quella finivano nella
        # stessa cartella — la seconda si trovava il checkpoint gia'
        # completo, la pipeline lo dichiarava non completata e la run
        # finiva con errore.
        if (OUTPUT_DIR / stem / REQUIRED_OUTPUT).exists():
            suffix = hashlib.sha1(nome_originale.encode("utf-8")).hexdigest()[:6]
            stem = f"{stem}-{suffix}"
            logger.warning(
                "[%d/%d] %s: l'orario %s è gia' stato elaborato da un altro "
                "file; lo scrivo in %s per non confonderli",
                i, len(files), nome_originale,
                recorded.isoformat() if recorded else "ignoto", stem,
            )
            # Se anche questa cartella esiste, è lo stesso file di una
            # run precedente: non è una terza registrazione.
            if (OUTPUT_DIR / stem / REQUIRED_OUTPUT).exists():
                logger.info("[%d/%d] %s: identico a una sessione gia' scritta",
                            i, len(files), nome_originale)
                results["skipped"] += 1
                done_hashes.add(digest)
                _try_delete(f, digest, "gia' elaborato come questa sessione", args)
                continue

        # La cartella definitiva: da qui in avanti tutto — verifica,
        # cancellazione, archivio, database — punta qui.
        out_dir = OUTPUT_DIR / stem

        logger.info("[%d/%d] %s (%.1f MB) — orario %s [%s]",
                    i, len(files), nome_originale, f.stat().st_size / 1e6,
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
            ok = process_file(f, config, ns, stem=stem)
        except Exception as exc:  # noqa: BLE001
            logger.error("  elaborazione fallita: %s: %s", type(exc).__name__, exc)
            ok = False
        elapsed = time.time() - t0
        # Il chip appena ha finito un blocco di lavoro: e' l'unico momento
        # in cui la pausa puo' essere calcolata su quello che ha davvero
        # costato, e su quello che costa rispetto ai file precedenti.
        gov.note_work(elapsed, audio_sec=_probe_duration(f) or 0.0)

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
        _stamp_session(stem, recorded, label, nome_originale)

        with CorpusDB() as cdb:
            cdb.ingest_session(
                stem=stem,
                transcript=json.loads((out_dir / REQUIRED_OUTPUT).read_text(encoding="utf-8")),
                vad_stats={},
                recorded_at=recorded.isoformat() if recorded else None,
                source_device=label,
                source_filename=nome_originale,
            )

        # --- 6. archivio, poi cancellazione --------------------------
        # La durata va letta **prima** che il file sparisca. Dopo la
        # cancellazione ffprobe non trova piu' niente e restituisce None,
        # e `finished_file(None or 0.0)` finisce con `max(0.0, 1.0)`: il
        # budget riceve **un secondo** di audio dove ne aveva 3.600. Da
        # lì in poi l'RTF che ha imparato è 482 invece di 0,13, la stima
        # del file successivo dice venti giorni, e con la finestra
        # notturna la coda si ferma dopo il primo file e gli altri restano
        # sul registratore per sempre.
        #
        # Il bug era invisibile finche' nessuno lanciava `pull` con un
        # budget: senza `--max-seconds` la coda non si ferma e sembra
        # andare tutto bene. E' la differenza fra una prova e la notte.
        durata_reale = _probe_duration(f) or 0.0

        archived = _archive(f, stem)
        if archived is not None:
            # Il pezzo salvato è una copia di lavoro: se l'archivio l'ha
            # preso, non serve piu' da nessuna parte. Se l'archivio non c'e'
            # (A2T_KEEP_LOCAL=0) il pezzo salvato e' l'unica copia dei byte
            # recuperabili, e va tenuto.
            f.unlink(missing_ok=True)
        # cancellare un file gia' cancellato non e' un pericolo, e' solo
        # rumore: un avviso di impossibilita' stampato accanto a un
        # cancellamento riuscito e' il modo piu' rapido per non far
        # notare il prossimo avviso che conta davvero.
        deleted = False
        if f.exists():
            deleted = _try_delete(f, digest, f"elaborato: {why}", args)
        if originale is not None:
            # L'originale troncato sparisce solo adesso: dopo il
            # salvataggio, l'elaborazione e la verifica, sul device non
            # resta nulla che non sia gia' in archivio. TENERLO non
            # proteggerebbe nulla — non si puo' piu' elaborarlo perche'
            # manca proprio il pezzo che manca— e occuperebbe la card per
            # sempre, davanti a ogni pull futuro.
            deleted = _try_delete(originale, digest,
                                  f"recuperato il pezzo leggibile: {why}",
                                  args) or deleted
        # Il file appena elaborato entra nell'indice dei hash gia' fatti:
        # senza questo, una copia identica presente piu' avanti nella
        # stessa coda verrebbe trascritta una seconda volta.
        done_hashes.add(digest)

        results["ok"] += 1
        results["deleted" if deleted else "kept"] += 1
        budget.finished_file(durata_reale)
        append_manifest({
            "ts": _now_iso(), "device": label, "file": nome_originale,
            "sha256": digest, "stem": stem,
            "action": "deleted" if deleted else "kept",
            "archived": str(archived) if archived else None,
            "elapsed_sec": round(elapsed, 1), "verified": why,
            "troncato": nome_originale if originale is not None else None,
        })
        logger.info("  fatto in %.0fs — %s", elapsed, why)

    logger.info(
        "Riepilogo: %d completati, %d falliti, %d saltati, %d spazzatura | "
        "cancellati %d, rimasti sul device %d",
        results["ok"], results["failed"], results["skipped"], results["junk"],
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
    # session_start_wall sta al primo livello di session.json, non sotto
    # "meta": l'assembler lo scrive li' e non esiste un dizionario "meta".
    # Scriverlo nel posto sbagliato non era un dettaglio: sollevava un
    # KeyError che faceva fallire l'intero pull DOPO che la trascrizione
    # era finita, quindi il file restava sul device per sempre.
    if recorded:
        doc["session_start_wall"] = recorded.isoformat()
    try:
        p.write_text(json.dumps(doc, ensure_ascii=False, indent=2),
                     encoding="utf-8")
    except (OSError, TypeError) as exc:
        # L'orario è un arricchimento, non la trascrizione. Se non si
        # scrive, la notte continua: perderla sarebbe un danno, ma
        # fermare il pull — e quindi non cancellare più nulla dal
        # registratore — sarebbe un danno maggiore.
        logger.warning("Annotazione di sessione non scrivibile per %s: %s",
                       stem, exc)


class _Budget:
    """
    Budget di tempo della notte, con stima del file successivo.

    La stima non viene da una tabella: si impara dai file già fatti in
    questa stessa run. Dopo il primo file da un'ora si sa quanto costa
    un file da un'ora, e la decisione di iniziare o no smette di essere
    un'ipotesi. Prima del primo file si usa un RTF medio, che è una
    stima dichiarata come tale.
    """

    # RTF complessivo della pipeline (elaborazione / audio), misurato su
    # questo Mac il 4 ottobre su sei file da un'ora interi, presi da un
    # registratore USB vero:
    #
    #   parole   tempo    RTF
    #    1.434   482 s   0,134
    #    2.628   716 s   0,199
    #    3.649   762 s   0,212
    #    4.376   845 s   0,238
    #    5.590  1.004 s  0,279
    #    6.492  1.035 s  0,287
    #
    # totale 4.844 s per 21.145 s di audio: **0,229**.
    #
    # Il numero non e' il 0,76 che c'era scritto qui prima: quello veniva
    # da un estratto di 97,8 secondi, dove la parte fissa (caricamento
    # modello, VAD, diarizzazione) pesa su un'ora di file che non c'e'.
    #
    # C'e' un'altra cosa che questi numeri dicono e che la stima non puo'
    # sapere: **l'RTF segue il parlato, non la durata**. Fra il file con
    # 1.434 parole e quello con 6.492 la durata e' la stessa e il tempo
    # raddoppia. Un modello che desse lo stesso costo a due file da un
    # ora sbaglierebbe sempre, e sbaglierebbe di piu' proprio sul file
    # peggiore. Non e' correggibile qui — il numero di parole si conosce
    # solo dopo aver trascritto — ma e' il motivo per cui la stima si
    # impara dai file della run invece di fidarsi di una tabella.
    MEASURED_RTF = 0.27

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

    @property
    def speech_ratio(self) -> float | None:
        """Frazione di parlato delle sessioni già elaborate, se nota."""
        return _measured_speech_ratio()

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
        """Secondi stimati per il file.

        Prima di avere misure reali si stima con il modello di costo, che
        conosce sia i costi fissi sia la frazione di parlato: un file da
        un'ora non costa come dieci file da sei minuti, e una stima a
        costo costante sbaglia di un fattore due sul primo file della
        notte.

        Dopo il primo file la stima si impara dai dati: gli RTF
        osservati sostituiscono il modello.
        """
        dur = _probe_duration(f) or 3600.0
        if self._time_done > 0 and self._audio_done > 0:
            return dur * self.rtf()
        ratio = self.speech_ratio
        return estimate_seconds(dur, dur * ratio if ratio else None) * self.SAFETY

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

    def sleep(self, seconds: float) -> None:
        """Tempo di respiro fra due file.

        In simulazione non si dorme davvero ma il tempo si conta lo
        stesso: altrimenti un --dry-run mostrerebbe un piano che
        non esiste, e il piano è la cosa che l'utente legge per
        decidere se fidarsi.
        """
        if seconds <= 0:
            return
        if self.simulate:
            self._time_done += seconds
        else:
            time.sleep(seconds)

    def finish_all(self) -> None:
        self.finished_file(self._current_est / max(self.rtf(), 0.01))


def _measured_speech_ratio() -> float | None:
    """Frazione di parlato media delle sessioni gia' elaborate.

    Nessun dato -> None, e la stima resta sul 100% di parlato, che e'
    la caso peggiore: meglio iniziare un file in meno che iniziarne uno
    che non finisce dentro la finestra.
    """
    out_dir = OUTPUT_DIR / ""
    if not out_dir.is_dir():
        return None
    ratios = []
    for d in out_dir.iterdir():
        p = d / "transcript.json"
        if not d.is_dir() or not p.exists():
            continue
        try:
            m = json.loads(p.read_text(encoding="utf-8")).get("meta", {})
        except (json.JSONDecodeError, OSError):
            continue
        r = m.get("speech_ratio")
        if isinstance(r, (int, float)) and 0 < r <= 1:
            ratios.append(float(r))
    return (sum(ratios) / len(ratios)) if ratios else None


def _is_not_audio(f: Path) -> bool:
    """True solo se ffprobe afferma che nel file non c'e' audio decodificabile.

    Un registratore economico lascia spazzatura: `00000001_000000.MP3` con
    l'orologio mai inizializzato, `untitled.mp3`, file da 0 byte. Questi
    hanno estensione e dimensione sufficienti per superare i filtri, ma
    non sono registrazioni.

    Senza questo controllo ogni notte si brucia un tentativo di pipeline
    su un file che fallira' identico, per sempre: il file non puo' essere
    elaborato, quindi non viene cancellato, quindi la coda non si svuota
    e il tempo speso è perso. Peggio: se arriva per primo in ordine di
    nome, blocca la coda.

    Il dubbio resta dubbio: se ffprobe non sa rispondere, la risposta è
    False e decide la pipeline, che ha piu' informazioni. Solo quando
    ffprobe e' sicuro che non c'e' audio la risposta e' True. Non si
    cancella mai nulla qui: lo scarto e' solo una decisione di non
    elaborare, il file resta dove e'.
    """
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        return False
    try:
        out = subprocess.run(
            [ffprobe, "-v", "error", "-show_entries",
             "stream=codec_type:format=duration", "-of", "json", str(f)],
            capture_output=True, text=True, timeout=60,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    if out.returncode != 0:
        # ffprobe non riuscito può voler dire due cose very diverse:
        # il file non è media ("Invalid data found"), oppure non sono
        # riuscito a leggerlo per un motivo esterno (file aperto, permessi).
        # Solo il primo è una sentenza sul contenuto; nel secondo caso
        # lascia decidere la pipeline, che ha piu' strumenti.
        err = (out.stderr or "").lower()
        if "invalid data" in err or "could not find codec" in err:
            return True
        logger.debug("ffprobe non ha analizzato %s: %s", f.name, out.stderr.strip()[:200])
        return False
    try:
        doc = json.loads(out.stdout or "{}")
    except json.JSONDecodeError:
        return False

    streams = doc.get("streams") or []
    # Nessuno stream, o nessuno di tipo audio: non c'e' niente da
    # trascrivere, e affermarlo e' un fatto, non una supposizione.
    if not streams or not any(s.get("codec_type") == "audio" for s in streams):
        return True
    try:
        dur = float((doc.get("format") or {}).get("duration") or 0.0)
    except (TypeError, ValueError):
        return False
    return dur <= 0.0


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
# scarica — il registratore serve solo per il tempo della copia
# ---------------------------------------------------------------------------

def _avvia_elaborazione() -> str:
    """Fa partire subito una passata diurna, senza aspettare l'orario.

    La passata diurna e' lo stesso ciclo della notte con un budget breve,
    pochi thread e priorita' bassa (core/config.py, DAYTIME_*): elabora la
    coda senza rubare la macchina. Si avvia con `launchctl kickstart` sul
    job gia' installato, cosi' gira con le stesse impostazioni di quando
    parte da solo. Se il job non c'e' (setup_launchd non eseguito), la
    coda aspetta la notte.
    """
    try:
        from setup_launchd import LABEL_DAYTIME
    except Exception as exc:  # noqa: BLE001
        return f"passata diurna non avviata ({exc})"
    launchctl = shutil.which("launchctl")
    if not launchctl:
        return "launchctl non disponibile: la coda aspetta la notte"
    r = subprocess.run([launchctl, "kickstart", f"gui/{os.getuid()}/{LABEL_DAYTIME}"],
                       capture_output=True, text=True)
    if r.returncode != 0:
        return (f"passata diurna non avviata ({(r.stderr or r.stdout).strip()}): "
                "la coda aspetta la prossima passata")
    return "elaborazione avviata"


def cmd_scarica(args) -> int:
    """Copia il registratore in coda, lo libera, lo espelle, avvisa.

    Con `--auto` (cosi' lo lancia launchd a ogni volume montato) un volume
    che non e' il registratore fa uscire in silenzio: il job parte anche
    per una chiavetta o un'immagine disco, e non deve far rumore.
    """
    from core.scarico import CODA_DIR, Lucchetto, descrivi, espelli, notifica, scarica

    if args.auto:
        # Lo scarico automatico gira senza terminale: il log va su file.
        LOGS_DIR.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(LOGS_DIR / "scarico.log", encoding="utf-8")
        fh.setFormatter(logging.Formatter(
            "%(asctime)s [%(levelname)s] %(name)s: %(message)s", "%Y-%m-%d %H:%M:%S"))
        logging.getLogger().addHandler(fh)

    lucchetto = Lucchetto()
    if not lucchetto.prendi():
        logger.info("Uno scarico e' gia' in corso: esco")
        return 0
    try:
        if args.source:
            src = Path(args.source)
            if not src.is_dir():
                print(f"Non esiste: {src}", file=sys.stderr)
                return 1
            from core.device import MIN_AUDIO_BYTES
            files = [f for f in src.rglob("*") if f.is_file()
                     and f.suffix.lower() in AUDIO_EXTENSIONS
                     and not f.name.startswith(".")
                     and f.stat().st_size >= MIN_AUDIO_BYTES]
            volume, label = None, str(src)
        else:
            # Il volume puo' comparire un attimo prima che il suo
            # contenuto sia leggibile: qualche tentativo prima di dire
            # che non c'e'.
            chosen = None
            for _ in range(3 if args.auto else 1):
                chosen = pick_recorder(discover(args.mounts))
                if chosen:
                    break
                time.sleep(2)
            if not chosen:
                if args.auto:
                    return 0
                print("Nessun registratore collegato.", file=sys.stderr)
                return 1
            files, volume, label = list(chosen.audio_files), chosen.path, str(chosen.path)

        if volume is not None and not args.dry_run:
            from core.scarico import attendi_volume_fermo
            files = attendi_volume_fermo(
                lambda: list((pick_recorder(discover(args.mounts)) or chosen).audio_files))
        files.sort(key=lambda f: (parse_recording_time(f.name)[0] or datetime.max, f.name))
        logger.info("Registratore %s: %d file da copiare in %s", label, len(files), CODA_DIR)
        if not files:
            if volume is not None and not args.no_eject and not args.dry_run:
                espelli(volume)
            notifica("TileRec: niente di nuovo", "Nessuna registrazione da copiare.",
                     suono=None)
            return 0
        if not args.dry_run:
            notifica("TileRec collegato",
                     f"Copio {len(files)} file: non staccarlo finché non te lo dico.",
                     suono=None)

        esito = scarica(
            files, coda=CODA_DIR, manifest=MANIFEST_PATH, etichetta=label,
            cancella=not args.no_delete, dry_run=args.dry_run,
            gia_elaborati=already_processed_hashes(),
        )
        logger.info(
            "Scarico: %d copiati (%.1f h di audio, %.0f MB) in %.0f s, %d gia' presenti, "
            "%d cancellati dal registratore, %d lasciati, %d troncati, %d spazzatura, "
            "%d errori%s",
            esito.copiati, esito.secondi_audio / 3600, esito.byte / 1e6, esito.durata,
            esito.gia_presenti, esito.cancellati, len(esito.lasciati),
            len(esito.troncati), len(esito.spazzatura), len(esito.errori),
            " — INTERROTTO (registratore staccato?)" if esito.interrotto else "",
        )
        for e in esito.errori:
            logger.warning("  %s", e)
        if args.dry_run:
            return 0

        titolo, testo = descrivi(esito)
        if volume is not None and not args.no_eject and not esito.interrotto:
            ok, msg = espelli(volume)
            if ok:
                logger.info("Registratore espulso")
            else:
                logger.warning("Espulsione non riuscita: %s", msg)
                titolo = "Copia finita: espelli il TileRec dal Finder"
        notifica(titolo, testo, suono="Glass" if esito.tutto_a_posto else "Basso")

        if esito.copiati and not args.no_elabora:
            logger.info("Coda: %s", _avvia_elaborazione())
        return 0 if not esito.errori else 1
    finally:
        lucchetto.lascia()


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
    p.add_argument(
        "--cooldown-sec", type=float, default=0.0,
        help="pausa di respiro fra un file e il successivo, in secondi. "
             "Costa tempo di elaborazione e toglie calore: con una "
             "macchina che si scalda, la coda avanza un po' meno ma la "
             "macchina resta usabile di giorno. 0 = disattivata",
    )
    p.set_defaults(func=cmd_pull)

    sc = sub.add_parser(
        "scarica",
        help="copia il registratore in coda, verifica, lo libera e lo espelle "
             "(la trascrizione avviene dopo, dalla coda)",
    )
    sc.add_argument("--mounts", default="/Volumes")
    sc.add_argument("--source", help="cartella esplicita invece del registratore")
    sc.add_argument("--auto", action="store_true",
                    help="modalita' launchd: esce in silenzio se non c'e' il "
                         "registratore, log in logs/scarico.log")
    sc.add_argument("--no-delete", action="store_true",
                    help="copia senza cancellare dal registratore")
    sc.add_argument("--no-eject", action="store_true", help="non espellere alla fine")
    sc.add_argument("--no-elabora", action="store_true",
                    help="non avviare subito la passata diurna sulla coda")
    sc.add_argument("--dry-run", action="store_true")
    sc.set_defaults(func=cmd_scarica)

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
