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

import argparse
import io
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
from pipeline.assembler import _arrotonda, _write_tokens_jsonl  # noqa: E402
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
# Punteggiatura: segnale debole nel confronto denoise
# -------------------------------------------------------------------

# Testo lungo a sufficienza perché la punteggiatura sia misurabile
# (sotto MIN_PUNCT_CHARS il rapporto è un numero inventato) e con un
# ritmo plausibile, così il confronto non parte già da "entrambe
# degradate" per motivi che non hanno niente a che fare con la prova.
_TESTO = (
    "oggi abbiamo parlato a lungo del progetto nuovo e di come organizzare "
    "il lavoro della settimana prossima con tutto il team, perche i numeri "
    "non tornano e nessuno capisce bene chi debba muoversi per primo, "
    "e allora se ne parla ancora per un'ora intera"
)


def _chunk(testo: str, prob: float = 0.9) -> dict:
    return {"text": testo, "words": [{"word": w, "prob": prob}
                                     for w in testo.split()]}


def _variante(nome: str, testo: str) -> "object":
    from pipeline.denoise import score_variant
    return score_variant(nome, [_chunk(testo)],
                         {"speech_duration_sec": 30, "total_duration_sec": 100})


def test_punctuation_is_measured_only_on_long_text(tmp: Path) -> None:
    """Sotto una certa lunghezza il rapporto è un numero inventato, e un
    numero inventato che può decidere una scelta è peggio di nessun
    numero: su quattro lettere, un punto cambia tutto."""
    corto = _variante("original", "va bene")
    assert corto.punctuation_per_100ch == 0.0, corto.punctuation_per_100ch

    lungo = _variante("original", _TESTO)
    assert lungo.punctuation_per_100ch > 0.0, lungo.punctuation_per_100ch


def test_punctuation_breaks_a_tie(tmp: Path) -> None:
    """Il caso per cui esiste: tutte le metriche quantitative in pari, e
    la punteggiatura dice quale delle due è il testo vero."""
    from pipeline.denoise import compare
    # Stessa confidenza, testo identico salvo i segni di fine frase:
    # è esattamente "che è il modo realistico" contro "che è il modo".
    originale = _variante("original", _TESTO)
    ripulita = _variante("denoised", _TESTO + ".")
    assert abs(originale.asr_confidence - ripulita.asr_confidence) < 1e-9

    d = compare(originale, ripulita)
    # La differenza (una virgola in piu') non basta: sotto il margine si
    # tiene l'originale, e va detto perche' il dato e' rumoroso.
    assert d["winner"] == "original", d["reasons"]
    assert "punteggio" in " ".join(d["reasons"]).lower() or "spareggio" in " ".join(d["reasons"])


def test_punctuation_never_overrides_confidence(tmp: Path) -> None:
    """Il suo campo è esattamente questo: spareggio. Con la confidenza
    che dice chiaramente una delle due, la punteggiatura non ha voto.

    Nota sui nomi: `compare()` tiene le varianti in un dizionario
    indicato per nome, quindi le due si devono chiamare per forza
    "original" e "denoised". E i numeri sono impostati a mano: le
    metriche derivate da un testo finto non hanno niente a che fare con
    quello che si sta provando qui.
    """
    from pipeline.denoise import compare

    # Più punteggiatura ma confidenza peggiore: vince l'originale.
    originale = _variante("original", _TESTO)
    ripulita = _variante("denoised", _TESTO + ". " + _TESTO)
    ripulita.asr_confidence = originale.asr_confidence - 0.30
    ripulita.words_per_sec = originale.words_per_sec
    d = compare(originale, ripulita)
    assert d["winner"] == "original", d["reasons"]
    assert "confidenza" in " ".join(d["reasons"]).lower(), d["reasons"]

    # Caso opposto: confidenza migliore, punteggiatura peggiore.
    originale2 = _variante("original", _TESTO + ". " + _TESTO)
    migliore = _variante("denoised", _TESTO)
    migliore.asr_confidence = originale2.asr_confidence + 0.30
    migliore.words_per_sec = originale2.words_per_sec
    d2 = compare(originale2, migliore)
    assert d2["winner"] == "denoised", d2["reasons"]


def test_apostrophe_is_not_punctuation(tmp: Path) -> None:
    """L'apostrofo italiano è elisione ("l'acqua"), non confine di
    frase. Contarlo inflazionerebbe il numero di ogni frase con due
    parole elise.

    Il confronto è sul NUMERO di segni, non sul rapporto per 100
    caratteri: togliere tre apostrofi cambia anche la lunghezza del
    testo, quindi il rapporto cambierebbe anche se l'apostrofo fosse
    contato correttamente. Il numero è la misura che risponde alla
    domanda.
    """
    base = _TESTO + " e l'acqua e un'altra ora"
    senza = base.replace("'", "")
    assert base.count("'") == 3, base.count("'")

    from pipeline.denoise import _PUNCT_RE  # noqa: PLC0415
    assert len(_PUNCT_RE.findall(base)) == len(_PUNCT_RE.findall(senza)), (
        "l'apostrofo non deve essere contato come punteggiatura")

    # E la conseguenza sui numeri che finiscono nella decisione.
    con_apo = _variante("original", base)
    no_apo = _variante("original", senza)
    n_seg = len(_PUNCT_RE.findall(base))
    atteso = 100.0 * n_seg / len(base)
    assert abs(con_apo.punctuation_per_100ch - atteso) < 0.01, (
        con_apo.punctuation_per_100ch, atteso)


