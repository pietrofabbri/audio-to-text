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
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

logger = logging.getLogger("nightly")

# Finestra notturna. launchd avvia il job alle 03:00 e lo ferma poco
# dopo la mezzanotte: la macchina resta utilizzabile di giorno.
DEFAULT_WINDOW_SEC = 3 * 3600

# Tempo di elaborazione per secondo di audio, misurato su questo Mac
# (VAD + ASR + denoise + diarizzazione + prosodia). Unico riferimento
# per le stime: due costanti diverse per la stessa cosa farebbero
# divergere il piano dalla realta'.
MEASURED_RTF = 0.76


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
        "--no-publish", action="store_true",
        help="non pubblicare il corpus (utile per un test a secco)",
    )
    ap.add_argument(
        "--dry-run", action="store_true",
        help="mostra il piano senza elaborare né cancellare",
    )
    args = ap.parse_args()

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
            "Stima: %.1f ore di audio, ~%.1f ore di elaborazione "
            "(misurati ~3,3x realtime su ASR)",
            plan["est_hours"], plan["est_process_hours"],
        )
    if plan.get("fits"):
        logger.info(
            "Nella finestra entrano ~%d file; gli altri aspettano la notte dopo",
            plan["fits"],
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
    done = {d.name for d in (ROOT / "output").iterdir()
            if d.is_dir() and (d / "transcript.json").exists()} \
        if (ROOT / "output").is_dir() else set()

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

    est_hours = sum(_duration_h(f) or 0 for f in pending) / 3600
    # RTF complessivo misurato su questo Mac: 74s di elaborazione per
    # 97,8s di audio = 0,76. E' il numero che viene da una misura, non
    # da una stima, e la differenza rispetto alle supposizioni precedenti
    # (8-10x realtime) e' il fattore 3 di cui sopra.
    est_process_hours = est_hours * MEASURED_RTF
    budget_h = args.max_seconds / 3600

    return {
        "device": device,
        "pending": len(pending),
        "files": [f.name for f in pending],
        "est_hours": round(est_hours, 2),
        "est_process_hours": round(est_process_hours, 2),
        "fits": int(budget_h / (MEASURED_RTF * 1.15)) if est_hours else 0,
        "budget_hours": round(budget_h, 2),
    }


def _duration_h(path: Path) -> float | None:
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
    done = {d.name for d in (ROOT / "output").iterdir()
            if d.is_dir() and (d / "transcript.json").exists()} \
        if (ROOT / "output").is_dir() else set()
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
    path = ROOT / "logs" / "nightly_runs.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "started_at": started,
        "elapsed_sec": round(elapsed, 1),
        "device": plan.get("device"),
        "pending_at_start": plan.get("pending", 0),
        "est_hours": plan.get("est_hours"),
        "est_process_hours": plan.get("est_process_hours"),
        "left_on_device": leftover,
        "pull_exit": pull_code,
    }
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


if __name__ == "__main__":
    raise SystemExit(main())
