"""
SpeakerDB — identità vocali persistenti cross-file.

Il diarizer di pyannote etichetta gli speaker con ID locali (SPEAKER_00,
SPEAKER_01) che valgono solo per una singola sessione. Questo modulo
mette in relazione le voci tra sessioni diverse usando gli embedding
vocali che pyannote calcola già durante il clustering.

Flusso:
    sessione A: SPEAKER_00 -> nessun match -> nasce GLOBAL_001
    sessione B: SPEAKER_00 -> coseno vs GLOBAL_001 = 0.86 -> GLOBAL_001
    sessione C: SPEAKER_01 -> coseno 0.61 (sotto soglia) -> nasce GLOBAL_002

Il "centroide" di ogni voce globale viene aggiornato come media pesata
dalle durate, quindi la qualità del match migliora con l'accumulo di
materiale. Rilavorare la stessa sessione non gonfia le statistiche: i
contributi sono indicizzati per sessione e quindi sovrascritti.

Persistenza: un JSON in data/speakers_db.json. Sono dati biometrici
(embedding vocali = identificatore biometrico), quindi il file è in
.gitignore e va trattato come dato sensibile.

Nessuna dipendenza dai modelli: il modulo lavora su vettori float già
calcolati, il che lo rende testabile senza audio e senza GPU.
"""

from __future__ import annotations

import copy
import json
import logging
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.config import ROOT_DIR  # noqa: E402

logger = logging.getLogger(__name__)


# Sotto ROOT_DIR come tutto il resto: gli embedding vocali di un test
# non devono entrare nel DB di produzione e falsarne i match.
DEFAULT_DB_PATH = ROOT_DIR / "data" / "speakers_db.json"

# Soglia di similarità coseno per considerare due voci la stessa persona.
# pyannote usa 0.7045 per il proprio clustering agglomerativo: partiamo
# leggermente sopra per essere più restrittivi, dato che qui il confronto
# avviene tra sessioni diverse (voce, rumore, distanza microfonica
# variano) e non dentro un singolo file.
DEFAULT_THRESHOLD = 0.78

# Gli ID che non sono di questo formato vengono ignorati da `_next_id`:
# un ID portato da un formato precedente deve contare come occupato,
# altrimenti il numero successivo lo riutilizzerebbe.
_GLOBAL_ID = re.compile(r"^GLOBAL_(\d+)$")


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


def to_vector(embedding: Any):
    """
    Normalizza un embedding pyannote in un vettore 1-D float32.

    pyannote 4.x restituisce un ndarray di shape (1, D) per speaker
    (il centroide calcolato dal clustering), ma la forma può variare
    fra versioni. Se l'array è 2-D si media sulle righe: concatenare
    would've prodotto un vettore di lunghezza sbagliata e cosine senza
    significato.
    """
    import numpy as np

    arr = np.asarray(embedding, dtype=np.float32)
    if arr.ndim == 0:
        return arr.reshape(1)
    if arr.ndim == 1:
        return arr
    if arr.ndim == 2:
        return arr.mean(axis=0)
    return arr.reshape(arr.shape[-1])


def cosine_similarity(a, b) -> float:
    """Similarità coseno fra due vettori. 0.0 se uno dei due è nullo."""
    import numpy as np

    va, vb = np.asarray(a, dtype=np.float32), np.asarray(b, dtype=np.float32)
    if va.size == 0 or vb.size == 0 or va.shape != vb.shape:
        return 0.0
    denom = float(np.linalg.norm(va) * np.linalg.norm(vb))
    if denom == 0.0:
        return 0.0
    return float(np.dot(va, vb) / denom)