def test_merge_relabels_written_sessions(tmp: Path) -> None:
    """Unire due identità senza rietichettare le sessioni già scritte
    lascia due ID per la stessa persona nel corpus, con statistiche che
    non si sommano. È esattamente il problema che il merge doveva
    risolvere, quindi il merge non è finito senza questo passaggio.
    """
    import importlib
    from core.speaker_db import SpeakerDB

    root = tmp / "root"
    job = root / "output" / "s1"
    job.mkdir(parents=True, exist_ok=True)
    doc = {
        "meta": {"stem": "s1", "speakers": ["GLOBAL_003"],
                 "speaker_names": {}},
        "segments": [{"idx": 0, "speaker": "GLOBAL_003", "text": "ciao"}],
    }
    (job / "transcript.json").write_text(json.dumps(doc, ensure_ascii=False),
                                         encoding="utf-8")
    (job / "session.json").write_text(json.dumps({
        "stem": "s1",
        "speakers": {"GLOBAL_003": {"segments_count": 1}},
        "speaker_names": {},
    }, ensure_ascii=False), encoding="utf-8")

    corpus_path = root / "data" / "corpus.db"
    corpus_path.parent.mkdir(parents=True, exist_ok=True)
    with CorpusDB(corpus_path) as cdb:
        cdb.ingest_session("s1", json.loads((job / "transcript.json").read_text()), {})
        sync = importlib.import_module("core.speaker_sync")
        # output_dir esplicito: il default è la cartella di produzione,
        # e un test che ci scrive sopra sarebbe peggio di un test che
        # non gira.
        rep = sync.relabel_sessions({"GLOBAL_003": "GLOBAL_001"},
                                    output_dir=root / "output", corpus_db=cdb)
        rimasti = cdb.query("SELECT DISTINCT speaker FROM segments")
        voci = cdb.query("SELECT global_id FROM speakers")

    assert rep["sessions"] >= 1, rep
    assert [r["speaker"] for r in rimasti] == ["GLOBAL_001"], [dict(r) for r in rimasti]

    tr = json.loads((job / "transcript.json").read_text(encoding="utf-8"))
    assert tr["meta"]["speakers"] == ["GLOBAL_001"], tr["meta"]["speakers"]
    assert tr["segments"][0]["speaker"] == "GLOBAL_001", tr["segments"]
    assert "GLOBAL_003" not in json.dumps(tr), "resta un riferimento al vecchio ID"

    sess = json.loads((job / "session.json").read_text(encoding="utf-8"))
    assert list(sess["speakers"]) == ["GLOBAL_001"], sess["speakers"]

    # L'ID assorbito sparisce dalla tabella voci: lasciarlo significa
    # che la prossima sessione lo ricrea da capo.
    assert [r["global_id"] for r in voci] == ["GLOBAL_001"], [dict(r) for r in voci]


def test_relabel_dry_run_changes_nothing(tmp: Path) -> None:
    """In simulazione si conta ma non si scrive: un merge andato a
    metto su una sessione reale non si ri-fa da capo."""
    import importlib
    root = tmp / "root"
    job = root / "output" / "s1"
    job.mkdir(parents=True, exist_ok=True)
    originale = {"meta": {"speakers": ["GLOBAL_003"]}, "segments": []}
    (job / "transcript.json").write_text(json.dumps(originale), encoding="utf-8")
    before = (job / "transcript.json").read_text(encoding="utf-8")

    sync = importlib.import_module("core.speaker_sync")
    rep = sync.relabel_sessions({"GLOBAL_003": "GLOBAL_001"},
                                output_dir=root / "output", dry_run=True)
    assert rep["substitutions"] >= 1, "il dry-run deve contare"
    assert (job / "transcript.json").read_text(encoding="utf-8") == before, \
        "il dry-run non deve scrivere"


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


def test_purge_removes_derivatives_of_finished_session(tmp: Path) -> None:
    """Un derivato della sessione finita sparisce anche senza essere citato.

    Il caso reale: il VAD produceva una copia della variante ripulita e
    nessun file di metadati la nominava, quindi finiva nel ramo degli
    orfani e aspettava sei ore. Con diciotto file da un'ora a notte
    sono oltre due gigabyte che sopravvivono alla passata.
    """
    root = tmp / "root"
    wav = _wav(tmp, "c_cafebabe_16k.wav", root)
    derivato = _wav(tmp, "c_cafebabe_16k_dn_deadbeef_16k.wav", root)
    _checkpoint(root, "c", wav, done=True)

    vad = _vad_under(root)
    try:
        n, freed = vad.purge_wav_cache()
    finally:
        os.environ.pop("A2T_ROOT_DIR", None)

    assert not wav.exists(), "il WAV citato della sessione finita si cancella"
    assert not derivato.exists(), (
        f"il derivato di una sessione finita si cancella subito, "
        f"non dopo sei ore: resta {derivato.name}"
    )
    assert n == 2, f"cancellati {n} file invece di 2"


def test_purge_keeps_derivatives_of_unfinished_session(tmp: Path) -> None:
    """La regola che protegge il lavoro di notte vale anche per i derivati."""
    root = tmp / "root"
    wav = _wav(tmp, "d_cafebabe_16k.wav", root)
    derivato = _wav(tmp, "d_cafebabe_16k_dn_deadbeef_16k.wav", root)
    _checkpoint(root, "d", wav, done=False)

    vad = _vad_under(root)
    try:
        n, _ = vad.purge_wav_cache()
    finally:
        os.environ.pop("A2T_ROOT_DIR", None)

    assert n == 0, "sessione a meta': non si cancella niente"
    assert wav.exists() and derivato.exists(), (
        "checkpoint incompleto: il WAV e i suoi derivati devono restare"
    )


def test_derivatives_of_another_session_are_left_alone(tmp: Path) -> None:
    """Due sessioni diverse non si cancellano a vicenda.

    Il nome di una sessione puo' essere prefisso di un'altra, quindi il
    confronto si fa sul nome intero piu' il separatore.
    """
    root = tmp / "root"
    wav = _wav(tmp, "e_cafebabe_16k.wav", root)
    altro = _wav(tmp, "e2_deadbeef_16k.wav", root)
    _checkpoint(root, "e", wav, done=True)

    vad = _vad_under(root)
    try:
        vad.purge_wav_cache()
    finally:
        os.environ.pop("A2T_ROOT_DIR", None)

    assert not wav.exists(), "la sessione finita si cancella"
    assert altro.exists(), (
        "un file di una sessione diversa non si tocca: "
        "il prefisso del nome non basta a dirgli che appartiene a questa"
    )


