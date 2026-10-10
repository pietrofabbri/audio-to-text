"""
Test per SpeakerDB — eseguibili senza audio, senza modelli e senza GPU.

    python tests/test_speaker_db.py

Gli embedding sono sintetici: vettori casuali su un "ipersfero" con
rumore controllato. Non provano la qualità del riconoscimento vocale,
ma la meccanica del DB, che è la parte che si rompe in silenzio.
"""

from __future__ import annotations

import json
import random
import sys
import tempfile
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.speaker_db import SpeakerDB, cosine_similarity, to_vector  # noqa: E402


DIM = 256
SEED = 42


def make_voice(rng: random.Random) -> np.ndarray:
    """Vettore unitario casuale: una voce "ideale"."""
    v = np.array([rng.gauss(0, 1) for _ in range(DIM)], dtype=np.float32)
    return v / np.linalg.norm(v)


def degrade(voice: np.ndarray, rng: random.Random, noise: float) -> np.ndarray:
    """La stessa voce in condizioni diverse (rumore, distanza, microfono).

    Va applicata a una voce giá esistente: rigenerare da zero a ogni
    chiamata produrrebbe un'altra persona, e il test passerebbe o
    fallirebbe per il motivo sbagliato.
    """
    n = np.array([rng.gauss(0, 1) for _ in range(DIM)], dtype=np.float32)
    n /= np.linalg.norm(n)
    v = (1 - noise) * voice + noise * n
    return v / np.linalg.norm(v)


def approx(v):
    return [float(x) for x in v]


def test_new_speaker_gets_global_id(tmp: Path) -> None:
    db = SpeakerDB(path=tmp / "db.json")
    mapping = db.resolve("sessione_a", {
        "SPEAKER_00": {"embedding": approx(np.zeros(DIM) + 1), "seconds": 120.0},
    })
    assert mapping == {"SPEAKER_00": "GLOBAL_001"}, mapping
    assert (tmp / "db.json").exists()


def test_same_voice_across_sessions_matches(tmp: Path) -> None:
    rng = random.Random(SEED)
    voice_pietro = make_voice(rng)
    voice_marco = make_voice(rng)

    db = SpeakerDB(path=tmp / "db.json", threshold=0.78)
    a = db.resolve("lunedi", {
        "SPEAKER_00": {"embedding": approx(voice_pietro), "seconds": 1800.0},
        "SPEAKER_01": {"embedding": approx(voice_marco), "seconds": 900.0},
    })
    assert a == {"SPEAKER_00": "GLOBAL_001", "SPEAKER_01": "GLOBAL_002"}, a

    # Stesse voci, registrazione successiva, con un po' di rumore:
    # la stessa persona deve ricadere sulla stessa voce globale.
    b = db.resolve("martedi", {
        "SPEAKER_00": {"embedding": approx(degrade(voice_pietro, rng, 0.25)), "seconds": 1500.0},
        "SPEAKER_01": {"embedding": approx(degrade(voice_marco, rng, 0.25)), "seconds": 800.0},
    })
    assert b["SPEAKER_00"] == "GLOBAL_001", b
    assert b["SPEAKER_01"] == "GLOBAL_002", b

    # Il rumore non deve degradare il coseno sotto soglia
    assert cosine_similarity(degrade(voice_pietro, rng, 0.25), voice_pietro) > 0.78


def test_local_labels_can_swap_between_sessions(tmp: Path) -> None:
    """Il punto MOTIVO del DB: SPEAKER_00 oggi può essere un'altra
    persona rispetto a ieri, ma le identità globali restano giuste."""
    rng = random.Random(7)
    a_voice = make_voice(rng)
    b_voice = make_voice(rng)

    db = SpeakerDB(path=tmp / "db.json", threshold=0.78)
    day1 = db.resolve("giorno1", {
        "SPEAKER_00": {"embedding": approx(a_voice), "seconds": 600.0},
    })
    # Il giorno dopo pyannote etichetta l'altro come SPEAKER_00
    day2 = db.resolve("giorno2", {
        "SPEAKER_00": {"embedding": approx(b_voice), "seconds": 700.0},
    })
    assert day1["SPEAKER_00"] == "GLOBAL_001"
    assert day2["SPEAKER_00"] == "GLOBAL_002", (
        "voce nuova non deve essere fusa con una voce nota"
    )