def _merge_into(data: dict, keep: str, drop: str) -> None:
    """Versa `drop` dentro `keep` dentro il dizionario passato.

    Funzione pura a parte dal dizionario che riceve: nessun file, nessun
    salvataggio. Serve perche' `merge_ids` con `dry_run` deve poter fare
    il conto senza toccare il DB — e il conto non si puo' fare a meta',
    perche' una meta' fusione scrive gia' i secondi sommati.
    """
    src = data["speakers"].pop(drop)
    dst = data["speakers"][keep]
    for k, v in src.get("sessions", {}).items():
        dst.setdefault("sessions", {})
        if k in dst["sessions"]:
            dst["sessions"][k]["seconds"] = round(
                dst["sessions"][k].get("seconds", 0) + v.get("seconds", 0), 2
            )
        else:
            dst["sessions"][k] = v
    dst["total_seconds"] = round(
        sum(s.get("seconds", 0) for s in dst.get("sessions", {}).values()), 2
    )
    dst["sessions_count"] = len({
        s.get("stem") for s in dst.get("sessions", {}).values()
    })
    for campo, peggio in (("first_seen", min), ("last_seen", max)):
        if src.get(campo) and dst.get(campo):
            dst[campo] = peggio(dst[campo], src[campo])
    if not dst.get("name") and src.get("name"):
        dst["name"] = src["name"]
    if not dst.get("info") and src.get("info"):
        dst["info"] = src["info"]