def _write_wav(path: Path, *, rate: int, channels: int, seconds: float = 1.0):
    """Un WAV vero, con il formato richiesto."""
    import numpy as np
    import soundfile as sf
    path.parent.mkdir(parents=True, exist_ok=True)
    t = np.arange(int(rate * seconds)) / rate
    onda = 0.2 * np.sin(2 * np.pi * 220.0 * t)
    if channels == 1:
        sf.write(str(path), onda.astype("float32"), rate, subtype="PCM_16")
    else:
        stereo = np.stack([onda, onda * 0.5], axis=1)
        sf.write(str(path), stereo.astype("float32"), rate, subtype="PCM_16")
    return path


def test_wav_16k_mono_is_reused_not_copied(tmp: Path) -> None:
    """Un audio gia' 16 kHz mono non viene copiato.

    La variante ripulita di afftdn e' gia' 16 kHz mono, quindi il VAD la
    riconverteva in un file identico: 115 MB e qualche decina di secondi
    di CPU per un'ora di audio, ogni volta.
    """
    root = tmp / "root"
    sorgente = _write_wav(root / "variante.wav", rate=16_000, channels=1)

    import shutil

    from core.config import ASRConfig, VADConfig

    vad = _vad_under(root)
    try:
        if not shutil.which("ffmpeg"):
            # La prova vera richiede ffmpeg, che questa suite non deve
            # dare per presente: il predicato resta verificato lo stesso.
            assert vad._is_wav_16k_mono(sorgente)
            return
        ottenuto = vad.VoiceActivityDetector(
            VADConfig(), ASRConfig()
        )._to_wav(sorgente)
    finally:
        os.environ.pop("A2T_ROOT_DIR", None)

    assert ottenuto == sorgente, (
        "un WAV 16 kHz mono va riusato cosi' com'e', non convertito"
    )
    cache = root / "data" / "wav_cache"
    assert not cache.exists() or not list(cache.glob("*.wav")), (
        "riusare il file non deve produrre copie in cache"
    )


def test_wav_in_other_format_is_not_reused(tmp: Path) -> None:
    """Stereo o frequenza diversa: la copia serve, e va fatta."""
    root = tmp / "root"
    stereo = _write_wav(root / "stereo.wav", rate=44_100, channels=2)

    vad = _vad_under(root)
    try:
        ok = vad._is_wav_16k_mono(stereo)
    finally:
        os.environ.pop("A2T_ROOT_DIR", None)

    assert not ok, "uno stereo a 44.1 kHz non e' un 16 kHz mono"


def test_wav_16k_mono_is_detected(tmp: Path) -> None:
    """Il formato giusto viene riconosciuto, non dedotto dal nome."""
    root = tmp / "root"
    buono = _write_wav(root / "a.wav", rate=16_000, channels=1)
    cattivo = _write_wav(root / "b.wav", rate=16_000, channels=2)
    vuoto = root / "non-esiste.wav"

    vad = _vad_under(root)
    try:
        assert vad._is_wav_16k_mono(buono), "16 kHz mono va riconosciuto"
        assert not vad._is_wav_16k_mono(cattivo), "stereo non va riconosciuto"
        assert not vad._is_wav_16k_mono(vuoto), (
            "un file che non esiste non puo' essere un formato valido"
        )
    finally:
        os.environ.pop("A2T_ROOT_DIR", None)


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


# ----------------------------------------------------------------------
# Il testo corretto dentro il corpus
# ----------------------------------------------------------------------

_PAROLE_GREZZE = "a stegnavano a matiala vera"


def _trascrizione_grezza() -> dict:
    """Una sessione minima, con word-level timestamps come le vere."""
    parole = _PAROLE_GREZZE.split()
    return {
        "meta": {"speakers": ["GLOBAL_001"], "speaker_names": {}},
        "segments": [{
            "idx": 0, "speaker": "GLOBAL_001", "start": 0.0, "end": 5.0,
            "text": _PAROLE_GREZZE,
            "words": [{"word": w, "start": float(i), "end": i + 0.4}
                      for i, w in enumerate(parole)],
            "quality": "ok",
        }],
    }


def _correzione() -> dict:
    """La correzione che il modello produrrebbe per quel segmento."""
    return {
        0: {
            "idx": 0, "discarded": False,
            "original_text": _PAROLE_GREZZE,
            "corrected_text": "a segnavano a maiala vera",
            "n_words": 5, "n_changed": 2,
            "words": [
                {"i": 0, "raw": "a", "fixed": "a", "changed": False},
                {"i": 1, "raw": "stegnavano", "fixed": "segnavano", "changed": True},
                {"i": 2, "raw": "a", "fixed": "a", "changed": False},
                {"i": 3, "raw": "matiala", "fixed": "maiala", "changed": True},
                {"i": 4, "raw": "vera", "fixed": "vera", "changed": False},
            ],
        }
    }


def test_senza_correzione_il_corpus_non_cambia(tmp: Path) -> None:
    """Nessun file di correzione, nessun cambiamento.

    Il caso normale non e' il caso eccezionale: il 90% delle sessioni
    non ha ancora una correzione, e per quelle il database deve essere
    identico a prima — non «quasi identico». Se questa prova passasse ma
    l'altra no, il silenzio sarebbe il difetto.
    """
    with CorpusDB(tmp / "c.db") as db:
        db.ingest_session(stem="s1", transcript=_trascrizione_grezza())
        r = db.query("SELECT text, text_raw, corrected, n_words_changed "
                     "FROM segments")[0]
        assert r["text"] == _PAROLE_GREZZE, dict(r)
        assert r["text_raw"] == _PAROLE_GREZZE, dict(r)
        assert r["corrected"] == 0, dict(r)
        assert r["n_words_changed"] == 0, (
            f"un conteggio e' 0 anche quando non si e' corretto, "
            f"risulta {r['n_words_changed']!r}")
        tok = [t["word"] for t in db.query(
            "SELECT word FROM tokens ORDER BY token_idx")]
        assert tok == _PAROLE_GREZZE.split(), tok