def test_below_threshold_creates_new_voice(tmp: Path) -> None:
    rng = random.Random(11)
    db = SpeakerDB(path=tmp / "db.json", threshold=0.99)  # soglia irraggiungibile
    db.resolve("s1", {"SPEAKER_00": {"embedding": approx(make_voice(rng)), "seconds": 10.0}})
    m = db.resolve("s2", {"SPEAKER_00": {"embedding": approx(make_voice(rng)), "seconds": 10.0}})
    assert m["SPEAKER_00"] == "GLOBAL_002", m


def test_rerun_same_session_does_not_inflate_totals(tmp: Path) -> None:
    """Rilanciare la stessa sessione (es. dopo un crash) non deve
    contare le ore due volte."""
    rng = random.Random(3)
    db = SpeakerDB(path=tmp / "db.json", threshold=0.78)
    args = {"SPEAKER_00": {"embedding": approx(make_voice(rng)), "seconds": 3600.0}}
    db.resolve("notte", args)
    after_first = db.profiles()["GLOBAL_001"]["total_seconds"]
    db.resolve("notte", args)
    after_second = db.profiles()["GLOBAL_001"]["total_seconds"]
    assert after_first == after_second == 3600.0, (after_first, after_second)
    assert db.profiles()["GLOBAL_001"]["sessions_count"] == 1


def test_missing_embedding_is_skipped(tmp: Path) -> None:
    db = SpeakerDB(path=tmp / "db.json")
    m = db.resolve("s1", {"SPEAKER_00": {"seconds": 10.0}})
    assert m == {}, m
    assert not (tmp / "db.json").exists(), "non deve creare il DB se non c'è nulla da salvare"


def test_names_roundtrip(tmp: Path) -> None:
    rng = random.Random(5)
    db = SpeakerDB(path=tmp / "db.json")
    db.resolve("s1", {"SPEAKER_00": {"embedding": approx(make_voice(rng)), "seconds": 100.0}})
    assert db.get_name("GLOBAL_001") == "GLOBAL_001"  # fallback all'ID
    db.set_name("GLOBAL_001", "Pietro")
    assert db.get_name("GLOBAL_001") == "Pietro"

    reopened = SpeakerDB(path=tmp / "db.json")
    assert reopened.get_name("GLOBAL_001") == "Pietro", "il nome deve sopravvivere al reload"

    profiles = reopened.profiles()
    assert profiles["GLOBAL_001"]["name"] == "Pietro"
    assert "centroid" not in profiles["GLOBAL_001"], "i vettori non devono finire nei profili"
    try:
        reopened.set_name("GLOBAL_999", "inesistente")
    except KeyError:
        pass
    else:
        raise AssertionError("set_name su ID inesistente deve sollevare KeyError")


def test_dimension_mismatch_does_not_corrupt(tmp: Path) -> None:
    """Se il modello di embedding cambia, i confronti con vettori di
    altra dimensione vanno ignorati, non distorti."""
    rng = random.Random(9)
    db = SpeakerDB(path=tmp / "db.json", threshold=0.5)
    db.resolve("s1", {"SPEAKER_00": {"embedding": approx(make_voice(rng)), "seconds": 100.0}})

    m = db.resolve("s2", {
        "SPEAKER_00": {"embedding": approx(np.ones(64)), "seconds": 100.0},
    })
    assert m["SPEAKER_00"] == "GLOBAL_002", m
    assert cosine_similarity(np.ones(4), np.ones(8)) == 0.0