class SpeakerDB:
    """
    Database di identità vocali globali.

    Uso:
        db = SpeakerDB(db_path, threshold=0.78)
        mapping = db.resolve("2026-10-02_notte", {
            "SPEAKER_00": {"embedding": [...], "seconds": 3600.0},
        })
        # mapping = {"SPEAKER_00": "GLOBAL_001"}

        db.set_name("GLOBAL_001", "Pietro")   # etichetta manuale
    """

    SCHEMA_VERSION = 1

    def __init__(
        self,
        path: Path | str = DEFAULT_DB_PATH,
        threshold: float = DEFAULT_THRESHOLD,
        update_centroid: bool = True,
    ) -> None:
        self.path = Path(path)
        self.threshold = threshold
        self.update_centroid = update_centroid
        self._data = self._load()

    # ------------------------------------------------------------------
    # Persistenza
    # ------------------------------------------------------------------

    def _empty(self) -> dict[str, Any]:
        return {
            "schema_version": self.SCHEMA_VERSION,
            "speakers": {},
            "created_at": _now_iso(),
            "updated_at": _now_iso(),
        }

    def _load(self) -> dict[str, Any]:
        if not self.path.exists():
            return self._empty()

        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            # Non perdere il lavoro notturno per un file corrotto: lo
            # si mette da parte e si riparte da zero, che è il caso
            # meno distruttivo (i match si ricostruiscono dai file audio).
            backup = self.path.with_suffix(".corrupt.json")
            logger.error(
                "speakers_db.json illeggibile (%s), backup in %s e riparto da zero",
                exc, backup.name,
            )
            try:
                self.path.replace(backup)
            except OSError:
                pass
            return self._empty()

        if data.get("schema_version") != self.SCHEMA_VERSION:
            logger.warning(
                "speakers_db.json con schema %s (atteso %s), riparto da zero",
                data.get("schema_version"), self.SCHEMA_VERSION,
            )
            return self._empty()

        data.setdefault("speakers", {})
        return data

    def save(self) -> None:
        """Scrive atomicamente: un crash notturno non deve mai
        corrompere il DB, altrimenti si perdono tutte le identità."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._data["updated_at"] = _now_iso()
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(self._data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        tmp.replace(self.path)

    # ------------------------------------------------------------------
    # Matching
    # ------------------------------------------------------------------

    def resolve(
        self,
        session_stem: str,
        local_speakers: dict[str, dict[str, Any]],
    ) -> dict[str, str]:
        """
        Associa ogni speaker locale della sessione a un ID globale.

        Args:
            session_stem: nome della sessione (es. "2026-10-02_notte"),
                usato come chiave per non contare due volte la stessa
                sessione se rilanciata.
            local_speakers: {"SPEAKER_00": {"embedding": [...], "seconds": 3600.0}}

        Returns:
            {"SPEAKER_00": "GLOBAL_001", ...}. Le voci senza embedding
            sono skippate (il chiamante deve tenere il label locale).
        """
        speakers = self._data["speakers"]
        mapping: dict[str, str] = {}

        for local, info in sorted(local_speakers.items()):
            raw_emb = info.get("embedding")
            if raw_emb is None:
                logger.debug("Nessun embedding per %s, label locale tenuto", local)
                continue

            vector = to_vector(raw_emb)
            seconds = float(info.get("seconds", 0.0) or 0.0)

            best_gid, best_sim = self._best_match(vector)

            if best_gid is not None and best_sim >= self.threshold:
                gid = best_gid
                decision = "match"
            else:
                gid = self._next_id()
                decision = "nuova"
                if best_gid is not None:
                    logger.info(
                        "  %s: somiglianza %.3f con %s sotto soglia %.2f -> %s",
                        local, best_sim, best_gid, self.threshold, gid,
                    )

            self._register(gid, session_stem, local, vector, seconds)
            mapping[local] = gid

            logger.info(
                "  %s -> %s (%s, coseno %.3f)%s",
                local, gid, decision, best_sim if best_gid else 0.0,
                f' "{speakers[gid]["name"]}"'
                if speakers.get(gid, {}).get("name") else "",
            )

        if mapping:
            self.save()
        return mapping

    def _best_match(self, vector) -> tuple[str | None, float]:
        """Miglior coseno contro tutti i centroidi memorizzati."""
        best_gid: str | None = None
        best_sim = -1.0

        for gid, rec in self._data["speakers"].items():
            stored = rec.get("centroid")
            if not stored:
                continue
            if len(stored) != len(vector):
                # Modello di embedding diverso (o DB di una versione
                # precedente): confrontare vettori di lunghezze diverse
                # produrrebbe silenziosamente match sbagliati.
                logger.warning(
                    "Embedding di dimensione %d incompatibile con %s (%d): "
                    "voce ignorata nel matching",
                    len(vector), gid, len(stored),
                )
                continue
            sim = cosine_similarity(vector, stored)
            if sim > best_sim:
                best_gid, best_sim = gid, sim

        return best_gid, (best_sim if best_gid is not None else 0.0)

    def _register(
        self,
        gid: str,
        session_stem: str,
        local: str,
        vector,
        seconds: float,
    ) -> None:
        """Registra il contributo della sessione e aggiorna il centroide."""
        import numpy as np

        rec = self._data["speakers"].setdefault(
            gid,
            {
                "name": None,
                "centroid": [],
                "sessions": {},
                "first_seen": _now_iso(),
            },
        )

        # Chiave composed: rilanciare la stessa sessione sovrascrive
        # il contributo invece di sommare due volte.
        rec["sessions"][f"{session_stem}|{local}"] = {
            "stem": session_stem,
            "local_speaker": local,
            "seconds": round(seconds, 2),
        }
        rec["last_seen"] = _now_iso()
        rec["total_seconds"] = round(
            sum(s["seconds"] for s in rec["sessions"].values()), 2
        )
        rec["sessions_count"] = len(rec["sessions"])

        if not self.update_centroid:
            rec["centroid"] = [float(x) for x in vector]
            return

        if not rec["centroid"]:
            rec["centroid"] = [float(x) for x in vector]
            return

        old = np.asarray(rec["centroid"], dtype=np.float32)
        weight = max(seconds, 1.0)  # una sessione brevissima non deve
        # pesare come venti ore di parlato
        new = (old + np.asarray(vector, dtype=np.float32) * weight) / (1.0 + weight)
        rec["centroid"] = [float(x) for x in new]

    # ------------------------------------------------------------------
    # Ripensamento
    # ------------------------------------------------------------------

    def forget_session(self, session_stem: str) -> list[str]:
        """Dimentica una sessione: via i suoi contributi dalle voci.

        Serve quando una sessione va rielaborata con criteri diversi da
        quelli con cui era stata risolta la prima volta — per esempio
        dopo che i cluster deboli sono stati fusi. Senza questo,
        ririsolvere la sessione non corregge niente: il DB ha gia'
        un'identita' per ogni cluster, la risoluzione ritrova le stesse
        identita' e il frammento resta un'identita' per sempre.

        Ritorna gli ID che non hanno piu' nessun contributo e che
        vengono rimossi. Un ID con un nome non viene mai rimosso:
        cancellare un nome che qualcuno ha assegnato a mano e' una
        perdita di informazione, non una pulizia, e una voce senza
        contributi ma con un nome e' una persona che si ricorda ma che
        non ha ancora parlato in nessuna sessione.

        La scansione guarda tutte le voci, non solo quelle che la
        sessione toccava. Il motivo e' che una voce puo' essere gia'
        vuota al momento della chiamata — perche' una sessione
        precedente l'ha svuotata e la voce non aveva un nome — e in
        quel caso il `continue` la lascerebbe nel DB per sempre. Il
        sintomo e' una voce che `review_speakers.py list` continua a
        mostrare con zero minuti, perche' nessuna sessione la genera
        piu' e nessun codice la toglie.
        """
        vuoti = []
        for gid, rec in self._data["speakers"].items():
            sessions = rec.get("sessions", {})
            for k in [k for k, v in sessions.items()
                      if v.get("stem") == session_stem]:
                del sessions[k]
            rec["total_seconds"] = round(
                sum(s.get("seconds", 0) for s in sessions.values()), 2
            )
            rec["sessions_count"] = len({
                s.get("stem") for s in sessions.values()
            })
            if not sessions:
                vuoti.append(gid)

        for gid in vuoti:
            if self._data["speakers"][gid].get("name"):
                logger.info(
                    "  %s resta nel DB senza contributi: ha un nome",
                    gid,
                )
                continue
            del self._data["speakers"][gid]
            logger.info("  %s rimossa: nessun contributo e nessun nome", gid)

        if vuoti:
            self.save()
        return [g for g in vuoti if g not in self._data["speakers"]]

    def merge_ids(self, keep: str, drop: str, dry_run: bool = False) -> bool:
        """Versa tutti i contributi di `drop` dentro `keep`.

        La stessa operazione di `review_speakers.py merge`, messa nel
        DB cosi' che anche i programmi possano rifarla: consolidare
        piu' sessioni in una passata sola significa chiamare questa
        funzione, non invocare un altro processo.

        Con `dry_run` il merge avviene su una copia e muore lì: il
        chiamante riceve la risposta senza che niente sia cambiato.
        """
        if keep == drop or drop not in self._data["speakers"] \
                or keep not in self._data["speakers"]:
            return False
        if dry_run:
            # Il merge e' possibile ma non deve accadere: l'aritmetica
            # gira su una copia che muore qui. `--dry-run` che cancella
            # una voce dal DB e' peggio di non fare niente, perche' le
            # sessioni che la citano restano con un ID che non esiste
            # piu' e nessuno se ne accorge.
            _merge_into(copy.deepcopy(self._data), keep, drop)
            return True
        _merge_into(self._data, keep, drop)
        self.save()
        return True

    def _next_id(self) -> str:
        """Il primo ID libero dopo il piu' alto mai usato.

        Contare le voci non basta, e il motivo e' che cosi' i numeri si
        riassegnano. Un ID non e' un numero: e' il nome con cui una
        persona e' citata in ogni sessione, in `corpus.db` e nella repo
        del corpus. Se dopo una fusione il DB passa da dodici voci a
        nove, `len()` restituirebbe 9 e la prossima voce nuova
        prenderebbe GLOBAL_010, che era gia' stata qualcun altro: le
        sessioni vecchie che citano GLOBAL_010 si troverebbero a
        parlare di una persona diversa, senza che niente lo segnali.

        Il numero resta progressivo e non riusato, che e' il prezzo:
        dopo varie fusioni si hanno dei buchi (GLOBAL_004, poi
        GLOBAL_018). E' il prezzo giusto, perche' un buco e' innocuo e
        un ID riusato e' silenziosamente falso.
        """
        usati = [
            int(m.group(1)) for gid in self._data["speakers"]
            if (m := _GLOBAL_ID.match(gid))
        ]
        return f"GLOBAL_{(max(usati) + 1 if usati else 1):03d}"

    # ------------------------------------------------------------------
    # Assegnazione manuale dei nomi
    # ------------------------------------------------------------------

    def set_name(self, gid: str, name: str | None) -> None:
        """Associa (o toglie) un nome umano a una voce globale."""
        if gid not in self._data["speakers"]:
            raise KeyError(f"Voce sconosciuta: {gid}")
        self._data["speakers"][gid]["name"] = name
        self.save()
        logger.info("Nome impostato: %s -> %s", gid, name)

    def set_info(self, gid: str, info: str | None) -> None:
        """Note libere su una voce: chi e', che rapporto ha con te.

        Restano qui, nel DB locale delle voci, e non vanno mai nel corpus
        pubblicato: sono informazioni su persone, date a mano.
        """
        if gid not in self._data["speakers"]:
            raise KeyError(f"Voce sconosciuta: {gid}")
        self._data["speakers"][gid]["info"] = (info or "").strip() or None
        self.save()

    def get_name(self, gid: str) -> str | None:
        """Nome umano se assegnato, altrimenti l'ID globale."""
        rec = self._data["speakers"].get(gid, {})
        return rec.get("name") or gid

    def centroids(self) -> dict[str, list[float]]:
        """I centroidi salvati, voce per voce.

        Servono alla matrice per confrontare le voci con lo stesso numero
        che usa `_best_match`. Restano in memoria: sono impronte
        biometriche, e chi li riceve ne pubblica solo i coseni.
        """
        return {gid: list(rec["centroid"])
                for gid, rec in self._data["speakers"].items()
                if rec.get("centroid")}

    # ------------------------------------------------------------------
    # Revisione: quali voci hai gia' guardato
    # ------------------------------------------------------------------

    def mark_reviewed(self, gid: str, reviewed: bool = True) -> None:
        """Segna una voce come gia' vista da te, con o senza nome.

        Serve all'elenco `review_speakers.py nuove`: dopo ogni notte deve
        proporre solo le voci che non hai ancora guardato. Una voce con un
        nome e' gia' vista per definizione; una senza nome puo' esserlo
        lo stesso — un passante, la televisione, qualcuno che non vuoi
        nominare — e `ignora` la toglie dall'elenco senza inventarle un
        nome.
        """
        if gid not in self._data["speakers"]:
            raise KeyError(f"Voce sconosciuta: {gid}")
        rec = self._data["speakers"][gid]
        if reviewed:
            rec["reviewed_at"] = _now_iso()
        else:
            rec.pop("reviewed_at", None)
        self.save()

    def is_reviewed(self, gid: str) -> bool:
        rec = self._data["speakers"].get(gid, {})
        return bool(rec.get("name") or rec.get("reviewed_at"))

    def profiles(self) -> dict[str, dict[str, Any]]:
        """Profilo aggregato per voce globale, senza i vettori grezzi."""
        out = {}
        for gid, rec in self._data["speakers"].items():
            out[gid] = {
                "name": rec.get("name"),
                "total_seconds": rec.get("total_seconds", 0.0),
                "sessions_count": rec.get("sessions_count", 0),
                "first_seen": rec.get("first_seen"),
                "last_seen": rec.get("last_seen"),
                "reviewed": bool(rec.get("name") or rec.get("reviewed_at")),
                "info": rec.get("info"),
                "sessions": sorted({s["stem"] for s in rec.get("sessions", {}).values()}),
            }
        return out
