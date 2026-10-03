"""
Test della fusione dei cluster di voce deboli.

    python tests/test_speakers_merge.py

Niente rete, niente modelli, niente audio: solo i numeri.

Il difetto che questo test copre non e' un errore di programma. La
diarizzazione su quattro ore di una stessa conversazione aveva prodotto
ventuno voci globali, undici delle quali parlavano meno di novanta
secondi. Tutto era andato a buon fine: nessuna eccezione, tutti i file
scritti, tutte le sessioni complete. Il difetto e' che ventuno voci non
erano ventuno persone, e un corpus con ventuno voci su una conversazione
a tavolo non e' interrogabile per interlocutore.

I test qui sotto coprono le regole che tengono la fusione al sicuro. La
regola che conta piu' di tutte e' la prima: due voci grandi non si
fondono mai. E' l'unica protezione contro l'errore che costerebbe di
piu', cioe' unire due persone vere e farlo per sempre, perche' il DB
delle voci non torna mai indietro.
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

from core.speaker_db import SpeakerDB  # noqa: E402
from core.speakers_merge import (  # noqa: E402
    MergePolicy, cosine, merge_weak_clusters, speaking_seconds,
)


class Failure(Exception):
    pass


def require(cond: bool, msg: str) -> None:
    if not cond:
        raise Failure(msg)


CHECKS: list[tuple[str, callable]] = []


def check(fn):
    CHECKS.append((fn.__name__, fn))
    return fn


# ---------------------------------------------------------------------------
# Oggetti di prova
# ---------------------------------------------------------------------------

def _seg(sp: str, start: float, durata: float) -> dict:
    return {"speaker": sp, "start": start, "end": start + durata}


DIM = 24


def _norm(v: list[float]) -> list[float]:
    n = sum(x * x for x in v) ** 0.5
    return [x / n for x in v]


def _neutro() -> list[float]:
    """Direzione quasi perpendicolare a ogni voce della griglia.

    Serve da "riempitivo": un vettore costruito come `voce` mescolata
    con questo si allontana dalla voce di cui e' frammento e basta,
    senza avvicinarsi a nessun'altra. E' quello che permette di
    costruire un frammento con una somiglianza esatta e scelta.
    """
    return _norm([1.0 if k % 2 == 0 else -1.0 for k in range(DIM)])


def _voce(n: int, e: float = 0.75) -> list[float]:
    """Voce n: una componente comune a tutte piu' una tutta sua.

    Il coseno fra due voci diverse e' esattamente `1 - e`, cioe' 0.25
    di default: la separazione e' un numero scelto, non una conseguenza.
    E sta volutamente sotto la soglia di fusione (0.45), cosi' due
    voci costruite come diverse sono davvero diverse e il test non
    puo' passare per caso.
    """
    v = [0.0] * DIM
    v[n % DIM] = 1.0
    comune = [(1 - e) ** 0.5 / (DIM ** 0.5)] * DIM
    return _norm([comune[k] + (e ** 0.5 if k == n % DIM else 0.0)
                  for k in range(DIM)])


def _frammento(voce: list[float], quanto: float) -> list[float]:
    """Voce frammentata: `quanto` e' quanto si allontana dalla originale.

    0 = identica, 1 = completamente diversa. Il coseno con l'originale
    scende di circa 0.9 per ogni 0.1 di `quanto`.
    """
    n = _neutro()
    return _norm([voce[k] * (1 - quanto) + n[k] * quanto for k in range(DIM)])


def _ruotata(voce: list[float], gradi: float) -> list[float]:
    """Voce ruotata di un angolo esatto rispetto a quella di partenza.

    Serve al test della catena, che ha bisogno di una situazione che con
    l'allontanamento lineare non si ottiene: un frammento A che non
    arriva alla voce grande, e un frammento B che invece arriva, e che
    pero' e' piu' vicino ad A che alla grande. Ruotando gli angoli si
    scelgono i tre numeri esatti, e sono 0.29, 0.72 e 0.87.
    """
    import math
    a = math.radians(gradi)
    n = _neutro()
    return _norm([voce[k] * math.cos(a) + n[k] * math.sin(a)
                  for k in range(DIM)])


# ---------------------------------------------------------------------------
# La regola che conta: due voci grandi non si fondono mai
# ---------------------------------------------------------------------------

@check
def due_voci_grandi_restano_due() -> None:
    """La protezione principale: anche simili, due voci vere non si uniscono."""
    segmenti = [_seg("A", 0, 600.0), _seg("B", 600, 600.0)]
    emb = {"A": _voce(1), "B": _frammento(_voce(1), 0.1)}   # simili, ma non tanto
    out, out_emb, rep = merge_weak_clusters(segmenti, emb, MergePolicy())
    require(rep.merged == {},
            f"due voci da dieci minuti non devono fondersi, "
            f"risulta {rep.merged}")
    require(len(speaking_seconds(out)) == 2, "devono restare due voci")
    require(out_emb.keys() == emb.keys(), "e nessun embedding deve sparire")


@check
def voci_grandi_identiche_non_si_fondono() -> None:
    """Anche a somiglianza 1.0 restano due: la regola e' sui secondi, non sul coseno."""
    segmenti = [_seg("A", 0, 600.0), _seg("B", 600, 600.0)]
    emb = {"A": _voce(1), "B": _voce(1)}            # identiche
    out, _, rep = merge_weak_clusters(segmenti, emb, MergePolicy())
    require(rep.merged == {},
            "embedding identico non significa stessa persona: "
            "sono due voci grandi")