def test_corrupt_db_is_backed_up_not_lost(tmp: Path) -> None:
    path = tmp / "db.json"
    path.write_text("{questo non è json", encoding="utf-8")
    db = SpeakerDB(path=path)
    assert db.profiles() == {}
    assert path.with_suffix(".corrupt.json").exists()
    # E ricomincia a funzionare
    rng = random.Random(13)
    db.resolve("s1", {"SPEAKER_00": {"embedding": approx(make_voice(rng)), "seconds": 1.0}})
    assert json.loads(path.read_text(encoding="utf-8"))["speakers"]


def test_to_vector_shapes(tmp: Path) -> None:
    """pyannote può restituire (1, D): concatenare le righe produrrebbe
    un vettore di lunghezza D*(righe-1) e un coseno senza significato."""
    one_d = np.array([1.0, 2.0, 3.0], dtype=np.float32)
    assert to_vector(one_d).shape == (3,)

    two_d = np.array([[1.0, 2.0, 3.0], [3.0, 2.0, 1.0]], dtype=np.float32)
    assert to_vector(two_d).shape == (3,)
    assert to_vector(two_d)[0] == pytest_approx(2.0)

    assert to_vector([[[1.0, 1.0]]]).shape == (2,)


def test_merge_in_dry_run_non_scrive_il_db(tmp: Path) -> None:
    """`merge_ids(dry_run=True)` deve lasciare il file com'era.

    Il caso reale: `review_speakers.py merge --dry-run` cancellava la voce
    dal database delle voci. Il comando prometteva di non scrivere e
    scriveva: la voce spariva, e le sessioni che la citavano restavano con
    un ID che non esisteva piu'. Trovato eseguendo i quattro merge delle
    coppie in zona grigia «per vedere cosa cambierebbe»: al quarto comando
    la coppia diceva «una delle due voci non esiste» perche' il dry-run
    precedente l'aveva già cancellata.

    Il pericolo non e' la perdita di un numero: e' che il corpus diventa
    incoerente senza che niente lo segnali, perche' un ID assente e'
    semplicemente un ID che nessuno genera piu'.
    """
    rng = random.Random(SEED)
    path = tmp / "db.json"
    db = SpeakerDB(path=path)
    a, b = make_voice(rng), make_voice(rng)
    mappa = db.resolve("s1", {
        "SPEAKER_00": {"embedding": approx(a), "seconds": 100.0},
        "SPEAKER_01": {"embedding": approx(b), "seconds": 200.0},
    })
    prima_id = sorted(db._data["speakers"])
    prima_bytes = path.read_bytes()

    keep, drop = mappa["SPEAKER_00"], mappa["SPEAKER_01"]
    assert db.merge_ids(keep, drop, dry_run=True), "il merge deve essere possibile"
    assert sorted(db._data["speakers"]) == prima_id, (
        f"in dry-run le voci non devono cambiare: {sorted(db._data['speakers'])} "
        f"contro {prima_id}")
    assert path.read_bytes() == prima_bytes, "in dry-run il file non si tocca"

    # E il merge vero deve funzionare ancora.
    assert db.merge_ids(keep, drop), "il merge vero deve riuscire"
    assert drop not in db._data["speakers"], "il merge vero deve cancellare la voce"
    assert pytest_approx(db._data["speakers"][keep]["total_seconds"]) == 300.0, (
        "i secondi delle due voci devono sommarsi")


