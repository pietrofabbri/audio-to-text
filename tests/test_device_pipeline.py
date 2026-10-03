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
import os
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


def test_budget_stops_between_files_not_mid_file(tmp: Path) -> None:
    """La coda si deve fermare FRA un file e l'altro. Un file iniziato e
    non finito costa il suo tempo senza produrre nulla, e la notte
    dopo si rifarebbe da capo."""
    import importlib
    sd = importlib.import_module("sync_device")

    f = tmp / "a.mp3"
    f.write_bytes(b"x" * 5000)

    b = sd._Budget(0)  # nessun limite
    assert not b.exhausted(10_000), "senza limite non deve mai fermarsi"

    b = sd._Budget(1000, simulate=True)
    b.started_file(900)
    b.finished_file(3600.0, simulated=True)
    assert b.elapsed() == 900, b.elapsed()
    assert b.remaining() == 100, b.remaining()
    assert b.exhausted(900), "con 100s residui non si deve iniziare un file da 900s"
    assert not b.exhausted(50)


def test_budget_learns_from_files_already_done(tmp: Path) -> None:
    """Dopo il primo file la stima non e' piu' un'ipotesi: si impara dal
    RTF reale della run in corso."""
    import importlib
    sd = importlib.import_module("sync_device")
    f = tmp / "a.mp3"
    f.write_bytes(b"x" * 5000)

    b = sd._Budget(10_000, simulate=True)
    assert abs(b.rtf() - b.DEFAULT_RTF) < 1e-9, "prima del primo file: RTF di default"

    # un file da 3600s di audio elaborato "in" 3600s simulati
    b.started_file(3600.0)
    b.finished_file(3600.0, simulated=True)
    assert abs(b.rtf() - 1.0) < 1e-9, b.rtf()

    # il file successivo costa ora secondo l'RTF imparato (1.0), non
    # secondo la stima di default (0,87): con un RTF reale di 1.0 la
    # stima deve crescere, non restare quella ottimistica.
    assert b.estimate(f) > 3600.0 * 0.9, b.estimate(f)


def test_dry_run_advances_budget_by_estimate(tmp: Path) -> None:
    """In simulazione il tempo non passa, ma consumarlo comunque e' quello
    che rende il dry-run un piano invece di un'eco."""
    import importlib
    sd = importlib.import_module("sync_device")
    b = sd._Budget(100, simulate=True)
    b.started_file(60)
    b.finished_file(3600.0, simulated=True)
    assert b.elapsed() == 60
    assert b.remaining() == 40


def test_fallback_stem_is_unique_per_file(tmp: Path) -> None:
    """Senza hash nella riserva, due file senza data importati nello
    stesso secondo finirebbero nella stessa cartella e il secondo
    verrebbe scambiato per un output gia' presente."""
    import importlib
    sd = importlib.import_module("sync_device")
    a = sd._stem_for(tmp / "uno.mp3", None)
    b = sd._stem_for(tmp / "due.mp3", None)
    assert a != b, (a, b)
    # con data, invece, la data è la chiave
    from datetime import datetime
    dt = datetime(2026, 10, 3, 22, 4, 15)
    assert sd._stem_for(tmp / "x.mp3", dt) == "2026-10-03_22-04-15"
    assert sd._stem_for(tmp / "y.mp3", dt) == "2026-10-03_22-04-15"


# -------------------------------------------------------------------
# Cache WAV: la pulizia non deve rompere la ripresa
# -------------------------------------------------------------------

def _checkpoint(root: Path, stem: str, wav: Path, done: bool) -> Path:
    """Scrive un checkpoint finto nella forma che produce la pipeline."""
    job = root / "output" / stem
    job.mkdir(parents=True, exist_ok=True)
    p = job / f"{stem}.checkpoint.json"
    p.write_text(json.dumps({
        "stem": stem,
        "stages": {
            "ffmpeg": {"done": True, "wav_path": str(wav)},
            "vad": {"done": True},
            "transcription": {"done": done},
            "diarization": {"done": done},
            "prosody": {"done": done},
            "assembly": {"done": done},
        },
    }, ensure_ascii=False), encoding="utf-8")
    return p


def _wav(tmp: Path, name: str, root: Path, size: int = 1024) -> Path:
    cache = root / "data" / "wav_cache"
    cache.mkdir(parents=True, exist_ok=True)
    p = cache / name
    p.write_bytes(b"x" * size)
    return p


def _vad_under(root: Path):
    """Importa pipeline.vad con ROOT_DIR che punta a `root`.

    Rileggerlo da solo non basta: ROOT_DIR viene calcolato in
    core.config all'import, e se quel modulo è già in sys.modules il
    reload di vad rillega la costante vecchia — cioè la cartella di
    produzione. Un test che gira sulla cache vera è peggio di un test
    che non gira.
    """
    import importlib
    os.environ["A2T_ROOT_DIR"] = str(root)
    for mod in [m for m in sys.modules if m.split(".")[0] in ("core", "pipeline")]:
        sys.modules.pop(mod, None)
    return importlib.import_module("pipeline.vad")