# ---------------------------------------------------------------------------
# Il caso normale: il frammento si scioglie
# ---------------------------------------------------------------------------

@check
def il_frammento_si_scioglie_nel_piu_simile() -> None:
    """Un cluster da 40 secondi dentro uno da venti minuti, e simile."""
    segmenti = [
        _seg("G1", 0, 1200.0),
        _seg("G2", 1200, 900.0),
        _seg("F", 2100, 40.0),      # il frammento
    ]
    emb = {"G1": _voce(1), "G2": _voce(5), "F": _frammento(_voce(5), 0.3)}
    out, out_emb, rep = merge_weak_clusters(segmenti, emb, MergePolicy())
    require(rep.merged == {"F": "G2"},
            f"il frammento deve finire in G2, risulta {rep.merged}")
    require(rep.clusters_before == 3 and rep.clusters_after == 2,
            f"3 -> 2 voci, risulta {rep.clusters_before} -> {rep.clusters_after}")
    require(speaking_seconds(out)["G2"] == 940.0,
            f"G2 deve avere 940 secondi, ha {speaking_seconds(out)['G2']}")
    require(len(out_emb["G2"]) == len(emb["G2"]),
            "il centroide di G2 resta un vettore della stessa lunghezza")
    require("F" not in out_emb,
            "il frammento non deve restare con un suo embedding")


@check
def il_piu_simile_vince_anche_se_e_piccolo() -> None:
    """Fra due partner possibili vince la somiglianza, non la durata."""
    segmenti = [
        _seg("GRANDE", 0, 1200.0),
        _seg("PICCOLO", 1200, 60.0),
        _seg("F", 1260, 30.0),
    ]
    # F e' molto piu' simile a PICCOLO che a GRANDE, anche se PICCOLO
    # sta lui stesso sotto la soglia di novanta secondi.
    # GRANDE e' lontano da F, PICCOLO no: vince la somiglianza.
    emb = {"GRANDE": _voce(1), "PICCOLO": _voce(9),
           "F": _frammento(_voce(9), 0.3)}
    _, _, rep = merge_weak_clusters(segmenti, emb, MergePolicy())
    require(rep.merged.get("F") == "PICCOLO",
            f"il frammento deve andare nel piu' simile, risulta {rep.merged}")


@check
def frammento_orphan_sotto_soglia_resta() -> None:
    """Nessuno abbastanza simile: il frammento resta, e lo dichiara."""
    segmenti = [_seg("G1", 0, 1200.0), _seg("F", 1200, 30.0)]
    emb = {"G1": _voce(1), "F": _frammento(_voce(1), 0.9)}   # irriconoscibile
    out, _, rep = merge_weak_clusters(segmenti, emb, MergePolicy())
    require(rep.merged == {}, "nessuno se lo prende, nessuna fusione")
    require("F" in rep.orphans,
            f"e deve essere dichiarato come orfano, risulta {rep.orphans}")
    require(len(speaking_seconds(out)) == 2, "il frammento resta una voce")


