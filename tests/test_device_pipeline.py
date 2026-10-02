"""
Test per device, denoise, corpus_db e archive — senza audio reale, senza
modelli, senza rete.

    python tests/test_device_pipeline.py

I nomi dei file sono l'unica cosa su cui il rilevamento può basarsi per
capire quando è stata fatta una registrazione: se sbaglia, ogni analisi
successiva è spostata nel tempo. Per questo i casi negativi contano
quanto quelli positivi.
"""

from __future__ import annotations

import json
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.device import (  # noqa: E402
    VolumeInfo, _collect_audio, _find_record_dir, parse_recording_time,
)
from core.corpus_db import CorpusDB, _normalize_word  # noqa: E402
from pipeline.denoise import QualityScore, compare, denoised_path_for  # noqa: E402


# ---------------------------------------------------------------------------
# Nomi file e orario di registrazione
# ---------------------------------------------------------------------------

def test_filename_formats(tmp: Path) -> None:
    atteso = datetime(2026, 10, 3, 22, 4, 15)
    for nome in (
        "REC_20261003_220415.mp3",
        "20261003_220415.wav",
        "2026-10-03 22-04-15.m4a",
        "VID_20261003_220415.mp4",
        "03-10-2026_22-04-15.wav",
        "20261003-220415.mp3",
        "Record_2026_10_03_22_04_15.amr",
        "rec_20261003220415.mp3",
        "Nuova registrazione 20261003_220415.aac",
    ):
        dt, pattern = parse_recording_time(nome)
        assert dt == atteso, f"{nome} -> {dt} (pattern {pattern})"


def test_compact_digits_are_read_as_written(tmp: Path) -> None:
    """Senza separatori, le cifre sono le cifre: 20261003022015 sono le
    02:20:15, non le 22:04:15. Il tool non deve 'indovinare' un orario
    plausibile a partire da uno implausibile."""
    dt, _ = parse_recording_time("rec_20261003022015.mp3")
    assert dt == datetime(2026, 10, 3, 2, 20, 15), dt


def test_filename_rejects_junk(tmp: Path) -> None:
    """Meglio nessun orario che un orario inventato: un orario sbagliato
    sposta ogni correlazione successiva senza che nulla lo segnali."""
    for nome in (
        "untitled.mp3",
        "New Recording 3.aac",
        "00000001_000000.MP3",      # orologio mai inizializzato
        "99999999_999999.wav",      # data inesistente
        "99991232_999999.mp3",      # 32 dicembre
        "audio_20261003.wav",       # solo data, nessuna ora
        "20261003_254500.mp3",      # ora 25
    ):
        dt, _ = parse_recording_time(nome)
        assert dt is None, f"{nome} avrebbe prodotto {dt}"


def test_record_dir_found_and_junk_skipped(tmp: Path) -> None:
    rec = tmp / "untitled" / "record"
    rec.mkdir(parents=True)
    (rec / "REC_20261003_220000.mp3").write_bytes(b"x" * 5000)
    (rec / "vuoto.mp3").write_bytes(b"")            # segnaposto
    (rec / "appunto.txt").write_text("nota")

    found = _find_record_dir(tmp / "untitled")
    # su macOS /var è un symlink di /private/var: i due path sono lo
    # stesso posto ma non lo stesso oggetto
    assert found == rec or found.resolve() == rec.resolve(), found

    audio = _collect_audio(found)
    assert [f.name for f in audio] == ["REC_20261003_220000.mp3"], audio


def test_app_bundle_is_not_a_recorder(tmp: Path) -> None:
    """I .app contengono asset .mp3: senza il filtro, il rilevamento
    sceglieva l'IDE montata come se fosse il registratore."""
    app = tmp / "Kiro.app" / "Contents" / "Resources"
    app.mkdir(parents=True)
    (app / "suono.mp3").write_bytes(b"x" * 9000)
    (tmp / "Kiro.app" / "Contents").mkdir(exist_ok=True)
    from core.device import _is_skippable
    assert _is_skippable(tmp / "Kiro.app")
    assert _is_skippable(tmp / "Kiro.app" / "Contents" / "Resources")
    assert not _is_skippable(tmp / "untitled")


def test_read_only_volume_is_not_picked(tmp: Path) -> None:
    """Un volume in sola lettura non può essere la sorgente da cui si
    cancella: va escluso, non proposto."""
    ro = VolumeInfo(
        path=tmp / "readonly", label="ro", total_mb=8000, free_mb=4000,
        writable=False, audio_files=[tmp / "a.mp3"],
    )
    assert not ro.looks_like_recorder
    assert "sola lettura" in ro.rejection_reason()


