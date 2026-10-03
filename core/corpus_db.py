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
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.config import ROOT_DIR  # noqa: E402

logger = logging.getLogger(__name__)

# Sotto ROOT_DIR come tutto il resto: un test end-to-end gira in una radice
# temporanea e non deve scrivere nel database di produzione.
DEFAULT_DB_PATH = ROOT_DIR / "data" / "corpus.db"

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

# Le colonne di qualità. Non sono prosodia e non sono linguistica: sono
# l'avvertenza che il testo accanto potrebbe non essere il testo che è
# stato detto. Senza, un segmento inventato dal modello entra nelle
# statistiche indistinguibile da uno vero — e in un corpus che vuole
# misurare la propria voce è il tipo di errore che non si vede.
QUALITY_COLUMNS = ("quality", "quality_reasons")

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
    name       TEXT             -- nome umano, NULL se non assegnato.
                               -- Copia: la fonte è data/speakers_db.json,
                               -- allineata con CorpusDB.sync_speaker_names()
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
    quality       TEXT,      -- ok | low | unreliable, vedi core/quality.py
    quality_reasons TEXT,
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
        self._migrate()
        self.conn.execute(
            "INSERT OR IGNORE INTO meta(key, value) VALUES(?, ?)",
            ("schema_version", str(SCHEMA_VERSION)),
        )
        self.conn.commit()

    def _migrate(self) -> None:
        """Aggiunge le colonne mancanti a un database gia' esistente.

        `CREATE TABLE IF NOT EXISTS` non aggiorna una tabella che c'e'
        gia': senza questo, un database creato prima di una colonna
        nuova la ignora silenziosamente e ogni scrittura notturna va a
        finire in una colonna che non c'e'. Il sintomo sarebbe una
        query che restituisce sempre NULL, che e' il modo piu' silenzioso
        in cui un database mente.

        SQLite ha `ALTER TABLE ... ADD COLUMN` per sempre, quindi qui si
        aggiunge e non si ricrea: i dati gia' dentro restano.
        """
        wanted = {"segments": {
            "quality": "TEXT",
            "quality_reasons": "TEXT",
        }}
        for table, cols in wanted.items():
            have = {r["name"] for r in self.conn.execute(f"PRAGMA table_info({table})")}
            for name, typ in cols.items():
                if name not in have:
                    logger.info("Migrazione: aggiungo %s.%s", table, name)
                    self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {typ}")

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "CorpusDB":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # ------------------------------------------------------------------
    # Ingestione
    # ------------------------------------------------------------------

    def ingest_session_dir(self, output_dir: Path) -> bool:
        """Ingesta la sessione scritta in `output_dir`, se e' completa.

        Ritorna True se il database e' stato aggiornato. Non solleva mai:
        chi chiama decide cosa fare di un database indietro, e di solito
        la risposta giusta e' continuare a elaborare.

        Serve perche' `ingest_session` da sola richiedeva a chi chiama di
        leggere tre file e passargli i campi giusti, e ogni percorso ne
        leggeva un insieme diverso: il device passava i dati del VAD,
        gli altri percorsi non passavano niente e il database restava
        vuoto.
        """
        output_dir = Path(output_dir)
        transcript_path = output_dir / "transcript.json"
        if not transcript_path.exists():
            return False
        try:
            transcript = json.loads(transcript_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return False

        stem = transcript.get("meta", {}).get("stem") or output_dir.name

        recorded_at = None
        session_json = output_dir / "session.json"
        if session_json.exists():
            try:
                recorded_at = json.loads(
                    session_json.read_text(encoding="utf-8")
                ).get("session_start_wall")
            except (json.JSONDecodeError, OSError):
                recorded_at = None

        vad_stats: dict[str, Any] = {}
        ck_file = output_dir / f"{stem}.checkpoint.json"
        if ck_file.exists():
            try:
                vad_stats = json.loads(
                    ck_file.read_text(encoding="utf-8")
                ).get("vad_stats") or {}
            except (json.JSONDecodeError, OSError):
                vad_stats = {}

        try:
            self.ingest_session(
                stem=stem,
                transcript=transcript,
                vad_stats=vad_stats,
                recorded_at=recorded_at,
            )
        except Exception:  # noqa: BLE001
            return False
        return True

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
        seg_placeholders = ", ".join(["?"] * (8 + len(PROSODY_COLUMNS)
                                             + len(QUALITY_COLUMNS)))
        prosody_names = ", ".join(PROSODY_COLUMNS)
        quality_names = ", ".join(QUALITY_COLUMNS)
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
            ] + [pros.get(c) for c in PROSODY_COLUMNS] + [
                s.get("quality"),
                ";".join(s.get("quality_reasons") or []) or None,
            ]
            self.conn.execute(
                f"INSERT OR REPLACE INTO segments "
                f"(stem, idx, speaker, start_sec, end_sec, duration_sec, text, n_words, "
                f"{prosody_names}, {quality_names}) "
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
    # Nomi dei parlanti
    # ------------------------------------------------------------------

    def sync_speaker_names(self, names: dict[str, str | None]) -> int:
        """Allinea la tabella `speakers` a {GLOBAL_00x: nome}.

        Il nome è di proprietà del DB delle voci (`data/speakers_db.json`),
        non di questo database: qui finisce la copia che le query del
        corpus usano. Senza questo allineamento, rinominare una voce
        lascia le query su `GLOBAL_001` per sempre, e il rename sembra
        non essere successo.

        Returns:
            quante righe sono cambiate davvero. Le voci senza nome
            diventano NULL: è preferibile un NULL onesto a un
            pseudonimo memorizzato nella colonna `name`, che sembrerebbe
            un nome e non lo è.
        """
        changed = 0
        for gid, name in names.items():
            row = self.conn.execute(
                "SELECT name FROM speakers WHERE global_id = ?", (gid,)
            ).fetchone()
            wanted = (name or None)
            if row is None:
                self.conn.execute(
                    "INSERT OR IGNORE INTO speakers(global_id, name) VALUES(?, ?)",
                    (gid, wanted),
                )
                continue
            if row["name"] == wanted:
                continue
            self.conn.execute(
                "UPDATE speakers SET name = ? WHERE global_id = ?", (wanted, gid)
            )
            changed += 1

        # E il caso inverso, che e' quello che fa male. Un nome che qui
        # c'e' e nel DB delle voci no non e' un dato che questo database
        # conosce: e' una copia rimasta indietro, e la fonte e' la
        # sola che puo' dire che quel nome non esiste piu'. Senza questo
        # cancellamento, togliere un nome dal DB delle voci non lo
        # toglie da qui, e la voce continua a comparire con quel nome
        # nelle query — che e' il contrario di quello che chiede
        # `review_speakers.py name GLOBAL_001` senza argomento.
        for row in self.conn.execute(
            "SELECT global_id FROM speakers WHERE name IS NOT NULL"
        ).fetchall():
            if row["global_id"] not in names:
                self.conn.execute(
                    "UPDATE speakers SET name = NULL WHERE global_id = ?",
                    (row["global_id"],),
                )
                changed += 1

        self.conn.commit()
        return changed

    def prune_speakers(self) -> int:
        """Rimuove dalla tabella le voci che nessuna sessione cita piu'.

        La tabella `speakers` si riempie con `INSERT OR IGNORE` e non
        aveva nessuna via per svuotirsi. Dopo un merge delle identita'
        restano li le voci assorbite: query come «chi parla di piu' nel
        corpus» continuano a dividerne il tempo fra una persona e un
        frammento di lei, e il risultato e' sbagliato in un modo che non
        si vede.

        Rimuove la riga, nome compreso, e non solleva obiezioni: qui non
        c'e' nessuna informazione da conservare. I nomi appartengono a
        `data/speakers_db.json`, che resta la fonte e non viene toccato;
        questa tabella ne e' una copia, e una copia che parla di una
        voce che non esiste piu' e' rumore. Se un nome serve ancora,
        `review_speakers.py name` lo rimette, e questa volta nel posto
        giusto.

        Returns:
            quante righe sono state rimosse.
        """
        citate = {
            r["speaker"] for r in self.conn.execute(
                "SELECT DISTINCT speaker FROM segments WHERE speaker IS NOT NULL"
            )
        }
        citate |= {
            r["speaker"] for r in self.conn.execute(
                "SELECT DISTINCT speaker FROM tokens WHERE speaker IS NOT NULL"
            )
        }
        # 'UNKNOWN' non e' una voce: e' l'assenza di un'etichetta, e
        # viene usato nelle query come "non attribuito". Non e' un
        # interlocutore e non deve sparire dalla tabella.
        citate.add("UNKNOWN")

        da_rimuovere = [
            r["global_id"] for r in self.conn.execute("SELECT global_id FROM speakers")
            if r["global_id"] not in citate
        ]
        for gid in da_rimuovere:
            self.conn.execute("DELETE FROM speakers WHERE global_id = ?", (gid,))
        self.conn.commit()
        logger.info("CorpusDB: %d voci obsolete rimosse dalla tabella",
                    len(da_rimuovere))
        return len(da_rimuovere)

    def relabel_speakers(self, renames: dict[str, str], dry_run: bool = False) -> int:
        """Rietichetta gli ID nelle tabelle del corpus dopo un merge.

        Unire due identità vocali senza questo passaggio lascia il
        database con due voci per la stessa persona: le statistiche non
        si sommano e un'analisi «per persona» divide in due chi è uno.

        Returns:
            quante righe sono cambiate. In simulazione sono contate ma
            non scritte.
        """
        if not renames:
            return 0
        changed = 0
        for old, new in renames.items():
            if old == new:
                continue
            if dry_run:
                changed += self.conn.execute(
                    "SELECT COUNT(*) c FROM segments WHERE speaker = ?", (old,)
                ).fetchone()["c"]
                continue
            for table in ("segments", "tokens"):
                if not self._has_table(table):
                    continue
                cur = self.conn.execute(
                    f"UPDATE {table} SET speaker = ? WHERE speaker = ?", (new, old)
                )
                changed += max(0, cur.rowcount or 0)
        if not dry_run:
            # La tabella speakers: la voce assorbita sparisce. Ma
            # l'ID che la assorbe deve esistere, altrimenti — cioè se
            # nel corpus c'era solo la voce che viene unita e non
            # quella che la conserva — cancellando l'unica riga si
            # lascia la tabella vuota e le query per parlante non
            # trovano più nessuno. Il nome, se c'era, segue: è
            # un'informazione che non si butta via con l'ID vecchio.
            for old, new in renames.items():
                nome = self.conn.execute(
                    "SELECT name FROM speakers WHERE global_id = ?", (old,)
                ).fetchone()
                if nome is not None and self.conn.execute(
                    "SELECT 1 FROM speakers WHERE global_id = ?", (new,)
                ).fetchone() is None:
                    self.conn.execute(
                        "INSERT OR IGNORE INTO speakers(global_id, name) VALUES(?, ?)",
                        (new, nome["name"]),
                    )
                self.conn.execute("DELETE FROM speakers WHERE global_id = ?", (old,))
            self.conn.commit()
        return changed

    def _has_table(self, name: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name = ?",
            (name,),
        ).fetchone()
        return row is not None

    # ------------------------------------------------------------------
    # Query
    # ------------------------------------------------------------------

    def query(self, sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
        return list(self.conn.execute(sql, tuple(params)))

    def quality_report(self, stem: str | None = None) -> list[dict[str, Any]]:
        """Quanto materiale è poco affidabile, per sessione.

        È la query che rende il flag utile: senza, il `quality` è una
        colonna che nessuno guarda. I motivi sono aggregati perche' la
        domanda utile non è "quanti segmenti sono brutti" ma "quante
        parole ho perso e perche'" — e la seconda è l'unica che si
        puo' correggere (un loop si blocca, una probilita bassa no).
        """
        where = "WHERE stem = ?" if stem else ""
        params = (stem,) if stem else ()
        rows = self.conn.execute(
            f"SELECT stem, quality, quality_reasons, n_words FROM segments {where}",
            params,
        )
        per_stem: dict[str, dict[str, Any]] = {}
        for r in rows:
            s = per_stem.setdefault(r["stem"], {
                "stem": r["stem"], "segments": 0, "words": 0,
                "suspect_segments": 0, "suspect_words": 0,
                "reasons": Counter(),
            })
            s["segments"] += 1
            s["words"] += r["n_words"] or 0
            if r["quality"] in ("low", "unreliable"):
                s["suspect_segments"] += 1
                s["suspect_words"] += r["n_words"] or 0
                for reason in (r["quality_reasons"] or "").split(";"):
                    if reason:
                        # Si tiene solo la parte prima dei due punti:
                        # "no_speech:0.72" e "no_speech:0.81" sono lo
                        # stesso motivo, e contarne due maschererebbe
                        # un motivo raro vicino a uno frequente.
                        s["reasons"][reason.split(":")[0]] += 1
        for s in per_stem.values():
            s["suspect_share"] = (
                round(s["suspect_words"] / s["words"], 3) if s["words"] else 0.0
            )
            s["reasons"] = dict(s["reasons"].most_common())
        return sorted(per_stem.values(),
                      key=lambda s: s["suspect_share"], reverse=True)

    def suspect_text(self, limit: int = 50) -> list[sqlite3.Row]:
        """I segmenti peggiori del corpus, per revisione a mano.

        Pensata per il momento in cui si guarda un risultato e ci si
        chiede se è reale: un elenco dei peggiori, non una media. La
        media di un corpus in cui il 5% è inventato sembra quasi uguale
        a quella di un corpus pulito, ed è per questo che il flag
        serve: la differenza si vede solo scendendo al singolo segmento.
        """
        return self.query(
            "SELECT stem, idx, speaker, start_sec, end_sec, quality, "
            "quality_reasons, text FROM segments "
            "WHERE quality IN ('low','unreliable') "
            "ORDER BY CASE quality WHEN 'unreliable' THEN 0 ELSE 1 END, n_words "
            "LIMIT ?",
            (limit,),
        )

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
