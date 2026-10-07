"""
Scarico veloce dal registratore: copia, verifica, libera, espelli.

Il problema che risolve. Fino al 7 ottobre il registratore restava la
fonte di verita' finche' ogni file non era **trascritto**: `pull` leggeva
l'audio direttamente dal TileRec e lo cancellava solo a trascrizione
verificata. Diciotto file da un'ora sono ore di elaborazione, e per
tutte quelle ore il registratore doveva restare collegato — cioe' non
registrava. Quando lo si staccava prima, il lavoro si rompeva: il 5
ottobre `2026-10-05_09-39-09.MP3` e' sparito da `/Volumes/Untitled/RECORD`
a meta' elaborazione, l'archiviazione e' fallita con «No such file or
directory», e quella sessione oggi non ha piu' l'audio originale da
nessuna parte.

La regola nuova: **il registratore serve solo per il tempo della copia.**
Si copiano i file in una coda locale (`input/coda/`), si verifica ogni
copia, si cancella dal registratore solo cio' che e' verificato, lo si
espelle e si avvisa che si puo' staccare. La trascrizione avviene dopo,
dalla coda, quando il registratore e' gia' di nuovo al braccio.

Ordine di sicurezza, in una riga: copio (calcolando l'impronta mentre
leggo) → riscrivo su disco e rileggo la copia → le due impronte
coincidono → solo allora cancello dal registratore.

Perche' una sola lettura del registratore e non due. Rileggere il device
per confrontarlo raddoppierebbe il tempo in cui deve restare collegato,
che e' proprio la cosa da ridurre. La lettura USB e' gia' protetta da
CRC a livello di trasporto; il rischio reale e' la scrittura sul disco
locale o un'interruzione a meta', e quello lo copre la rilettura della
copia locale, che sul SSD del Mac costa una frazione di secondo.

I file troncati (registratore spento per batteria mentre scriveva, il
caso del 4 ottobre) si leggono finche' il filesystem risponde. La parte
leggibile va in coda e viene trascritta, ma l'originale **resta sul
registratore la prima volta**: un errore di lettura puo' anche essere un
cavo che si muove, e cancellare in quel caso perderebbe la parte che non
si e' riusciti a leggere. Se all'inserimento successivo lo stesso file
(stesso nome, dimensione, data di modifica) si ferma di nuovo allo
stesso byte con la stessa impronta, il troncamento e' del file e non del
cavo: a quel punto l'originale si cancella.

Tutto quello che succede finisce nel manifest (`logs/device_manifest.jsonl`)
con impronta ed esito, come per `pull`.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import logging
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.config import INPUT_DIR, LOGS_DIR  # noqa: E402

logger = logging.getLogger("audio-to-text.scarico")

# La coda locale: qui finiscono le copie verificate, e da qui la notte e
# le passate diurne le trascrivono. Una cartella sola, piatta, con i nomi
# originali del registratore: la pipeline ricava il nome della sessione
# dal nome del file, e cambiarlo romperebbe il legame con il corpus.
CODA_DIR = INPUT_DIR / "coda"

LOCK_PATH = LOGS_DIR / "scarico.lock"
SUFFISSO_PARZIALE = ".parziale"

BLOCCO = 4 << 20                # 4 MiB: pochi passaggi per un file da 57 MB
BLOCCO_MINIMO = 1 << 16         # 64 KiB: sotto, il recupero non vale piu'
MIN_BYTES_UTILI = 1 << 19       # sotto 512 KiB un troncato non e' una registrazione


def _ora() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


def sha256_file(path: Path, blocco: int = BLOCCO) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while b := f.read(blocco):
            h.update(b)
    return h.hexdigest()


# ----------------------------------------------------------------------
# Copia di un file
# ----------------------------------------------------------------------

@dataclass
class Copia:
    """L'esito della copia di un file dal registratore."""

    sorgente: Path
    dest: Path | None
    sha256: str | None
    byte: int
    troncato: bool = False
    motivo: str = ""

    @property
    def ok(self) -> bool:
        return self.dest is not None and self.sha256 is not None