# ---------------------------------------------------------------------------
# Scelta della variante denoised
# ---------------------------------------------------------------------------

def _score(name: str, **kw) -> QualityScore:
    return QualityScore(name=name, **kw)


def test_denoised_wins_when_original_is_broken(tmp: Path) -> None:
    orig = _score("original", asr_confidence=0.41, words_per_sec=7.5,
                  token_count=100, has_vad_stats=True, speech_ratio=0.5)
    dn = _score("denoised", asr_confidence=0.92, words_per_sec=2.4,
                token_count=210, has_vad_stats=True, speech_ratio=0.5)
    d = compare(orig, dn)
    assert d["winner"] == "denoised", d
    assert any("degradato" in r and "ripulita" in r for r in d["reasons"]), d["reasons"]


def test_original_wins_when_denoise_destroys_speech(tmp: Path) -> None:
    orig = _score("original", asr_confidence=0.90, words_per_sec=2.4, token_count=200,
                  has_vad_stats=True, speech_ratio=0.5)
    # denoise che mangia le parole: confidenza alta ma parlato azzerato
    dn = _score("denoised", asr_confidence=0.95, speech_ratio=0.01, words_per_sec=9.0,
                token_count=180, has_vad_stats=True)
    d = compare(orig, dn)
    assert d["winner"] == "original", d
    assert "quasi nessun parlato" in " ".join(d["broken"]["denoised"]), d
    assert "degradata" in " ".join(d["reasons"]), d["reasons"]


def test_tie_keeps_original(tmp: Path) -> None:
    """A parità si tiene l'originale: non si cambia mai una variante
    senza un motivo misurabile, altrimenti la scelta oscilla ogni notte."""
    a = _score("original", asr_confidence=0.90, words_per_sec=2.4, token_count=200,
               has_vad_stats=True, speech_ratio=0.5)
    b = _score("denoised", asr_confidence=0.902, words_per_sec=2.4, token_count=200,
               has_vad_stats=True, speech_ratio=0.5)
    d = compare(a, b)
    assert d["winner"] == "original", d
    assert "non significativa" in d["reasons"][0], d["reasons"]


def test_missing_vad_stats_do_not_condemn_either_variant(tmp: Path) -> None:
    """Dato assente non è dato zero. Senza questo controllo, ogni
    variante senza statistiche VAD veniva dichiarata rotta e il
    confronto non poteva mai scegliere il denoise."""
    a = _score("original", asr_confidence=0.80, words_per_sec=2.4, token_count=200,
               has_vad_stats=False, speech_ratio=0.0)
    b = _score("denoised", asr_confidence=0.95, words_per_sec=2.4, token_count=205,
               has_vad_stats=False, speech_ratio=0.0)
    d = compare(a, b)
    assert not d["broken"]["original"] and not d["broken"]["denoised"], d
    assert d["winner"] == "denoised", d


def test_silent_audio_is_broken_for_both(tmp: Path) -> None:
    """Il caso opposto: nessun testo su nessuna delle due varianti è un
    guasto, non una soglia non raggiunta."""
    a = _score("original", asr_confidence=0.0, words_per_sec=0.0, token_count=0)
    b = _score("denoised", asr_confidence=0.0, words_per_sec=0.0, token_count=0)
    d = compare(a, b)
    assert d["winner"] == "original", d
    assert "nessun testo" in " ".join(d["broken"]["original"]), d


def test_repeated_word_segment_is_flagged(tmp: Path) -> None:
    """'parlare dire fare' ripetuto in un segmento è allineamento rotto,
    non linguaggio: va contato come anomalia."""
    from pipeline.denoise import score_variant
    chunks = [{
        "idx": 0, "start": 0.0, "end": 20.0,
        "text": "parlare dire fare parlare dire fare parlare dire fare",
        "words": [{"word": w, "start": i * 2.0, "end": i * 2.0 + 1.0, "prob": 0.9}
                  for i, w in enumerate("parlare dire fare".split() * 3)],
    }]
    s = score_variant("x", chunks, {"speech_duration_sec": 20.0, "total_duration_sec": 20.0})
    assert s.degenerate_segments == 1, s.as_dict()
    assert s.token_count == 9, s.as_dict()


def test_denoised_path_keeps_stem(tmp: Path) -> None:
    p = denoised_path_for(Path("input/.wav_cache/2026-10-03_16k.wav"))
    assert p.name == "2026-10-03_16k_dn.wav", p


# ---------------------------------------------------------------------------
# Normalizzazione parole
# ---------------------------------------------------------------------------

