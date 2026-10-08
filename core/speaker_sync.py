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


def _rewrite_ids(node: Any, renames: dict[str, str]) -> int:
    """Sostituisce gli ID globali in tutto il documento. Ritorna quante
    sostituzioni ha fatto.

    Serve per il merge. Unire GLOBAL_003 in GLOBAL_001 non basta nel
    database delle voci: le sessioni gia' scritte citano ancora
    GLOBAL_003 nei segmenti, nelle statistiche per speaker e nelle
    mappe locali. E il corpus diventa incoerente: due ID per la stessa
    persona, con statistiche che non si sommano e un'analisi per
    persona che non funziona — che è esattamente la ragione per cui
    si fa un merge a mano.

    La sostituzione è su tutto l'albero, non sui campi noti: se domani
    un formato nuovo che cita un ID in un posto che oggi non conosco,
    viene rietichettato lo stesso invece di restare indietro. Il costo
    è che il documento viene visitato per intero, che per una sessione
    sono poche migliaia di nodi e non un problema.
    """
    n = 0
    if isinstance(node, dict):
        # Le chiavi si rinominano per prime, in una copia: farlo durante
        # l'iterazione solleva "dictionary keys changed during
        # iteration", che è il modo più innocuo di ricordare che in
        # Python si itera su una copia ma si modifica l'originale.
        for k in [k for k in node if isinstance(k, str) and k in renames]:
            v = node.pop(k)
            nuovo = renames[k]
            if nuovo in node:
                # Le due voci unite parlavano nella stessa sessione: le
                # loro statistiche si sommano, non si sovrascrivono. Prima
                # dell'8 ottobre la seconda cancellava la prima.
                node[nuovo] = _somma(node[nuovo], v)
            else:
                node[nuovo] = v
            n += 1
        for k, v in node.items():
            if isinstance(v, str):
                if v in renames:
                    node[k] = renames[v]
                    n += 1
                continue
            n += _rewrite_ids(v, renames)
    elif isinstance(node, list):
        for i, item in enumerate(node):
            if isinstance(item, str):
                if item in renames:
                    node[i] = renames[item]
                    n += 1
            else:
                n += _rewrite_ids(item, renames)
    return n


def _somma(a: Any, b: Any) -> Any:
    """Unisce due valori della stessa voce: numeri sommati, dizionari per
    chiave, liste concatenate senza doppioni; altrimenti vince il primo."""
    if isinstance(a, bool) or isinstance(b, bool):
        return a
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return round(a + b, 4) if isinstance(a, float) or isinstance(b, float) else a + b
    if isinstance(a, dict) and isinstance(b, dict):
        out = dict(a)
        for k, v in b.items():
            out[k] = _somma(out[k], v) if k in out else v
        return out
    if isinstance(a, list) and isinstance(b, list):
        return a + [x for x in b if x not in a]
    return a if a is not None else b


# File di sessione che citano le voci globali, oltre a session.json e
# transcript.json. Fino all'8 ottobre il merge rietichettava solo quei due:
# segmenti, testo, sottotitoli e CSV restavano con l'ID vecchio, e la
# giornata pubblicata (che si costruisce da segments.jsonl) continuava a
# mostrare due voci per la stessa persona.
_JSONL = ("segments.jsonl", "segments.corrected.jsonl")
_TESTI = ("transcript.txt", "transcript.srt", "transcript.corrected.txt",
          "transcript.corrected.srt", "analysis_ready.md")


def _relabel_jsonl(f: Path, renames: dict[str, str], dry_run: bool) -> int:
    righe, n = [], 0
    for r in f.read_text(encoding="utf-8").splitlines():
        if not r.strip():
            continue
        try:
            doc = json.loads(r)
        except json.JSONDecodeError:
            righe.append(r)
            continue
        n += _rewrite_ids(doc, renames)
        righe.append(json.dumps(doc, ensure_ascii=False))
    if n and not dry_run:
        f.write_text("\n".join(righe) + "\n", encoding="utf-8")
    return n


def _relabel_testo(f: Path, renames: dict[str, str], dry_run: bool) -> int:
    import re
    testo = f.read_text(encoding="utf-8")
    patt = re.compile(r"\b(" + "|".join(re.escape(k) for k in renames) + r")\b")
    nuovo, n = patt.subn(lambda m: renames[m.group(1)], testo)
    if n and not dry_run:
        f.write_text(nuovo, encoding="utf-8")
    return n