@check
def la_catena_si_risolve() -> None:
    """Un frammento arriva alla grande solo attraverso un altro.

    A e' piu' simile a B (0.87) che alla voce grande (0.29), e B e'
    simile alla grande (0.72). Quindi B si scioglie nella grande al
    primo giro, e solo al secondo A trova la strada. Senza iterazione A
    resterebbe un frammento sciolto dal nulla: unito a niente, con
    venti secondi di parlato e nessuno a cui attribuirli.

    E' la situazione che si e' vista davvero sui dati: dodici secondi
    e quindici secondi che passavano insieme e finivano nella voce da
    diciotto minuti.
    """
    grande = _voce(1)
    segmenti = [
        _seg("GRANDE", 0, 1100.0),
        _seg("A", 1100, 40.0),
        _seg("B", 1140, 40.0),
    ]
    emb = {"GRANDE": grande, "A": _ruotata(grande, 65.0), "B": _ruotata(grande, 40.0)}
    out, _, rep = merge_weak_clusters(segmenti, emb, MergePolicy())
    finali = speaking_seconds(out)
    require(set(finali) == {"GRANDE"},
            f"tutti i frammenti devono finire nella grande, risultano {finali}")
    require(finali["GRANDE"] == 1180.0,
            f"la voce grande deve avere tutti i 1180 secondi, ha {finali['GRANDE']}")
    require(len(rep.merged) == 2,
            f"le due fusioni devono essere dichiarate, risultano {rep.merged}")


@check
def nessuna_etichetta_si_perde() -> None:
    """Ogni secondo parlato resta da qualche parte."""
    segmenti = []
    t = 0.0
    for i in range(6):
        durata = 1000.0 if i % 2 == 0 else 37.0
        segmenti.append(_seg(f"S{i}", t, durata))
        t += durata
    emb = {f"S{i}": _voce(i) for i in range(6)}
    out, _, rep = merge_weak_clusters(segmenti, emb, MergePolicy())
    prima = sum(speaking_seconds(segmenti).values())
    dopo = sum(speaking_seconds(out).values())
    require(abs(prima - dopo) < 1e-6,
            f"il parlato totale non puo' cambiare: {prima} -> {dopo}")
    require(set(speaking_seconds(out)) <= set(speaking_seconds(segmenti)),
            "non puo' comparire una voce che non c'era")


@check
def e_idempotente() -> None:
    """Rifare la fusione non cambia niente: il comando si puo' ritentare."""
    segmenti = [_seg("G1", 0, 1200.0), _seg("F", 1200, 40.0)]
    emb = {"G1": _voce(1), "F": _frammento(_voce(1), 0.3)}
    una, emb1, _ = merge_weak_clusters(segmenti, emb, MergePolicy())
    due, _, rep2 = merge_weak_clusters(una, emb1, MergePolicy())
    require(rep2.merged == {},
            f"la seconda passata non deve fondere nulla, "
            f"risulta {rep2.merged}")
    require(speaking_seconds(due) == speaking_seconds(una),
            "ne i segmenti ne i secondi")


@check
def relazione_aggiorna_il_verbale() -> None:
    """Il file di traccia dice da dove a dove, non solo il numero finale."""
    segmenti = [_seg("G1", 0, 1200.0), _seg("F", 1200, 40.0)]
    emb = {"G1": _voce(1), "F": _frammento(_voce(1), 0.3)}
    _, _, rep = merge_weak_clusters(segmenti, emb, MergePolicy())
    d = rep.to_dict()
    require(d["merged"] == {"F": "G1"}, f"la relazione deve esserci: {d}")
    require(d["seconds_before"]["F"] == 40.0,
            f"i secondi prima della fusione devono esserci: {d}")
    require(d["clusters_before"] == 2 and d["clusters_after"] == 1,
            f"e il conteggio: {d}")


# ---------------------------------------------------------------------------
# Il calcolo di somiglianza
# ---------------------------------------------------------------------------

@check
def il_coseno_di_un_vettore_nullo_e_zero() -> None:
    """Un embedding assente non e' "voce diversa", e' "non lo so".

    Restituisce 0.0, che nel codice vuol dire "sotto soglia, nessuna
    fusione": la risposta cauta. Il contrario sarebbe fondere sulla
    base di un dato mancante.
    """
    require(cosine([1.0, 2.0], []) == 0.0, "vettore vuoto")
    require(cosine([], [1.0, 2.0]) == 0.0, "vettore vuoto invertito")
    require(cosine([0.0, 0.0], [1.0, 2.0]) == 0.0, "vettore di zeri")