def test_il_testo_analizzato_e_il_corretto(tmp: Path) -> None:
    """Su `text` finisce il testo da analizzare, su `text_raw` l'originale.

    Non e' una questione di stile: senza il grezzo conservato uno dei
    due errori sparisce. Whisper sbaglia, il correttore sbaglia a suo
    volta, e senza i due testi affiancati non si sa quale dei due ha
    prodotto una frase sbagliata — che e' la domanda che rende
    giudicabile un correttore automatico.
    """
    with CorpusDB(tmp / "c.db") as db:
        db.ingest_session(stem="s1", transcript=_trascrizione_grezza(),
                          correzioni=_correzione())
        r = db.query("SELECT text, text_raw, corrected, n_words_changed "
                     "FROM segments")[0]
        assert r["text"] == "a segnavano a maiala vera", dict(r)
        assert r["text_raw"] == _PAROLE_GREZZE, dict(r)
        assert r["corrected"] == 1, dict(r)
        assert r["n_words_changed"] == 2, dict(r)


def test_i_timestamp_restano_validi_sul_testo_corretto(tmp: Path) -> None:
    """Le parole corrette prendono le posizioni di quelle di Whisper.

    E' il regalo che fa la regola «il numero di parole non puo'
    cambiare»: se quella regola regge, l'i-esima parola del testo
    corretto e' l'i-esima parola che l'ASR ha collocato nel tempo, e i
    timestamp restano quelli giusti. Senza quella regola i due elenchi
    si sfalserebbero e un tempo di 1,4 secondi finirebbe addosso alla
    parola sbagliata — un errore invisibile, perche' il numero c'e' e
    sembra giusto.
    """
    with CorpusDB(tmp / "c.db") as db:
        db.ingest_session(stem="s1", transcript=_trascrizione_grezza(),
                          correzioni=_correzione())
        righe = db.query("SELECT word, start_sec FROM tokens ORDER BY token_idx")
        parole = [r["word"] for r in righe]
        assert parole == "a segnavano a maiala vera".split(), parole
        assert [r["start_sec"] for r in righe] == [0.0, 1.0, 2.0, 3.0, 4.0], righe


def test_allineamento_rotto_non_viene_applicato(tmp: Path) -> None:
    """Se i due elenchi hanno lunghezze diverse, non si tocca niente.

    Non dovrebbe mai succedere — e' la garanzia del correttore — ma il
    database non puo' fidarsi di un file scritto da un altro programma.
    Applicare una correzione disallineata sposterebbe ogni parola su
    quella successiva, e i timestamp su parole sbagliate: peggio del
    testo grezzo, che almeno e' quello che qualcuno ha detto.
    """
    rotta = _correzione()
    rotta[0]["words"] = rotta[0]["words"][:3]     # tre parole per cinque
    with CorpusDB(tmp / "c.db") as db:
        db.ingest_session(stem="s1", transcript=_trascrizione_grezza(),
                          correzioni=rotta)
        parole = [t["word"] for t in db.query(
            "SELECT word FROM tokens ORDER BY token_idx")]
        assert parole == _PAROLE_GREZZE.split(), (
            f"i token devono restare quelli di Whisper, sono {parole}")


def test_ricalcolare_torna_al_grezzo(tmp: Path) -> None:
    """Rifare l'ingestione senza correzioni non lascia residui.

    `wordfreq` e `bigrams` si ricostruiscono ogni volta: se il reingest
    aggiungesse le parole corrette senza cancellare quelle di Whisper,
    il conteggio delle parole conterrebbe entrambe le versioni, e la
    statistica che si voleva correggere sarebbe falsata in modo che
    nessuno vedrebbe — il numero sarebbe semplicemente piu' alto.
    """
    with CorpusDB(tmp / "c.db") as db:
        db.ingest_session(stem="s1", transcript=_trascrizione_grezza(),
                          correzioni=_correzione())
        db.ingest_session(stem="s1", transcript=_trascrizione_grezza())
        parole = {r["word"] for r in db.query("SELECT word FROM wordfreq")}
        assert "stegnavano" in parole, parole
        assert "segnavano" not in parole, (
            f"resta una parola che nessuno ha detto: {parole}")
        r = db.query("SELECT text, corrected FROM segments")[0]
        assert r["text"] == _PAROLE_GREZZE and r["corrected"] == 0, dict(r)


def test_uno_scarto_non_entra_nel_corpus(tmp: Path) -> None:
    """Un segmento scartato resta grezzo, e resta dichiarato tale.

    Il correttore scarta quando non e' sicuro. Applicare lo scarto
    significherebbe fidarsi di una risposta che il codice stesso ha
    deciso di non fidarsi, e prenderebbe la forma peggiore: un testo
    che sembra curato ma su cui nessuno ha potuto verificare niente.
    """
    scartata = _correzione()
    scartata[0]["discarded"] = True
    with CorpusDB(tmp / "c.db") as db:
        db.ingest_session(stem="s1", transcript=_trascrizione_grezza(),
                          correzioni=scartata)
        r = db.query("SELECT text, corrected FROM segments")[0]
        assert r["text"] == _PAROLE_GREZZE and r["corrected"] == 0, dict(r)


def test_quanta_correzione_c_e(tmp: Path) -> None:
    """Le statistiche dicono quanto materiale e' davvero corretto.

    Non serve a giudicare il correttore — quello si giudica guardando le
    coppie — ma a non lasciarsi sfuggire che un corpus «corretto al 40%»
    e' un corpus su cui il conteggio delle parole continua a sbagliare
    per il 60% restante, e su cui fare le analisi e' prematuro.
    """
    with CorpusDB(tmp / "c.db") as db:
        db.ingest_session(stem="s1", transcript=_trascrizione_grezza(),
                          correzioni=_correzione())
        st = db.correction_stats()
        assert st["segments"] == 1 and st["corrected_segments"] == 1, st
        assert st["corrected_share"] == 1.0, st
        assert st["words"] == 5 and st["words_changed"] == 2, st
        assert abs(st["changed_share"] - 0.4) < 1e-9, st

        db.ingest_session(stem="s1", transcript=_trascrizione_grezza())
        st = db.correction_stats()
        assert st["corrected_segments"] == 0 and st["words_changed"] == 0, st
        assert st["corrected_share"] == 0.0, st