def test_normalize_word(tmp: Path) -> None:
    assert _normalize_word("Parlare,") == "parlare"
    assert _normalize_word("«E»") == "e"
    assert _normalize_word("  modo.  ") == "modo", _normalize_word("  modo.  ")
    assert _normalize_word(" ...") == "", repr(_normalize_word(" ..."))
    assert _normalize_word("") == ""


# ---------------------------------------------------------------------------
# CorpusDB
# ---------------------------------------------------------------------------

def _fake_transcript() -> dict:
    return {
        "meta": {
            "file": "x.mp3", "stem": "s1", "processed_at": "2026-10-03T22:00:00",
            "total_duration_sec": 3600.0, "speech_duration_sec": 1800.0,
            "speech_ratio": 0.5, "segments_count": 2, "total_words": 10,
            "speakers": ["GLOBAL_001"],
            "speaker_names": {"GLOBAL_001": "Pietro"},
        },
        "segments": [
            {"idx": 0, "start": 0.0, "end": 30.0, "text": "Buongiorno, oggi parliamo",
             "speaker": "GLOBAL_001", "duration_sec": 30.0,
             "prosody": {"f0_mean_hz": 120.0, "intensity_mean_db": 60.0}},
            {"idx": 1, "start": 30.0, "end": 60.0, "text": "Poi ci vediamo",
             "speaker": "GLOBAL_001", "duration_sec": 30.0,
             "prosody": {"f0_mean_hz": 140.0}},
        ],
    }


def test_corpus_ingest_is_idempotent(tmp: Path) -> None:
    with CorpusDB(tmp / "c.db") as db:
        a = db.ingest_session("s1", _fake_transcript(), {"speech_duration_sec": 1800.0})
        b = db.ingest_session("s1", _fake_transcript(), {"speech_duration_sec": 1800.0})
        assert a == b, (a, b)
        assert db.stats()["segments"] == 2, db.stats()
        assert db.stats()["tokens"] == a["tokens"], "rilettura ha duplicato i token"


def test_corpus_prosody_columns_are_queryable(tmp: Path) -> None:
    with CorpusDB(tmp / "c.db") as db:
        db.ingest_session("s1", _fake_transcript(), {"speech_duration_sec": 1800.0})
        row = db.query("SELECT AVG(f0_mean_hz) f FROM segments")[0]
        assert abs(row["f"] - 130.0) < 0.01, dict(row)
        # una feature assente deve restare NULL, non diventare 0:
        # 0 Hz di F0 è una voce senza voce, ed è un'altra cosa
        nulls = db.query("SELECT COUNT(*) c FROM segments WHERE shimmer_local IS NULL")[0]
        assert nulls["c"] == 2, dict(nulls)


def test_corpus_keeps_case_and_normalizes(tmp: Path) -> None:
    doc = _fake_transcript()
    # senza word-level timestamps i token non hanno posizione: vengono
    # contati per le frequenze ma non memorizzati come righe
    with CorpusDB(tmp / "a.db") as db:
        db.ingest_session("s1", doc, {"speech_duration_sec": 1800.0})
        assert db.stats()["tokens"] == 0, db.stats()
        parole = {r["word"] for r in db.top_words(20)}
        assert "buongiorno" in parole and "parliamo" in parole, parole

    # con word-level: riga per riga, con forma originale e normalizzata
    doc["segments"][0]["words"] = [
        {"word": " Buongiorno", "start": 0.0, "end": 0.5, "prob": 0.9},
        {"word": "oggi,", "start": 0.5, "end": 0.9, "prob": 0.8},
    ]
    with CorpusDB(tmp / "b.db") as db:
        db.ingest_session("s1", doc, {"speech_duration_sec": 1800.0})
        rows = [dict(r) for r in db.query("SELECT word, word_norm FROM tokens ORDER BY token_idx")]
        assert rows[0] == {"word": "Buongiorno", "word_norm": "buongiorno"}, rows
        assert rows[1] == {"word": "oggi,", "word_norm": "oggi"}, rows


def test_analysis_is_one_row_per_day_and_kind(tmp: Path) -> None:
    with CorpusDB(tmp / "c.db") as db:
        db.record_analysis("2026-10-03", "daily_digest", "prima")
        db.record_analysis("2026-10-03", "daily_digest", "seconda")
        db.record_analysis("2026-10-03", "wordfreq_delta", "altra")
        rows = db.query("SELECT kind, summary FROM analyses ORDER BY kind")
        assert len(rows) == 2, [dict(r) for r in rows]
        assert dict(rows[0])["summary"] == "seconda", dict(rows[0])


# ---------------------------------------------------------------------------

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
