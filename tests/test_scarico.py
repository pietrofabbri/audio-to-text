"""
Test dello scarico veloce dal registratore.

    python tests/test_scarico.py

Niente modelli, niente audio vero, niente registratore: il «registratore»
e' una cartella temporanea, e i controlli che di solito fa ffprobe
(spazzatura, durata) sono iniettati. Gira ovunque, anche fuori dal Mac.

Cosa si protegge, in ordine di quanto costerebbe sbagliare.

  1. **Non si cancella mai dal registratore cio' che non e' in coda,
     identico byte per byte.** E' l'unica operazione irreversibile.
  2. Un file troncato non si cancella al primo inserimento: l'errore di
     lettura potrebbe essere il cavo, non il file.
  3. Un registratore staccato a meta' lascia la coda coerente: quello che
     e' copiato e' intero, il resto e' ancora sul registratore.
  4. Un file gia' trascritto in passato non torna in coda.
  5. La spazzatura del registratore non entra in coda e non si cancella.
"""

from __future__ import annotations

import io
import json
import sys
import tempfile
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

import core.scarico as sc  # noqa: E402


class Failure(Exception):
    pass


def require(cond: bool, msg: str) -> None:
    if not cond:
        raise Failure(msg)


def _registratore(base: Path, n: int = 3, size: int = 700_000) -> list[Path]:
    rec = base / "Untitled" / "RECORD"
    rec.mkdir(parents=True)
    out = []
    for i in range(n):
        f = rec / f"2026-10-08_{9 + i:02d}-00-00.MP3"
        f.write_bytes(bytes((i * 7 + j) % 251 for j in range(size)))
        out.append(f)
    return out


def _scarica(files, base: Path, **kw) -> sc.Esito:
    kw.setdefault("non_audio", lambda p: False)
    kw.setdefault("durata_audio", lambda p: 3600.0)
    return sc.scarica(files, coda=base / "coda", manifest=base / "manifest.jsonl",
                      etichetta="/Volumes/Untitled", **kw)


def _manifest(base: Path) -> list[dict]:
    p = base / "manifest.jsonl"
    return [json.loads(r) for r in p.read_text().splitlines()] if p.exists() else []


# ----------------------------------------------------------------------

def copia_verifica_e_libera() -> None:
    """Il caso normale: tutto in coda, identico, e il registratore vuoto."""
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        files = _registratore(base)
        originali = {f.name: f.read_bytes() for f in files}
        e = _scarica(files, base)
        require(e.copiati == 3 and e.cancellati == 3, f"3 copiati e cancellati: {e}")
        require(e.tutto_a_posto, f"nessun problema atteso: {e}")
        for nome, dati in originali.items():
            require((base / "coda" / nome).read_bytes() == dati,
                    f"{nome}: la copia deve essere identica")
        require(not any(f.exists() for f in files), "il registratore deve essere vuoto")
        azioni = [r["action"] for r in _manifest(base)]
        require(azioni == ["copiato"] * 3, f"manifest: {azioni}")
        require(all(r.get("sha256") for r in _manifest(base)),
                "ogni cancellazione deve avere l'impronta nel manifest")
        require(abs(e.secondi_audio - 3 * 3600) < 1, "le ore di audio si sommano")
        require(not list((base / "coda").glob(".*")), "nessun file temporaneo rimasto")


def senza_cancellare_poi_libera_senza_ricopiare() -> None:
    """--no-delete, poi un inserimento normale: si libera senza rileggere."""
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        files = _registratore(base)
        e1 = _scarica(files, base, cancella=False)
        require(e1.copiati == 3 and e1.cancellati == 0 and len(e1.lasciati) == 3,
                f"primo giro senza cancellare: {e1}")
        require(all(f.exists() for f in files), "i file restano sul registratore")

        with mock.patch.object(sc, "copia_verificata",
                               side_effect=AssertionError("non doveva ricopiare")):
            e2 = _scarica(files, base)
        require(e2.gia_presenti == 3 and e2.cancellati == 3,
                f"secondo giro: libera senza ricopiare: {e2}")
        require(not any(f.exists() for f in files), "ora il registratore e' vuoto")


def se_la_copia_sparisce_si_ricopia() -> None:
    """Copiato e lasciato, ma la copia in coda non c'e' piu': si ricopia."""
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        files = _registratore(base, n=1)
        _scarica(files, base, cancella=False)
        (base / "coda" / files[0].name).unlink()
        e = _scarica(files, base)
        require(e.copiati == 1 and e.gia_presenti == 0 and e.cancellati == 1,
                f"senza la copia si deve ricopiare prima di cancellare: {e}")
        require((base / "coda" / files[0].name).exists(), "la copia e' tornata")


def gia_trascritto_non_torna_in_coda() -> None:
    """Un'impronta gia' elaborata: niente coda, solo spazio liberato."""
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        files = _registratore(base, n=2)
        impronta = sc.sha256_file(files[0])
        e = _scarica(files, base, gia_elaborati={impronta})
        require(e.copiati == 1 and e.gia_presenti == 1 and e.cancellati == 2,
                f"uno nuovo e uno gia' trascritto: {e}")
        require(not (base / "coda" / files[0].name).exists(),
                "il file gia' trascritto non deve tornare in coda")