@check
def il_coseno_rifiuta_lunghezze_diverse() -> None:
    """Vettori di dimensioni diverse non si confrontano: `zip` li troncherebbe.

    Senza il controllo, `zip` taglia il vettore piu' lungo e il risultato
    e' un numero che sembra una somiglianza fra voci e non e' niente.
    """
    require(cosine([1.0, 0.0, 0.0], [1.0, 0.0]) == 0.0,
            "dimensioni diverse non producono un coseno")


@check
def il_coseno_conta_giusto() -> None:
    """Ortogonale zero, identico uno, opposto meno uno."""
    require(abs(cosine([1.0, 0.0], [0.0, 1.0]) - 0.0) < 1e-9,
            "vettori perpendicolari")
    require(abs(cosine([1.0, 2.0], [1.0, 2.0]) - 1.0) < 1e-9,
            "vettori identici")
    require(abs(cosine([1.0, 0.0], [-1.0, 0.0]) + 1.0) < 1e-9,
            "vettori opposti")


# ---------------------------------------------------------------------------
# Il DB delle voci deve poter dimenticare
# ---------------------------------------------------------------------------

@check
def dimenticare_una_sessione_libera_le_voci() -> None:
    """Il DB deve poter togliere i contributi di una sessione.

    E' la condizione per poter correggere una sessione gia' scritta:
    senza questo, rifondere i cluster non cambierebbe nulla, perche'
    il frammento ritroverebbe subito l'identita' che si era creato la
    prima volta.
    """
    with tempfile.TemporaryDirectory() as tmp:
        db = SpeakerDB(path=Path(tmp) / "db.json")
        db.resolve("s1", {
            "A": {"embedding": _voce(1), "seconds": 600.0},
            "B": {"embedding": _voce(5), "seconds": 30.0},
        })
        require(len(db._data["speakers"]) == 2, "due voci nuove")
        db.resolve("s2", {"A": {"embedding": _voce(1), "seconds": 600.0}})
        require(len(db._data["speakers"]) == 2, "s2 e' la stessa voce di s1")

        db.forget_session("s1")
        # A resta (ha ancora s2), B sparisce: era solo di s1.
        rimaste = set(db._data["speakers"])
        require(len(rimaste) == 1,
                f"solo la voce con contributi in s2 deve restare, "
                f"restano {rimaste}")
        _, sec = next(iter(db._data["speakers"].values()))["sessions"].popitem()
        require(sec["stem"] == "s2",
                f"e il contributo rimasto e' quello di s2, risulta {sec}")


@check
def la_voce_nominata_non_viene_cancellata() -> None:
    """Una voce con un nome non si cancella quando perde i contributi.

    Il nome e' stato messo a mano: cancellarlo sarebbe perdere
    informazione, non fare pulizia. La persona esiste anche se non ha
    ancora parlato in nessuna sessione.
    """
    with tempfile.TemporaryDirectory() as tmp:
        db = SpeakerDB(path=Path(tmp) / "db.json")
        db.resolve("s1", {"A": {"embedding": _voce(1), "seconds": 30.0}})
        gid = next(iter(db._data["speakers"]))
        db.set_name(gid, "Pietro")
        db.forget_session("s1")
        require(gid in db._data["speakers"],
                f"{gid} ha un nome e deve restare")
        require(db.get_name(gid) == "Pietro", "col suo nome")


@check
def unire_due_identita_somma_i_secondi() -> None:
    """`merge_ids` deve sommare, non sostituire: altrimenti il totale si perde."""
    with tempfile.TemporaryDirectory() as tmp:
        db = SpeakerDB(path=Path(tmp) / "db.json")
        db.resolve("s1", {"A": {"embedding": _voce(1), "seconds": 100.0}})
        db.resolve("s2", {"A": {"embedding": _voce(1), "seconds": 50.0}})
        gid = next(iter(db._data["speakers"]))
        require(db._data["speakers"][gid]["total_seconds"] == 150.0,
                "150 secondi su due sessioni")

        db.resolve("s3", {"B": {"embedding": _voce(4), "seconds": 25.0}})
        altro = next(g for g in db._data["speakers"] if g != gid)
        require(db.merge_ids(gid, altro), "l'unione deve riuscire")
        require(altro not in db._data["speakers"],
                "la voce assorbita sparisce dal DB")
        require(db._data["speakers"][gid]["total_seconds"] == 175.0,
                f"175 secondi dopo l'unione, risulta "
                f"{db._data['speakers'][gid]['total_seconds']}")


