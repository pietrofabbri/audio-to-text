"""
Domande sui file multimediali che non richiedono di decodificarli.

Esiste perché due moduli avevano bisogno della stessa risposta (quanto
dura questo file) e ognuno se la stava chiedendo per conto proprio. La
sonda è economica e va bene anche in coda a un file da un'ora: legge i
metadati, non l'audio, quindi costa decine di millisecondi contro
un'ora di decoding.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path


def probe_duration(path: Path) -> float | None:
    """Durata del file in secondi, letta dai metadati con ffprobe.

    Non si decodifica l'audio: sui metadati si legge la durata, e sul
    file intero no. None se ffprobe non c'è, se il file non esiste o se
    i metadati non dicono niente — che sono tre modi diversi di dire
    «non lo so», e tutti e tre vanno bene: nessuno di loro merita di
    far fallire la pipeline.
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