def copia_verificata(sorgente: Path, cartella: Path, nome: str | None = None) -> Copia:
    """Copia `sorgente` in `cartella`, calcolando l'impronta mentre legge.

    La copia si scrive con un nome temporaneo (`.<nome>.parziale`), si
    forza su disco, si rinomina, e poi si rilegge per confrontarne
    l'impronta con quella calcolata durante la lettura. Se il registratore
    smette di rispondere a meta', si riprova con blocchi piu' piccoli fino
    a 64 KiB e si tiene il pezzo leggibile, marcato `troncato`.

    Una copia che non si riesce a verificare viene cancellata: in coda
    entra solo cio' che e' uguale a quello che si e' letto.
    """
    cartella.mkdir(parents=True, exist_ok=True)
    nome = nome or sorgente.name
    tmp = cartella / f".{nome}{SUFFISSO_PARZIALE}"
    dest = cartella / nome
    h = hashlib.sha256()
    scritti = 0
    troncato = False
    motivo = "letto per intero"
    blocco = BLOCCO
    try:
        with open(sorgente, "rb") as src, open(tmp, "wb") as out:
            while True:
                try:
                    b = src.read(blocco)
                except OSError as exc:
                    if blocco <= BLOCCO_MINIMO:
                        troncato = True
                        motivo = (f"lettura interrotta a {scritti} byte: "
                                  f"{exc.strerror or exc}")
                        break
                    blocco = max(BLOCCO_MINIMO, blocco // 4)
                    try:
                        src.seek(scritti)
                    except OSError:
                        troncato = True
                        motivo = f"registratore non risponde a {scritti} byte"
                        break
                    continue
                if not b:
                    break
                h.update(b)
                out.write(b)
                scritti += len(b)
            out.flush()
            os.fsync(out.fileno())
    except OSError as exc:
        tmp.unlink(missing_ok=True)
        return Copia(sorgente, None, None, scritti, motivo=f"copia fallita: {exc.strerror or exc}")

    if troncato and scritti < MIN_BYTES_UTILI:
        tmp.unlink(missing_ok=True)
        return Copia(sorgente, None, None, scritti, troncato=True,
                     motivo=f"{motivo}; troppo poco per essere audio")

    impronta = h.hexdigest()
    try:
        riletta = sha256_file(tmp)
    except OSError as exc:
        tmp.unlink(missing_ok=True)
        return Copia(sorgente, None, None, scritti, troncato,
                     motivo=f"copia locale illeggibile: {exc.strerror or exc}")
    if riletta != impronta:
        tmp.unlink(missing_ok=True)
        return Copia(sorgente, None, None, scritti, troncato,
                     motivo="la copia su disco non coincide con quanto letto")
    tmp.replace(dest)
    return Copia(sorgente, dest, impronta, scritti, troncato, motivo)


# ----------------------------------------------------------------------
# Manifest
# ----------------------------------------------------------------------

def _leggi_manifest(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    for riga in path.read_text(encoding="utf-8").splitlines():
        try:
            out.append(json.loads(riga))
        except json.JSONDecodeError:
            continue
    return out


def _scrivi_manifest(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def _firma(f: Path) -> dict:
    st = f.stat()
    return {"file": f.name, "size": st.st_size, "mtime": int(st.st_mtime)}


# ----------------------------------------------------------------------
# Scarico di un registratore intero
# ----------------------------------------------------------------------

@dataclass
class Esito:
    copiati: int = 0
    gia_presenti: int = 0
    cancellati: int = 0
    lasciati: list[str] = field(default_factory=list)   # restano sul registratore
    troncati: list[str] = field(default_factory=list)
    spazzatura: list[str] = field(default_factory=list)
    errori: list[str] = field(default_factory=list)
    byte: int = 0
    secondi_audio: float = 0.0
    durata: float = 0.0
    interrotto: bool = False

    @property
    def tutto_a_posto(self) -> bool:
        return not self.errori and not self.interrotto and not self.lasciati


def scarica(
    file: Iterable[Path],
    coda: Path = CODA_DIR,
    manifest: Path | None = None,
    etichetta: str = "",
    cancella: bool = True,
    dry_run: bool = False,
    gia_elaborati: set[str] | None = None,
    non_audio: Callable[[Path], bool] | None = None,
    durata_audio: Callable[[Path], float | None] | None = None,
) -> Esito:
    """Copia ogni file in coda, verifica, e libera il registratore.

    `gia_elaborati` sono le impronte gia' trascritte e cancellate in
    passato (dal manifest): se il registratore ripresenta lo stesso file,
    non si rimette in coda, si libera solo lo spazio. `non_audio` e
    `durata_audio` sono iniettati per i test; di default usano ffprobe.
    """
    if manifest is None:
        manifest = LOGS_DIR / "device_manifest.jsonl"
    if non_audio is None:
        from sync_device import _is_not_audio as non_audio  # noqa: N813
    if durata_audio is None:
        from core.media import probe_duration as durata_audio
    gia_elaborati = set(gia_elaborati or ())

    t0 = time.time()
    esito = Esito()
    storia = _leggi_manifest(manifest)
    lasciati_prima = {(r.get("file"), r.get("size"), r.get("mtime")): r
                      for r in storia if r.get("action") in ("copiato_lasciato",
                                                             "troncato_salvato")}
    coda.mkdir(parents=True, exist_ok=True)
    for vecchio in coda.glob(f".*{SUFFISSO_PARZIALE}"):
        vecchio.unlink(missing_ok=True)      # resti di una copia interrotta

    def registra(f: Path, firma: dict, **campi) -> None:
        _scrivi_manifest(manifest, {"ts": _ora(), "device": etichetta,
                                    **firma, **campi})

    def libera(f: Path, firma: dict, sha: str | None, perche: str) -> bool:
        if not cancella:
            esito.lasciati.append(f.name)
            registra(f, firma, sha256=sha, action="copiato_lasciato",
                     reason=f"{perche}; cancellazione disattivata")
            return False
        try:
            f.unlink()
            # macOS lascia accanto ai file di un volume exFAT un «._nome»
            # con gli attributi estesi: senza l'originale non serve a niente.
            (f.parent / f"._{f.name}").unlink(missing_ok=True)
        except OSError as exc:
            esito.lasciati.append(f.name)
            registra(f, firma, sha256=sha, action="copiato_lasciato",
                     reason=f"{perche}; non cancellato: {exc.strerror or exc}")
            return False
        esito.cancellati += 1
        registra(f, firma, sha256=sha, action="copiato", reason=perche)
        return True

    for f in file:
        try:
            firma = _firma(f)
        except OSError as exc:
            # Il registratore e' sparito (staccato a meta'): ci si ferma,
            # quello che e' gia' in coda e' al sicuro e il resto aspetta il
            # prossimo inserimento.
            esito.interrotto = True
            esito.errori.append(f"{f.name}: {exc.strerror or exc}")
            break

        if dry_run:
            logger.info("[dry-run] copierei %s (%.1f MB)", f.name, firma["size"] / 1e6)
            continue

        # Gia' copiato in un inserimento precedente, ma non cancellato
        # (per esempio staccato subito dopo la copia): la firma coincide,
        # non serve rileggerlo per liberare lo spazio.
        # Vale solo se la copia c'e' ancora (in coda) o e' gia' stata
        # trascritta: altrimenti si ricopia.
        prima = lasciati_prima.get((firma["file"], firma["size"], firma["mtime"]))
        if prima and prima.get("action") == "copiato_lasciato":
            in_coda = (coda / f.name).exists()
            trascritto = prima.get("sha256") in gia_elaborati
            if in_coda or trascritto:
                esito.gia_presenti += 1
                libera(f, firma, prima.get("sha256"),
                       "gia' copiato in coda" if in_coda else "gia' trascritto")
                continue

        copia = copia_verificata(f, coda)
        if not copia.ok:
            if copia.troncato:
                esito.troncati.append(f.name)
            esito.errori.append(f"{f.name}: {copia.motivo}")
            esito.lasciati.append(f.name)
            registra(f, firma, sha256=None, action="kept",
                     reason=f"scarico: {copia.motivo}")
            if not f.parent.exists():
                esito.interrotto = True
                break
            continue

        esito.byte += copia.byte

        # Un file gia' trascritto in passato: la copia non serve.
        if copia.sha256 in gia_elaborati:
            copia.dest.unlink(missing_ok=True)
            esito.gia_presenti += 1
            libera(f, firma, copia.sha256, "gia' trascritto in passato")
            continue

        if non_audio(copia.dest):
            copia.dest.unlink(missing_ok=True)
            esito.spazzatura.append(f.name)
            registra(f, firma, sha256=copia.sha256, action="junk",
                     reason="ffprobe: nessun audio decodificabile, lasciato sul registratore")
            continue

        esito.copiati += 1
        esito.secondi_audio += float(durata_audio(copia.dest) or 0.0)

        if copia.troncato:
            esito.troncati.append(f.name)
            gia_visto = prima and prima.get("action") == "troncato_salvato" \
                and prima.get("sha256") == copia.sha256
            if gia_visto:
                libera(f, firma, copia.sha256,
                       f"troncato, confermato al secondo inserimento ({copia.motivo})")
            else:
                esito.lasciati.append(f.name)
                registra(f, firma, sha256=copia.sha256, action="troncato_salvato",
                         reason=f"{copia.motivo}; parte leggibile in coda, "
                                "originale lasciato fino al prossimo inserimento")
            continue

        libera(f, firma, copia.sha256, "copiato e verificato")

    esito.durata = time.time() - t0
    return esito


# ----------------------------------------------------------------------
# Attesa che il volume sia fermo
# ----------------------------------------------------------------------

def _istantanea(file: Iterable[Path]) -> dict[str, int]:
    out = {}
    for f in file:
        try:
            out[f.name] = f.stat().st_size
        except OSError:
            out[f.name] = -1
    return out


def attendi_volume_fermo(elenca: Callable[[], list[Path]], intervallo: float = 2.0,
                         massimo: float = 30.0,
                         dormi: Callable[[float], None] = time.sleep) -> list[Path]:
    """Restituisce i file quando elenco e dimensioni smettono di cambiare.

    Trovato nella prova del 7 ottobre con un registratore finto: launchd
    lancia lo scarico nell'istante del montaggio, e se qualcuno (o
    qualcosa) sta ancora scrivendo sul volume, si copiano file a meta'.
    Il TileRec quando si monta ha gia' chiuso i suoi file, quindi di
    solito questa attesa costa un solo intervallo; ma una copia di un file
    incompleto seguita dalla cancellazione dell'originale sarebbe una
    perdita, e due secondi sono un prezzo piccolo.
    """
    prima = _istantanea(elenca())
    atteso = 0.0
    while atteso < massimo:
        dormi(intervallo)
        atteso += intervallo
        file = elenca()
        ora = _istantanea(file)
        if ora == prima:
            return file
        prima = ora
    logger.warning("Il volume cambia ancora dopo %.0f s: copio lo stesso", massimo)
    return elenca()


# ----------------------------------------------------------------------
# Lucchetto, espulsione, notifica
# ----------------------------------------------------------------------

class Lucchetto:
    """Un solo scarico alla volta.

    launchd lancia lo scarico a ogni volume montato, e un inserimento puo'
    produrre piu' montaggi a pochi secondi di distanza. Due scarichi in
    parallelo leggerebbero gli stessi file e si contenderebbero la
    cancellazione.
    """

    def __init__(self, path: Path = LOCK_PATH) -> None:
        self.path = path
        self._fd: int | None = None

    def prendi(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            os.close(self._fd)
            self._fd = None
            return False

    def lascia(self) -> None:
        if self._fd is not None:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
            os.close(self._fd)
            self._fd = None


def espelli(volume: Path, tentativi: int = 3) -> tuple[bool, str]:
    """Espelle il volume con `diskutil`, riprovando se e' ancora occupato.

    Subito dopo la copia Spotlight o il Finder possono tenere aperto il
    volume per qualche secondo: un rifiuto immediato non vuol dire che
    l'espulsione sia impossibile.
    """
    diskutil = shutil.which("diskutil")
    if not diskutil:
        return False, "diskutil non disponibile (non e' un Mac?)"
    ultimo = ""
    for i in range(tentativi):
        r = subprocess.run([diskutil, "eject", str(volume)],
                           capture_output=True, text=True)
        if r.returncode == 0:
            return True, r.stdout.strip()
        ultimo = (r.stderr or r.stdout).strip()
        time.sleep(2 + 2 * i)
    return False, ultimo


def notifica(titolo: str, testo: str, suono: str | None = "Glass") -> None:
    """Notifica di macOS. Se non si puo', resta solo il log."""
    osascript = shutil.which("osascript")
    if not osascript:
        return

    def q(s: str) -> str:
        return s.replace("\\", "\\\\").replace('"', '\\"')

    script = f'display notification "{q(testo)}" with title "{q(titolo)}"'
    if suono:
        script += f' sound name "{q(suono)}"'
    try:
        subprocess.run([osascript, "-e", script], capture_output=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        pass


def descrivi(esito: Esito) -> tuple[str, str]:
    """Titolo e testo della notifica, in una riga che si legge al volo."""
    ore = esito.secondi_audio / 3600
    base = (f"{esito.copiati} file ({ore:.1f} h di audio) copiati in "
            f"{esito.durata:.0f} s")
    if esito.interrotto:
        return ("TileRec staccato durante la copia",
                f"{base}. Il resto è ancora sul registratore: reinseriscilo.")
    if esito.errori:
        return ("TileRec: copia con problemi",
                f"{base}. {len(esito.errori)} file restano sul registratore "
                "(vedi logs/scarico.log).")
    if esito.lasciati:
        return ("TileRec copiato, puoi staccarlo",
                f"{base}. {len(esito.lasciati)} file lasciati sul registratore "
                "(troncati o non cancellabili).")
    if esito.copiati == 0 and esito.gia_presenti == 0:
        return ("TileRec: niente di nuovo", "Nessuna registrazione da copiare.")
    return ("TileRec copiato, puoi staccarlo", base + ".")
