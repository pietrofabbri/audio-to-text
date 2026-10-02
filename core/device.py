"""
Device — rilevamento del registratore e lettura dei file audio.

Il registratore si presenta come un volume rimovibile (spesso
"untitled", il nome di fabbrica) con dentro una cartella "record" e file
da ~1 ora il cui titolo contiene data, ora, minuti e secondi.

Due compiti distinti, tenuti separati perché falliscono in modi diversi:

1. `discover()` — capire *che cosa* è montato. Non presume nulla: cerca
   volumi che contengono audio, guarda come sono chiamati i file e ne
   deduce il formato della data. Va eseguito con il device collegato,
   una volta sola, per capire cosa scrivere nella configurazione.

2. `parse_recording_time()` — ricavare l'ora di registrazione dal nome
   del file. È il dato che rende possibile sincronizzare in futuro i
   dati biometrici: senza un orario di parecchio notturno, la
   trascrizione non è agganciabile a nient'altro.

Il parsing è a tentativi, non a regex singola: i registratori scrivono
nomi tipo "REC_20261003_220415.mp3", "2026-10-03 22-04-15.wav",
"VID_20261003_220415.mp4" e non si sa quale sarà il tuo. Ogni tentativo
ha un punteggio e vince il primo che produce una data plausibile.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

logger = logging.getLogger(__name__)


# Estensioni accettate come registrazione. Ordinate per priorità: alcuni
# registratori chiamano .mp3 quello che è un .wav container, quindi
# l'estensione non è mai usata per dedurre il formato.
AUDIO_EXTENSIONS = (".mp3", ".wav", ".m4a", ".aac", ".amr", ".ogg", ".opus", ".flac")

# Sotto questo nome è quasi certamente il registratore, non una chiavetta
# qualsiasi. Non è un requisito: se non c'è, si usa comunque il volume che
# contiene audio e cartella "record".
RECORD_DIR_NAMES = ("record", "records", "recorder", "voice", "audio", "wav", "mp3")

# Sotto questa soglia un volume non è un registratore: i dischi delle IDE
# e quello di Freebuff montati durante una sessione superano abbondantemente
# i 20 GB, e vanno esclusi senza chiedere.
MIN_PLAUSIBLE_SIZE_MB = 8

# File audio più piccoli di questo sono segnaposto o spazzatura, non una
# registrazione. Una registrazione vuota non si processa e non si cancella.
MIN_AUDIO_BYTES = 1024

# Contenitori di applicazioni: dentro un .app ci sono asset .mp3/.wav che
# non hanno niente a che fare con una registrazione. Senza questo filtro il
# rilevamento sceglieva Kiro.app come "registratore".
SKIP_DIR_SUFFIXES = (".app", ".bundle", ".framework", ".appimage", ".dmg", ".pkg")

# Quanti livelli scandire sotto il volume. I registratori hanno record/ o
# file in radice; i dischi image-based possono avere alberi profondi.
MAX_SCAN_DEPTH = 3


def _is_skippable(path: Path) -> bool:
    """Il percorso attraversa un bundle applicazione o un disco immagine?"""
    return any(part.endswith(SKIP_DIR_SUFFIXES) for part in path.parts)


def _safe_stat(path: Path) -> os.stat_result | None:
    """lstat che non solleva mai.

    Sotto /Volumes ci sono symlink verso /Applications e mount di terze
    parti: un controllo di permessi che solleva eccezione fa fallire
    l'intero rilevamento, e un rilevamento che crasha è un rilevamento
    che non verrà eseguito.
    """
    try:
        return path.lstat()
    except OSError:
        return None


def _iter_dirs(root: Path, max_depth: int = MAX_SCAN_DEPTH):
    """Directory sotto root fino a max_depth, senza symlink né bundle."""
    try:
        root = root.resolve()
    except OSError:
        return
    stack: list[tuple[Path, int]] = [(root, 0)]
    while stack:
        current, depth = stack.pop()
        try:
            entries = sorted(current.iterdir())
        except OSError:
            continue
        for entry in entries:
            st = _safe_stat(entry)
            if st is None:
                continue
            if os.path.islink(entry) or not os.path.isdir(entry):
                continue
            if entry.name.endswith(SKIP_DIR_SUFFIXES):
                continue
            yield entry
            if depth < max_depth:
                stack.append((entry, depth + 1))


def _iter_files(root: Path, max_depth: int = MAX_SCAN_DEPTH):
    """File sotto root fino a max_depth, senza seguire symlink e senza
    entrare nei bundle. I symlink in /Volumes puntano a /Applications:
    seguirli significa scansionare l'intero disco di sistema."""
    try:
        root = root.resolve()
    except OSError:
        return
    stack: list[tuple[Path, int]] = [(root, 0)]
    while stack:
        current, depth = stack.pop()
        try:
            entries = sorted(current.iterdir())
        except OSError:
            continue
        for entry in entries:
            st = _safe_stat(entry)
            if st is None or os.path.islink(entry):
                continue
            if os.path.isdir(entry):
                if depth < max_depth and not entry.name.endswith(SKIP_DIR_SUFFIXES):
                    stack.append((entry, depth + 1))
            elif os.path.isfile(entry) and not entry.name.startswith("."):
                yield entry


