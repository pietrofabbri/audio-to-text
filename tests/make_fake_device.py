"""
Genera un registratore finto, per provare tutta la catena senza il
registratore vero.

    python tests/make_fake_device.py --out /tmp/fake-recorder --count 3

L'audio non è un tono: è voce vera, sintetizzata con `say` in italiano e
poi convertita in mp3. Un beep non attraversa VAD, diarizzazione e prosodia
come una voce, quindi un test fatto di beep passerebbe senza provare
niente di ciò che la notte dovrà fare. Qui l'ASR deve trovare parole
vere, altrimenti la verifica che autorizza la cancellazione scatta e il
file resta sul device — ed è esattamente il comportamento giusto.

Cosa contiene, per default:

- N file da `--minutes` minuti, con un nome che contiene data e ora;
- rumore di fondo, perché una registrazione non è mai silenzio puro;
- una cartella vuota e un file non audio, che il rilevamento deve ignorare;
- i casi patologici in `--edge`: nomi senza data, file da 0 byte, doppioni.

`--edge` è separato perché quei file sono rumore deliberato: in una prova
del percorso felice non devono esserci, altrimenti il test misura la
robustezza invece che il funzionamento.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.device import AUDIO_EXTENSIONS  # noqa: E402

# Testi italiani con frasi diverse: una trascrizione tutta uguale
# maschererebbe un errore di segmentazione che un testo vario mostrerebbe.
PHRASES = [
    "Buongiorno, oggi parliamo di come organizzare la giornata di lavoro.",
    "Il progetto è andato avanti bene, mancano solo un paio di dettagli.",
    "Ricordati di mandare il documento prima delle sei di sera.",
    "Poi andiamo a casa, passeggiando lungo il fiume.",
    "Questa è la seconda parte dell'intervista, la prima l'abbiamo già letta.",
    "Secondo me conviene aspettare qualche giorno prima di decidere.",
]


def _have(binary: str) -> bool:
    return shutil.which(binary) is not None


def _say_to_mp3(text: str, dest: Path, voice: str, noise_db: float) -> bool:
    """Una frase in mp3, con rumore di fondo.

    `say` è la voce di sistema: esiste su ogni Mac, non richiede installa
    niente e produce italiano comprensibile dall'ASR. ffprobe ne ricava
    la durata, che serve per costruire file da N minuti.
    """
    aiff = dest.with_suffix(".aiff")
    try:
        subprocess.run(
            ["say", "-v", voice, "-o", str(aiff), text],
            check=True, capture_output=True, timeout=120,
        )
    except (subprocess.SubprocessError, OSError):
        return False

    cmd = ["ffmpeg", "-y", "-loglevel", "error", "-i", str(aiff)]
    if noise_db:
        # anoisesrc e' un sorgente, non un filtro: va dato come secondo
        # input. E vuole dB positivi, quindi l'attenuazione la fa un
        # 'volume' esplicito — piu' leggibile di un segno negativo che
        # ffmpeg rifiuta.
        level = 10 ** (noise_db / 20.0)
        cmd += [
            "-f", "lavfi", "-i", "anoisesrc=c=pink:r=44100",
            "-filter_complex",
            f"[1:a]volume={level:.5f}[n];[0:a][n]"
            "amix=inputs=2:duration=first:normalize=0",
        ]
    cmd += ["-ac", "1", "-ar", "44100", "-b:a", "64k", str(dest)]
    try:
        subprocess.run(cmd, check=True, capture_output=True, timeout=300)
    except (subprocess.SubprocessError, OSError):
        return False
    finally:
        # Dopo ffmpeg, non prima: l'aiff e' il suo input.
        aiff.unlink(missing_ok=True)
    return dest.exists() and dest.stat().st_size > 1024


def _probe_sec(path: Path) -> float:
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "quiet", "-show_entries", "format=duration",
             "-of", "csv=p=0", str(path)],
            capture_output=True, text=True, timeout=60,
        ).stdout.strip()
        return float(out)
    except (ValueError, OSError, subprocess.SubprocessError):
        return 0.0


def _pad_to_minutes(src: Path, dest: Path, minutes: float) -> None:
    """Allunga un file breve alla durata richiesta, con silenzio in coda.

    Non si allunga ripetendo l'audio: un file da un'ora in cui la stessa
    frase si ripete 200 volte produrrebbe una coda di segmenti identici
    e falserebbe sia la verifica di qualità sia la misura del rapporto di
    parlato. Il silenzio in coda è invece esattamente ciò che produce una
    registrazione vera con lunghe pause.
    """
    want = minutes * 60.0
    have = _probe_sec(src)
    if have <= 0:
        return
    if abs(have - want) < 1.0:
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)
        return
    if have < want:
        pad = want - have
        cmd = ["ffmpeg", "-y", "-loglevel", "error", "-i", str(src),
               "-af", f"apad=pad_dur={pad}", "-t", str(want), str(dest)]
    else:
        # File più lungo del richiesto: si taglia, non si comprime.
        cmd = ["ffmpeg", "-y", "-loglevel", "error", "-i", str(src),
               "-t", str(want), str(dest)]
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        subprocess.run(cmd, check=True, capture_output=True, timeout=900)
    except (subprocess.SubprocessError, OSError):
        shutil.copy2(src, dest)


def build(out: Path, count: int, minutes: float, start: datetime,
          voice: str, noise_db: float, edge: bool,
          name_pattern: str) -> list[Path]:
    """Crea il device finto e restituisce i file audio creati."""
    record = out / "record"
    record.mkdir(parents=True, exist_ok=True)

    made: list[Path] = []
    for i in range(count):
        text = PHRASES[i % len(PHRASES)]
        tmp = out / f"_tmp_{i}.mp3"
        if not _say_to_mp3(text, tmp, voice, noise_db):
            raise SystemExit(
                "Generazione audio fallita: servono `say` (macOS) e ffmpeg."
            )
        stamp = start + timedelta(hours=1, minutes=7 * i)
        name = stamp.strftime(name_pattern)
        final = record / name
        _pad_to_minutes(tmp, final, minutes)
        tmp.unlink(missing_ok=True)
        if final.exists():
            made.append(final)

    # --- rumore che il rilevamento deve ignorare ------------------------
    (record / "notes.txt").write_text("non sono audio\n", encoding="utf-8")
    (out / "System").mkdir(exist_ok=True)
    (out / "System" / "config.bin").write_bytes(b"\x00" * 2048)

    if edge:
        # casi patologici: devono essere RESPINTI, non elaborati
        (record / "untitled.mp3").write_bytes(b"ID3" + b"\x00" * 4096)
        (record / "00000001_000000.MP3").write_bytes(b"ID3" + b"\x00" * 4096)
        (record / "REC_99999932_999999.mp3").write_bytes(b"ID3" + b"\x00" * 4096)
        (record / "vuoto.mp3").write_bytes(b"")          # 0 byte

        # Due file con lo STESSO orario ma audio DIVERSO: e' la collisione
        # di cui soffrirebbe un registratore con l'orologio non impostato.
        # Devono essere due registrazioni distinte e venire entrambe
        # trascritte, in due cartelle diverse.
        #
        # La differenza di contenuto e' essenziale: due copie identiche
        # hanno lo stesso hash, e la deduplicazione le confonderebbe —
        # verrebbero trattate come lo stesso file, che e' un altro caso,
        # giusto ma non quello che qui si vuole provare.
        if made:
            other = PHRASES[-1]
            tmp = out / "_tmp_twin.mp3"
            if _say_to_mp3(other, tmp, voice, noise_db):
                _pad_to_minutes(tmp, record / (made[0].stem + "-alt.mp3"), minutes)
            tmp.unlink(missing_ok=True)

        # E una copia identica: stesso hash, deve essere riconosciuta come
        # duplicato e non trascritta due volte.
        if made:
            shutil.copy2(made[0], record / (made[0].stem + "-dup.mp3"))

    return made


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--out", default="/tmp/fake-recorder",
                    help="dove creare il device finto")
    ap.add_argument("--count", type=int, default=3, help="numero di file da creare")
    ap.add_argument("--minutes", type=float, default=1.0,
                    help="durata di ogni file (1.0 = un minuto, non un'ora: "
                         "un test deve stare in minuti, non in ore)")
    ap.add_argument("--voice", default="Alice")
    ap.add_argument("--noise-db", type=float, default=-25.0,
                    help="0 per silenzio totale")
    ap.add_argument("--edge", action="store_true",
                    help="aggiunge i casi patologici che devono essere respinti")
    ap.add_argument("--name-pattern", default="REC_%Y%m%d_%H%M%S.mp3",
                    help="formato del nome; i pattern sono quelli che il "
                         "rilevamento riconosce")
    ap.add_argument("--start", default="2026-10-03T20:00:00",
                    help="inizio delle registrazioni (ISO)")
    ap.add_argument("--clean", action="store_true", help="cancella e ricrea")
    args = ap.parse_args()

    if not _have("say") or not _have("ffmpeg"):
        print("Servono `say` e ffmpeg. ffmpeg: brew install ffmpeg", file=sys.stderr)
        return 1

    out = Path(args.out).expanduser()
    if args.clean and out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True, exist_ok=True)

    made = build(
        out, args.count, args.minutes,
        datetime.fromisoformat(args.start),
        args.voice, args.noise_db, args.edge, args.name_pattern,
    )

    total = sum(_probe_sec(f) for f in made)
    print(f"Device finto in {out}")
    print(f"  {len(made)} file da {args.minutes:g} min in record/ "
          f"({total/60:.1f} min totali)")
    for f in made:
        print(f"    {f.name}  ({f.stat().st_size/1e6:.1f} MB)")
    if args.edge:
        print("  + casi patologici (untitled, 00000001, 99999932, 0 byte, doppione)")
    print(f"\nProva con:  python sync_device.py pull --source {out} --dry-run")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