def test_ingest_dir_legge_la_correzione(tmp: Path) -> None:
    """Il percorso vero della pipeline: la cartella, non i parametri.

    Tutti gli altri test passano le correzioni a mano. Questo verifica
    che `ingest_session_dir` trovi da solo il file nella cartella della
    sessione, perche' e' l'unica chiamata che fa la notte.
    """
    d = tmp / "2026-10-02_19-42-33"
    d.mkdir(parents=True)
    tr = _trascrizione_grezza()
    (d / "transcript.json").write_text(
        json.dumps({"meta": {"stem": "2026-10-02_19-42-33"}, **tr}),
        encoding="utf-8")
    (d / "text_correction.json").write_text(
        json.dumps({"segments": [{**_correzione()[0]}]}), encoding="utf-8")

    with CorpusDB(tmp / "c.db") as db:
        assert db.ingest_session_dir(d), "la sessione doveva essere ingestata"
        r = db.query("SELECT text, text_raw FROM segments")[0]
        assert r["text"] == "a segnavano a maiala vera", dict(r)
        assert r["text_raw"] == _PAROLE_GREZZE, dict(r)


def test_correzione_illeggibile_non_blocca(tmp: Path) -> None:
    """Un file di correzione rotto non deve far perdere la sessione.

    Una notte interrotta lascia file a meta'. Se un JSON troncato
    facesse saltare l'ingestione, si perderebbe tutto il resto della
    sessione — e proprio nel momento in cui i dati sono incompleti e
    quindi piu' utili, si avrebbe un database con un buco in piu' e
    nessuna spiegazione.
    """
    d = tmp / "2026-10-02_19-42-33"
    d.mkdir(parents=True)
    (d / "transcript.json").write_text(
        json.dumps({"meta": {"stem": "2026-10-02_19-42-33"},
                    **_trascrizione_grezza()}), encoding="utf-8")
    (d / "text_correction.json").write_text("{rotto", encoding="utf-8")

    with CorpusDB(tmp / "c.db") as db:
        assert db.ingest_session_dir(d), "la sessione doveva entrare lo stesso"
        r = db.query("SELECT text, corrected FROM segments")[0]
        assert r["text"] == _PAROLE_GREZZE and r["corrected"] == 0, dict(r)


def test_correzioni_ignora_chi_non_ha_indice(tmp: Path) -> None:
    """Una riga senza indice non deve far cadere la lettura del file.

    Il file lo scrive un altro programma e puo' contenere una riga
    strana. Perderla e' giusto; far fallire tutte le altre no.
    """
    d = tmp / "s1"
    d.mkdir()
    (d / "text_correction.json").write_text(json.dumps({"segments": [
        {"discarded": True, "corrected_text": "niente"},
        {"idx": "non_un_numero", "corrected_text": "x"},
        {"idx": 7, "corrected_text": "a segnavano a maiala vera",
         "n_changed": 2, "words": []},
    ]}), encoding="utf-8")
    corr = CorpusDB.load_corrections(d)
    assert set(corr) == {7}, corr


# ---------------------------------------------------------------------------
# La probabilita' per parola arriva al file delle parole
# ---------------------------------------------------------------------------

# Il numero con cui si distingue un errore di riconoscimento da una
# parola che suona stretta solo perche' e' dialettale esiste gia' nel
# checkpoint, ma il checkpoint e' un file di lavoro: si cancella, si
# rigenera, e con lui la traccia di quale parola il modello acustico
# aveva capito. Se il dato non arriva a tokens.jsonl, il correttore
# finisce per non poterlo usare, ed e' gia' successo.


def test_probabilita_per_parola_in_tokens(tmp: Path) -> None:
    segmenti = [
        {"idx": 0, "start": 0.0, "end": 2.0, "text": "siamo qui",
         "speaker": "GLOBAL_001",
         "words": [{"word": " siamo", "start": 0.0, "end": 0.4, "prob": 0.9},
                   {"word": " qui", "start": 0.5, "end": 0.9, "prob": 0.2}]},
        # Senza timestamp di parola: le parole vengono distribuite
        # nell'intervallo, e la probabilita' non esiste.
        {"idx": 1, "start": 3.0, "end": 4.0, "text": "va bene",
         "speaker": "GLOBAL_001"},
    ]
    path = tmp / "tokens.jsonl"
    _write_tokens_jsonl(segmenti, path)
    righe = [json.loads(l) for l in
             path.read_text(encoding="utf-8").splitlines() if l.strip()]
    assert len(righe) == 4, righe
    assert righe[0]["asr_prob"] == 0.9, righe[0]
    assert righe[1]["asr_prob"] == 0.2, righe[1]
    for r in righe[2:]:
        assert r["asr_prob"] is None, (
            "senza timestamp di parola la probabilita' resta None, "
            f"non 0.0: {r}")


def test_probabilita_non_numerica_non_diventa_zero(tmp: Path) -> None:
    # 0.0 direbbe che Whisper era sicuro che la parola non fosse stata
    # pronunciata: e' un'altra affermazione, e non e' vera.
    assert _arrotonda(None) is None
    assert _arrotonda("alta") is None
    assert _arrotonda(True) is None
    assert _arrotonda(0.123456) == 0.1235


def test_migrazione_riempie_il_testo_originale(tmp: Path) -> None:
    """Un database gia' scritto deve avere l'originale anche lui.

    `ALTER TABLE ADD COLUMN` aggiunge la colonna vuota, e una riga
    scritta prima della correzione avrebbe l'originale a NULL. Non e'
    un dettaglio: `suspect_text` serve a rivedere a mano i segmenti
    sospetti, e una colonna vuota proprio li' e' useless. Il valore non
    si indovina, si copia: per una riga mai corretta il testo da
    analizzare *e'* quello di Whisper.
    """
    path = tmp / "vecchio.db"
    with CorpusDB(path) as db:
        db.ingest_session(stem="s1", transcript=_trascrizione_grezza())

    # Si simulate il database di prima: colonne nuove assenti e vuote.
    with CorpusDB(path) as db:
        db.conn.execute("UPDATE segments SET text_raw = NULL")
        db.conn.execute("UPDATE segments SET n_words_changed = NULL")

    with CorpusDB(path) as db:
        r = db.query("SELECT text, text_raw, n_words_changed FROM segments")[0]
        assert r["text_raw"] == _PAROLE_GREZZE, dict(r)
        assert r["n_words_changed"] == 0, dict(r)

    # E riaprirlo non deve cambiare niente: la migrazione non deve
    # fare male neppure quando non c'e' niente da fare.
    with CorpusDB(path) as db:
        r = db.query("SELECT text_raw FROM segments")[0]
        assert r["text_raw"] == _PAROLE_GREZZE, dict(r)