# ---------------------------------------------------------------------------
# Il corpus deve dimenticare le voci che non esistono piu'
# ---------------------------------------------------------------------------

@check
def la_potatura_tiene_le_voci_vive() -> None:
    """Dopo un merge, la tabella dei parlanti deve contare solo chi parla.

    Senza, «chi parla di piu' nel corpus» divide il tempo di una persona
    fra lei e i suoi frammenti, e il risultato e' sbagliato senza che
    niente lo segnali.
    """
    from core.corpus_db import CorpusDB

    with tempfile.TemporaryDirectory() as tmp:
        db = CorpusDB(path=Path(tmp) / "corpus.db")
        # La sessione prima dei segmenti: `segments.stem` punta a
        # `sessions.stem`, e senza questo il test si fermerebbe sul
        # vincolo di integrita' senza mai arrivare alla potatura.
        db.conn.execute(
            "INSERT INTO sessions(stem, n_speakers, n_segments, n_words) "
            "VALUES('s1', 2, 2, 2)"
        )
        # UNKNOWN e' una riga come le altre: non e' un interlocutore, ma la
        # potatura deve lasciarla stare, altrimenti le query che
        # distinguono "detto" da "non attribuito" non hanno piu' nulla
        # su cui distinguere.
        for gid in ("GLOBAL_001", "GLOBAL_002", "UNKNOWN"):
            db.conn.execute(
                "INSERT OR IGNORE INTO speakers(global_id, name) VALUES(?, NULL)",
                (gid,),
            )
        db.conn.execute(
            "INSERT INTO segments(stem, idx, speaker, text, n_words) "
            "VALUES('s1', 0, 'GLOBAL_001', 'uno', 1)"
        )
        db.conn.execute(
            "INSERT INTO segments(stem, idx, speaker, text, n_words) "
            "VALUES('s1', 1, 'UNKNOWN', 'boh', 1)"
        )
        db.conn.commit()

        n = db.prune_speakers()
        rimaste = {r["global_id"] for r in db.query("SELECT global_id FROM speakers")}
        require(rimaste == {"GLOBAL_001", "UNKNOWN"},
                f"devono restare la voce che parla e UNKNOWN, risulta {rimaste}")
        require(n == 1, f"una sola voce da togliere, risulta {n}")


@check
def il_nome_stale_viene_cancellato() -> None:
    """Un nome che il DB delle voci non ha piu' deve sparire dal corpus.

    Il DB delle voci e' la fonte dei nomi. Se li cancello li e la copia
    nel corpus li tiene, le query continuano a dare quel nome a una voce
    che non lo ha piu': il comando che toglie un nome sembra non aver
    fatto niente.
    """
    from core.corpus_db import CorpusDB

    with tempfile.TemporaryDirectory() as tmp:
        db = CorpusDB(path=Path(tmp) / "corpus.db")
        db.conn.execute(
            "INSERT OR IGNORE INTO speakers(global_id, name) VALUES('GLOBAL_001', 'Pietro')"
        )
        db.conn.commit()

        db.sync_speaker_names({})          # il DB delle voci non ha nomi
        nome = db.query(
            "SELECT name FROM speakers WHERE global_id = 'GLOBAL_001'"
        )[0]["name"]
        require(nome is None,
                f"il nome che non e' piu' nella fonte deve sparire, "
                f"risulta {nome!r}")

        db.sync_speaker_names({"GLOBAL_001": "Pietro"})
        nome = db.query(
            "SELECT name FROM speakers WHERE global_id = 'GLOBAL_001'"
        )[0]["name"]
        require(nome == "Pietro",
                f"e un nome che c'e' nella fonte deve arrivare, risulta {nome!r}")


