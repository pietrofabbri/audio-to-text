"""
Test del collegamento fra testo e voce: quando una trascrizione viene
riffatta, chi stava parlando non deve sparire.

    python tests/test_speaker_assignment.py

Il difetto che questo test copre non dava errori e non lasciava tracce:
la sessione si completava senza problemi, con tutti i file scritti,
e i
segmenti risultavano senza interlocutore. Una ora di conversazione
registrata, tutta etichettata come voce ignota, senza che nulla
fallisse.

Perche' succedeva: rifare la trascrizione svuota i chunk e li ricrea,
ma i turni di diarizzazione stanno nello stadio accanto, che risultava
gia' fatto e veniva saltato. Il collegamento fra testo e voce vive nei
chunk, quindi i nuovi chunk nascevano senza.
"""

from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

from pipeline.diarizer import Diarizer  # noqa: E402


class Failure(Exception):
    pass


def require(cond: bool, msg: str) -> None:
    if not cond:
        raise Failure(msg)


CHECKS: list[tuple[str, callable]] = []


def check(fn):
    CHECKS.append((fn.__name__, fn))
    return fn


TURNI = [
    {"speaker": "SPEAKER_00", "start": 0.0, "end": 10.0},
    {"speaker": "SPEAKER_01", "start": 10.0, "end": 20.0},
    {"speaker": "SPEAKER_00", "start": 20.0, "end": 30.0},
]


def _chunk(idx=0, start=0.0, end=5.0, parole=True):
    """Un chunk come arriva dal trascrittore, parole comprese."""
    c = {"idx": idx, "start": start, "end": end, "text": "prova"}
    if parole:
        passo = (end - start) / 3
        c["words"] = [
            {"word": w, "start": start + passo * i, "end": start + passo * (i + 1),
             "prob": 0.9}
            for i, w in enumerate(("una", "due", "tre"))
        ]
    return c


@check
def il_chunk_prende_la_voce_del_turno() -> None:
    """Il caso normale: la parola cade dentro un turno, prende quel nome."""
    out = Diarizer.assign_speakers_word_level([_chunk()], TURNI)
    require(out[0]["speaker"] == "SPEAKER_00",
            f"un chunk da 0 a 5 secondi e' dentro il primo turno, "
            f"risulta {out[0]['speaker']}")
    for w in out[0]["words"]:
        require(w["speaker"] == "SPEAKER_00", "anche le parole")


@check
def il_cambio_di_voce_dentro_un_chunk() -> None:
    """Due persone nella stessa finestra di 29 secondi: conta la dominante."""
    parole = [
        {"word": "a", "start": 1.0, "end": 2.0, "prob": 0.9},
        {"word": "b", "start": 2.0, "end": 3.0, "prob": 0.9},
        {"word": "c", "start": 11.0, "end": 12.0, "prob": 0.9},
        {"word": "d", "start": 12.0, "end": 13.0, "prob": 0.9},
    ]
    chunk = {"idx": 0, "start": 0.0, "end": 14.0, "text": "a b c d", "words": parole}
    out = Diarizer.assign_speakers_word_level([chunk], TURNI)
    voci = [w["speaker"] for w in out[0]["words"]]
    require(voci[:2] == ["SPEAKER_00"] * 2 and voci[2:] == ["SPEAKER_01"] * 2,
            f"ogni parola prende la voce del proprio momento, sono {voci}")
    require(out[0]["speaker"] == "SPEAKER_00",
            f"a parita' di parole prende la prima, risulta {out[0]['speaker']}")


@check
def zone_senza_turni_danno_unknown() -> None:
    """Fuori dai turni non si inventa: resta sconosciuto, e va bene cosi'."""
    fuori = Diarizer.assign_speakers_word_level(
        [_chunk(start=100.0, end=110.0)], TURNI)
    require(fuori[0]["speaker"] == "UNKNOWN",
            f"un chunk lontano dai turni deve restare sconosciuto, "
            f"risulta {fuori[0]['speaker']}")


@check
def chunk_rifatto_riceve_il_speaker() -> None:
    """Il caso che non falliva: testo rifatto, turni gia' in checkpoint.

    E' il percorso di --retranscribe. I chunk arrivano senza speaker,
    esattamente come appena vengono creati, e devono riceverlo lo
    stesso dai turni gia' calcolati.
    """
    fresco = _chunk(idx=7, start=11.0, end=16.0)
    require("speaker" not in fresco,
            "il chunk appena trascritto non deve avere gia' un speaker")
    out = Diarizer.assign_speakers_word_level([fresco], TURNI)
    require(out[0]["speaker"] == "SPEAKER_01",
            f"un chunk da 11 a 16 secondi cade nel secondo turno, risulta "
            f"{out[0]['speaker']}")
    require(all(w.get("speaker") == "SPEAKER_01" for w in out[0]["words"]),
            "anche le parole del chunk rifatto")


@check
def riapplicare_non_robbina_nulla() -> None:
    """Riapplicare i turni a chunk che hanno gia' il speaker e' sicuro.

    Il ramo di ripresa chiama questa funzione ogni volta, anche quando
    i turni sono stati gia' usati: deve poterlo fare senza danno.
    """
    once = Diarizer.assign_speakers_word_level([_chunk()], TURNI)
    twice = Diarizer.assign_speakers_word_level(once, TURNI)
    require(twice[0]["speaker"] == once[0]["speaker"],
            "il secondo giro non deve cambiare la voce del chunk")
    require(len(twice[0]["words"]) == len(once[0]["words"]),
            f"le parole sono {len(once[0]['words'])} dopo un giro e "
            f"{len(twice[0]['words'])} dopo due")


@check
def chunk_senza_parole_usa_l_intervallo() -> None:
    """Senza timestamp di parola si decide sull'intervallo intero."""
    senza = _chunk(start=2.0, end=8.0, parole=False)
    out = Diarizer.assign_speakers_word_level([senza], TURNI)
    require(out[0]["speaker"] == "SPEAKER_00",
            f"un chunk senza parole prende la voce dominante "
            f"sull'intervallo, risulta {out[0]['speaker']}")


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