@dataclass
class VolumeInfo:
    """Un volume candidato con le sue caratteristiche."""

    path: Path
    label: str
    total_mb: float
    free_mb: float
    filesystem: str = ""
    writable: bool = False
    has_record_dir: bool = False
    audio_files: list[Path] = field(default_factory=list)

    @property
    def audio_count(self) -> int:
        return len(self.audio_files)

    @property
    def audio_mb(self) -> float:
        if not self.audio_files:
            return 0.0
        return sum(f.stat().st_size for f in self.audio_files) / 1e6

    @property
    def looks_like_recorder(self) -> bool:
        """Criterio stretto: solo i volumi da cui ha senso importare.

        La scrittura è un requisito, non una preferenza: il registratore
        è la fonte da cui si cancella, e un volume in sola lettura non
        può esserlo. Meglio chiedere --source che proporre un candidate
        che poi non si può svuotare.
        """
        return bool(
            self.audio_files
            and self.writable
            and self.total_mb >= MIN_PLAUSIBLE_SIZE_MB
            and not _is_skippable(self.path)
            and not self.path.name.startswith(".")
        )

    def rejection_reason(self) -> str:
        if not self.audio_files:
            return "nessun file audio reale"
        if _is_skippable(self.path):
            return "bundle applicazione, non un dispositivo"
        if not self.writable:
            return "in sola lettura: i file non potrebbero essere cancellati"
        if self.total_mb < MIN_PLAUSIBLE_SIZE_MB:
            return f"troppo piccolo ({self.total_mb:.0f} MB)"
        return ""

    def summary(self) -> str:
        flags = []
        if self.has_record_dir:
            flags.append("cartella record")
        flags.append("scrivibile" if self.writable else "SOLO LETTURA")
        return (
            f"{self.path}  [{self.label}]\n"
            f"    filesystem : {self.filesystem}\n"
            f"    spazio     : {self.free_mb:.0f} MB liberi su {self.total_mb:.0f} MB\n"
            f"    audio      : {self.audio_count} file, {self.audio_mb:.1f} MB\n"
            f"    {' · '.join(flags)}"
        )


# ---------------------------------------------------------------------------
# Rilevamento
# ---------------------------------------------------------------------------

def _volume_label(path: Path) -> str:
    """Nome interno del volume (dalle tabelle di sistema), non il nome
    della cartella: alcuni dispositivi hanno nomi diversi in FDA."""
    try:
        out = subprocess.run(
            ["diskutil", "info", str(path)],
            capture_output=True, text=True, timeout=10,
        ).stdout
        m = re.search(r"Volume Name:\s*(.+)", out)
        if m:
            return m.group(1).strip()
    except (OSError, subprocess.SubprocessError):
        pass
    return path.name


def _volume_free_space(path: Path) -> float:
    try:
        usage = shutil.disk_usage(str(path))
        return usage.free / 1e6
    except OSError:
        return 0.0