def test_split_in_dry_run_non_cancella_la_voce(tmp: Path) -> None:
    """`split --dry-run` dichiarava il flag e non lo leggeva.

    Il caso reale: `--dry-run` e' dichiarato in argparse dal primo giorno e
    `cmd_split` non lo guardava mai. Il comando cancellava la voce dal
    database e scriveva, e su una voce vera con sette contributi in sette
    sessioni diverse. Il risultato e' che sette sessioni citavano un ID
    che nessuno generava piu'.

    E' il difetto piu' assurdo dei tre trovati eseguendo i comandi, perche'
    `split` e' proprio il comando che si usa per non perdere una voce: il
    `--dry-run` che dovrebbe mettere al sicuro e' l'unico modo per
    perderla davvero.
    """
    import argparse

    import review_speakers as rs

    rng = random.Random(SEED)
    path = tmp / "db.json"
    db = SpeakerDB(path=path)
    mappa = db.resolve("s1", {
        "SPEAKER_00": {"embedding": approx(make_voice(rng)), "seconds": 100.0},
        "SPEAKER_01": {"embedding": approx(make_voice(rng)), "seconds": 200.0},
    })
    gid = mappa["SPEAKER_00"]
    prima_bytes = path.read_bytes()

    rc = rs.cmd_split(db, argparse.Namespace(gid=gid, dry_run=True))
    assert rc == 0, "il dry-run deve uscire pulito"
    assert gid in db._data["speakers"], (
        f"in dry-run la voce non deve sparire: {gid} non c'e' piu'")
    assert path.read_bytes() == prima_bytes, "in dry-run il file non si tocca"

    # E senza dry-run la voce deve sparire davvero.
    rc = rs.cmd_split(db, argparse.Namespace(gid=gid, dry_run=False))
    assert rc == 0, "lo split vero deve uscire pulito"
    assert gid not in db._data["speakers"], "lo split vero deve togliere la voce"


def test_info_si_salva_e_segue_il_merge(tmp: Path) -> None:
    """Le note su una voce restano dopo il salvataggio e dopo un merge."""
    p = tmp / "db.json"
    db = SpeakerDB(path=p)
    db._register("GLOBAL_001", "s1", "SPEAKER_00", [1.0, 0.0], 100)
    db._register("GLOBAL_002", "s2", "SPEAKER_00", [0.9, 0.1], 50)
    db.save()
    db.set_info("GLOBAL_002", "  la mia ragazza ")
    db2 = SpeakerDB(path=p)
    assert db2.profiles()["GLOBAL_002"]["info"] == "la mia ragazza", db2.profiles()
    db2.merge_ids("GLOBAL_001", "GLOBAL_002")
    assert db2.profiles()["GLOBAL_001"]["info"] == "la mia ragazza", \
        "le note della voce assorbita passano a quella che resta"
    db2.set_info("GLOBAL_001", "")
    assert db2.profiles()["GLOBAL_001"]["info"] is None


def pytest_approx(x: float) -> float:
    return round(float(x), 5)


def main() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0

    for fn in tests:
        # Ogni test gira in una directory temporanea pulita
        with tempfile.TemporaryDirectory() as d:
            try:
                fn(Path(d))
            except AssertionError as exc:
                print(f"FAIL  {fn.__name__}: {exc}")
                failed += 1
            except Exception as exc:  # noqa: BLE001
                print(f"ERROR {fn.__name__}: {type(exc).__name__}: {exc}")
                failed += 1
            else:
                print(f"ok    {fn.__name__}")

    print(f"\n{len(tests) - failed}/{len(tests)} test superati")
    return 1 if failed else 0




def test_merge_media_pesata_dei_centroidi(tmp_path):
    """Unire due voci deve mescolare i centroidi in base ai secondi.

    Prima il merge teneva solo il centroide della voce conservata: con gli
    argomenti scambiati (5 ore unite dentro 2 minuti) la voce principale
    restava con l'impronta dei 2 minuti.
    """
    from core.speaker_db import _merge_into
    data = {"speakers": {
        "GLOBAL_001": {"centroid": [1.0, 0.0], "total_seconds": 300.0,
                       "sessions": {"a|S0": {"stem": "a", "seconds": 300.0}}},
        "GLOBAL_002": {"centroid": [0.0, 1.0], "total_seconds": 100.0,
                       "sessions": {"b|S0": {"stem": "b", "seconds": 100.0}}},
    }}
    _merge_into(data, "GLOBAL_002", "GLOBAL_001")
    c = data["speakers"]["GLOBAL_002"]["centroid"]
    assert abs(c[0] - 0.75) < 1e-6 and abs(c[1] - 0.25) < 1e-6, c
    assert data["speakers"]["GLOBAL_002"]["total_seconds"] == 400.0


if __name__ == "__main__":
    raise SystemExit(main())
