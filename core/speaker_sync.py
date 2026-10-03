"""
Allineamento dei nomi dei parlanti a tutto il materiale già scritto.

Il problema che risolve. Il nome di una voce vive in un posto solo,
`data/speakers_db.json`, ed è l'unico che va aggiornato quando si rinomina
qualcuno. Ma il nome era stato copiato — al momento in cui la sessione è
stata scritta — dentro altri file: `transcript.json`, `session.json`, la
tabella `speakers` di `corpus.db`. Copie che non parlano fra loro: dopo
`review_speakers.py name GLOBAL_001 Pietro` le sessioni vecchie continuavano
a dire `GLOBAL_001`, e le query sul corpus restituivano pseudonimi per un
corpus che da settimane ha un nome per quelle voci.

Qui la copia è diventata derivata, non memorizzata. Il DB delle voci è la
fonte; tutto il resto si riallinea da lì con un comando. Il prezzo è che
l'allineamento va eseguito dopo un rename — ed è per questo che esiste
`review_speakers.py sync`, che chiama questa funzione e dice cosa ha
toccato.

Cosa viene allineato:

  - `corpus.db`, tabella `speakers`: nome aggiornato, comprese le voci che
    nel database c'erano senza nome;
  - `output/<stem>/session.json`: `speaker_names` rifatto dai ID che
    quella sessione contiene davvero;
  - `output/<stem>/transcript.json`: `meta.speaker_names`, che è la copia
    più grande e quella che finisce sulla repo del corpus.

Le sessioni vengono riscritte solo se il contenuto cambia davvero: si
toccano file che l'utente potrebbe avere aperto, e una riscrittura a ogni
sync — con il solo effetto di cambiare la data di modifica — è rumore che
poi sembra un'attività.
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.config import OUTPUT_DIR, ROOT_DIR  # noqa: E402
from core.corpus_db import CorpusDB  # noqa: E402
from core.speaker_db import SpeakerDB  # noqa: E402

logger = logging.getLogger(__name__)


def names_from_db(db: SpeakerDB) -> dict[str, str]:
    """Solo le voci con un nome assegnato.

    Una voce senza nome non finisce nella mappa: `GLOBAL_004: "GLOBAL_004"`
    è rumore che sembra un'informazione. Chi non ha nome resta pseudonimo,
    ed è la scelta giusta finché non gli si dà un nome.
    """
    out: dict[str, str] = {}
    for gid, rec in db._data.get("speakers", {}).items():
        name = (rec.get("name") or "").strip()
        if name:
            out[gid] = name
    return out


def _write_if_changed(path: Path, data: dict[str, Any]) -> bool:
    """Scrive solo se il contenuto è diverso. True se il file è stato toccato."""
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    try:
        if path.read_text(encoding="utf-8") == text:
            return False
    except OSError:
        pass                      # non esiste o non leggibile: si scrive
    path.write_text(text, encoding="utf-8")
    return True


def _sync_session_file(path: Path, names: dict[str, str]) -> bool:
    """Allinea la mappa `speaker_names` di un singolo file di sessione.

    Funziona sia su `session.json` (chiave di primo livello) sia su
    `transcript.json` (sotto `meta`), perché i due file hanno la stessa
    informazione in posti diversi e devono dire la stessa cosa.

    La mappa viene ricostruita dagli ID che il file contiene davvero:
    una voce nominata che non parla in quella sessione non viene
    aggiunta. Copiare l'intero dizionario dei nomi in ogni file
    significherebbe che `session.json` di martedì contiene informazioni
    su persone che non hanno mai parlato martedì.
    """
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        logger.warning("Non leggibile, lo salto: %s", path)
        return False

    holder = doc["meta"] if isinstance(doc.get("meta"), dict) else doc
    gids = _global_ids_in(doc)
    wanted = {gid: names[gid] for gid in gids if gid in names}
    if holder.get("speaker_names") == wanted:
        # Niente da fare: si restituisce False senza riscrivere, così un
        # sync ripetuto non tocca file che l'utente potrebbe avere aperti.
        return False
    holder["speaker_names"] = wanted
    return _write_if_changed(path, doc)


def _global_ids_in(doc: Any) -> list[str]:
    """Gli ID globali citati dal documento, in ordine di prima comparsa."""
    found: list[str] = []
    seen: set[str] = set()

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            for k, v in node.items():
                if k in ("speaker", "speaker_global", "global_id") and isinstance(v, str):
                    if v.startswith("GLOBAL_") and v not in seen:
                        seen.add(v)
                        found.append(v)
                elif k == "speaker_global_map" and isinstance(v, dict):
                    for local, gid in v.items():
                        if isinstance(gid, str) and gid.startswith("GLOBAL_") and gid not in seen:
                            seen.add(gid)
                            found.append(gid)
                elif k == "speaker_names" and isinstance(v, dict):
                    # Anche gli ID già elencati nella mappa dei nomi
                    # contano come riferimenti. Serve a una cosa sola:
                    # rimuovere un nome deve poterlo togliere di qui. Se
                    # la mappa non fosse contata, cancellare un nome
                    # lascerebbe la voce nella mappa per sempre, perché
                    # nessun altro file la nominerebbe più.
                    for gid in v:
                        if isinstance(gid, str) and gid.startswith("GLOBAL_") and gid not in seen:
                            seen.add(gid)
                            found.append(gid)
                elif k == "speakers" and isinstance(v, dict):
                    # In session.json `speakers` è un dizionario indicato
                    # per ID globale: le chiavi sono gli ID, non i valori.
                    for gid in v:
                        if isinstance(gid, str) and gid.startswith("GLOBAL_") and gid not in seen:
                            seen.add(gid)
                            found.append(gid)
                    walk(v)
                else:
                    walk(v)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(doc)
    return found


def sync_speaker_names(
    output_dir: Path | None = None,
    db: SpeakerDB | None = None,
    db_path: Path | None = None,
    corpus_db: CorpusDB | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Riallinea i nomi su corpus.db e su tutte le sessioni già scritte.

    Args:
        output_dir: dove stanno le sessioni (default OUTPUT_DIR).
        db: SpeakerDB già aperto; se None viene aperto da db_path.
        corpus_db: CorpusDB già aperto; se None viene aperto e chiuso qui.

    Returns:
        un riepilogo numerico: quante voci nominate, quante righe
        aggiornate nel database, quante sessioni toccate, quante lasciate
        stare.
    """
    out_dir = Path(output_dir) if output_dir else OUTPUT_DIR
    db = db or SpeakerDB(path=db_path or (ROOT_DIR / "data" / "speakers_db.json"))
    names = names_from_db(db)

    report: dict[str, Any] = {
        "named_speakers": len(names),
        "db_rows": 0,
        "sessions_updated": 0,
        "sessions_unchanged": 0,
        "files_updated": 0,
        "dry_run": bool(dry_run),
    }

    # --- corpus.db -----------------------------------------------------
    # Si fa per primo: è il database che le query dell'analisi leggono,
    # ed è quello che senza sync resta fermo al pseudonimo.
    own_db = corpus_db is None
    cdb = corpus_db or (CorpusDB() if not dry_run else None)
    try:
        if cdb is not None:
            if dry_run:
                pending = cdb.query(
                    "SELECT global_id, name FROM speakers "
                    "WHERE name IS NOT NULL AND name != global_id"
                )
                report["db_rows"] = len(pending)
            else:
                report["db_rows"] = cdb.sync_speaker_names(names)
    finally:
        if own_db and cdb is not None:
            cdb.close()

    # --- le sessioni ----------------------------------------------------
    if not out_dir.is_dir():
        logger.warning("Nessuna cartella di sessioni: %s", out_dir)
        return report

    for job in sorted(p for p in out_dir.iterdir() if p.is_dir()):
        touched = False
        for fname in ("session.json", "transcript.json"):
            f = job / fname
            if not f.exists():
                continue
            if dry_run:
                try:
                    doc = json.loads(f.read_text(encoding="utf-8"))
                except (json.JSONDecodeError, OSError):
                    continue
                holder = doc["meta"] if isinstance(doc.get("meta"), dict) else doc
                wanted = {g: names[g] for g in _global_ids_in(doc) if g in names}
                if holder.get("speaker_names") != wanted:
                    touched = True
                continue
            if _sync_session_file(f, names):
                touched = True
                report["files_updated"] += 1

        if touched:
            report["sessions_updated"] += 1
        else:
            report["sessions_unchanged"] += 1

    return report