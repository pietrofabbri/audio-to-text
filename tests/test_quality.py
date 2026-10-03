"""
Test del flag di qualità della trascrizione.

    python tests/test_quality.py

Nessun modello, nessun audio, nessuna rete: qui si prova il giudizio su
testi finiti, e sono i casi limite a contare più dei casi facili. Un
flag di qualità che scatta su tutto è rumore, e il rumore in un corpus
si scopre mesi dopo — quando è già stato usato come se fosse dato.

I test coprono le due direzioni dell'errore, ed è la seconda la più
pericolosa: segnalare come inaffidabile un segmento che è parla
chiaramente fa scartare dati buoni, e in un corpus che si costruisce
piano piano la perdita non si nota subito.
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.corpus_db import CorpusDB  # noqa: E402
from core.quality import (  # noqa: E402
    LOW, OK, UNRELIABLE, has_repetition, mean_word_prob, score_segment,
    summarize,
)


def _seg(text: str, start: float = 0.0, dur: float = 8.0, **kw) -> dict:
    d = {"text": text, "start": start, "end": start + dur,
         "duration_sec": dur, "no_speech_prob": 0.02}
    d.update(kw)
    return d


# -------------------------------------------------------------------
# Loop: il caso più frequente e più rumoroso
# -------------------------------------------------------------------

def test_loop_of_one_word_is_caught(tmp: Path) -> None:
    """Il caso più tipico in assoluto: una sola parola ripetuta.

    Qui sotto stava un bug vero. Le occorrenze di un n-gramma sono
    sovrapposte — in "va va va va va" il trigramma compare tre volte,
    non una — e la guardia che contava le parole minime come se fossero
    disgiunte chiedeva 12 parole dove ne bastavano 7. Il loop più
    comune non veniva mai segnalato.
    """
    assert has_repetition("va va va va va va va va va va"), "loop di una parola"
    assert has_repetition("va va va va va va va"), "7 parole bastano per 3 occorrenze"


def test_real_faster_whisper_loop_is_caught(tmp: Path) -> None:
    """Il loop vero, misurato su una registrazione: 22 ripetizioni di
    "ma tu non vado a fare il bambino". È il caso da cui nasce
    `no_repeat_ngram_size`, ed è qui che il flag deve vederlo."""
    testo = "ma tu non vado a fare il bambino " * 22
    q = score_segment(_seg(testo, dur=30.0))
    assert q["loop"], q["reasons"]
    assert q["level"] == UNRELIABLE, q


def test_italian_repetition_is_not_a_loop(tmp: Path) -> None:
    """«no no no» è italiano. Un flag che lo marchiasse come rumore
    farebbe scartare mezzo corpus di colloquio."""
    for testo in ("no no no si va bene cosi",
                  "va va va bene",
                  "grazie grazie grazie mille",
                  "va bene, adesso andiamo a casa perche e tardi"):
        assert not has_repetition(testo), f"{testo!r} non è un loop"


# -------------------------------------------------------------------
# I cinque segnali
# -------------------------------------------------------------------

def test_no_speech_prob_marks_silence(tmp: Path) -> None:
    """Il modello stesso dice che lì non si parlava:audio non
    intellegibile, silenzio, rumore. Il testo che ne esce è plausibile
    e falso."""
    q = score_segment(_seg("una frase abbastanza lunga qui",
                           no_speech_prob=0.91))
    assert q["level"] == UNRELIABLE, q
    assert any(r.startswith("no_speech") for r in q["reasons"]), q["reasons"]


def test_implausible_speech_rate(tmp: Path) -> None:
    """Fuori dalla fascia umana non è parlato: è allineamento rotto."""
    lento = score_segment(_seg("una due tre quattro cinque", dur=60.0))
    veloce = score_segment(_seg(" ".join(["parola"] * 90), dur=5.0))
    assert lento["level"] == UNRELIABLE, lento
    assert veloce["level"] == UNRELIABLE, veloce
    # E il caso normale non deve scattare
    normale = score_segment(_seg("uno due tre quattro cinque sei sette", dur=5.0))
    assert normale["level"] == OK, normale


def test_low_word_probability_is_weak_signal(tmp: Path) -> None:
    """Una parola a probabilità 0,3 è ancora informazione: il verdetto
    è `low`, non `unreliable`. È la differenza che evita di buttare
    via dati solo perché il modello era incerto."""
    parole = [{"word": "x", "prob": p} for p in (0.9, 0.9, 0.9, 0.2)]
    q = score_segment(_seg("una frase con una parola incerta", words=parole))
    # La media è 0,725 e sembra ottima: è per questo che serve anche
    # la quota sotto la soglia.
    assert abs(q["mean_word_prob"] - 0.725) < 0.01, q
    assert q["low_word_share"] == 0.25, q
    assert q["level"] == LOW, q
    assert any(r.startswith("troppe_parole_insicure") for r in q["reasons"]), q["reasons"]


def test_certain_segment_is_not_penalized(tmp: Path) -> None:
    """Il caso normale non deve mai alzare un flag: un flag che scatta
    anche quando tutto va bene è un flag che verrà ignorato."""
    parole = [{"word": "x", "prob": p} for p in (0.9, 0.95, 0.88, 0.92)]
    q = score_segment(_seg("una frase normale e ben riconosciuta", words=parole))
    assert q["level"] == OK, q
    assert q["reasons"] == [], q["reasons"]


def test_missing_word_timestamps_are_not_penalized(tmp: Path) -> None:
    """Senza timestamp di parola non si può giudicare la confidenza,
    e un flag che lo facesse comunque sarebbe rumore. Il transcriber
    ricade apposta su questi chunk."""
    q = score_segment(_seg("una frase perfettamente normale qui", words=None))
    assert q["mean_word_prob"] is None, q
    assert q["level"] == OK, q
    assert mean_word_prob({}) is None


def test_empty_text_is_unreliable(tmp: Path) -> None:
    q = score_segment(_seg("   "))
    assert q["level"] == UNRELIABLE
    assert "testo_vuoto" in q["reasons"]


def test_two_signals_beat_one(tmp: Path) -> None:
    """Il livello è il peggiore fra i segnali, e i motivi si vedono
    tutti: senza i motivi, un `unreliable` non si può diagnosticare."""
    q = score_segment(_seg("va va va va va va va", dur=6.0, no_speech_prob=0.8))
    assert q["level"] == UNRELIABLE, q
    assert len(q["reasons"]) >= 2, q["reasons"]


# -------------------------------------------------------------------
# Riassunto
# -------------------------------------------------------------------

def test_summary_accepts_segments_not_only_scores(tmp: Path) -> None:
    """`summarize()` accetta i segmenti, che è la forma che ha
    l'assembler, e non solo i risultati di `score_segment`.

    Qui sotto c'era un KeyError che ha rotto l'intera pipeline in una
    notte vera: `summarize` leggeva `level` dai risultati di
    `score_segment`, l'assembler gli passava i segmenti, e la cosa si
    vedeva solo davanti a una trascrizione completa — cioè mai, nei
    test veloci.
    """
    segmenti = [_seg("una frase normale e ben riconosciuta qui", dur=4.0)
                for _ in range(5)]
    r = summarize(segmenti)
    assert r["segments"] == 5, r
    assert r["ok"] == 5, r
    assert r["unreliable"] == 0, r


def test_summary_respects_a_manual_verdict(tmp: Path) -> None:
    """Se un segmento è già stato giudicato, `summarize` non ricalcola:
    ricalcolare ignorerebbe un verdetto cambiato a mano, che è una cosa
    che capita quando si rivede un segmento sospetto."""
    segmenti = [_seg("va va va va va va va", dur=6.0)]
    # Simuliamo il verdetto corretto a mano.
    segmenti[0]["level"] = OK
    segmenti[0]["n_words"] = 7
    r = summarize(segmenti)
    assert r["ok"] == 1, r
    assert r["unreliable"] == 0, "il verdetto manuale è stato ricalcolato"


def test_summary_weights_words_not_segments(tmp: Path) -> None:
    """Venti segmenti brevi e un segmento lungo contano per le loro
    parole, non per il loro numero: contare i segmenti farebbe
    sembrare peggiore (o migliore) di quello che è una sessione con
    molti segmenti brevi e una frase lunga.

    I venti segmenti cattivi portano 7 parole ciascuno, quello buono
    40: contando le parole, il materiale sospetto è una minoranza —
    ed è quello che la somma dei segmenti nasconderebbe.
    """
    qs = [score_segment(_seg("va va va va va va va", dur=6.0))] * 20
    qs.append(score_segment(_seg("una frase lunga e normale che dice cose "
                                 "e racconta cosa e successo ieri", dur=12.0)))
    r = summarize(qs)
    assert r["segments"] == 21
    assert r["unreliable"] == 20, r

    # Il punto e' che i due conteggi dicono cose diverse: quasi tutti i
    # segmenti sono sospetti (0,95), ma non quasi tutte le parole,
    # perche' il segmento buono e' molto piu' lungo. Il conteggio per
    # parole e' quello che risponde a "posso analizzare questa
    # sessione"; quello per segmenti farebbe sembrare questa sessione
    # molto piu' compromessa di quanto sia.
    assert r["unreliable_share"] > r["low_or_worse_share"], r
    assert r["low_or_worse_share"] < 0.95, r


def test_summary_of_empty_is_zero_not_crash(tmp: Path) -> None:
    r = summarize([])
    assert r["segments"] == 0 and r["unreliable_share"] == 0.0, r


# -------------------------------------------------------------------
# Corpus: il flag deve arrivare dove si interroga
# -------------------------------------------------------------------

def test_quality_reaches_the_database(tmp: Path) -> None:
    """Senza questo il flag è una colonna che non si riempie: il
    sintomo sarebbe una query che restituisce sempre NULL, che è il
    modo più silenzioso in cui un database mente."""
    doc = {
        "meta": {"file": "x.mp3", "stem": "s1", "total_duration_sec": 100.0,
                 "speech_duration_sec": 80.0, "speech_ratio": 0.8,
                 "speakers": ["GLOBAL_001"], "speaker_names": {},
                 "segments_count": 2, "total_words": 20},
        "segments": [
            {"idx": 0, "start": 0.0, "end": 30.0, "text": "una buona frase di qui",
             "speaker": "GLOBAL_001", "quality": OK, "quality_reasons": []},
            {"idx": 1, "start": 30.0, "end": 60.0,
             "text": "va va va va va va va", "speaker": "GLOBAL_001",
             "quality": UNRELIABLE,
             "quality_reasons": ["ripetizione_n3", "no_speech:0.81"]},
        ],
    }
    with CorpusDB(tmp / "c.db") as db:
        db.ingest_session("s1", doc, {})
        rows = db.query("SELECT quality, quality_reasons FROM segments "
                        "ORDER BY idx")
        assert rows[0]["quality"] == OK, dict(rows[0])
        assert rows[1]["quality"] == UNRELIABLE, dict(rows[1])
        assert "ripetizione_n3" in rows[1]["quality_reasons"], dict(rows[1])

        rep = db.quality_report()
        assert len(rep) == 1, rep
        assert rep[0]["suspect_segments"] == 1, rep
        # I motivi si contano per tipo, non per valore: due no_speech
        # diversi sono un motivo solo.
        assert rep[0]["reasons"]["ripetizione_n3"] == 1, rep[0]["reasons"]

        peggiori = db.suspect_text()
        assert len(peggiori) == 1, [dict(r) for r in peggiori]
        assert "va va va" in peggiori[0]["text"]


def test_migration_adds_columns_to_old_database(tmp: Path) -> None:
    """Un database creato prima delle colonne di qualità deve
    continuare a funzionare. `CREATE TABLE IF NOT EXISTS` non
    aggiorna una tabella che c'è già: senza la migrazione, ogni
    scrittura notturna andrebbe a finire in una colonna inesistente.

    Il database "vecchio" si costruisce a mano, con lo schema di prima:
    SQLite non ha `DROP COLUMN` abbastanza vecchio da poterlo usare,
    e una tabella rifatta sarebbe un test di una cosa diversa.
    """
    import sqlite3
    p = tmp / "vecchio.db"
    conn = sqlite3.connect(str(p))
    conn.executescript(
        "CREATE TABLE segments ("
        " id INTEGER PRIMARY KEY, stem TEXT, idx INTEGER, speaker TEXT,"
        " start_sec REAL, end_sec REAL, duration_sec REAL, text TEXT,"
        " n_words INTEGER, f0_mean_hz REAL);"
    )
    conn.execute("INSERT INTO segments(stem, idx, text) VALUES('s1', 0, 'ciao')")
    conn.commit()
    conn.close()

    with CorpusDB(p) as db:
        cols = {r["name"] for r in db.query("PRAGMA table_info(segments)")}
        assert "quality" in cols, "la migrazione non ha aggiunto la colonna"
        assert "quality_reasons" in cols, cols
        # I dati di prima devono essere ancora lì: la migrazione
        # aggiunge, non ricrea.
        rows = db.query("SELECT text FROM segments")
        assert rows[0]["text"] == "ciao", [dict(r) for r in rows]


def test_quality_survives_json_roundtrip(tmp: Path) -> None:
    """Il flag attraversa transcript.json senza perdere i motivi: se
    i motivi sparissero, un `unreliable` non si potrebbe più
    diagnosticare e il flag diventerebbe un verdetto muto."""
    q = score_segment(_seg("va va va va va va va", dur=6.0, no_speech_prob=0.8))
    testo = json.dumps({"level": q["level"], "reasons": q["reasons"]})
    r = json.loads(testo)
    assert r["level"] == UNRELIABLE
    assert len(r["reasons"]) >= 2, r


# -------------------------------------------------------------------

def main() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for fn in tests:
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


if __name__ == "__main__":
    raise SystemExit(main())