def la_spazzatura_resta_sul_registratore() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        files = _registratore(base, n=2)
        e = _scarica(files, base, non_audio=lambda p: p.name == files[1].name)
        require(e.copiati == 1 and e.spazzatura == [files[1].name],
                f"uno buono e uno spazzatura: {e}")
        require(files[1].exists(), "la spazzatura non si cancella")
        require(not (base / "coda" / files[1].name).exists(),
                "la spazzatura non entra in coda")


class _Lettore(io.RawIOBase):
    """Un file del registratore che smette di rispondere dopo `limite` byte."""

    def __init__(self, dati: bytes, limite: int) -> None:
        self.dati, self.limite, self.pos = dati, limite, 0

    def readable(self) -> bool:
        return True

    def read(self, n: int = -1) -> bytes:
        if self.pos >= len(self.dati):
            return b""
        if self.pos + n > self.limite:
            raise OSError(5, "Input/output error")
        b = self.dati[self.pos:self.pos + n]
        self.pos += len(b)
        return b

    def seek(self, pos: int, whence: int = 0) -> int:
        self.pos = pos
        return pos


def _apri_troncato(nome: str, limite: int):
    vero_open = open

    def finto(path, mode="r", *a, **k):
        if Path(path).name == nome and "r" in mode and "b" in mode:
            return _Lettore(Path(path).read_bytes(), limite)
        return vero_open(path, mode, *a, **k)
    return finto


def troncato_si_cancella_solo_al_secondo_inserimento() -> None:
    """La parte leggibile va in coda; l'originale resta finche' non si conferma."""
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        files = _registratore(base, n=1, size=3_000_000)
        f = files[0]
        limite = 2_000_000
        with mock.patch("builtins.open", _apri_troncato(f.name, limite)):
            e1 = _scarica(files, base)
        copia = base / "coda" / f.name
        require(e1.copiati == 1 and e1.troncati == [f.name], f"primo inserimento: {e1}")
        require(f.exists(), "al primo inserimento il troncato resta sul registratore")
        require(copia.exists() and limite - (1 << 16) <= copia.stat().st_size <= limite,
                f"in coda la parte leggibile: {copia.stat().st_size} byte")
        require(copia.read_bytes() == f.read_bytes()[:copia.stat().st_size],
                "la parte leggibile e' identica all'inizio dell'originale")

        with mock.patch("builtins.open", _apri_troncato(f.name, limite)):
            e2 = _scarica(files, base)
        require(e2.cancellati == 1 and not f.exists(),
                f"stesso troncamento al secondo inserimento: si cancella ({e2})")


def troncato_diverso_non_si_cancella() -> None:
    """Se al secondo inserimento si legge di piu', era il cavo: non si cancella."""
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        files = _registratore(base, n=1, size=3_000_000)
        f = files[0]
        with mock.patch("builtins.open", _apri_troncato(f.name, 1_000_000)):
            _scarica(files, base)
        with mock.patch("builtins.open", _apri_troncato(f.name, 2_000_000)):
            e2 = _scarica(files, base)
        require(f.exists(), f"impronte diverse: l'originale resta ({e2})")


def registratore_staccato_a_meta() -> None:
    """Il terzo file non c'e' piu': ci si ferma, i primi due sono salvi."""
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        files = _registratore(base)
        files[2].unlink()
        e = _scarica(files, base)
        require(e.interrotto, f"lo scarico deve dirsi interrotto: {e}")
        require(e.copiati == 2 and e.cancellati == 2, f"i primi due copiati: {e}")
        titolo, testo = sc.descrivi(e)
        require("staccato" in titolo and "reinseriscilo" in testo,
                f"la notifica deve dirlo: {titolo} / {testo}")


def resti_di_una_copia_interrotta() -> None:
    """Un .parziale lasciato da un crash si pulisce e non entra mai in coda."""
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        coda = base / "coda"
        coda.mkdir()
        resto = coda / ".2026-10-08_09-00-00.MP3.parziale"
        resto.write_bytes(b"x" * 100)
        _scarica(_registratore(base, n=1), base)
        require(not resto.exists(), "il resto di una copia interrotta va tolto")


def un_solo_scarico_alla_volta() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        p = Path(tmp) / "scarico.lock"
        a, b = sc.Lucchetto(p), sc.Lucchetto(p)
        require(a.prendi(), "il primo prende il lucchetto")
        require(not b.prendi(), "il secondo deve trovare il lucchetto occupato")
        a.lascia()
        require(b.prendi(), "liberato, il secondo lo prende")
        b.lascia()


