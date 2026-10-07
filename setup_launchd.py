"""
setup_launchd.py — Installa il job notturno su macOS via launchd

Crea un LaunchAgent che esegue la pipeline ogni notte dalle 02:00 alle
06:00, piu' tre brevi passate diurne. Orario e durata stanno in
core/config.py: questo file li legge, non li decide.
launchd è il sistema di scheduling nativo di macOS: più affidabile di cron,
funziona anche se il Mac era in sleep (si attiva al risveglio).

Uso:
    python setup_launchd.py install    # installa i job (notte, giorno, inserimento)
    python setup_launchd.py install-tile  # solo il job all'inserimento del registratore
    python setup_launchd.py uninstall  # rimuove il job
    python setup_launchd.py status     # mostra stato
    python setup_launchd.py run-now    # esegue subito (test)
"""

from __future__ import annotations

import os
import plistlib
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

# Orario, finestra, thread e priorita' vivono in core/config.py: sono gli
# stessi numeri che nightly.py usa a runtime. Prima erano scritti qui e
# li' separatamente, con un valore discorde sulla durata della notte —
# e la conseguenza era che il piano che leggi a mano e quello che gira
# di notte erano due piani diversi per la stessa notte.
from core.config import (  # noqa: E402
    DAYTIME_BUDGET_SEC,
    DAYTIME_COOLDOWN_SEC,
    DAYTIME_HOURS,
    DAYTIME_NICE,
    DAYTIME_THREADS,
    NIGHT_COOLDOWN_SEC,
    NIGHT_NICE,
    NIGHT_START_HOUR,
    NIGHT_START_MINUTE,
    NIGHT_THREADS,
    NIGHT_WINDOW_SEC,
)

# ---------------------------------------------------------------------------
# Configurazione del job
# ---------------------------------------------------------------------------

PROJECT_DIR  = Path(__file__).resolve().parent
VENV_PYTHON  = Path.home() / "Desktop" / "Titoli Fabbri" / "whisperx_env" / "bin" / "python"
# nightly.py, non run.py: il ciclo notturno parte dal registratore
# (import + elaborazione entro budget + pubblicazione). run.py da solo
# legge input/ e non sa nulla del device.
NIGHTLY_SCRIPT = PROJECT_DIR / "nightly.py"
LOGS_DIR     = PROJECT_DIR / "logs"

# Identificatori univoci dei LaunchAgent (stile reverse-DNS)
LABEL         = "it.pietrofabbri.audio-to-text"
LABEL_DAYTIME = "it.pietrofabbri.audio-to-text-daytime"
# Il job che parte quando si inserisce il registratore (StartOnMount).
LABEL_TILE    = "it.pietrofabbri.audio-to-text-tile"
SYNC_SCRIPT   = PROJECT_DIR / "sync_device.py"

# --- Finestra notturna: pieno regime -------------------------------------
# La macchina è libera e serve: 02:00 → 06:00.
START_HOUR   = NIGHT_START_HOUR
START_MINUTE = NIGHT_START_MINUTE
MAX_RUNTIME_SEC = NIGHT_WINDOW_SEC
MAX_RUNTIME_HOURS = NIGHT_WINDOW_SEC / 3600

# --- Passate diurne: processo leggero in background ----------------------
# Di giorno la macchina è in uso. Il lavoro diurna esiste per non lasciare
# la coda ferma otto ore, ma è volutamente piccolo: budget breve, pochi
# thread, priorità bassa. Se non serve, non costa quasi niente; se la
# macchina è occupata, rallenta e basta.
DAYTIME_BUDGET_MIN = DAYTIME_BUDGET_SEC // 60   # minuti per passata

# Percorso plist LaunchAgent
PLIST_DIR  = Path.home() / "Library" / "LaunchAgents"
PLIST_PATH = PLIST_DIR / f"{LABEL}.plist"
PLIST_PATH_DAYTIME = PLIST_DIR / f"{LABEL_DAYTIME}.plist"
PLIST_PATH_TILE = PLIST_DIR / f"{LABEL_TILE}.plist"


# ---------------------------------------------------------------------------
# Costruzione del plist
# ---------------------------------------------------------------------------

def build_plist() -> dict:
    return {
        "Label": LABEL,
        "ProgramArguments": [
            str(VENV_PYTHON),
            str(NIGHTLY_SCRIPT),
            "--max-seconds", str(MAX_RUNTIME_SEC),
            "--threads", str(NIGHT_THREADS),
            "--cooldown-sec", str(NIGHT_COOLDOWN_SEC),
        ],
        # Esegui ogni notte alle START_HOUR:START_MINUTE
        "StartCalendarInterval": {
            "Hour":   START_HOUR,
            "Minute": START_MINUTE,
        },
        # Se il Mac era spento/in sleep all'orario previsto,
        # esegui appena si sveglia
        "RunAtLoad": False,
        ** _common_plist_fields(),
        # Priorità CPU bassa: di notte la macchina non serve a nessuno,
        # ma lascia comunque il sistema libero di gestire la priorità.
        "ProcessType": "Background",
        "Nice": NIGHT_NICE,
        # Timeout esplicito (secondi) — margine oltre il budget interno
        "TimeOut": int(MAX_RUNTIME_SEC + 1800),
    }