def _volume_size_mb(path: Path) -> float:
    """Dimensione del supporto in MB. Non è la somma dei file: un
    registratore quasi vuoto è comunque un dispositivo da 8 GB."""
    try:
        out = subprocess.run(
            ["diskutil", "info", str(path)],
            capture_output=True, text=True, timeout=10,
        ).stdout
        m = re.search(r"Volume Total Space:\s*(\d+)\s*(bytes|B)", out)
        if m:
            n = int(m.group(1))
            return n / 1e6 if m.group(2) == "B" else n / 1e6
    except (OSError, subprocess.SubprocessError):
        pass
    return _volume_free_space(path)


def _find_record_dir(root: Path) -> Path | None:
    """Cerca una cartella tipo "record" a uno o due livelli di profondità."""
    for d in _iter_dirs(root, max_depth=2):
        if d.name.lower() in RECORD_DIR_NAMES:
            return d
    return None


def _collect_audio(root: Path, limit: int = 400) -> list[Path]:
    """File audio reali: estensione nota, dimensione plausibile, fuori
    dai bundle. I file vuoti si scartano qui e non più a valle."""
    out: list[Path] = []
    for f in _iter_files(root):
        if f.suffix.lower() not in AUDIO_EXTENSIONS:
            continue
        try:
            if f.stat().st_size < MIN_AUDIO_BYTES:
                logger.debug("Scartato (troppo piccolo): %s", f.name)
                continue
        except OSError:
            continue
        out.append(f)
        if len(out) >= limit:
            return out
    return out


def discover(mounts: Path | str = "/Volumes") -> list[VolumeInfo]:
    """
    Elenca i volumi montati che potrebbero essere registratori.

    Non filtra in modo aggressivo: mostra tutto quello che contiene
    audio, con un flag `looks_like_recorder`, così la decisione resta
    di chi legge l'output e non di una euristica nascosta.
    """
    mounts = Path(mounts)
    results: list[VolumeInfo] = []

    if not mounts.is_dir():
        logger.error("Directory di mount inesistente: %s", mounts)
        return results

    for entry in sorted(mounts.iterdir()):
        if entry.name.startswith("."):
            continue
        # "Macintosh HD" è un symlink a /: scansirlo significa leggere
        # l'intero disco di sistema. Solo i volumi veramente montati
        # sono candidati.
        if _safe_stat(entry) is None or os.path.islink(entry):
            continue
        if not os.path.isdir(entry):
            continue
        if _is_skippable(entry):
            continue

        record_dir = _find_record_dir(entry)
        scan_root = record_dir or entry
        audio = _collect_audio(scan_root)
        if not audio:
            # Il volume potrebbe avere audio ma non in una cartella nota:
            # si riprova dalla radice prima di scartarlo.
            audio = _collect_audio(entry)
        if not audio:
            continue

        free = _volume_free_space(entry)
        writable = os.access(entry, os.W_OK)

        results.append(VolumeInfo(
            path=entry,
            label=_volume_label(entry),
            total_mb=_volume_size_mb(entry),
            free_mb=free,
            has_record_dir=record_dir is not None,
            audio_files=audio,
            writable=writable,
        ))

    return results


def pick_recorder(volumes: Iterable[VolumeInfo]) -> VolumeInfo | None:
    """
    Sceglie il volume più probabilmente registratoore.

    Ordine di preferenza: cartella "record" presente, poi cardinalità di
    audio, poi spazio (i registratori sono piccoli). In caso di parità
    restituisce None: due candidati indistinguibili sono un motivo per
    chiedere, non per indovinare e cancellare file sul disco sbagliato.
    """
    candidates = [v for v in volumes if v.looks_like_recorder]
    if not candidates:
        return None

    candidates.sort(
        key=lambda v: (v.has_record_dir, v.audio_count, -v.total_mb),
        reverse=True,
    )
    if len(candidates) > 1:
        top, second = candidates[0], candidates[1]
        same = (
            top.has_record_dir == second.has_record_dir
            and top.audio_count == second.audio_count
        )
        if same:
            logger.error(
                "Due volumi indistinguibili (%s e %s): specifica il percorso "
                "a mano con --source invece di lasciare che il tool scelga",
                top.path, second.path,
            )
            return None
    return candidates[0]


# ---------------------------------------------------------------------------
# Data e ora nel nome del file
# ---------------------------------------------------------------------------