# ---------------------------------------------------------------------------
# Il file troncato: si salva il pezzo, non si lascia tutto sul device
# ---------------------------------------------------------------------------


class _LettoreCheSiBlocca:
    """Un file che dopo N byte smette di rispondere.

    È quello che fa un registratore spento mentre scrive: la voce in
    directory promette un'ora di audio, ma gli offset oltre la dimensione
    allocata non si possono leggere e ogni lettura li chiede. Il difetto
    si vede solo con dati veri — l'ho trovato solo sul device, non in un
    test precedente — e qui lo si riproduce a mano per poterlo provare.
    """

    def __init__(self, dati: bytes, dopo: int, max_chunk: int | None = None) -> None:
        self._buf = io.BytesIO(dati)
        self._dopo = dopo
        self._max = max_chunk

    def __enter__(self) -> "_LettoreCheSiBlocca":
        return self

    def __exit__(self, *_exc) -> bool:
        return False

    def read(self, n: int = -1) -> bytes:
        # `max_chunk` modella quello che fa il driver exFAT: non rifiuta
        # l'offset, rifiuta la lettura che lo VARCA. Un blocco largo che
        # arriva oltre il confine muore, uno stretto che ci arriva passa.
        if self._max is not None and n > self._max:
            raise OSError(22, "Invalid argument")
        # La letture finisce dove finisce la zona leggibile, non a blocchi
        # interi oltre: un file vero restituisce fino alla dimensione
        # allocata e poi solleva, e il doppio di qui sta nel fascio di
        # byte che il salvataggio deve tenere.
        if self._buf.tell() >= self._dopo:
            raise OSError(22, "Invalid argument")
        return self._buf.read(min(n, self._dopo - self._buf.tell()))


class _FonteCheSiBlocca:
    """Un Path che apre su un lettore che si blocca."""

    def __init__(self, dati: bytes, dopo: int, max_chunk: int | None = None) -> None:
        self._leggitore = _LettoreCheSiBlocca(dati, dopo, max_chunk)

    def open(self, mode: str):  # noqa: D102
        return self._leggitore


def test_salvataggio_copia_fino_all_errore(tmp: Path) -> None:
    """Il pezzo salvato deve essere esattamente quello che si e' potuto
    leggere: ne un byte in piu' (inventato) ne uno in meno (buttato via)."""
    import importlib
    sd = importlib.import_module("sync_device")

    dati = b"a" * 3000
    dest = tmp / "salvato.mp3"
    scritti, motivo = sd._salva_prefisso(_FonteCheSiBlocca(dati, 1000), dest)

    assert scritti == 1000, scritti
    assert dest.read_bytes() == dati[:1000], dest.stat().st_size
    # Il motivo porta i byte recuperati: senza quel numero la risposta
    # "ho salvato qualcosa" non dice se si puo' chiudere il caso.
    assert "1000" in motivo, motivo


def test_salvataggio_di_un_file_intero_non_inventa_problemi(tmp: Path) -> None:
    import importlib
    sd = importlib.import_module("sync_device")

    src = tmp / "sano.mp3"
    src.write_bytes(b"b" * 4096)
    dest = tmp / "fuori.mp3"
    scritti, motivo = sd._salva_prefisso(src, dest)

    assert scritti == 4096, scritti
    assert dest.read_bytes() == src.read_bytes()
    assert "interamente" in motivo, motivo


def test_salvataggio_riprova_con_blocchi_piu_piccoli(tmp: Path) -> None:
    """Il confine non e' netto: si perde solo se si smette di chiedere.

    Il driver rifiuta la lettura che varca il confine, non quella che ci
    arriva esattamente. Leggendo a 1 MiB ci si ferma all'ultimo MiB intero
    e si buttano via i byte in mezzo. Su un file vero misurato: 3.145.728
    byte con blocchi da 1 MiB, 3.538.944 con blocchi da 4 KiB.
    """
    import importlib
    sd = importlib.import_module("sync_device")

    dati = b"c" * 3000
    # tutto leggibile, ma solo con richieste sotto 100.000 byte
    fonte = _FonteCheSiBlocca(dati, dopo=3000, max_chunk=100_000)
    scritti, motivo = sd._salva_prefisso(fonte, tmp / "fuori.mp3")

    assert scritti == 3000, scritti
    assert (tmp / "fuori.mp3").read_bytes() == dati
    # il motivo deve dire con quale blocco si e' arrivati al fondo: un
    # "si ferma a 3000 byte" senza altro non distingue il salvataggio
    # riuscito da quello che si e' arreso subito
    assert str(sd.SALVATAGGIO_CHUNK_MINIMO) in motivo, motivo


def test_salvataggio_si_arrende_sul_blocco_minimo(tmp: Path) -> None:
    """Se anche il blocco piu' piccolo viene rifiutato, si smette: non si
    deve provare un blocco alla volta fino alla fine del file."""
    import importlib
    sd = importlib.import_module("sync_device")

    fonte = _FonteCheSiBlocca(b"d" * 3000, dopo=3000, max_chunk=1)
    scritti, motivo = sd._salva_prefisso(fonte, tmp / "vuoto.mp3")

    assert scritti == 0, scritti
    assert (tmp / "vuoto.mp3").stat().st_size == 0
    assert "blocchi da" in motivo, motivo


def _wav_silenzio(path: Path, secondi: float = 20.0, rate: int = 16000) -> int:
    """Un WAV vero, scritto a mano: la pipeline e ffprobe devono
    riconoscerlo come audio, altrimenti il test del salvataggio non
    prova niente sul salvataggio."""
    import wave
    n = int(rate * secondi)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"\x00\x00" * n)
    return path.stat().st_size