@check
def la_voce_gia_vuota_viene_ripulita() -> None:
    """Una voce senza contributi sparisce anche se la sessione non la toccava.

    Il caso e' quello che si crea in due passate: una sessione svuota
    la voce, e se in quel momento la voce non aveva un nome resta li.
    Un secondo giro su un'altra sessione non la tocca piu', quindi
    senza questa scansione `review_speakers.py list` la mostra per
    sempre con zero minuti.
    """
    with tempfile.TemporaryDirectory() as tmp:
        db = SpeakerDB(path=Path(tmp) / "db.json")
        db.resolve("s1", {"A": {"embedding": _voce(1), "seconds": 60.0}})
        db.resolve("s2", {"A": {"embedding": _voce(1), "seconds": 60.0}})
        gid = next(iter(db._data["speakers"]))
        db._data["speakers"][gid]["sessions"] = {}      # come se fosse vuota

        rimosse = db.forget_session("s2")
        require(rimosse == [gid],
                f"la voce vuota doveva sparire, sono state rimosse {rimosse}")
        require(not db._data["speakers"],
                f"il DB doveva restare vuoto, contiene {db._data['speakers']}")


@check
def forget_su_una_sessione_ignota_non_fa_nulla() -> None:
    """Dimenticare una sessione che non e' mai stata vista lascia tutto com'era.

    Non e' paranoia: `consolidate` chiama `forget_session` per ogni
    cartella che trova in output/, e una cartenza puo' essere un
    tentativo abbandonato senza checkpoint.
    """
    with tempfile.TemporaryDirectory() as tmp:
        db = SpeakerDB(path=Path(tmp) / "db.json")
        db.resolve("s1", {"A": {"embedding": _voce(1), "seconds": 100.0}})
        prima = json.dumps(db._data["speakers"], sort_keys=True)

        db.forget_session("non_esiste")
        require(json.dumps(db._data["speakers"], sort_keys=True) == prima,
                "una sessione sconosciuta non deve toccare il DB")


@check
def gli_id_non_si_riusano_dopo_un_merge() -> None:
    """Un ID liberato da un merge non torna mai in circolazione.

    L'ID e' il nome con cui una persona e' citata in ogni sessione e
    nella repo del corpus. `_next_id` contava le voci: dopo una fusione
    il conteggio calava e la voce successiva prendeva un numero che
    era gia' stato di qualcun altro, senza che niente lo segnalasse. Il
    numero deve salire, e i buchi sono il prezzo.
    """
    with tempfile.TemporaryDirectory() as tmp:
        db = SpeakerDB(path=Path(tmp) / "db.json")
        for i, stem in enumerate(("s1", "s2", "s3")):
            db.resolve(stem, {"L": {"embedding": _voce(i + 1),
                                    "seconds": 100.0}})
        create = sorted(db._data["speakers"])
        require(create == ["GLOBAL_001", "GLOBAL_002", "GLOBAL_003"],
                f"le prime tre devono essere 1, 2, 3: {create}")

        db.merge_ids("GLOBAL_001", "GLOBAL_002")
        nuova = db._next_id()
        require(nuova == "GLOBAL_004",
                f"dopo il merge la nuova voce deve essere GLOBAL_004, "
                f"e' {nuova}: sta riusando GLOBAL_002 che era di qualcuno")


@check
def un_id_di_formato_strano_resta_occupato() -> None:
    """Un ID che non segue il formato non viene contato, ma non viene riusato.

    `GLOBAL_042` viene regolato dalla regola; `VECCHIO_001` no. Il
    numero successivo deve comunque superare il massimo dei regolari,
    altrimenti un ID importato da un formato precedente viene
    riassegnato a una persona nuova.
    """
    with tempfile.TemporaryDirectory() as tmp:
        db = SpeakerDB(path=Path(tmp) / "db.json")
        db.resolve("s1", {"A": {"embedding": _voce(1), "seconds": 100.0}})
        require(db._next_id() == "GLOBAL_002", "il conteggio parte da 1")
        db._data["speakers"]["GLOBAL_042"] = {
            "name": None, "centroid": [], "sessions": {},
        }
        require(db._next_id() == "GLOBAL_043",
                f"GLOBAL_042 occupa il posto, la successiva deve essere "
                f"GLOBAL_043, e' {db._next_id()}")


# ---------------------------------------------------------------------------
# Il comando che rifonde deve poter essere ritentato
# ---------------------------------------------------------------------------