def build_plist_daytime() -> dict:
    """Passate diurne: brevi, a bassa priorità, con pochi thread.

    Non è un secondo ciclo completo: è lo stesso ciclo con un budget
    piccolo, pensato per far avanzare la coda durante il giorno senza
    rubare la macchina a chi la sta usando. Se la coda è vuota non costa
    nulla; se la macchina è occupata, il thread limitato rallenta e basta.
    """
    return {
        "Label": LABEL_DAYTIME,
        "ProgramArguments": [
            # niente "nice -n" wrapper: la priorita' la applica
            # nightly.py, cosi' il comando a mano e quello di launchd
            # si comportano allo stesso modo.
            str(VENV_PYTHON),
            str(NIGHTLY_SCRIPT),
            "--max-seconds", str(DAYTIME_BUDGET_SEC),
            "--threads", str(DAYTIME_THREADS),
            "--nice", str(DAYTIME_NICE),
            "--cooldown-sec", str(DAYTIME_COOLDOWN_SEC),
            "--no-publish",   # si pubblica di notte, quando la coda è intera
        ],
        "StartCalendarInterval": [
            {"Hour": h, "Minute": 30} for h in DAYTIME_HOURS
        ],
        "RunAtLoad": False,
        **_common_plist_fields(),
        "ProcessType": "Background",
        "TimeOut": int(DAYTIME_BUDGET_SEC + 900),
    }


def build_plist_tile() -> dict:
    """Il job dell'inserimento: parte a ogni volume montato.

    `StartOnMount` fa partire il job ogni volta che macOS monta un
    filesystem — il registratore, ma anche una chiavetta o un'immagine
    disco. Per questo il comando e' `scarica --auto`: se il volume non e'
    il registratore esce in un paio di secondi senza fare niente. Se lo
    e', copia in coda, libera il registratore, lo espelle e avvisa con una
    notifica che si puo' staccare; poi fa partire la passata diurna sulla
    coda (`launchctl kickstart` del job diurno).

    Priorita' normale e nessun limite di thread: qui non si elabora, si
    copia, e la cosa che conta e' finire presto perche' il registratore
    torni al braccio.
    """
    campi = _common_plist_fields()
    campi["StandardOutPath"] = str(LOGS_DIR / "launchd_scarico.log")
    campi["StandardErrorPath"] = str(LOGS_DIR / "launchd_scarico.log")
    return {
        "Label": LABEL_TILE,
        "ProgramArguments": [
            str(VENV_PYTHON), str(SYNC_SCRIPT), "scarica", "--auto",
        ],
        "StartOnMount": True,
        "RunAtLoad": False,
        **campi,
        "ProcessType": "Interactive",
        "TimeOut": 3600,
    }


def install_tile() -> None:
    """Installa (o reinstalla) solo il job dell'inserimento."""
    if not VENV_PYTHON.exists():
        print(f"ERRORE: venv Python non trovato: {VENV_PYTHON}")
        sys.exit(1)
    PLIST_DIR.mkdir(parents=True, exist_ok=True)
    if PLIST_PATH_TILE.exists():
        subprocess.run(["launchctl", "unload", "-w", str(PLIST_PATH_TILE)],
                       capture_output=True)
    with open(PLIST_PATH_TILE, "wb") as f:
        plistlib.dump(build_plist_tile(), f)
    r = subprocess.run(["launchctl", "load", "-w", str(PLIST_PATH_TILE)],
                       capture_output=True, text=True)
    if r.returncode != 0:
        print(f"ERRORE launchctl load (tile): {r.stderr}")
        sys.exit(1)
    print(f"✓ Job all'inserimento installato: {LABEL_TILE}")
    print("  Quando inserisci il registratore: copia in input/coda/, lo libera,")
    print("  lo espelle e ti avvisa che puoi staccarlo. Log: logs/scarico.log")


def _common_plist_fields() -> dict:
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    return {
        "WorkingDirectory": str(PROJECT_DIR),
        "StandardOutPath": str(LOGS_DIR / "launchd_stdout.log"),
        "StandardErrorPath": str(LOGS_DIR / "launchd_stderr.log"),
        "EnvironmentVariables": {
            # /usr/sbin per diskutil (espulsione del registratore).
            "PATH": f"{VENV_PYTHON.parent}:/opt/homebrew/bin:/usr/local/bin:"
                    "/usr/bin:/bin:/usr/sbin:/sbin",
            "HOME": str(Path.home()),
        },
        "KeepAlive": False,
    }


# ---------------------------------------------------------------------------
# Comandi
# ---------------------------------------------------------------------------

