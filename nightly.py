#!/usr/bin/env python
"""
nightly.py — la notte, in un comando.

    import dal device → elaborazione entro un budget di tempo →
    pubblicazione del corpus → bilancio

È questo che il job launchd esegue. Esiste perché i tre passi hanno
vincoli diversi e falliscono in modi diversi: l'import cancella file,
l'elaborazione può andare oltre il tempo disponibile, la pubblicazione
non deve mai bloccare nessuno dei due. Metterli in sequenza in un
unico punto rende l'ordine esplicito e il fallimento parziale visibile.

Il budget di tempo è la cosa più importante qui. Le misure reali su
M1 Pro danno circa 3,3x realtime per l'ASR: 18 file da un'ora sono
~8 ore di elaborazione, e la finestra notturna è di 3. Il sistema
non può finire tutto in una notte, e non è un errore: è il punto in
cui la coda deve poter avanzare di un pezzo alla volta, in ordine,
senza perdere niente. I file non elaborati restano sul device e
ripartono dalla stessa identica condizione la notte dopo.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from core.config import (  # noqa: E402
    NIGHT_COOLDOWN_SEC,
    NIGHT_NICE,
    NIGHT_THREADS,
    NIGHT_WINDOW_SEC,
    ROOT_DIR,
)
from core.cost import estimate_seconds  # noqa: E402

# Due radici distinte, per due scopi distinti. ROOT e' dove stanno gli
# script: non si sposta, perché è lì che devono essere eseguiti. ROOT_DIR
# è dove finiscono output, log e bilanci, e segue A2T_ROOT_DIR: è ciò che
# permette di provare il ciclo notturno completo senza scrivere sopra la
# produzione.
DATA_ROOT = ROOT_DIR

logger = logging.getLogger("nightly")

# Finestra notturna. Il numero vive in core/config.py, letto sia da questo
# file sia da setup_launchd.py: prima era scritto in due posti con due
# valori diversi, e lanciando nightly.py a mano la finestra era piu'
# corta di quella del job launchd — la stessa notte, due piani diversi.
DEFAULT_WINDOW_SEC = NIGHT_WINDOW_SEC

# Anche l'intensita' e' centralizzata: thread, priorita' e pausa di
# respiro fra un file e il successo, cosi' il job launchd e la passata
# manuale fanno lo stesso lavoro.
DEFAULT_THREADS = NIGHT_THREADS
DEFAULT_NICE = NIGHT_NICE
DEFAULT_COOLDOWN_SEC = NIGHT_COOLDOWN_SEC

# Il modello di costo vive in core/cost.py: separa i costi fissi per
# file dai costi per secondo di parlato, ed e' tarato sulle registrazioni
# vere (prevede entro l'1% della misura). Qui la media per secondo di
# audio serve solo come riferimento veloce: un file da un'ora con il
# 65% di parlato costa circa 16 minuti, cioe' 0,27.
#
# La costante che c'era prima, 0,76, veniva da novanta secondi di audio
# di prova e sbagliava di un fattore due sul materiale vero.
MEASURED_RTF = 0.27

# Quanti secondi di cpu si lasciano accendere insieme. Non e' una
# concessione alla macchina: 4 thread sono risultati PIU' veloci di 8
# sulla M1 Pro (0,26 contro 0,32 sul parlato), perche' gli altri 4 core
# sono efficiency e insieme ai primi non fanno lavoro, fanno contesa e
# consumo. Vedere la tabella in core/cost.py.


def _run(cmd: list[str], timeout: int | None = None) -> tuple[int, str]:
    proc = subprocess.run(
        cmd, cwd=str(ROOT), capture_output=True, text=True,
        timeout=timeout,
    )
    return proc.returncode, (proc.stdout + proc.stderr).strip()


def main() -> int:
    ap = argparse.ArgumentParser(description="Ciclo notturno completo")
    ap.add_argument(
        "--max-seconds", type=int, default=DEFAULT_WINDOW_SEC,
        help=f"budget di elaborazione (default {DEFAULT_WINDOW_SEC//3600}h)",
    )
    ap.add_argument("--source", help="percorso del device (default: rilevamento)")
    ap.add_argument("--limit", type=int, help="massimo file da prendere")
    ap.add_argument(
        "--threads", type=int, default=DEFAULT_THREADS,
        help=f"thread CPU per l'elaborazione (default {DEFAULT_THREADS}: "
             "non tutto il processore, per non scaldare la macchina)",
    )
    ap.add_argument(
        "--nice", type=int, default=0,
        help="abbassa la priorita' del processo (0 = lascia stare launchd)",
    )
    ap.add_argument(
        "--cooldown-sec", type=float, default=0.0,
        help=f"pausa di respiro fra un file e il successivo in secondi "
             f"(0 = usa il default di {DEFAULT_COOLDOWN_SEC})",
    )
    ap.add_argument(
        "--no-publish", action="store_true",
        help="non pubblicare il corpus (utile per un test a secco)",
    )
    ap.add_argument(
        "--dry-run", action="store_true",
        help="mostra il piano senza elaborare né cancellare",
    )
    args = ap.parse_args()

    if args.threads and args.threads > 0:
        # Vale per faster-whisper (CTranslate2) e per i worker della
        # prosodia: limitarli qui evita che una passata si appropri dei
        # core tutti e lasci la macchina bollente per il resto della
        # giornata. Non e' una prestazione che si ottiene per free — si
        # paga in tempo di elaborazione, e il tempo si misura in code
        # che avanzano piu' lentamente.
        os.environ["OMP_NUM_THREADS"] = str(args.threads)
        os.environ["CT2_NUM_THREADS"] = str(args.threads)
        logger.info("Thread CPU limitati a %d", args.threads)

    # La priorita' la si abbassa anche quando si lancia a mano: il
    # termico non deve dipendere da chi ha lanciato il comando.
    nice_target = args.nice or DEFAULT_NICE
    if nice_target > 0:
        try:
            os.nice(nice_target)
            logger.info("Priorita' abbassata di %d (nice)", nice_target)
        except (OSError, PermissionError) as exc:
            # nice() su un processo non alleviato e' un errore, non un
            # motivo per non elaborare nulla.
            logger.debug("nice(%d) non applicato: %s", nice_target, exc)

    cooldown = args.cooldown_sec if args.cooldown_sec > 0 else DEFAULT_COOLDOWN_SEC

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[logging.StreamHandler(sys.stdout)],
        force=True,
    )

    started = time.time()
    started_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    logger.info("=" * 60)
    logger.info("Ciclo notturno — budget %s", _hms(args.max_seconds))
    logger.info("=" * 60)

    # ------------------------------------------------------------------
    # 1. Stato di partenza: cosa c'è da fare e cosa manca
    # ------------------------------------------------------------------
    plan = _plan(args)
    logger.info("Dispositivo: %s", plan.get("device") or "non trovato")
    logger.info("File in attesa: %d", plan.get("pending", 0))
    if plan.get("est_hours"):
        logger.info(
            "Stima: %.1f ore di audio grezza, ~%.1f ore di elaborazione",
            plan["est_hours"], plan["est_process_hours"],
        )
    if plan.get("speech_ratio") is not None:
        logger.info(
            "Parlato effettivo stimato: %.0f%% delle registrazioni già "
            "elaborate (l'ASR lavora sul parlato, non sui silenzi: è questa "
            "frazione che decide se la coda cresce)",
            100 * plan["speech_ratio"],
        )
    if plan.get("fits"):
        logger.info(
            "Nella finestra entrano ~%d file; gli altri aspettano la "
            "prossima passata", plan["fits"],
        )

    # ------------------------------------------------------------------
    # 2. Import ed elaborazione
    # ------------------------------------------------------------------
    pull = ["python", str(ROOT / "sync_device.py"), "pull",
            "--max-seconds", str(args.max_seconds)]
    if args.source:
        pull += ["--source", args.source]
    if args.limit:
        pull += ["--limit", str(args.limit)]
    if args.dry_run:
        pull.append("--dry-run")
    # La pausa di respiro la applica sync_device, che e' dove i file
    # vengono presi uno alla volta: qui non ha un file fra le mani.
    os.environ["A2T_COOLDOWN_SEC"] = str(cooldown)
    if cooldown > 0:
        pull += ["--cooldown-sec", str(cooldown)]

    logger.info("--- 1/2 import ed elaborazione ---")
    code_pull, out_pull = _run(pull)
    if code_pull != 0:
        # L'import può uscire non-zero per un file fallito, non per un
        # errore generale: si prosegue comunque a pubblicare quello che
        # è stato prodotto.
        logger.warning("sync_device pull ha restituito %d", code_pull)

    # ------------------------------------------------------------------
    # 3. Pubblicazione del corpus
    # ------------------------------------------------------------------
    logger.info("--- 2/2 pubblicazione del corpus ---")
    if args.no_publish:
        logger.info("Saltata (--no-publish)")
    else:
        code_pub, out_pub = _run(["python", str(ROOT / "publish_corpus.py"), "push"])
        if code_pub != 0:
            logger.warning("publish_corpus push ha restituito %d", code_pub)
        else:
            logger.info("Pubblicazione: %s", out_pub.splitlines()[-1] if out_pub else "ok")

    # ------------------------------------------------------------------
    # 4. Bilancio
    # ------------------------------------------------------------------
    elapsed = time.time() - started
    leftover = _leftover()
    logger.info("=" * 60)
    logger.info("Ciclo terminato in %s", _hms(elapsed))
    if leftover is None:
        logger.info("Nessun device rilevato: niente da fare")
    elif leftover:
        logger.info(
            "Restano %d file sul device: verranno presi la notte dopo, "
            "dal più vecchio", leftover,
        )
    else:
        logger.info("Device svuotato: niente rimane da elaborare")
    logger.info("=" * 60)

    _write_report(started_iso, elapsed, plan, leftover, code_pull)
    return 0


def _hms(seconds: float) -> str:
    m, s = divmod(int(seconds), 60)
    return f"{m // 60}h {m % 60:02d}m" if m >= 60 else f"{m}m {s:02d}s"


def _plan(args) -> dict:
    """Stima il lavoro della notte prima di iniziare a farlo."""
    sys.path.insert(0, str(ROOT))
    from core.device import AUDIO_EXTENSIONS, discover, pick_recorder, parse_recording_time

    files: list[Path] = []
    device = None
    if args.source:
        src = Path(args.source)
        device = str(src)
        if src.is_dir():
            files = [f for f in sorted(src.rglob("*"))
                     if f.is_file() and f.suffix.lower() in AUDIO_EXTENSIONS]
    else:
        vol = pick_recorder(discover())
        if vol:
            device = str(vol.path)
            files = vol.audio_files

    # I file già elaborati non si contano: il ciclo notturno è
    # incrementale, non ripete la coda da capo ogni sera.
    done = {d.name for d in (DATA_ROOT / "output").iterdir()
            if d.is_dir() and (d / "transcript.json").exists()} \
        if (DATA_ROOT / "output").is_dir() else set()

    pending = []
    for f in files:
        dt, _ = parse_recording_time(f.name)
        stem = dt.strftime("%Y-%m-%d_%H-%M-%S") if dt else None
        if stem and stem in done:
            continue
        pending.append(f)
    pending.sort(key=lambda f: f.name)   # ordine cronologico dal nome

    if args.limit:
        pending = pending[:args.limit]

    durations = [(_duration_sec(f) or 0.0) for f in pending]  # gia" in secondi
    est_hours = sum(durations) / 3600

    # Il costo non e' una costante per secondo di audio: i costi fissi
    # (caricamento modelli, campione denoise) si pagano una volta per
    # file, e l'ASR paga solo sul parlato. La frazione di parlato si
    # misura sulle sessioni gia' elaborate; senza dati si assume il
    # 100%, che e' la stima peggiore e quindi quella giusta quando non
    # si sa.
    #
    # Il modello in core/cost.py e' tarato sulle registrazioni vere e
    # prevede il tempo di elaborazione entro l'1% della misura.
    ratio = _measured_speech_ratio()
    est_process_sec = sum(
        estimate_seconds(a, a * ratio if ratio else None) for a in durations
    )
    est_process_hours = est_process_sec / 3600
    budget_h = args.max_seconds / 3600
    per_file_h = estimate_seconds(3600, 3600 * ratio if ratio else None) / 3600

    return {
        "device": device,
        "pending": len(pending),
        "files": [f.name for f in pending],
        "est_hours": round(est_hours, 2),
        "est_process_hours": round(est_process_hours, 2),
        "speech_ratio": round(ratio, 3) if ratio else None,
        "fits": int(budget_h / (per_file_h * 1.15)) if est_hours and per_file_h else 0,
        "per_file_hours": round(per_file_h, 3),
        "budget_hours": round(budget_h, 2),
    }


def _measured_speech_ratio() -> float | None:
    """Frazione di parlato media delle sessioni gia' elaborate.

    Nessun dato -> None, e la stima resta quella conservativa sul
    campione peggiore invece di indovinare una frazione ottimistica.
    """
    out_dir = DATA_ROOT / "output"
    if not out_dir.is_dir():
        return None
    ratios = []
    for d in out_dir.iterdir():
        p = d / "transcript.json"
        if not p.is_dir() and p.exists():
            try:
                m = json.loads(p.read_text(encoding="utf-8")).get("meta", {})
            except (json.JSONDecodeError, OSError):
                continue
            r = m.get("speech_ratio")
            if isinstance(r, (int, float)) and 0 < r <= 1:
                ratios.append(float(r))
    return (sum(ratios) / len(ratios)) if ratios else None


def _duration_sec(path: Path) -> float | None:
    """Durata in secondi via ffprobe: legge i metadati senza decodificare."""
    import shutil
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        return None
    try:
        out = subprocess.run(
            [ffprobe, "-v", "quiet", "-show_entries", "format=duration",
             "-of", "csv=p=0", str(path)],
            capture_output=True, text=True, timeout=60,
        ).stdout.strip()
        return float(out)
    except (ValueError, OSError, subprocess.SubprocessError):
        return None


def _leftover() -> int | None:
    """Quanti file audio ci sono ancora sul device, esclusi quelli già
    elaborati. È il numero che dice se la coda avanza o è ferma.
    None significa che nessun device è collegato, che è una situazione
    diversa da "device svuotato"."""
    from core.device import AUDIO_EXTENSIONS, discover, pick_recorder, parse_recording_time
    vol = pick_recorder(discover())
    if not vol:
        return None
    done = {d.name for d in (DATA_ROOT / "output").iterdir()
            if d.is_dir() and (d / "transcript.json").exists()} \
        if (DATA_ROOT / "output").is_dir() else set()
    n = 0
    for f in vol.audio_files:
        dt, _ = parse_recording_time(f.name)
        stem = dt.strftime("%Y-%m-%d_%H-%M-%S") if dt else None
        if stem and stem in done:
            continue
        n += 1
    return n


def _write_report(started: str, elapsed: float, plan: dict,
                  leftover: int | None, pull_code: int) -> None:
    """Bilancio su disco: una riga per notte, confrontabile nel tempo.
    È il primo segnale di se la coda tiene il passo con la produzione."""
    path = DATA_ROOT / "logs" / "nightly_runs.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "started_at": started,
        "elapsed_sec": round(elapsed, 1),
        "device": plan.get("device"),
        "pending_at_start": plan.get("pending", 0),
        "est_hours": plan.get("est_hours"),
        "est_process_hours": plan.get("est_process_hours"),
        "speech_ratio": plan.get("speech_ratio"),
        "left_on_device": leftover,
        "pull_exit": pull_code,
    }
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


if __name__ == "__main__":
    raise SystemExit(main())
