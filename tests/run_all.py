"""
Tutti i test, in un comando solo.

    python tests/run_all.py              # veloce: senza modelli, ~1 minuto
    python tests/run_all.py --full       # anche la catena vera, ~10 minuti
    python tests/run_all.py --full --minutes 2

Il default esclude il test end-to-end perche' quello carica davvero
Whisper e pyannote e ci mette minuti. Ma il default include tutto il
resto, e cio' tutto quello che si puo' rompere senza accorgersene.

Perche' due livelli: i test veloci trovano i bug logici (nomi, budget,
collisioni, cosa viene cancellato e cosa no) e sono quelli che girano
spesso. Quello end-to-end trova i bug che nascono solo quando i pezzi
sono collegati — ed e' esattamente la categoria di bug che piu' fa
danno, perche' un pezzo da solo funziona e insieme no.

Exit code 0 solo se tutto passa: e' quello che il job notturno puo'
guardare per sapere se il sistema e' sano.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent

# (comando, etichetta, serve il modello)
FAST = [
    ([sys.executable, str(HERE / "test_speaker_db.py")], "speaker_db"),
    ([sys.executable, str(HERE / "test_device_pipeline.py")], "device/denoise/corpus"),
    ([sys.executable, str(HERE / "test_nightly.py")], "piano notturno"),
]


def run(cmd: list[str], timeout: int) -> tuple[bool, str, float]:
    t0 = time.time()
    try:
        r = subprocess.run(cmd, capture_output=True, text=True,
                           cwd=str(ROOT), timeout=timeout)
    except subprocess.TimeoutExpired:
        return False, f"timeout dopo {timeout}s", time.time() - t0
    return r.returncode == 0, (r.stdout or "") + (r.stderr or ""), time.time() - t0


def main() -> int:
    ap = argparse.ArgumentParser(description="Esegui tutti i test")
    ap.add_argument("--full", action="store_true",
                    help="aggiunge il test end-to-end con i modelli veri")
    ap.add_argument("--minutes", type=float, default=0.5,
                    help="durata dei file finti nel test end-to-end")
    ap.add_argument("--count", type=int, default=2,
                    help="quanti file finti nel test end-to-end")
    args = ap.parse_args()

    suites = list(FAST)
    if args.full:
        suites.append((
            [sys.executable, str(HERE / "test_e2e.py"),
             "--minutes", str(args.minutes), "--count", str(args.count)],
            "end-to-end (modelli veri)",
        ))

    print("=" * 64)
    print(f"  Test — ciclo end-to-end {'incluso' if args.full else 'escluso'}")
    print("=" * 64)

    failed = []
    for cmd, label in suites:
        print(f"\n>>> {label}")
        ok, out, secs = run(cmd, timeout=3600 if args.full else 600)
        # Solo le righe che riassumono: l'output per intero di un test
        # end-to-end è rumore che nasconde l'esito.
        summary = [l for l in out.splitlines()
                   if "superat" in l or l.startswith("FAIL") or l.startswith("ERROR")
                   or l.startswith("  KO") or "KO —" in l]
        for line in summary[-6:]:
            print(f"    {line.strip()}")
        print(f"    {'SUPERATO' if ok else 'FALLITO'} in {secs:.0f}s")
        if not ok:
            failed.append(label)
            print("    --- ultimi errori ---")
            for line in out.splitlines():
                if "KO —" in line or line.startswith("FAIL") or "Traceback" in line:
                    print(f"    {line.strip()[:160]}")

    print("\n" + "=" * 64)
    if failed:
        print(f"  {len(suites) - len(failed)}/{len(suites)} suite superate.")
        print(f"  Fallite: {', '.join(failed)}")
        print("=" * 64)
        return 1
    print(f"  Tutte le {len(suites)} suite superate.")
    if not args.full:
        print("  (manca il ciclo end-to-end: usa --full per provarlo)")
    print("=" * 64)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())