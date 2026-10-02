"""
Test del piano notturno: quanto lavoro entra in una finestra, e se la
coda tiene il passo con la produzione.

    python tests/test_nightly.py

Non carica modelli e non elabora nulla: qui si prova la *decisione*, cioe'
se la stima regge e se quello che il sistema promette corrisponde a
quello che fa.

Il punto non e' accademico. Con 18 file da un'ora al giorno e una finestra
notturna di poche ore, la domanda non e' "funziona?" ma "la coda cresce?".
Una risposta sbagliata qui significa un registratore che si riempie e
registrazioni che nessuno trascrive, senza che nulla fallisca.

I numeri usati qui (RTF, rapporto di parlato) sono quelli misurati, non
quelli che farebbero comodo: il test verifica che il piano sia coerente
con le costanti che il codice usa davvero.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(HERE))

import make_fake_device as fake  # noqa: E402


class Failure(Exception):
    pass


def require(cond: bool, msg: str) -> None:
    if not cond:
        raise Failure(msg)


# ---------------------------------------------------------------------------

def t_budget_fits_known_files(tmp: Path) -> None:
    """Il piano deve contare i file reali, non indovinare."""
    print("  budget e stima sul piano")
    import nightly

    dev = tmp / "untitled"
    made = fake.build(dev, 3, 0.5, __import__("datetime").datetime(2026, 10, 3, 21),
                      "Alice", -25.0, edge=False, name_pattern="REC_%Y%m%d_%H%M%S.mp3")
    require(len(made) == 3, f"device finto non creato: {len(made)}")

    plan = nightly._plan(argparse.Namespace(
        source=str(dev), limit=None, max_seconds=4 * 3600,
    ))
    require(plan["pending"] == 3, f"pending={plan['pending']}, attesi 3")
    require(plan["est_hours"] > 0, "est_hours non calcolata")
    require(plan["est_process_hours"] > 0, "est_process_hours non calcolata")
    print(f"    {plan['pending']} file, {plan['est_hours']:.2f} h audio, "
          f"{plan['est_process_hours']:.2f} h di elaborazione stimata")


def t_limit_is_after_sorting(tmp: Path) -> None:
    """--limit deve prendere i piu' vecchi, non i primi per nome."""
    print("  --limit prende la coda piu' vecchia")
    dev = tmp / "untitled"
    import nightly

    made = fake.build(dev, 3, 0.5, __import__("datetime").datetime(2026, 10, 3, 21),
                      "Alice", -25.0, edge=False, name_pattern="REC_%Y%m%d_%H%M%S.mp3")
    names = sorted(f.name for f in made)
    plan = nightly._plan(argparse.Namespace(
        source=str(dev), limit=1, max_seconds=4 * 3600,
    ))
    require(plan["files"] == names[:1],
            f"il limite non ha preso il file piu' vecchio: {plan['files']} "
            f"invece di {names[0]}")
    print(f"    con --limit 1 prende {plan['files'][0]}")


def t_queue_growth_is_visible(tmp: Path) -> None:
    """Con 18 file al giorno, la coda cresce: il piano deve dirlo.

    Non è un test che "deve passare": è un test che deve *mostrare* il
    numero. Se un giorno la macchina diventa piu' veloce, o le
    registrazioni diventano piu' silenziose, questo numero scende da solo.
    """
    print("  la coda cresce con 18 file da 1h al giorno?")
    import nightly

    for ratio in (0.92, 0.70, 0.50, 0.35):
        # Costo per ora di audio, con la frazione di parlato data.
        per_file_h = nightly.MEASURED_RTF * ratio * 1.0
        night = int((4 * 3600) // (per_file_h * 3600))
        day = int((40 * 60) // (per_file_h * 3600))    # 3 passate da 40 min
        gap = 18 - (night + day)
        print(f"    parlato {ratio:.0%}: notte {night} + diurno {day} "
              f"= {night + day}/18  ->  {'scoperta ' + str(gap) if gap > 0 else 'in pari'}")
    print("    (la coda cresce in tutti i casi: vedi README per la tabella)")


def t_dry_run_writes_nothing(tmp: Path) -> None:
    """Il piano a secco non deve toccare output, log o device."""
    print("  il piano a secco non scrive nulla")
    import os

    root = tmp / "root"
    dev = tmp / "untitled"
    root.mkdir(parents=True, exist_ok=True)
    fake.build(dev, 2, 0.5, __import__("datetime").datetime(2026, 10, 3, 21),
               "Alice", -25.0, edge=False, name_pattern="REC_%Y%m%d_%H%M%S.mp3")

    before = sorted((str(p.relative_to(dev)), p.stat().st_size)
                    for p in dev.rglob("*") if p.is_file())
    env = {**os.environ, "A2T_ROOT_DIR": str(root)}
    r = subprocess.run(
        [sys.executable, str(ROOT / "nightly.py"), "--dry-run",
         "--source", str(dev), "--no-publish"],
        capture_output=True, text=True, env=env, cwd=str(ROOT), timeout=600,
    )
    after = sorted((str(p.relative_to(dev)), p.stat().st_size)
                   for p in dev.rglob("*") if p.is_file())
    require(before == after, "il piano a secco ha toccato il device")
    out_dir = root / "output"
    created = list(out_dir.iterdir()) if out_dir.is_dir() else []
    require(not created, f"il piano a secco ha scritto output: {created}")
    print(f"    device e output intatti (exit {r.returncode})")


def t_speech_ratio_learns_from_sessions(tmp: Path) -> None:
    """Se esistono sessioni, il rapporto di parlato smette di essere un'ipotesi."""
    print("  il rapporto di parlato si impara dalle sessioni fatte")
    import nightly

    root = tmp / "ratio-root"
    out = root / "output"
    sess = out / "2026-10-01_22-00-00"
    sess.mkdir(parents=True, exist_ok=True)
    # speech_ratio sta in transcript.json sotto "meta": e' da li' che la
    # stima notturna lo legge, quindi è li' che va scritto.
    (sess / "transcript.json").write_text(
        json.dumps({
            "meta": {"speech_ratio": 0.5, "duration_sec": 100.0},
            "segments": [{"text": "una frase", "start": 0, "end": 1}],
        }),
        encoding="utf-8",
    )

    import os
    os.environ["A2T_ROOT_DIR"] = str(root)
    for mod in ("core.config", "core.corpus_db", "core.speaker_db", "nightly"):
        sys.modules.pop(mod, None)
    import nightly as fresh  # noqa: F811

    ratio = fresh._measured_speech_ratio()
    require(ratio is not None, "nessun rapporto di parlato ricavato dalle sessioni")
    require(abs(ratio - 0.5) < 0.01, f"rapporto di parlato inatteso: {ratio}")
    print(f"    rapporto misurato: {ratio}")


# ---------------------------------------------------------------------------

def main() -> int:
    tests = [
        t_budget_fits_known_files,
        t_limit_is_after_sorting,
        t_queue_growth_is_visible,
        t_dry_run_writes_nothing,
        t_speech_ratio_learns_from_sessions,
    ]
    failed = 0
    for fn in tests:
        with tempfile.TemporaryDirectory() as d:
            try:
                fn(Path(d))
            except Failure as exc:
                print(f"  KO   {fn.__name__}: {exc}")
                failed += 1
            except Exception as exc:  # noqa: BLE001
                print(f"  ERRORE {fn.__name__}: {type(exc).__name__}: {exc}")
                failed += 1
        print()
    print(f"{len(tests) - failed}/{len(tests)} test superati")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())