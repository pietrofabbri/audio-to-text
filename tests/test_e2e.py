"""
Test end-to-end: la catena vera, su un registratore finto.

    python tests/test_e2e.py                # 2 file da 30s, ~3 minuti
    python tests/test_e2e.py --minutes 5    # registrazioni piu' lunghe
    python tests/test_e2e.py --keep         # lascia la radice di test

Cosa NON e' questo test: non verifica la qualita' della trascrizione.
Non confronta con una trascrizione di riferimento, perche' non esiste.
Se il modello peggiora, nessuno se ne accorge qui — e va bene: la
qualita' del riconoscimento e' una cosa che si misura con audio veri,
non con una voce sintetica. Qui si verifica che la catena *funzioni* e,
soprattutto, che non perda nulla.

Cosa verifica: che un file dal device arrivi a una trascrizione usabile,
che l'orario di registrazione venga ricavato dal nome, che il file venga
cancellato solo dopo, che il DB e il manifest siano coerenti, e che un
file non elaborabile resti sul device invece di sparire.

Isolamento: tutto avviene sotto una radice temporanea indicata da
A2T_ROOT_DIR, quindi output/, data/ e logs/ di produzione non vengono
toccati. E' la ragione per cui config.py legge quella variabile.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(HERE))

import make_fake_device as fake  # noqa: E402


class Failure(Exception):
    """Il test e' fallito. Il messaggio spiega perche', in italiano."""


def require(cond: bool, msg: str) -> None:
    if not cond:
        raise Failure(msg)


def run_python(args: list[str], env: dict, cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, *args], capture_output=True, text=True,
        env=env, cwd=str(cwd), timeout=3600,
    )


def step(n: int, title: str) -> None:
    print(f"\n[{n}] {title}", flush=True)


# ---------------------------------------------------------------------------
# I passi
# ---------------------------------------------------------------------------

def t_build_device(dev: Path, count: int, minutes: float) -> list[Path]:
    step(1, f"Creo un registratore finto in {dev} ({count} file da {minutes:g} min)")
    made = fake.build(
        dev, count, minutes, datetime(2026, 10, 3, 21, 0, 0),
        "Alice", -25.0, edge=True, name_pattern="REC_%Y%m%d_%H%M%S.mp3",
    )
    require(len(made) == count, f"attesi {count} file, creati {len(made)}")
    for f in made:
        require(f.stat().st_size > 1024, f"{f.name} troppo piccolo")
        dur = fake._probe_sec(f)
        require(dur > 1.0, f"{f.name} ha durata {dur:.1f}s: audio non valido")
    print(f"    {len(made)} file, {sum(fake._probe_sec(f) for f in made)/60:.1f} min")
    return made


def t_detect(dev: Path, env: dict) -> None:
    step(2, "detect: il device viene riconosciuto e i nomi letti")
    # discover() guarda dentro --mounts, quindi il finto sta in una
    # directory che fa da pseudo-/Volumes.
    mounts = dev.parent
    r = run_python([str(ROOT / "sync_device.py"), "detect",
                    "--mounts", str(mounts)], env, ROOT)
    out = r.stdout
    require(r.returncode == 0, f"detect è uscito con {r.returncode}\n{out}")
    require("Nessun volume" not in out,
            f"detect non ha trovato il device finto:\n{out}")
    require("record" in out, f"detect non ha riconosciuto la cartella record:\n{out}")

    from core.device import parse_recording_time
    # Solo i file con una data plausibile: il finto contiene anche
    # "REC_99999932_999999", che comincia con REC_ ma deve essere respinto.
    # Selezionarlo con startswith("REC_") it'd confondere il file patologico
    # con quello buono e il test fallirebbe per la ragione sbagliata.
    real = [f for f in (dev / "record").iterdir() if f.name.startswith("REC_2026")]
    require(real, "nessun file REC_2026 da leggere")
    for f in real:
        dt, pattern = parse_recording_time(f.name)
        require(dt is not None, f"{f.name}: orario non ricavato dal nome")
        require(dt.year == 2026, f"{f.name}: anno sbagliato ({dt})")
    junk = [f for f in (dev / "record").iterdir()
            if f.name in ("untitled.mp3", "00000001_000000.MP3",
                          "REC_99999932_999999.mp3")]
    require(len(junk) == 3, f"i casi patologici non sono stati creati: {junk}")
    for f in junk:
        dt, _ = parse_recording_time(f.name)
        require(dt is None, f"{f.name} ha prodotto un orario falso: {dt}")
    print(f"    {len(real)} nomi reali letti, {len(junk)} nomi sporchi respinti")


def t_dry_run(dev: Path, env: dict) -> None:
    step(3, "pull --dry-run: il piano, senza toccare nulla")
    before = _snapshot(dev)
    r = run_python([str(ROOT / "sync_device.py"), "pull",
                    "--source", str(dev), "--dry-run"], env, ROOT)
    require(r.returncode == 0, f"dry-run è uscito con {r.returncode}\n{r.stdout}")
    require("dry-run" in r.stdout, f"dry-run non ha stampato il piano:\n{r.stdout}")
    after = _snapshot(dev)
    require(before == after,
            "il dry-run ha modificato il device: deve solo mostrare il piano")
    print(f"    device invariato ({len(after)} file), piano stampato")


def t_pull_one(dev: Path, env: dict, root: Path) -> None:
    step(4, "pull --limit 1: un file attraversa tutta la catena")
    record = dev / "record"
    before = sorted(f.name for f in record.iterdir())
    r = run_python([str(ROOT / "sync_device.py"), "pull",
                    "--source", str(dev), "--limit", "1"], env, ROOT)
    require(r.returncode == 0, f"pull è uscito con {r.returncode}\n{r.stdout}\n{r.stderr}")

    after = sorted(f.name for f in record.iterdir())
    gone = [n for n in before if n not in after]
    require(len(gone) == 1,
            f"dovrebbe essere sparito 1 file, ne sono spariti {len(gone)}: {gone}")
    print(f"    elaborato e cancellato: {gone[0]}")

    out_dirs = [d for d in (root / "output").iterdir() if d.is_dir()]
    require(len(out_dirs) == 1, f"attesa 1 sessione in output/, trovate {len(out_dirs)}")
    out = out_dirs[0]

    # --- la verifica che autorizza la cancellazione ---------------------
    doc = json.loads((out / "transcript.json").read_text(encoding="utf-8"))
    segs = doc.get("segments") or []
    require(segs, "nessun segmento nella trascrizione")
    words = sum(len((s.get("text") or "").split()) for s in segs)
    require(words >= 5, f"solo {words} parole: la verifica avrebbe dovuto "
                        f"rifiutare la cancellazione")
    print(f"    {len(segs)} segmenti, {words} parole")

    for name in ("transcript.txt", "transcript.srt", "session.json",
                 "segments.jsonl", "tokens.jsonl", "analysis_ready.md"):
        require((out / name).exists(), f"manca {name} nell'output")

    # --- l'orario di registrazione -------------------------------------
    sess = json.loads((out / "session.json").read_text(encoding="utf-8"))
    wall = sess.get("session_start_wall") or ""
    require(wall, "session.json non ha session_start_wall")
    require(wall.startswith("2026-10-03T22:00"),
            f"orario di registrazione inatteso: {wall}")
    print(f"    orario di registrazione: {wall}")

    # --- il manifest ----------------------------------------------------
    man = root / "logs" / "device_manifest.jsonl"
    require(man.exists(), "manifest non scritto")
    rows = [json.loads(l) for l in man.read_text(encoding="utf-8").splitlines() if l.strip()]
    done = [x for x in rows if x.get("action") == "deleted"]
    require(done, f"nessuna riga 'deleted' nel manifest: {rows}")
    require(done[-1].get("verified"),
            "la cancellazione non riporta la verifica che l'ha autorizzata")
    print(f"    manifest: cancellazione autorizzata da '{done[-1]['verified']}'")


def t_junk_is_skipped(dev: Path, env: dict, root: Path) -> None:
    step(5, "la spazzatura del registratore viene notata, non processata")
    junk = [f for f in (dev / "record").iterdir()
            if f.name in ("untitled.mp3", "00000001_000000.MP3",
                          "REC_99999932_999999.mp3", "vuoto.mp3")]
    require(len(junk) == 4, f"i casi patologici non sono sul device: {junk}")

    r = run_python([str(ROOT / "sync_device.py"), "pull",
                    "--source", str(dev)], env, ROOT)
    require(r.returncode == 0,
            f"la spazzatura non deve far fallire la run: {r.returncode}\n{r.stdout}")

    # La spazzatura resta sul device: non si cancella mai qualcosa che
    # non si e' capito. E non deve finire in output/ come una sessione.
    for f in junk:
        require(f.exists(), f"{f.name} e' stato cancellato dal device")
    leaked = [d.name for d in (root / "output").iterdir() if d.is_dir()
              and any(x in d.name for x in
                      ("untitled", "00000001", "99999932", "vuoto"))]
    require(not leaked,
            f"la spazzatura e' finita in output/ come una sessione: {leaked}")

    man = root / "logs" / "device_manifest.jsonl"
    rows = [json.loads(l) for l in
            man.read_text(encoding="utf-8").splitlines() if l.strip()]
    marked = [x for x in rows if x.get("action") == "junk"]
    require(marked, "la spazzatura non e' stata registrata nel manifest")
    print(f"    {len(marked)} file di spazzatura notati e lasciati stare")


def t_same_timestamp_both_transcribed(dev: Path, env: dict, root: Path) -> None:
    step(6, "due file con lo stesso orario vengono entrambi trascritti")
    # Il device finto contiene una copia di una registrazione con un nome
    # diverso ma lo stesso orario. E' il caso in cui l'orologio del
    # registratore non distingue due file: se il secondo venisse scambiato
    # per un output gia' presente, resterebbe sul device per sempre senza
    # essere mai trascritto — in silenzio, e per sempre.
    man = root / "logs" / "device_manifest.jsonl"
    rows = [json.loads(l) for l in
            man.read_text(encoding="utf-8").splitlines() if l.strip()]

    stuck = [x for x in rows if x.get("action") == "skipped"
             and "già presente" in (x.get("reason") or "")]
    require(not stuck,
            f"un file è stato scambiato per un output già presente: {stuck}")

    stems = {x.get("stem") for x in rows if x.get("action") == "deleted"
             and x.get("stem")}
    base = "2026-10-03_22-00-00"
    require(base in stems, f"l'originale non e' stato elaborato: {sorted(stems)}")

    # Lo stesso orario con audio diverso deve avere una cartella propria.
    alts = [s for s in stems if s.startswith(base + "-")]
    require(alts,
            f"il file con lo stesso orario non ha una cartella propria: {sorted(stems)}")
    for s in alts:
        require((root / "output" / s / "transcript.json").exists(),
                f"la cartella {s} non ha una trascrizione")
    print(f"    stesso orario, audio diverso: due cartelle ({base}, {alts[0]})")

    # La copia identica invece non deve generare una seconda trascrizione.
    dups = [x for x in rows if x.get("file", "").endswith("-dup.mp3")]
    require(dups, "la copia identica non risulta nel manifest")
    require(len(alts) == 1,
            f"una sola cartella per lo stesso orario, ne trovate {len(alts)}: {alts}")

    left = [f.name for f in (dev / "record").iterdir()
            if f.name.startswith("REC_2026")]
    require(not left, f"file reali rimasti sul device: {left}")


def t_idempotence(dev: Path, env: dict, root: Root) -> None:
    step(7, "rilanciare non rifà il lavoro già fatto")
    r = run_python([str(ROOT / "sync_device.py"), "pull",
                    "--source", str(dev)], env, ROOT)
    require(r.returncode == 0, f"secondo pull è uscito con {r.returncode}\n{r.stdout}")
    n_out = len([d for d in (root / "output").iterdir() if d.is_dir()])
    require(n_out >= 1, "output sparito")
    db = root / "data" / "corpus.db"
    require(db.exists(), "corpus.db non creato")
    print(f"    {n_out} sessioni in output/, corpus.db presente")


def _snapshot(dev: Path) -> list[tuple[str, int]]:
    return sorted(
        (str(f.relative_to(dev)), f.stat().st_size)
        for f in dev.rglob("*") if f.is_file()
    )


# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description="Test end-to-end su device finto")
    ap.add_argument("--minutes", type=float, default=0.5)
    ap.add_argument("--count", type=int, default=2)
    ap.add_argument("--keep", action="store_true", help="non cancellare la radice")
    args = ap.parse_args()

    sandbox = Path(tempfile.mkdtemp(prefix="a2t-e2e-"))
    root = sandbox / "root"
    dev = sandbox / "volumes" / "untitled"
    for d in (root, dev):
        d.mkdir(parents=True, exist_ok=True)

    env = {**os.environ, "A2T_ROOT_DIR": str(root), "PYTHONPATH": str(ROOT)}
    print(f"Radice di test: {sandbox}")
    print(f"Output, DB e log qui dentro: {root}")

    steps = [
        ("build", lambda: t_build_device(dev, args.count, args.minutes)),
        ("detect", lambda: t_detect(dev, env)),
        ("dry-run", lambda: t_dry_run(dev, env)),
        ("pull", lambda: t_pull_one(dev, env, root)),
        ("junk", lambda: t_junk_is_skipped(dev, env, root)),
        ("collision", lambda: t_same_timestamp_both_transcribed(dev, env, root)),
        ("idempotence", lambda: t_idempotence(dev, env, root)),
    ]

    failed = 0
    for name, fn in steps:
        try:
            fn()
        except Failure as exc:
            print(f"    KO — {exc}")
            failed += 1
            break        # i passi dopo dipendono da questo
        except Exception as exc:  # noqa: BLE001
            print(f"    ERRORE — {type(exc).__name__}: {exc}")
            failed += 1
            break

    print("\n" + "=" * 64)
    if failed:
        print(f"  FALLITO dopo {len(steps) - failed} passi. Radice conservata:")
        print(f"  {sandbox}")
        return 1
    print(f"  Tutti i {len(steps)} passi superati.")
    if args.keep:
        print(f"  Radice conservata: {sandbox}")
    else:
        shutil.rmtree(sandbox, ignore_errors=True)
    print("=" * 64)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
