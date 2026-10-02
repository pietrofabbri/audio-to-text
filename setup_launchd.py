"""
setup_launchd.py — Installa il job notturno su macOS via launchd

Crea un LaunchAgent che esegue la pipeline ogni notte dalle 03:00 alle 05:30.
launchd è il sistema di scheduling nativo di macOS: più affidabile di cron,
funziona anche se il Mac era in sleep (si attiva al risveglio).

Uso:
    python setup_launchd.py install    # installa il job
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

# Identificatore univoco del LaunchAgent (stile reverse-DNS)
LABEL = "it.pietrofabbri.audio-to-text"

# Orario di esecuzione notturna
START_HOUR   = 3   # 03:00
START_MINUTE = 0

# Finestra notturna. Con ~3,3x realtime misurati sull'ASR, 18 file da
# un'ora sono ~10 ore di elaborazione: in tre ore ne entrano circa 3.
# La coda avanza di un pezzo alla volta e il resto resta sul device,
# in ordine cronologico. Alzare questo numero non fa finire prima:
# finisce quando finisce, e quello che non c'e torna la notte dopo.
MAX_RUNTIME_HOURS = 3.0

# Percorso plist LaunchAgent
PLIST_DIR  = Path.home() / "Library" / "LaunchAgents"
PLIST_PATH = PLIST_DIR / f"{LABEL}.plist"


# ---------------------------------------------------------------------------
# Costruzione del plist
# ---------------------------------------------------------------------------

def build_plist() -> dict:
    LOGS_DIR.mkdir(parents=True, exist_ok=True)

    return {
        "Label": LABEL,
        "ProgramArguments": [
            str(VENV_PYTHON),
            str(NIGHTLY_SCRIPT),
            "--max-seconds", str(int(MAX_RUNTIME_HOURS * 3600)),
        ],
        # Esegui ogni notte alle START_HOUR:START_MINUTE
        "StartCalendarInterval": {
            "Hour":   START_HOUR,
            "Minute": START_MINUTE,
        },
        # Se il Mac era spento/in sleep all'orario previsto,
        # esegui appena si sveglia
        "RunAtLoad": False,
        # Directory di lavoro = directory del progetto
        "WorkingDirectory": str(PROJECT_DIR),
        # Log stdout e stderr in files separati
        "StandardOutPath": str(LOGS_DIR / "launchd_stdout.log"),
        "StandardErrorPath": str(LOGS_DIR / "launchd_stderr.log"),
        # Variabili d'ambiente
        "EnvironmentVariables": {
            "PATH": f"{VENV_PYTHON.parent}:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin",
            "HOME": str(Path.home()),
            # HF_TOKEN verrà letto da ~/.huggingface/token se non impostato qui
        },
        # Non riavviare automaticamente se fallisce
        "KeepAlive": False,
        # Priorità CPU bassa per non disturbare l'uso diurno
        "ProcessType": "Background",
        # Timeout esplicito (secondi) — 3 ore di sicurezza
        "TimeOut": int(MAX_RUNTIME_HOURS * 3600 + 1800),
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
    print(f"  Durata massima: {MAX_RUNTIME_HOURS} ore")
    print(f"  Log: {LOGS_DIR}/launchd_stdout.log")
    print(f"\nPer rimuoverlo: python setup_launchd.py uninstall")


def uninstall() -> None:
    if not PLIST_PATH.exists():
        print(f"Job non trovato: {PLIST_PATH}")
        return

    # Scarica il job
    subprocess.run(
        ["launchctl", "unload", "-w", str(PLIST_PATH)],
        capture_output=True,
    )
    PLIST_PATH.unlink(missing_ok=True)
    print(f"✓ Job rimosso: {LABEL}")


def status() -> None:
    result = subprocess.run(
        ["launchctl", "list", LABEL],
        capture_output=True, text=True,
    )
    if result.returncode == 0:
        print(f"Job attivo: {LABEL}")
        print(result.stdout)
    else:
        print(f"Job non trovato o non attivo: {LABEL}")

    # Mostra le ultime righe del log
    log_file = LOGS_DIR / "launchd_stdout.log"
    if log_file.exists():
        print(f"\n--- Ultime 20 righe di {log_file.name} ---")
        lines = log_file.read_text(encoding="utf-8", errors="replace").splitlines()
        for line in lines[-20:]:
            print(line)


def run_now() -> None:
    """Esegue il job subito (utile per test)."""
    print(f"Esecuzione manuale: {VENV_PYTHON} {RUN_SCRIPT} --scheduled")
    os.execv(str(VENV_PYTHON), [str(VENV_PYTHON), str(RUN_SCRIPT), "--scheduled"])


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

COMMANDS = {
    "install":   install,
    "uninstall": uninstall,
    "status":    status,
    "run-now":   run_now,
}

def main() -> None:
    if len(sys.argv) < 2 or sys.argv[1] not in COMMANDS:
        print("Uso: python setup_launchd.py [install|uninstall|status|run-now]")
        print()
        print("  install    Installa il job notturno (03:00 ogni notte)")
        print("  uninstall  Rimuove il job")
        print("  status     Mostra stato e ultime righe di log")
        print("  run-now    Esegue subito (test)")
        sys.exit(1)

    COMMANDS[sys.argv[1]]()


if __name__ == "__main__":
    main()