def _ambiente_pull(sd, tmp: Path, *, ok: bool = True):
    """Un `cmd_pull` che non tocca niente fuori da `tmp`.

    Restituisce (namespace, modulo finto di `run`, elaborati, corpus finto).
    Il modulo finto sta al posto di quello vero: process_file con un
    registratore vero dentro costerebbe un'ora e un modello, e qui si
    vuole provare la catena del salvataggio, non l'ASR.
    """
    import types

    device = tmp / "device" / "RECORD"
    device.mkdir(parents=True)
    out = tmp / "output"
    logs = tmp / "logs"
    for p in (out, logs):
        p.mkdir(parents=True, exist_ok=True)

    sd.ROOT = tmp
    sd.OUTPUT_DIR = out
    sd.LOGS_DIR = logs
    sd.LAVORO_DIR = logs / "lavoro"
    sd.MANIFEST_PATH = logs / "device_manifest.jsonl"

    elaborati: list[str] = []

    def process_file(path, config, ns, stem=None):
        elaborati.append(stem)
        if not ok:
            return False
        doc = {
            "segments": [
                {"start": 0.0, "end": 1.0,
                 "text": "una frase con dentro almeno cinque parole"}
            ],
        }
        d = out / stem
        d.mkdir(parents=True, exist_ok=True)
        (d / "transcript.json").write_text(json.dumps(doc), encoding="utf-8")
        return True

    run_finto = types.ModuleType("run")
    run_finto.process_file = process_file

    presi: list[dict] = []

    class _CorpusFinto:
        def __enter__(self): return self
        def __exit__(self, *_exc): return False
        def ingest_session(self, **kw): presi.append(kw)

    sd.CorpusDB = _CorpusFinto

    args = argparse.Namespace(
        source=str(tmp / "device"), mounts=None, limit=None, no_prosody=False,
        dry_run=False, max_seconds=0, cooldown_sec=0, no_delete=False,
    )
    return args, run_finto, elaborati, presi


def _hash_rotto(sd, path: Path, rotto: bool):
    """file_sha256 che si rifiuta di leggere tutto `path`, come fa il
    driver exFAT quando gli offset chiesti sono oltre la dimensione
    allocata. Gli altri file si hashano normalmente."""
    vero = sd.file_sha256

    def wrapper(p, chunk=1 << 20):
        if rotto and Path(p) == path:
            raise OSError(22, "Invalid argument")
        return vero(p, chunk)

    sd.file_sha256 = wrapper
    return vero


def test_il_rtf_dichiarato_copre_il_caso_peggiore_misurato(tmp: Path) -> None:
    """La stima di partenza deve stare SOPRA il caso peggiore misurato.

    Se sta accanto, o peggio sotto, la coda inizia un file che non finisce
    dentro la finestra: il tempo di notte e' sprecato e il file si rifa'
    da capo. Il margine c'e' apposta, e questa e' la misura che lo
    protegge.

    I sei valori vengono dal manifest della notte del 4 ottobre: sei file
    da un'ora da un registratore USB vero, tempi presi da
    logs/device_manifest.jsonl. Non sono un'opinione e non si possono
    aggiornare da soli: se la macchina cambia, vanno rimesi e questo
    test va aggiornato con loro.
    """
    import importlib
    sd = importlib.import_module("sync_device")

    misurati = [0.134, 0.199, 0.212, 0.238, 0.279, 0.287]
    peggiore = max(misurati)
    # DEFAULT_RTF e' una property: va letta su un'istanza, non sulla classe
    stimato = sd._Budget(0).DEFAULT_RTF
    assert stimato >= peggiore, (
        f"la stima {stimato:.3f} sta sotto il caso peggiore "
        f"misurato {peggiore:.3f}: la notte inizierebbe un file che non finisce")

    # e resta sotto un ordine di grandezza, altrimenti non si inizia
    # piu' nulla e la stima e' solo un altro modo di non lavorare
    assert stimato < 1.0, stimato


def _righe_manifest(sd) -> list[dict]:
    return [json.loads(x) for x in
            sd.MANIFEST_PATH.read_text(encoding="utf-8").splitlines() if x]


def test_la_copia_di_lavoro_ha_lo_stesso_nome_del_device(tmp: Path) -> None:
    """Il nome della copia di lavoro non si puo' cambiare.

    La pipeline scrive `meta.stem` in transcript.json a partire dal nome
    del file che le si passa, e da li il corpus prende il nome della
    sessione. Rinominare la copia produceva due nomi per la stessa
    registrazione — cartella `2026-10-04_10-49-40`, corpus
    `2026-10-04_10-49-40-1508d6ee` — che non tornavano piu' insieme. A
    separarli basta la cartella, quindi il nome si lascia com'e'.
    """
    import importlib
    sd = importlib.import_module("sync_device")

    nome = "2026-10-04_10-49-40.MP3"
    lavoro = sd._lavoro_path(nome)
    assert lavoro.name == nome, lavoro
    assert lavoro.parent == sd.LAVORO_DIR, lavoro
    # e non sta accanto all'originale, altrimenti non potrebbe chiamarsi
    # come lui senza schiacciarlo
    assert (tmp / nome).resolve() != lavoro.resolve()


