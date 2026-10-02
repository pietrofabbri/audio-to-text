"""
CorpusDB — indice locale del corpus, interrogabile.

La repo online tiene i file leggibili (JSONL, CSV, markdown) e il suo
storico git. Questo database tiene ciò che il repository non sa fare
bene: aggregare mesi di dati in una query.

Perché entrambi, e non uno solo:

- git dà diff e storia ("come è cambiata la mia produzione verbale fra
  marzo e ottobre?"), ma aggregare richiede di rileggere tutto;
- SQLite aggrega in millisecondi, ma non ha storia né è leggibile da un
  LLM senza uno strumento.

Lo schema è pensato per le analisi che farai *domani*, non per quelle di
oggi: le tabelle sono larghe e poco normalizzate, con colonne esplicite
per ciò che si vorrà prima o poi correlare (prosodia, speaker, data).
Oggi riempie quasi tutto, domani no — e non si rifà.

Scelta deliberata: SQLite locale, non un database hosted. Il corpus è
dato biometrico-verbale di una persona; metterlo su un servizio esterno
aggiunge costo, operazioni e un punto di esposizione in più senza
guadagno che non si veda oggi.

Nessun dato audio, nessun embedding vocale, nessun nome reale: qui
finiscono i segmenti con lo pseudonimo GLOBAL_00x.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

logger = logging.getLogger(__name__)

DEFAULT_DB_PATH = Path(__file__).resolve().parent.parent / "data" / "corpus.db"

SCHEMA_VERSION = 1

# Le feature prosodiche estratte per segmento. Sono colonne esplicite
# e non un blob JSON: è ciò che permette query del tipo "la variabilità
# di F0 cambia quando parlo al mattino presto", che su un JSON in colonna
# richiederebbe di leggerlo tutto.
PROSODY_COLUMNS = (
    "f0_mean_hz", "f0_std_hz", "f0_min_hz", "f0_max_hz", "f0_range_hz",
    "intensity_mean_db", "intensity_max_db", "voiced_fraction",
    "jitter_local", "shimmer_local", "speech_rate_syl_per_sec", "pause_ratio",
)

# Punteggiatura da togliere per la forma normalizzata. La forma originale
# resta in tokens.word: in italiano la maiuscola dopo un punto ("Parlare")
# è informazione, non rumore, e un'analisi che la perde non è un'analisi.
_WORD_STRIP = ".,;:!?()[]{}\"'«»…—-"


def _normalize_word(word: str) -> str:
    # Spaziature e punteggiatura ai due estremi: il token del transcriber
    # arriva come " modo.  " e senza il primo strip la parola normalizzata
    # sarebbe "modo" con due spazi dentro, invisibile a ogni ricerca.
    return word.strip().strip(_WORD_STRIP).strip().lower()


SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- Una sessione = un file audio da ~1h importato dal registratore.
-- stem è la chiave naturale: è anche il nome della cartella di output.
CREATE TABLE IF NOT EXISTS sessions (
    stem                TEXT PRIMARY KEY,
    source_device       TEXT,          -- volume da cui è stato importato
    source_filename     TEXT,          -- nome file sul device (per audit)
    recorded_at         TEXT,          -- ISO, ora di pareggio del device
    processed_at        TEXT,
    duration_sec        REAL,
    speech_sec          REAL,
    speech_ratio        REAL,
    denoise_winner      TEXT,          -- 'original' | 'denoised'
    n_speakers          INTEGER,
    n_segments          INTEGER,
    n_words             INTEGER
);

CREATE TABLE IF NOT EXISTS speakers (
    global_id  TEXT PRIMARY KEY,
    name       TEXT             -- nome umano, NULL se non assegnato
);

-- Chi ha parlato quando, con quanto materiale e con che prosodia.
CREATE TABLE IF NOT EXISTS segments (
    id            INTEGER PRIMARY KEY,
    stem          TEXT NOT NULL REFERENCES sessions(stem) ON DELETE CASCADE,
    idx           INTEGER NOT NULL,
    speaker       TEXT,
    start_sec     REAL,
    end_sec       REAL,
    duration_sec  REAL,
    text          TEXT,
    n_words       INTEGER,
    {prosody_cols},
    UNIQUE(stem, idx)
);

-- Una parola per riga: la tabella che rende possibile KWIC, n-grammi,
-- collocazione e sincronizzazione con dati biometrici al secondo.
CREATE TABLE IF NOT EXISTS tokens (
    id          INTEGER PRIMARY KEY,
    stem        TEXT NOT NULL REFERENCES sessions(stem) ON DELETE CASCADE,
    segment_idx INTEGER NOT NULL,
    token_idx   INTEGER NOT NULL,
    word        TEXT,               -- forma originale, con maiuscola
    word_norm   TEXT,               -- minuscolo, senza punteggiatura
    start_sec   REAL,
    end_sec     REAL,
    speaker     TEXT,
    estimated   INTEGER DEFAULT 0,   -- 1 = timestamp distribuito, non reale
    UNIQUE(stem, token_idx)
);

-- Indice invertito: parola -> freq. Rende "quante volte ho detto X"
-- una query, non un parsing di CSV a ogni domanda.
CREATE TABLE IF NOT EXISTS wordfreq (
    stem        TEXT NOT NULL REFERENCES sessions(stem) ON DELETE CASCADE,
    word        TEXT NOT NULL,
    freq        INTEGER NOT NULL,
    PRIMARY KEY (stem, word)
);

-- Le analisi giornaliere scrivono qui una riga ciascuna: data, tipo,
-- esito in sintesi, e dove sta il markdown completo. È l'unico punto
-- che le analisi future dovranno toccare per diventare comparabili
-- nel tempo.
CREATE TABLE IF NOT EXISTS analyses (
    id          INTEGER PRIMARY KEY,
    date        TEXT NOT NULL,       -- YYYY-MM-DD
    kind        TEXT NOT NULL,       -- es. 'daily_digest', 'wordfreq_delta'
    summary     TEXT,                -- una riga, per il confronto nel tempo
    payload     TEXT,                -- JSON dei risultati
    source      TEXT,                -- repo o percorso del markdown
    created_at  TEXT,
    UNIQUE(date, kind)
);

-- Libri: segmenti di testo in posizioni fisse, per il confronto
-- linguistico nel tempo (registrato vs riferimento).
CREATE TABLE IF NOT EXISTS corpora (
    id        INTEGER PRIMARY KEY,
    name      TEXT UNIQUE NOT NULL,
    kind      TEXT NOT NULL,        -- 'self' | 'reference'
    path      TEXT,
    added_at  TEXT
);

CREATE TABLE IF NOT EXISTS corpora_tokens (
    corpus_id  INTEGER NOT NULL REFERENCES corpora(id) ON DELETE CASCADE,
    token_idx  INTEGER NOT NULL,
    word       TEXT NOT NULL,
    PRIMARY KEY (corpus_id, token_idx)
);

-- Co-occorrenze: con 100mila token la join token×token è 10^10 righe,
-- quindi si precalcola solo a 1 e 2 token di distanza, che è la
-- finestra standard per collocazione.
CREATE TABLE IF NOT EXISTS bigrams (
    stem  TEXT NOT NULL REFERENCES sessions(stem) ON DELETE CASCADE,
    w1    TEXT NOT NULL,
    w2    TEXT NOT NULL,
    freq  INTEGER NOT NULL,
    PRIMARY KEY (stem, w1, w2)
);

CREATE INDEX IF NOT EXISTS idx_segments_stem    ON segments(stem);
CREATE INDEX IF NOT EXISTS idx_segments_speaker ON segments(speaker);
CREATE INDEX IF NOT EXISTS idx_segments_start   ON segments(stem, start_sec);
CREATE INDEX IF NOT EXISTS idx_tokens_stem      ON tokens(stem);
CREATE INDEX IF NOT EXISTS idx_tokens_word      ON tokens(word);
CREATE INDEX IF NOT EXISTS idx_tokens_norm      ON tokens(word_norm);
CREATE INDEX IF NOT EXISTS idx_tokens_pos       ON tokens(stem, start_sec);
CREATE INDEX IF NOT EXISTS idx_wordfreq_word    ON wordfreq(word);
CREATE INDEX IF NOT EXISTS idx_analyses_date    ON analyses(date);
"""


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