def _relabel_csv(f: Path, renames: dict[str, str], dry_run: bool) -> int:
    """prosody.csv: celle con l'ID. wordfreq.csv: colonne freq_<ID> sommate."""
    import csv
    import io
    with f.open(encoding="utf-8", newline="") as h:
        righe = list(csv.reader(h))
    if not righe:
        return 0
    testa, corpo = righe[0], righe[1:]
    n = 0
    vecchie = {f"freq_{k}": f"freq_{v}" for k, v in renames.items()}
    if any(c in vecchie for c in testa):
        nuova_testa = []
        for c in testa:
            c2 = vecchie.get(c, c)
            if c2 not in nuova_testa:
                nuova_testa.append(c2)
        idx = {c: i for i, c in enumerate(nuova_testa)}
        nuovo_corpo = []
        for r in corpo:
            out = [0] * len(nuova_testa)
            for c, v in zip(testa, r):
                j = idx[vecchie.get(c, c)]
                if c.startswith("freq_") and c != "freq_global":
                    try:
                        out[j] = int(out[j] or 0) + int(float(v or 0))
                    except ValueError:
                        out[j] = v
                else:
                    out[j] = v
            nuovo_corpo.append(out)
        n = sum(1 for c in testa if c in vecchie)
        testa, corpo = nuova_testa, nuovo_corpo
    for r in corpo:
        for i, v in enumerate(r):
            if isinstance(v, str) and v in renames:
                r[i] = renames[v]
                n += 1
    if n and not dry_run:
        buf = io.StringIO()
        w = csv.writer(buf, lineterminator="\n")
        w.writerow(testa)
        w.writerows(corpo)
        f.write_text(buf.getvalue(), encoding="utf-8")
    return n


def relabel_sessions(
    renames: dict[str, str],
    output_dir: Path | None = None,
    corpus_db: CorpusDB | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Rietichetta le sessioni già scritte dopo un merge di identità.

    Args:
        renames: {vecchio_id: nuovo_id}, es. {"GLOBAL_003": "GLOBAL_001"}.

    Returns:
        quante sessioni e quanti segmenti sono stati rietichettati.
    """
    out_dir = Path(output_dir) if output_dir else OUTPUT_DIR
    report = {"sessions": 0, "substitutions": 0, "db_segments": 0,
              "dry_run": bool(dry_run)}

    if out_dir.is_dir():
        for job in sorted(p for p in out_dir.iterdir() if p.is_dir()):
            touched = False
            for fname in ("session.json", "transcript.json",
                          "speaker_profiles.json", f"{job.name}.checkpoint.json"):
                f = job / fname
                if not f.exists():
                    continue
                try:
                    doc = json.loads(f.read_text(encoding="utf-8"))
                except (json.JSONDecodeError, OSError):
                    logger.warning("Non leggibile, lo salto: %s", f)
                    continue
                n = _count_ids(doc, renames)
                if not n:
                    continue
                report["substitutions"] += n
                touched = True
                if not dry_run:
                    _rewrite_ids(doc, renames)
                    _write_if_changed(f, doc)
            for fname, fn in ([(x, _relabel_jsonl) for x in _JSONL]
                              + [(x, _relabel_testo) for x in _TESTI]
                              + [("prosody.csv", _relabel_csv),
                                 ("wordfreq.csv", _relabel_csv)]):
                f = job / fname
                if not f.exists():
                    continue
                try:
                    n = fn(f, renames, dry_run)
                except (OSError, UnicodeDecodeError) as exc:
                    logger.warning("Non rietichettato %s: %s", f, exc)
                    continue
                if n:
                    report["substitutions"] += n
                    touched = True
            if touched:
                report["sessions"] += 1

    # Anche il database: `speaker_global_map` e le colonne speaker dei
    # segmenti e dei token citano l'ID vecchio, e senza questa passata
    # corpus.db continuerebbe a tenere due voci per la stessa persona.
    if corpus_db is not None:
        report["db_segments"] = corpus_db.relabel_speakers(renames, dry_run=dry_run)

    return report


def _count_ids(node: Any, renames: dict[str, str]) -> int:
    if isinstance(node, dict):
        return (sum(1 for k in node if isinstance(k, str) and k in renames)
                + sum(_count_ids(v, renames) for v in node.values()))
    if isinstance(node, list):
        return sum(_count_ids(v, renames) for v in node)
    if isinstance(node, str):
        return 1 if node in renames else 0
    return 0


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