def test_purge_keeps_wav_of_unfinished_session(tmp: Path) -> None:
    """La regola che conta: un checkpoint incompleto ha ancora bisogno
    del suo WAV. Cancellarlo significa ricominciare dal primo chunk, e
    il lavoro di notte è buttato."""
    root = tmp / "root"
    wav = _wav(tmp, "a.mp3_deadbeef_16k.wav", root)
    _checkpoint(root, "a", wav, done=False)

    vad = _vad_under(root)
    try:
        n, _ = vad.purge_wav_cache()
    finally:
        os.environ.pop("A2T_ROOT_DIR", None)

    assert n == 0, "un WAV di sessione incompleta non si cancella"
    assert wav.exists(), "checkpoint incompleto: il WAV deve restare"


def test_purge_removes_wav_of_finished_session(tmp: Path) -> None:
    """Finita la sessione il WAV non serve piu'. Se resta, la cache
    cresce di 115 MB per file e il disco si riempie in qualche mese."""
    root = tmp / "root"
    wav = _wav(tmp, "b.mp3_cafebabe_16k.wav", root)
    _checkpoint(root, "b", wav, done=True)

    vad = _vad_under(root)
    try:
        n, freed = vad.purge_wav_cache()
    finally:
        os.environ.pop("A2T_ROOT_DIR", None)

    assert n == 1, "il WAV di una sessione finita va cancellato"
    assert freed >= 1024, freed
    assert not wav.exists()


def test_purge_removes_orphans_but_only_when_old_enough(tmp: Path) -> None:
    """Un WAV che nessun checkpoint cita e' orfano. Orfano vuol dire
    che nessuno lo riapre: si butta, ma non se e' appena stato scritto,
    perche' potrebbe essere una run ancora in corso."""
    root = tmp / "root"
    vecchio = _wav(tmp, "vecchio_16k.wav", root)
    recente = _wav(tmp, "recente_16k.wav", root)
    vecchio_time = time.time() - 12 * 3600
    os.utime(vecchio, (vecchio_time, vecchio_time))

    vad = _vad_under(root)
    try:
        n, _ = vad.purge_wav_cache()
    finally:
        os.environ.pop("A2T_ROOT_DIR", None)

    assert not vecchio.exists(), "orfano di 12 ore: va buttato"
    assert recente.exists(), "orfano di pochi secondi: puo' essere una run in corso"
    assert n == 1


def test_purge_on_missing_cache_is_noop(tmp: Path) -> None:
    root = tmp / "vuoto"
    root.mkdir()
    vad = _vad_under(root)
    try:
        assert vad.purge_wav_cache() == (0, 0)
    finally:
        os.environ.pop("A2T_ROOT_DIR", None)


# -------------------------------------------------------------------
# Nomi dei parlanti: fonte unica
# -------------------------------------------------------------------

def test_rename_propagates_to_corpus_and_sessions(tmp: Path) -> None:
    """Rinominare una voce deve aggiornare anche il materiale gia'
    scritto. Prima il rename valeva solo da quel momento in poi, e le
    sessioni vecchie continuavano a dire GLOBAL_001."""
    import importlib
    from core.speaker_db import SpeakerDB

    root = tmp / "root"
    spk_db_path = root / "data" / "speakers_db.json"
    spk_db_path.parent.mkdir(parents=True, exist_ok=True)

    db = SpeakerDB(path=spk_db_path)
    db._data["speakers"] = {
        "GLOBAL_001": {"name": None, "centroid": [], "sessions": {}},
        "GLOBAL_002": {"name": None, "centroid": [], "sessions": {}},
    }
    db.save()
    db.set_name("GLOBAL_001", "Pietro")

    # una sessione gia' scritta che contiene GLOBAL_001 e GLOBAL_002
    job = root / "output" / "s1"
    job.mkdir(parents=True, exist_ok=True)
    for fname, doc in (
        ("session.json", {"stem": "s1",
                          "speaker_names": {"GLOBAL_001": "GLOBAL_001"},
                          "speakers": {"GLOBAL_001": {"segments_count": 2},
                                       "GLOBAL_002": {"segments_count": 1}}}),
        ("transcript.json", {"meta": {"stem": "s1",
                                      "speakers": ["GLOBAL_001", "GLOBAL_002"],
                                      "speaker_names": {"GLOBAL_001": "GLOBAL_001"}},
                             "segments": [{"idx": 0, "speaker": "GLOBAL_001",
                                           "start": 0.0, "end": 1.0,
                                           "text": "ciao"}]}),
    ):
        (job / fname).write_text(json.dumps(doc, ensure_ascii=False),
                                 encoding="utf-8")

    corpus_path = root / "data" / "corpus.db"
    corpus_path.parent.mkdir(parents=True, exist_ok=True)
    with CorpusDB(corpus_path) as cdb:
        cdb.ingest_session("s1", json.loads((job / "transcript.json").read_text()),
                           {})
        sync = importlib.import_module("core.speaker_sync")
        report = sync.sync_speaker_names(
            output_dir=root / "output", db=SpeakerDB(path=spk_db_path), corpus_db=cdb,
        )
        row = cdb.query("SELECT name FROM speakers WHERE global_id='GLOBAL_001'")[0]

    assert row["name"] == "Pietro", "corpus.db deve aggiornarsi al sync"
    assert report["sessions_updated"] == 1, report

    sess = json.loads((job / "session.json").read_text(encoding="utf-8"))
    assert sess["speaker_names"] == {"GLOBAL_001": "Pietro"}, sess["speaker_names"]

    tr = json.loads((job / "transcript.json").read_text(encoding="utf-8"))
    # Solo le voci nominate: GLOBAL_002 non ha nome e non viene
    # inventato un "GLOBAL_002: GLOBAL_002" che sembrerebbe un nome.
    assert tr["meta"]["speaker_names"] == {"GLOBAL_001": "Pietro"}, tr["meta"]["speaker_names"]