def install() -> None:
    if not VENV_PYTHON.exists():
        print(f"ERRORE: venv Python non trovato: {VENV_PYTHON}")
        print("Esegui prima: ./setup_env.sh")
        sys.exit(1)

    PLIST_DIR.mkdir(parents=True, exist_ok=True)

    plist_data = build_plist()
    with open(PLIST_PATH, "wb") as f:
        plistlib.dump(plist_data, f)

    print(f"Plist scritto: {PLIST_PATH}")

    # Carica il job in launchd
    result = subprocess.run(
        ["launchctl", "load", "-w", str(PLIST_PATH)],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        print(f"ERRORE launchctl load: {result.stderr}")
        sys.exit(1)

    print(f"\n✓ Job installato: {LABEL}")
    print(f"  Esecuzione ogni notte alle {START_HOUR:02d}:{START_MINUTE:02d}")
    print(f"  Durata massima: {MAX_RUNTIME_HOURS:g} ore")
    print(f"  {NIGHT_THREADS} thread, nice {NIGHT_NICE}, "
          f"pausa {NIGHT_COOLDOWN_SEC}s fra un file e il successivo")
    print(f"  Log: {LOGS_DIR}/launchd_stdout.log")

    # Il passaggio diurno va installato con lo stesso comando: due job
    # con due plist, non uno che fa due cose in momenti diversi.
    install_daytime()
    install_tile()
    print(f"\nPer rimuoverli: python setup_launchd.py uninstall")


def install_daytime() -> None:
    """Job diurno: breve, a bassa priorita, con pochi thread.

    Non e' un secondo ciclo completo: e' lo stesso ciclo con un budget
    piccolo, pensato per non lasciare la coda ferma otto ore di giorno
    senza pero' rubare la macchina a chi la sta usando. Se la coda e'
    vuota non costa nulla.
    """
    plist = build_plist_daytime()
    with open(PLIST_PATH_DAYTIME, "wb") as f:
        plistlib.dump(plist, f)
    result = subprocess.run(
        ["launchctl", "load", "-w", str(PLIST_PATH_DAYTIME)],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        print(f"ERRORE launchctl load (daytime): {result.stderr}")
        return
    print(f"✓ Job diurno installato: {LABEL_DAYTIME}")
    print(f"  Passate alle {', '.join(f'{h:02d}:30' for h in DAYTIME_HOURS)}, "
          f"{DAYTIME_BUDGET_MIN} min ciascuna, {DAYTIME_THREADS} thread, "
          f"priorita -{DAYTIME_NICE}, pausa {DAYTIME_COOLDOWN_SEC}s")


def uninstall() -> None:
    for path, label in ((PLIST_PATH, LABEL), (PLIST_PATH_DAYTIME, LABEL_DAYTIME),
                        (PLIST_PATH_TILE, LABEL_TILE)):
        if not path.exists():
            continue
        subprocess.run(["launchctl", "unload", "-w", str(path)], capture_output=True)
        path.unlink(missing_ok=True)
        print(f"✓ Job rimosso: {label}")


def status() -> None:
    for label in (LABEL, LABEL_DAYTIME, LABEL_TILE):
        result = subprocess.run(
            ["launchctl", "list", label],
            capture_output=True, text=True,
        )
        print(f"{label}: {'attivo' if result.returncode == 0 else 'NON attivo'}")

    # Mostra le ultime righe del log
    log_file = LOGS_DIR / "launchd_stdout.log"
    if log_file.exists():
        print(f"\n--- Ultime 20 righe di {log_file.name} ---")
        lines = log_file.read_text(encoding="utf-8", errors="replace").splitlines()
        for line in lines[-20:]:
            print(line)


def run_now() -> None:
    """Esegue il ciclo subito (utile per test)."""
    print(f"Esecuzione manuale: {VENV_PYTHON} {NIGHTLY_SCRIPT}")
    os.execv(str(VENV_PYTHON), [str(VENV_PYTHON), str(NIGHTLY_SCRIPT)])


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

COMMANDS = {
    "install":   install,
    "install-tile": install_tile,
    "uninstall": uninstall,
    "status":    status,
    "run-now":   run_now,
}

def main() -> None:
    if len(sys.argv) < 2 or sys.argv[1] not in COMMANDS:
        print("Uso: python setup_launchd.py [install|uninstall|status|run-now]")
        print()
        print(f"  install    Installa i job: notturno ({START_HOUR:02d}:00, "
              f"{MAX_RUNTIME_HOURS:g}h) e daytime ({DAYTIME_BUDGET_MIN}min x "
              f"{len(DAYTIME_HOURS)})")
        print("  install-tile  Installa solo il job che scarica il registratore all'inserimento")
        print("  uninstall  Rimuove tutti i job")
        print("  status     Mostra stato dei job e ultime righe di log")
        print("  run-now    Esegue il ciclo subito (test)")
        sys.exit(1)

    COMMANDS[sys.argv[1]]()


if __name__ == "__main__":
    main()