# Ogni pattern è (nome, regex, ordine dei gruppi). L'ordine dei tentativi
# è quello più probabile per un registratore vocale; ogni pattern deve
# produrre una data plausibile o viene scartato.
_FILENAME_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("YYYYMMDD_HHMMSS", re.compile(r"(?P<Y>\d{4})(?P<m>\d{2})(?P<d>\d{2})[ _T-]?(?P<H>\d{2})(?P<M>\d{2})(?P<S>\d{2})")),
    ("DD-MM-YYYY_HH-MM-SS", re.compile(r"(?P<d>\d{2})[-_.](?P<m>\d{2})[-_.](?P<Y>\d{4})[ _T]+(?P<H>\d{2})[-_.](?P<M>\d{2})[-_.](?P<S>\d{2})")),
    ("YYYY-MM-DD_HH-MM-SS", re.compile(r"(?P<Y>\d{4})[-_.](?P<m>\d{2})[-_.](?P<d>\d{2})[ _T]+(?P<H>\d{2})[-_.](?P<M>\d{2})[-_.](?P<S>\d{2})")),
    ("DDMMYYYY_HHMMSS", re.compile(r"(?P<d>\d{2})(?P<m>\d{2})(?P<Y>\d{4})[ _T-]?(?P<H>\d{2})(?P<M>\d{2})(?P<S>\d{2})")),
    ("data nel solo nome", re.compile(r"(?P<Y>\d{4})(?P<m>\d{2})(?P<d>\d{2})[ _T-]?(?P<H>\d{2})(?P<M>\d{2})(?P<S>\d{2})?")),
]


def parse_recording_time(filename: str) -> tuple[datetime | None, str]:
    """
    Ricava l'istante di registrazione dal nome del file.

    Returns:
        (datetime o None, nome del pattern usato). Il datetime è
        naive: è l'ora di pareggio del dispositivo, che può non
        coincidere con quella del Mac. Diventa un orario di pareggio
        solo, non un riferimento assoluto — ed è comunque infinitamente
        meglio di non averlo.
    """
    stem = Path(filename).stem
    for name, pattern in _FILENAME_PATTERNS:
        m = pattern.search(stem)
        if not m:
            continue
        groups = m.groupdict()
        try:
            dt = datetime(
                year=int(groups["Y"]),
                month=int(groups["m"]),
                day=int(groups["d"]),
                hour=int(groups["H"]),
                minute=int(groups["M"]),
                second=int(groups.get("S") or 0),
            )
        except (ValueError, TypeError):
            continue  # data inesistente (31 febbraio, ora 25): pattern sbagliato

        if not (2000 <= dt.year <= 2100):
            continue
        if not (0 <= dt.hour < 24):
            continue
        # I registratori economici hanno orologi che partono da zero:
        # senza questo controllo "00000101" passerebbe per una data.
        if dt.year < 2010:
            continue
        return dt, name
    return None, ""


def describe_filenames(files: list[Path], sample: int = 8) -> dict[str, Any]:
    """
    Riassume come sono fatti i nomi dei file: serve a capire se il
    parsing sta funzionando davvero o sta indovinando.
    """
    patterns: dict[str, int] = {}
    parsed, unparsed = [], []
    for f in files:
        dt, name = parse_recording_time(f.name)
        if dt:
            patterns[name] = patterns.get(name, 0) + 1
            parsed.append((f.name, dt))
        else:
            unparsed.append(f.name)

    out: dict[str, Any] = {
        "total": len(files),
        "parsed": len(parsed),
        "unparsed": len(unparsed),
        "unparsed_examples": unparsed[:sample],
        "patterns": patterns,
        "examples": [n for n, _ in parsed[:sample]],
    }
    if parsed:
        times = sorted(dt for _, dt in parsed)
        out["earliest"] = times[0].isoformat()
        out["latest"] = times[-1].isoformat()
        gaps = [
            (times[i + 1] - times[i]).total_seconds() / 3600
            for i in range(len(times) - 1)
        ]
        if gaps:
            out["median_gap_hours"] = round(sorted(gaps)[len(gaps) // 2], 2)
    return out