def test_sync_is_idempotent_and_leaves_other_speakers_alone(tmp: Path) -> None:
    """Il sync non deve riscrivere file che sono gia' allineati: sono
    file che l'utente puo' avere aperto, e una riscrittura a ogni sync
    e' rumore che poi sembra un'attivita'."""
    import importlib
    from core.speaker_db import SpeakerDB

    root = tmp / "root"
    p = root / "data" / "speakers_db.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    db = SpeakerDB(path=p)
    db._data["speakers"] = {"GLOBAL_001": {"name": "Pietro", "centroid": [], "sessions": {}}}
    db.save()

    job = root / "output" / "s1"
    job.mkdir(parents=True, exist_ok=True)
    (job / "session.json").write_text(
        json.dumps({"stem": "s1", "speaker_names": {"GLOBAL_001": "Pietro"}}),
        encoding="utf-8")

    sync = importlib.import_module("core.speaker_sync")
    before = (job / "session.json").stat().st_mtime_ns
    report = sync.sync_speaker_names(output_dir=root / "output", db=SpeakerDB(path=p))
    after = (job / "session.json").stat().st_mtime_ns

    assert before == after, "il file era gia' allineato: non deve essere riscritto"
    assert report["files_updated"] == 0, report


def test_sync_does_not_touch_corrupt_session(tmp: Path) -> None:
    """Un file di sessione corrotto non si deve perdere e non si deve
    far fallire il sync delle altre: il resto del materiale è valido."""
    import importlib
    from core.speaker_db import SpeakerDB

    root = tmp / "root"
    p = root / "data" / "speakers_db.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    db = SpeakerDB(path=p)
    db._data["speakers"] = {"GLOBAL_001": {"name": "Pietro", "centroid": [], "sessions": {}}}
    db.save()

    rotto = root / "output" / "rotto"
    rotto.mkdir(parents=True, exist_ok=True)
    (rotto / "session.json").write_text("{non è json", encoding="utf-8")

    sync = importlib.import_module("core.speaker_sync")
    report = sync.sync_speaker_names(output_dir=root / "output", db=SpeakerDB(path=p))
    assert report["sessions_updated"] == 0, report
    assert (rotto / "session.json").read_text(encoding="utf-8") == "{non è json"


def test_corpus_db_name_sync_sets_null_not_pseudonym(tmp: Path) -> None:
    """Rimuovere un nome deve scrivere NULL, non il pseudonimo: nella
    colonna 'name' un valore che sembra un nome e non lo e' e' peggio
    di nessun valore."""
    with CorpusDB(tmp / "c.db") as db:
        db.sync_speaker_names({"GLOBAL_001": "Pietro"})
        assert db.sync_speaker_names({"GLOBAL_001": "Pietro"}) == 0, "nessun cambiamento"
        assert db.sync_speaker_names({"GLOBAL_001": None}) == 1
        row = db.query("SELECT name FROM speakers WHERE global_id='GLOBAL_001'")[0]
        assert row["name"] is None, dict(row)


# -------------------------------------------------------------------

def main() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for fn in tests:
        with tempfile.TemporaryDirectory() as d:
            try:
                fn(Path(d))
                # Un test che ha ricaricato i moduli con una radice
                # temporanea li lascia cosi' per il test successivo: il
                # verdeware seguente scriverebbe dentro la cartella
                # temporanea invece che in quella giusta, e nessuno se
                # ne accorgerebbe.
                for mod in [m for m in sys.modules
                            if m.split(".")[0] in ("core", "pipeline")]:
                    sys.modules.pop(mod, None)
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