def test_file_troncato_viene_elaborato_e_cancellato(tmp: Path) -> None:
    """Il caso trovato sul device vero.

    Un file che non si lascia leggere per intero non deve restare sul
    registratore per sempre: si salva il pezzo leggibile, lo si
    elabora, e solo dopo si cancella. E quando si cancella, non resta
    sul device nulla che non sia gia' in archivio.
    """
    import importlib
    sd = importlib.import_module("sync_device")

    nome = "2026-10-04_10-49-40.MP3"
    args, run_finto, elaborati, presi = _ambiente_pull(sd, tmp)
    troncato = tmp / "device" / "RECORD" / nome
    _wav_silenzio(troncato)

    sys.modules["run"] = run_finto
    vero = _hash_rotto(sd, troncato, rotto=True)
    try:
        codice = sd.cmd_pull(args)
    finally:
        sys.modules.pop("run", None)
        sd.file_sha256 = vero

    assert codice == 0, codice
    # elaborato, non saltato: era una registrazione a meta', non spazzatura
    assert elaborati == ["2026-10-04_10-49-40"], elaborati
    assert (sd.OUTPUT_DIR / "2026-10-04_10-49-40" / "transcript.json").exists()

    # il device e' pulito davvero
    assert not troncato.exists(), "il file troncato e' ancora sul device"

    # e il pezzo recuperabile e' in archivio: cancellare senza archiviare
    # sarebbe stato buttare via l'unica copia dei dati letti
    archivi = list((tmp / "archive").iterdir())
    assert len(archivi) == 1, archivi
    assert archivi[0].stat().st_size > sd.SALVATAGGIO_MIN_BYTES, archivi[0]
    # con il nome della sessione e l'estensione del device: un archivio
    # chiamato come la copia di lavoro non si riconosce da nessuna parte
    assert archivi[0].name == "2026-10-04_10-49-40.MP3", archivi[0]

    # il manifest deve dire che era troncato, e con quale nome vero
    righe = _righe_manifest(sd)
    assert len(righe) == 1, righe
    assert righe[0]["action"] == "deleted", righe[0]
    assert righe[0]["troncato"] == nome, righe[0]
    assert righe[0]["file"] == nome, righe[0]

    # e il corpus deve aver ricevuto il nome del device, non quello della
    # copia di lavoro: il nome che si vede sul registratore e' il dato
    assert presi and presi[0]["source_filename"] == nome, presi


def test_troncato_senza_audio_recuperabile_resta_sul_device(tmp: Path) -> None:
    """Se dal file rotto non esce niente di utilizzabile non si dichiara
    la sessione elaborata e non si cancella: e' meglio un file che resta
    che un device con dentro un buco che sembra una registrazione."""
    import importlib
    sd = importlib.import_module("sync_device")

    nome = "2026-10-04_11-00-00.MP3"
    args, run_finto, elaborati, presi = _ambiente_pull(sd, tmp)
    rotto = tmp / "device" / "RECORD" / nome
    rotto.write_bytes(b"\x00" * 200_000)  # spazzatura: nessun audio

    sys.modules["run"] = run_finto
    vero = _hash_rotto(sd, rotto, rotto=True)
    try:
        codice = sd.cmd_pull(args)
    finally:
        sys.modules.pop("run", None)
        sd.file_sha256 = vero

    assert elaborati == [], elaborati
    assert rotto.exists(), "un file senza audio recuperabile non si cancella"
    if sd.LAVORO_DIR.exists():
        assert not list(sd.LAVORO_DIR.iterdir()), "resta una copia di lavoro inutile"
    righe = _righe_manifest(sd)
    assert righe[0]["action"] == "kept", righe[0]
    assert righe[0]["sha256"] is None, righe[0]


def test_dry_run_su_file_troncato_non_scrive_nulla(tmp: Path) -> None:
    """Il dry-run e' un piano. Salvare un pezzo e' un'operazione vera:
    se il piano la fa, il piano non e' piu' un piano."""
    import importlib
    sd = importlib.import_module("sync_device")

    nome = "2026-10-04_12-00-00.MP3"
    args, run_finto, elaborati, presi = _ambiente_pull(sd, tmp)
    troncato = tmp / "device" / "RECORD" / nome
    _wav_silenzio(troncato)

    args.dry_run = True
    sys.modules["run"] = run_finto
    vero = _hash_rotto(sd, troncato, rotto=True)
    try:
        sd.cmd_pull(args)
    finally:
        sys.modules.pop("run", None)
        sd.file_sha256 = vero

    assert troncato.exists(), "il dry-run non tocca il device"
    assert not sd.LAVORO_DIR.exists(), "il dry-run non scrive copie di lavoro"
    assert elaborati == [], elaborati


def test_pull_fallito_lascia_il_file_troncato_sul_device(tmp: Path) -> None:
    """La catena vale anche quando la pipeline va male: senza output
    verificato non si cancella, nemmeno un file gia' parzialmente
    recuperato."""
    import importlib
    sd = importlib.import_module("sync_device")

    nome = "2026-10-04_13-00-00.MP3"
    args, run_finto, elaborati, presi = _ambiente_pull(sd, tmp, ok=False)
    troncato = tmp / "device" / "RECORD" / nome
    _wav_silenzio(troncato)

    sys.modules["run"] = run_finto
    vero = _hash_rotto(sd, troncato, rotto=True)
    try:
        codice = sd.cmd_pull(args)
    finally:
        sys.modules.pop("run", None)
        sd.file_sha256 = vero

    assert codice == 1, codice
    assert troncato.exists(), "pipeline fallita: il file resta sul device"


def test_salvataggio_non_stampa_avvisi_di_contraddizione(tmp: Path) -> None:
    """Un avviso di 'impossibile cancellare' accanto a un cancellamento
    riuscito e' rumore che copre il rumore vero.

    Il pezzo di lavoro sparisce con l'archivio, quindi cancellarlo dopo e'
    cancellare un file che non c'e' piu': si deve dire che c'e' gia' stato
    fatto, non che non si e' potuto fare.
    """
    import importlib
    import logging
    sd = importlib.import_module("sync_device")

    nome = "2026-10-04_14-00-00.MP3"
    args, run_finto, elaborati, presi = _ambiente_pull(sd, tmp)
    troncato = tmp / "device" / "RECORD" / nome
    _wav_silenzio(troncato)

    raccolti: list[str] = []

    class _Raccoglie(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            # getMessage() formatta gia' gli argomenti: riformattarlo
            # qui leverebbe un '%s' che nel testo non c'era piu'.
            raccolti.append(record.getMessage())

    manometro = _Raccoglie()
    log = logging.getLogger("sync_device")
    log.addHandler(manometro)
    sys.modules["run"] = run_finto
    vero = _hash_rotto(sd, troncato, rotto=True)
    try:
        codice = sd.cmd_pull(args)
    finally:
        sys.modules.pop("run", None)
        sd.file_sha256 = vero
        log.removeHandler(manometro)

    assert codice == 0, codice
    assert not troncato.exists()
    rumore = [m for m in raccolti if "impossibile cancellare" in m]
    assert not rumore, rumore


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