def _consolidate_su(tmp: Path, volte: int = 1) -> dict[str, float]:
    """Esegue `consolidate` in un albero finto e ritorna i secondi per voce."""
    import os

    # Il modulo, non l'istanza. `core/__init__.py` fa
    # `from .config import config`, quindi `core.config` come attributo
    # e' l'istanza PipelineConfig e non il modulo: `reload()` su
    # quello solleva TypeError. `sys.modules` conserva l'unico
    # riferimento vero al modulo.
    import sys as _sys
    config_mod = _sys.modules["core.config"]
    import review_speakers as rs

    radice = tmp / "root"
    out = radice / "output"
    out.mkdir(parents=True, exist_ok=True)

    # Due sessioni con lo stesso parlato: la stessa voce deve prendere
    # la stessa identita' nelle due.
    for nome, (v1, v2) in (
        ("2026-01-01_10-00-00", (_voce(1), _voce(4))),
        ("2026-01-01_11-00-00", (_voce(1), _voce(7))),
    ):
        d = out / nome
        d.mkdir()
        segmenti = [
            {"speaker": "A", "start": 0.0, "end": 600.0},
            {"speaker": "B", "start": 600.0, "end": 400.0},
        ]
        (d / f"{nome}.checkpoint.json").write_text(json.dumps({
            "stem": nome,
            "file": f"input/{nome}.mp3",
            "stages": {s: {"done": True} for s in (
                "ffmpeg", "vad", "transcription", "diarization", "prosody")},
            "diarization_segments": segmenti,
            "speaker_embeddings": {"A": v1, "B": v2},
            "speaker_global_map": {},
            "chunks": [],
        }), encoding="utf-8")

    class _A:
        dry_run = False
        min_seconds = None
        threshold = None

    # `A2T_ROOT_DIR` e' la stessa leva che usa il test end-to-end: e'
    # cio' che permette a un test di girare sulla catena vera senza
    # scrivere nel database delle voci di produzione.
    os.environ["A2T_ROOT_DIR"] = str(radice)
    import importlib
    try:
        importlib.reload(_sys.modules["core.config"])
        importlib.reload(_sys.modules["core.speakers_merge"])
        importlib.reload(rs)
        db = rs.SpeakerDB(path=radice / "data" / "speakers_db.json")
        for _ in range(volte):
            rs.cmd_consolidate(db, _A())
        return {g: r.get("total_seconds", 0.0)
                for g, r in db._data["speakers"].items()}
    finally:
        os.environ.pop("A2T_ROOT_DIR", None)
        importlib.reload(_sys.modules["core.config"])


@check
def consolidate_rifatto_non_cambia_nulla() -> None:
    """Un comando di riparazione deve essere idempotente.

    Senza, ogni esecuzione assegnava nuovi ID alle stesse voci e
    rietichettava l'intero corpus: il giorno in cui si fosse rieseguito
    per un motivo qualsiasi, tutte le sessioni passate avrebbero
    cambiato interlocutore, e nessuno avrebbe saputo perche'.
    """
    with tempfile.TemporaryDirectory() as tmp:
        una = _consolidate_su(Path(tmp) / "a")
        due = _consolidate_su(Path(tmp) / "b", volte=2)
        require(una and set(una) == set(due),
                f"gli ID dopo due giri devono essere gli stessi: "
                f"{sorted(una)} contro {sorted(due)}")
        require(una == due,
                f"e anche i secondi: {una} contro {due}")


@check
def consolidate_collega_le_stesse_voci() -> None:
    """La stessa voce in due sessioni diverse prende la stessa identita'."""
    with tempfile.TemporaryDirectory() as tmp:
        sec = _consolidate_su(Path(tmp) / "c")
        # Voce 1 in entrambe le sessioni, voci 4 e 7 distinte: si
        # aspettano tre identita', non quattro.
        require(len(sec) == 3,
                f"tre voci distinte su due sessioni, ne sono nate {len(sec)}: "
                f"{sec}")
        require(sorted(sec.values(), reverse=True)[0] == 1200.0,
                f"la voce in comune deve avere 2x600 secondi, ha "
                f"{sorted(sec.values(), reverse=True)[0]}")


def main() -> int:
    passed = 0
    failed: list[str] = []
    for name, fn in CHECKS:
        try:
            fn()
            passed += 1
            print(f"  ok  {name}")
        except Failure as exc:
            failed.append(f"{name}: {exc}")
            print(f"  KO  {name} - {exc}")
        except Exception as exc:  # noqa: BLE001
            failed.append(f"{name}: {type(exc).__name__}: {exc}")
            print(f"  ERR {name} - {type(exc).__name__}: {exc}")

    print(f"\n{passed}/{len(CHECKS)} superati")
    if failed:
        print("Falliti:")
        for f in failed:
            print(f"  - {f}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())