def registrazione_gia_trascritta_va_in_archivio() -> None:
    """Stesso nome di una sessione gia' trascritta: archivio, non coda.

    Il caso vero dell'8 ottobre: `2026-10-05_09-39-09.MP3` era rimasto sul
    TileRec dal 5, con un'impronta diversa da quella del manifest. Senza
    questo controllo la passata diurna l'ha cominciato a trascrivere come
    sessione duplicata `2026-10-05_09-39-09-237002`.
    """
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        files = _registratore(base, n=2)
        gia = {files[0].stem}
        e = _scarica(files, base, gia_trascritti=gia, archivio=base / "archive")
        require(e.copiati == 1 and e.gia_presenti == 1 and e.cancellati == 2,
                f"uno nuovo, uno gia' trascritto: {e}")
        require(not (base / "coda" / files[0].name).exists(),
                "la registrazione gia' trascritta non entra in coda")
        require((base / "archive" / files[0].name).exists(),
                "va in archivio: torna disponibile l'audio originale")


def troncato_gia_trascritto_resta_sul_registratore() -> None:
    """Una copia parziale di una registrazione gia' trascritta si scarta."""
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        files = _registratore(base, n=1, size=3_000_000)
        f = files[0]
        with mock.patch("builtins.open", _apri_troncato(f.name, 2_000_000)):
            e = _scarica(files, base, gia_trascritti={f.stem}, archivio=base / "archive")
        require(f.exists() and not (base / "coda" / f.name).exists()
                and not (base / "archive" / f.name).exists(),
                f"resta sul registratore, niente in coda ne' in archivio: {e}")


def aspetta_che_il_volume_sia_fermo() -> None:
    """Un file che cresce ancora non si copia finche' non si ferma."""
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        f = base / "2026-10-08_09-00-00.MP3"
        f.write_bytes(b"x" * 100)
        passi = []

        def dormi(sec):
            passi.append(sec)
            if len(passi) <= 2:                  # cresce per due intervalli
                with open(f, "ab") as h:
                    h.write(b"x" * 100)

        out = sc.attendi_volume_fermo(lambda: [f], intervallo=0.0, dormi=dormi)
        require(out == [f] and len(passi) == 3,
                f"deve attendere finche' la dimensione e' ferma: {len(passi)} attese")
        require(f.stat().st_size == 300, "il file e' quello finale")


def libera_anche_il_file_appledouble() -> None:
    """Cancellato l'originale, il «._nome» di macOS non deve restare."""
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        files = _registratore(base, n=1)
        doppio = files[0].parent / f"._{files[0].name}"
        doppio.write_bytes(b"\0" * 4096)
        _scarica(files, base)
        require(not doppio.exists(), "il ._ dell'originale cancellato va tolto")


def notifica_quando_tutto_va_bene() -> None:
    e = sc.Esito(copiati=12, cancellati=12, secondi_audio=12 * 3600, durata=95)
    titolo, testo = sc.descrivi(e)
    require(titolo == "TileRec copiato, puoi staccarlo", f"titolo: {titolo}")
    require("12 file" in testo and "12.0 h" in testo, f"testo: {testo}")


CHECKS = [
    ("copia, verifica e libera", copia_verifica_e_libera),
    ("senza cancellare, poi libera senza ricopiare",
     senza_cancellare_poi_libera_senza_ricopiare),
    ("se la copia in coda e' sparita si ricopia", se_la_copia_sparisce_si_ricopia),
    ("un file gia' trascritto non torna in coda", gia_trascritto_non_torna_in_coda),
    ("la spazzatura resta sul registratore", la_spazzatura_resta_sul_registratore),
    ("un troncato si cancella solo al secondo inserimento",
     troncato_si_cancella_solo_al_secondo_inserimento),
    ("un troncato diverso non si cancella", troncato_diverso_non_si_cancella),
    ("registratore staccato a meta'", registratore_staccato_a_meta),
    ("i resti di una copia interrotta si puliscono", resti_di_una_copia_interrotta),
    ("un solo scarico alla volta", un_solo_scarico_alla_volta),
    ("una registrazione gia' trascritta va in archivio",
     registrazione_gia_trascritta_va_in_archivio),
    ("un troncato gia' trascritto resta sul registratore",
     troncato_gia_trascritto_resta_sul_registratore),
    ("si aspetta che il volume sia fermo", aspetta_che_il_volume_sia_fermo),
    ("si toglie anche il ._ di macOS", libera_anche_il_file_appledouble),
    ("la notifica quando tutto va bene", notifica_quando_tutto_va_bene),
]


def main() -> int:
    passed = 0
    failed: list[str] = []
    for name, fn in CHECKS:
        try:
            fn()
            passed += 1
            print(f"  ok  {name}")
        except Failure as exc:
            failed.append(f"{name}: {exc}")
            print(f"  KO  {name} - {exc}")
        except Exception as exc:  # noqa: BLE001
            failed.append(f"{name}: {type(exc).__name__}: {exc}")
            print(f"  ERR {name} - {type(exc).__name__}: {exc}")
    print(f"\n{passed}/{len(CHECKS)} superati")
    if failed:
        print("Falliti:")
        for f in failed:
            print(f"  - {f}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