class CorpusDB:
    """
    Indice locale del corpus.

    Uso:
        db = CorpusDB()
        db.ingest_session(stem="2026-10-03_2200", transcript=doc, vad=stats)
        db.record_analysis(date="2026-10-03", kind="daily_digest", summary=..., payload=...)
        for row in db.query("SELECT word, SUM(freq) c FROM wordfreq GROUP BY word ORDER BY c DESC LIMIT 20"):
            ...
    """

    def __init__(self, path: Path | str = DEFAULT_DB_PATH) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.path))
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        # WAL: le query di analisi non devono bloccare l'inserimento notturno
        self.conn.execute("PRAGMA journal_mode = WAL")
        self._init_schema()

    def _init_schema(self) -> None:
        prosody_cols = ",\n    ".join(f"{c} REAL" for c in PROSODY_COLUMNS)
        self.conn.executescript(SCHEMA.format(prosody_cols=prosody_cols))
        self.conn.execute(
            "INSERT OR IGNORE INTO meta(key, value) VALUES(?, ?)",
            ("schema_version", str(SCHEMA_VERSION)),
        )
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "CorpusDB":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # ------------------------------------------------------------------
    # Ingestione
    # ------------------------------------------------------------------

    def ingest_session(
        self,
        stem: str,
        transcript: dict[str, Any],
        vad_stats: dict[str, Any] | None = None,
        recorded_at: str | None = None,
        source_device: str | None = None,
        source_filename: str | None = None,
        denoise_winner: str | None = None,
    ) -> dict[str, int]:
        """
        Inserisce una sessione e tutto ciò che ne deriva.

        Idempotente: rilanciare sullo stesso stem sostituisce i dati
        invece di duplicarli. Serve perché un file può essere
        rielaborato (denoise migliore, modello ASR aggiornato) e il
        database non deve crescere a ogni rerun.
        """
        vad_stats = vad_stats or {}
        meta = transcript.get("meta", {})
        segments = transcript.get("segments", [])

        n_speakers = len(meta.get("speakers", [])) or len(
            {s.get("speaker") for s in segments if s.get("speaker")}
        )

        self.conn.execute("DELETE FROM segments WHERE stem = ?", (stem,))
        self.conn.execute("DELETE FROM tokens   WHERE stem = ?", (stem,))
        self.conn.execute("DELETE FROM wordfreq WHERE stem = ?", (stem,))
        self.conn.execute("DELETE FROM bigrams  WHERE stem = ?", (stem,))

        self.conn.execute(
            """
            INSERT INTO sessions (
                stem, source_device, source_filename, recorded_at, processed_at,
                duration_sec, speech_sec, speech_ratio,
                denoise_winner, n_speakers, n_segments, n_words
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(stem) DO UPDATE SET
                source_device=excluded.source_device,
                source_filename=excluded.source_filename,
                recorded_at=excluded.recorded_at,
                processed_at=excluded.processed_at,
                duration_sec=excluded.duration_sec,
                speech_sec=excluded.speech_sec,
                speech_ratio=excluded.speech_ratio,
                denoise_winner=excluded.denoise_winner,
                n_speakers=excluded.n_speakers,
                n_segments=excluded.n_segments,
                n_words=excluded.n_words
            """,
            (
                stem, source_device, source_filename,
                recorded_at or meta.get("session_start_wall") or meta.get("processed_at"),
                meta.get("processed_at"),
                meta.get("total_duration_sec"),
                meta.get("speech_duration_sec"),
                meta.get("speech_ratio"),
                denoise_winner, n_speakers,
                len(segments), meta.get("total_words", 0),
            ),
        )

        for sp in meta.get("speakers", []):
            self.conn.execute(
                "INSERT OR IGNORE INTO speakers(global_id, name) VALUES(?, ?)",
                (sp, meta.get("speaker_names", {}).get(sp)),
            )
        for gid, name in (meta.get("speaker_names") or {}).items():
            self.conn.execute(
                "UPDATE speakers SET name = ? WHERE global_id = ?", (name, gid)
            )

        # --- segmenti + prosodia ----------------------------------------
        # 8 colonne fisse (stem, idx, speaker, start, end, durata, testo,
        # n_words) più le 12 feature prosodiche
        seg_placeholders = ", ".join(["?"] * (8 + len(PROSODY_COLUMNS)))
        prosody_names = ", ".join(PROSODY_COLUMNS)
        n_seg = 0
        for s in segments:
            pros = s.get("prosody", {}) or {}
            row = [
                stem, s.get("idx"), s.get("speaker"),
                s.get("start"), s.get("end"),
                s.get("duration_sec") or (
                    (s["end"] - s["start"]) if s.get("end") is not None else None
                ),
                s.get("text"),
                len((s.get("text") or "").split()),
            ] + [pros.get(c) for c in PROSODY_COLUMNS]
            self.conn.execute(
                f"INSERT OR REPLACE INTO segments "
                f"(stem, idx, speaker, start_sec, end_sec, duration_sec, text, n_words, {prosody_names}) "
                f"VALUES ({seg_placeholders})",
                row,
            )
            n_seg += 1

        # --- token + wordfreq + bigrams ---------------------------------
        words_this_session: list[str] = []
        freq: dict[str, int] = {}
        bi: dict[tuple[str, str], int] = {}
        n_tok = 0

        for s in segments:
            toks = s.get("words") or []
            if not toks:
                # Senza word-level timestamps si ricade sul testo: le
                # parole si contano lo stesso, le posizioni no. Ecco il
                # motivo del flag "estimated" nella tabella tokens.
                text = (s.get("text") or "")
                words_this_session.extend(
                    w for w in (text.split()) if _normalize_word(w)
                )
                continue
            for w in toks:
                word = (w.get("word") or "").strip()
                if not word:
                    continue
                self.conn.execute(
                    """
                    INSERT OR REPLACE INTO tokens
                        (stem, segment_idx, token_idx, word, word_norm,
                         start_sec, end_sec, speaker, estimated)
                    VALUES (?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        stem, s.get("idx"), n_tok, word,
                        _normalize_word(word),
                        w.get("start"), w.get("end"), s.get("speaker"),
                        1 if w.get("estimated") else 0,
                    ),
                )
                words_this_session.append(word)
                n_tok += 1

        clean = [n for n in (_normalize_word(x) for x in words_this_session) if n]
        for w in clean:
            freq[w] = freq.get(w, 0) + 1
        for a, b in zip(clean, clean[1:]):
            bi[(a, b)] = bi.get((a, b), 0) + 1

        self.conn.executemany(
            "INSERT OR REPLACE INTO wordfreq(stem, word, freq) VALUES(?,?,?)",
            [(stem, w, c) for w, c in freq.items()],
        )
        self.conn.executemany(
            "INSERT OR REPLACE INTO bigrams(stem, w1, w2, freq) VALUES(?,?,?,?)",
            [(stem, a, b, c) for (a, b), c in bi.items()],
        )

        self.conn.commit()
        logger.info(
            "CorpusDB: %s — %d segmenti, %d token, %d parole distinte",
            stem, n_seg, n_tok, len(freq),
        )
        return {"segments": n_seg, "tokens": n_tok, "distinct_words": len(freq)}

    # ------------------------------------------------------------------
    # Analisi
    # ------------------------------------------------------------------

    def record_analysis(
        self,
        date: str,
        kind: str,
        summary: str = "",
        payload: dict[str, Any] | None = None,
        source: str | None = None,
    ) -> None:
        """Registra il risultato di un'analisi. Una riga per (data, tipo):
        rilanciare l'analisi sostituisce, non accoda."""
        self.conn.execute(
            """
            INSERT INTO analyses (date, kind, summary, payload, source, created_at)
            VALUES (?,?,?,?,?,?)
            ON CONFLICT(date, kind) DO UPDATE SET
                summary=excluded.summary,
                payload=excluded.payload,
                source=excluded.source,
                created_at=excluded.created_at
            """,
            (date, kind, summary, json.dumps(payload or {}, ensure_ascii=False),
             source, _now_iso()),
        )
        self.conn.commit()

    def add_reference_corpus(self, name: str, path: Path | str, kind: str = "reference") -> int:
        """
        Ingesta un testo di riferimento token per token, per il confronto
        linguistico nel tempo (registrato vs testo noto).

        Nota: i corpora di riferimento sono dati *non* personali e
        vivono solo nel database locale, non nella repo.
        """
        text = Path(path).read_text(encoding="utf-8")
        words = re.findall(r"[\wÀ-ÿ']+", text.lower())

        self.conn.execute(
            "INSERT INTO corpora(name, kind, path, added_at) VALUES(?,?,?,?) "
            "ON CONFLICT(name) DO UPDATE SET path=excluded.path, kind=excluded.kind",
            (name, kind, str(path), _now_iso()),
        )
        cur = self.conn.execute("SELECT id FROM corpora WHERE name = ?", (name,))
        cid = cur.fetchone()["id"]
        self.conn.execute("DELETE FROM corpora_tokens WHERE corpus_id = ?", (cid,))
        self.conn.executemany(
            "INSERT INTO corpora_tokens(corpus_id, token_idx, word) VALUES(?,?,?)",
            [(cid, i, w) for i, w in enumerate(words)],
        )
        self.conn.commit()
        logger.info("Corpus di riferimento '%s': %d token", name, len(words))
        return len(words)

    # ------------------------------------------------------------------
    # Query
    # ------------------------------------------------------------------

    def query(self, sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
        return list(self.conn.execute(sql, tuple(params)))

    def daily(self, limit: int = 30) -> list[sqlite3.Row]:
        return self.query(
            "SELECT stem, recorded_at, duration_sec, speech_sec, n_words, n_speakers "
            "FROM sessions ORDER BY COALESCE(recorded_at, processed_at) DESC LIMIT ?",
            (limit,),
        )

    def top_words(self, limit: int = 50, since: str | None = None) -> list[sqlite3.Row]:
        if since:
            return self.query(
                "SELECT word, SUM(freq) AS freq FROM wordfreq w "
                "JOIN sessions s USING(stem) WHERE COALESCE(s.recorded_at, s.processed_at) >= ? "
                "GROUP BY word ORDER BY freq DESC LIMIT ?",
                (since, limit),
            )
        return self.query(
            "SELECT word, SUM(freq) AS freq FROM wordfreq GROUP BY word "
            "ORDER BY freq DESC LIMIT ?",
            (limit,),
        )

    def stats(self) -> dict[str, Any]:
        one = lambda sql: self.conn.execute(sql).fetchone()[0]  # noqa: E731
        return {
            "sessions": one("SELECT COUNT(*) FROM sessions"),
            "segments": one("SELECT COUNT(*) FROM segments"),
            "tokens": one("SELECT COUNT(*) FROM tokens"),
            "distinct_words": one("SELECT COUNT(DISTINCT word) FROM wordfreq"),
            "bigrams": one("SELECT COUNT(*) FROM bigrams"),
            "speakers": one("SELECT COUNT(*) FROM speakers"),
            "analyses": one("SELECT COUNT(*) FROM analyses"),
            "reference_corpora": one("SELECT COUNT(*) FROM corpora"),
            "total_speech_hours": round(
                (one("SELECT COALESCE(SUM(speech_sec), 0) FROM sessions") or 0) / 3600, 2
            ),
        }
