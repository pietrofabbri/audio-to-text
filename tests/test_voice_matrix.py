"""
Test della matrice di somiglianza fra voci di sessioni diverse.

    python tests/test_voice_matrix.py

Niente rete, niente modelli, niente audio: solo vettori e confronto.

Cosa serve questa matrice. Le voci sono identita' globali, ma un
identita' che si ripete in tre giornate diverse porta con se' un
problema: il numero che la rappresenta non e' mai due volte lo stesso,
perche' cambia l'acustica della stanza, la stanchezza, il microfono.
La domanda utile non e' «sono la stessa persona?» — quella l'ha gia'
decisa chi assegna le identita' — ma «quando due voci che so essere
diverse finiscono vicine alla soglia?». Sono le coppie in zona grigia,
e sono le uniche che il giudizio umano deve chiudere.

Il difetto che questo test copre e' di aggregazione. Se `per_voce`
raggruppa male, la matrice dice che hai incontrato undici persone e ti
mostra un'identita' sola mentre la stessa persona compare in tre
sessioni: il numero di interlocutori e' sbagliato e non si vede da
nessuna parte. E' la stessa classe di difetto della sovrasegmentazione,
una scala piu' piccola.
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

from core.voice_matrix import (  # noqa: E402
    VoiceSample, build_matrix, format_report, load_samples,
)


class Failure(Exception):
    pass


def require(cond: bool, msg: str) -> None:
    if not cond:
        raise Failure(msg)


def _voce(verso: int, dimensione: int = 8) -> list[float]:
    """Un vettore unitario con un angolo controllato.

    Le coordinate sono `cos` e `sin` di un angolo, quindi due vettori
    sono ortogonali a 90 gradi e opposti a 180. Serve per costruire
    coppie con una somiglianza nota, senza dipendere da come un vettore
    casuale capita di orientarsi.
    """
    import math
    a = verso * math.pi / 4
    v = [0.0] * dimensione
    v[0] = math.cos(a)
    v[1] = math.sin(a)
    return v


def _campione(gid: str, locale: str, sessione: str, secondi: float = 600.0,
              verso: int = 0) -> VoiceSample:
    return VoiceSample(gid=gid, locale=locale, sessione=sessione,
                       secondi=secondi, embedding=_voce(verso))


def _scrivi_ck(root: Path, sessione: str, segmenti: list[dict],
               emb: dict, mappa: dict) -> None:
    d = root / sessione
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{sessione}.checkpoint.json").write_text(json.dumps({
        "stem": sessione,
        "diarization_segments": segmenti,
        "speaker_embeddings": emb,
        "speaker_global_map": mappa,
    }), encoding="utf-8")


# ----------------------------------------------------------------------
# Il raggruppamento trasversale: la ragione per cui la matrice esiste
# ----------------------------------------------------------------------

def raggruppa_una_voce_fra_sessioni() -> None:
    """Una persona incontrata tre volte deve contare come una.

    E' la domanda per cui la matrice e' stata scritta. Se la stessa
    identita' compare in tre giornate e il conteggio ne dice tre, il
    numero di persone che hai incontrato e' sbagliato — ed e' sbagliato
    perche' il calcolo le tratta come tre, anche se il gruppo le
    recognizes come una. Qui la stessa `GLOBAL_006` arriva da tre
    sessioni diverse e deve tornare un'interlocutore solo, con i suoi
    secondi sommati.
    """
    campioni = [
        _campione("GLOBAL_006", "SPEAKER_01", "2026-10-01_10-00-00", 300, 0),
        _campione("GLOBAL_006", "SPEAKER_02", "2026-10-02_10-00-00", 400, 1),
        _campione("GLOBAL_006", "SPEAKER_03", "2026-10-03_10-00-00", 500, 2),
        _campione("GLOBAL_007", "SPEAKER_01", "2026-10-01_10-00-00", 900, 4),
    ]
    rep = build_matrix(campioni, soglia=0.78)
    per_voce = rep.per_voce()

    require(len(per_voce) == 2,
            f"devono essere 2 interlocutori, risultano {len(per_voce)}")
    sei = per_voce["GLOBAL_006"]
    require(len(sei) == 3,
            f"GLOBAL_006 compare in 3 sessioni, risultano {len(sei)}")
    require([c.sessione for c in sei] == sorted(c.sessione for c in sei),
            "le sessioni di una voce devono essere in ordine")
    require(sum(c.secondi for c in sei) == 1200.0,
            f"i secondi di GLOBAL_006 sommano a 1200, "
            f"sommano a {sum(c.secondi for c in sei)}")

    # La stessa voce globale non viene confrontata con se stessa: sono
    # la stessa persona per costruzione, e il numero che ne uscirebbe
    # misurerebbe quanto una voce cambia da un giorno all'altro — che
    # e' un'altra domanda, e mescolarla nasconderebbe le coppie utili.
    for p in rep.coppie:
        require(p.a.gid != p.b.gid,
                f"una voce non deve essere confrontata con se stessa: "
                f"{p.a.gid} x {p.b.gid}")


def confronta_sole_voci_diverse() -> None:
    """Le coppie sono solo fra voci globali diverse, e senza ripetizioni."""
    campioni = [
        _campione("A", "S1", "g1", 300, 0),
        _campione("B", "S2", "g1", 300, 1),
        _campione("C", "S3", "g1", 300, 2),
        _campione("D", "S4", "g2", 300, 4),
    ]
    rep = build_matrix(campioni, soglia=0.78)

    # Quattro voci distinte danno sei coppie, non dieci.
    require(len(rep.coppie) == 6,
            f"4 voci diverse danno 6 coppie, risultano {len(rep.coppie)}")
    chiavi = {(p.a.chiave, p.b.chiave) for p in rep.coppie}
    require(len(chiavi) == len(rep.coppie), "nessuna coppia ripetuta")


def non_confronta_lo_stesso_campione() -> None:
    """Lo stesso campione non deve essere confrontato con se stesso.

    Il confronto ha un senso solo fra due osservazioni diverse. Se la
    stessa osservazione entrasse due volte, la coppia avrebbe
    somiglianza 1.0 e finirebbe dritta sopra la soglia: la matrice
    direbbe «questa persona e' sicuramente qualcun altro» guardandola
    allo specchio.
    """
    c = _campione("A", "S1", "g1", 300, 0)
    rep = build_matrix([c], soglia=0.78)
    require(not rep.coppie,
            f"un campione solo non dà coppie, risultano {len(rep.coppie)}")


def zona_grigia() -> None:
    """La zona grigia e' quella che il giudizio umano deve chiudere.

    La soglia non e' una verita': e' un numero che divide due cose che
    non hanno una divisione netta. Le coppie entro un margine da essa
    potrebbero essere la stessa persona o due persone, e nessuna delle
    due letture si puo' scegliere dai numeri. Devono quindi stare in un
    elenco proprio, perche' e' l'unica parte della matrice che chiede
    una decisione e non la prende da sola.
    """
    campioni = [
        _campione("A", "S1", "g1", 300, 0),   # 1.00 verso 0 gradi
        _campione("B", "S2", "g2", 300, 0),   # 1.00: identico
        _campione("C", "S3", "g3", 300, 2),   # 0.00: a 90 gradi
    ]
    rep = build_matrix(campioni, soglia=0.78)

    zona = rep.zona_grigia(margine=0.06)
    require(len(zona) == 0,
            f"le coppie 1.00 e 0.00 non sono in zona grigia, "
            f"risultano {len(zona)}")

    sopra = rep.sicure_stessa_persona()
    sotto = rep.sicure_persone_diverse()
    # Tre vettori danno tre coppie: una identica (1.0) e due perpendicolari (0.0).
    require(len(sopra) == 1, f"una coppia sopra soglia, risultano {len(sopra)}")
    require(len(sotto) == 2, f"due coppie sotto soglia, risultano {len(sotto)}")
    require(sopra[0].simiglianza == 1.0, "la coppia identica e' a 1.0")
    require(abs(sum(p.simiglianza for p in sotto)) < 1e-9,
            "le coppie perpendicolari sono a 0.0")

    # Due vettori a 90 gradi valgono 0.0: lontanissimi dalla soglia, e
    # la zona grigia non li deve prendere.
    lontane = build_matrix([
        _campione("A", "S1", "g1", 300, 0),
        _campione("B", "S2", "g2", 300, 1),
    ], soglia=0.78)
    require(len(lontane.zona_grigia(margine=0.06)) == 0,
            "due vettori a 90 gradi non sono borderline (0.0)")

    # Con una soglia che passa vicino, la stessa coppia diventa incerta:
    # 0.707 contro 0.71 e' a tre millesimi dalla linea, e li' nessuna
    # delle due letture si puo' scegliere dai numeri.
    fila = build_matrix([
        _campione("A", "S1", "g1", 300, 0),
        _campione("B", "S2", "g2", 300, 1),
    ], soglia=0.71)
    zona = fila.zona_grigia(margine=0.06)
    require(len(zona) == 1,
            f"una coppia entro il margine deve finire in zona grigia, "
            f"risultano {len(zona)}")
    require(not zona[0].same_session(),
            "la coppia viene da due sessioni diverse")

    # E il margine non deve essere un secchio: poco piu' stretto della
    # distanza reale, la coppia esce dalla zona grigia.
    require(len(fila.zona_grigia(margine=0.001)) == 0,
            "un margine piu' stretto della distanza deve escluderla")


def campione_senza_identita_non_confrontato() -> None:
    """Un embedding senza identita' globale resta fuori.

    Non ha identita' non puo' essere confrontato con nessuno: finirebbe
    nella matrice come una voce che non parla mai, e il riepilogo
    direbbe un interlocutore in piu' che nessuno ha mai incontrato.
    Meglio che resti fuori, e che il conto torni con quello che si
    vede.
    """
    rep_con = build_matrix([_campione("A", "S1", "g1", 300, 0)], 0.78)

    # Stessa identita' presente: un campione, zero coppie.
    require(len(rep_con.coppie) == 0, "un campione non dà coppie con se stesso")
    require(len(rep_con.per_voce()) == 1, "l'identita' c'e'")

    # E un embedding vuoto non deve nemmeno arrivare al confronto.
    vuoto = _campione("A", "S1", "g1", 300, 0)
    vuoto.embedding = []
    rep = build_matrix([vuoto, _campione("B", "S2", "g2", 300, 4)], 0.78)
    require(not rep.coppie,
            f"un embedding vuoto non è confrontabile, risultano "
            f"{len(rep.coppie)} coppie")


# ----------------------------------------------------------------------
# La lettura dai checkpoint
# ----------------------------------------------------------------------

def legge_dai_checkpoint() -> None:
    """I campioni arrivano dai checkpoint, uno per voce locale."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _scrivi_ck(
            root, "2026-10-01_19-00-00",
            segmenti=[
                {"speaker": "SPEAKER_01", "start": 0.0, "end": 300.0},
                {"speaker": "SPEAKER_01", "start": 400.0, "end": 700.0},
                {"speaker": "SPEAKER_02", "start": 800.0, "end": 900.0},
            ],
            emb={"SPEAKER_01": _voce(0), "SPEAKER_02": _voce(4)},
            mappa={"SPEAKER_01": "GLOBAL_001", "SPEAKER_02": "GLOBAL_002"},
        )
        _scrivi_ck(
            root, "2026-10-02_20-00-00",
            segmenti=[{"speaker": "SPEAKER_01", "start": 0.0, "end": 500.0}],
            emb={"SPEAKER_01": _voce(1)},
            mappa={"SPEAKER_01": "GLOBAL_001"},
        )

        campioni = load_samples(root)
        require(len(campioni) == 3,
                f"2 voci nella prima + 1 nella seconda = 3 campioni, "
                f"risultano {len(campioni)}")

        rep = build_matrix(campioni, soglia=0.78)
        per_voce = rep.per_voce()
        require(set(per_voce) == {"GLOBAL_001", "GLOBAL_002"},
                f"due identita' globali, risultano {sorted(per_voce)}")
        require(len(per_voce["GLOBAL_001"]) == 2,
                "GLOBAL_001 compare in due sessioni diverse")

        # I secondi vengono dai segmenti, sommati per voce locale.
        secondi = {c.chiave: c.secondi for c in campioni}
        require(abs(secondi["2026-10-01_19-00-00|SPEAKER_01"] - 600.0) < 0.1,
                f"SPEAKER_01 parla 600s, risultano "
                f"{secondi['2026-10-01_19-00-00|SPEAKER_01']}")


def checkpoint_incompleti_ignorati() -> None:
    """Un checkpoint incompleto viene saltato, non fa fallire tutto.

    Le sessioni non finiscono tutte: una notte interrotta lascia cartelle
    a meta'. Se una di queste blocca la matrice, non si vede piu'
    niente, e la domanda che la matrice porta — chi e' vicino alla
    soglia — resta senza risposta proprio quando i dati sono incompleti
    e quindi piu' utili. Meglio una matrice con tre voci che
    nessuna.
    """
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)

        _scrivi_ck(root, "2026-10-01_10-00-00",
                   segmenti=[{"speaker": "S1", "start": 0, "end": 100}],
                   emb={"S1": _voce(0)}, mappa={"S1": "GLOBAL_001"})

        # Nessuna identita' globale: non e' confrontabile.
        _scrivi_ck(root, "2026-10-02_10-00-00",
                   segmenti=[{"speaker": "S1", "start": 0, "end": 100}],
                   emb={"S1": _voce(1)}, mappa={})

        # Nessun segmento.
        _scrivi_ck(root, "2026-10-03_10-00-00",
                   segmenti=[], emb={"S1": _voce(2)}, mappa={"S1": "G_1"})

        # JSON rottibile: deve essere saltato, non far esplodere la lettura.
        d = root / "2026-10-04_10-00-00"
        d.mkdir(parents=True)
        (d / "2026-10-04_10-00-00.checkpoint.json").write_text(
            "{rotto", encoding="utf-8")

        campioni = load_samples(root)
        require(len(campioni) == 1,
                f"solo la sessione completa vale, risultano {len(campioni)}")
        require(campioni[0].gid == "GLOBAL_001",
                f"deve restare la voce buona, risulta {campioni[0].gid}")


def cartella_vuota() -> None:
    """Nessuna sessione non e' un errore: e' un report vuoto."""
    with tempfile.TemporaryDirectory() as tmp:
        require(load_samples(Path(tmp)) == [], "una cartella vuota dà nulla")
        rep = build_matrix([], soglia=0.78)
        require(rep.per_voce() == {}, "nessuna voce")
        require(rep.coppie == [], "nessuna coppia")
        testo = format_report(rep)
        require("0 coppie" in testo,
                f"il report deve dirlo, dice: {testo[:80]!r}")


# ----------------------------------------------------------------------
# Il report, perche' e' l'unica parte che leggi davvero
# ----------------------------------------------------------------------

def report_dichiara_qual_confronto_e() -> None:
    """Il report deve dire quale operazione ha prodotto i suoi numeri.

    Il caso reale: la matrice confronta campione con campione, mentre
    `SpeakerDB._best_match()` confronta l'embedding della sessione contro
    i centroidi salvati. Per `GLOBAL_028` la matrice diceva 0,848 contro
    `GLOBAL_001` — ben sopra la soglia — mentre il sistema ne aveva visto
    0,734 e aveva giustamente aperto una voce nuova. Il sistema era
    coerente: quello che mancava era dirlo.

    Senza questa riga il report invites a una lettura sbagliata: si legge
    0,848, si conclude che il sistema abbia sbagliato, e si corregge a mano
    un merge che era giusto.

    Il test non controlla che il numero sia lo stesso — quello dipende
    dalla soglia e resta una decisione — ma che il report nomini le due
    operazioni, cosi' chi legge non puo' prendersi l'uno per l'altro.
    """
    campioni = [
        _campione("GLOBAL_001", "S1", "2026-10-01_19-42-33", 300, 0),
        _campione("GLOBAL_002", "S1", "2026-10-01_19-42-33", 600, 4),
        _campione("GLOBAL_002", "S2", "2026-10-02_20-44-03", 400, 3),
    ]
    rep = build_matrix(campioni, soglia=0.5)
    testo = format_report(rep).lower()

    require("campione" in testo,
            "il report non dice che confronta campioni")
    require("centroid" in testo,
            "il report non dice che l'assegnazione usa i centroidi")
    require("non sono lo stesso numero" in testo,
            "il report non avverte che i due numeri sono diversi")

    # I due campioni vengono da sessioni diverse: la matrice scarta
    # apposta le coppie della stessa sessione, perché due voci udite
    # nella stessa registrazione non si somigliano di piu' per il fatto
    # di aver coesisto. Prima di questo controllo la dichiarazione diceva
    # il contrario — «stessa sessione fra le due voci» — cioe' esattamente
    # l'errore che la riga doveva prevenire.
    require("sessioni diverse" in testo,
            "il report non dice che i due campioni vengono da sessioni diverse")
    n_diverse = sum(1 for p in rep.coppie if not p.same_session())
    require(n_diverse == len(rep.coppie),
            f"il report ha {len(rep.coppie)} coppie ma solo {n_diverse} "
            "fra sessioni diverse: la dichiarazione deve corrispondere")


def report_leggibile() -> None:
    """Il report mette in evidenza la zona grigia e resta serializzabile."""
    campioni = [
        _campione("GLOBAL_001", "S1", "2026-10-01_19-42-33", 300, 0),
        _campione("GLOBAL_002", "S1", "2026-10-01_19-42-33", 600, 4),
        _campione("GLOBAL_002", "S2", "2026-10-02_20-44-03", 400, 3),
    ]
    rep = build_matrix(campioni, soglia=0.5)

    testo = format_report(rep)
    require("GLOBAL_001" in testo and "GLOBAL_002" in testo,
            "il report deve nominare ogni voce")
    require("19-42" in testo and "20-44" in testo,
            "il report deve dire in quali sessioni hai sentito la voce")
    require(len(testo.splitlines()) > 4, "il report non e' vuoto")

    completo = format_report(rep, mostra_tutto=True)
    require(len(completo.splitlines()) > len(testo.splitlines()),
            "--tutto deve mostrare anche le coppie")

    # Il dizionario e' la forma che va in un file: deve stare in piedi.
    d = rep.to_dict()
    require(json.dumps(d, ensure_ascii=False),
            "to_dict deve essere serializzabile")
    require(d["n_voices"] == 2, f"2 voci, risultano {d['n_voices']}")
    require(d["n_samples"] == 3, f"3 campioni, risultano {d['n_samples']}")
    require(d["threshold"] == 0.5, "la soglia resta registrata")
    require(len(d["voices"]["GLOBAL_002"]) == 2,
            "GLOBAL_002 deve comparire con le sue due sessioni")
    # Le coppie sono ordinate dalla piu' somigliante: e' l'ordine in cui
    # si guardano, e quello che rende il file leggibile anche a mano.
    sim = [p["similarity"] for p in d["pairs"]]
    require(sim == sorted(sim, reverse=True),
            f"le coppie vanno dalla più somigliante, sono {sim}")


# ----------------------------------------------------------------------
# Per coppia di voci (APERTI 10): la decisione si prende sulle voci
# ----------------------------------------------------------------------

def _vettore(x: float, y: float) -> list[float]:
    return [x, y] + [0.0] * 6


def aggrega_per_coppia_di_voci() -> None:
    """Quattordici confronti della stessa coppia diventano una riga.

    E' il caso vero di GLOBAL_004 x GLOBAL_018: la stessa domanda posta
    per ogni coppia di sessioni, con risposte diverse. Qui due voci in
    tre sessioni ciascuna danno 9 confronti campione-campione, e il
    report deve restituirne una sola riga con media, massimo e quanti
    stanno sopra la soglia.
    """
    import math
    campioni = []
    for i, s in enumerate(["2026-10-01_10-00-00", "2026-10-02_10-00-00",
                           "2026-10-03_10-00-00"]):
        campioni.append(VoiceSample("GLOBAL_004", "SPEAKER_00", s, 300,
                                    _vettore(1.0, 0.0)))
        ang = [0.55, 0.65, 0.75][i]          # coseni 0.85, 0.80, 0.73
        campioni.append(VoiceSample("GLOBAL_018", "SPEAKER_01",
                                    s.replace("10-00", "11-00"), 300,
                                    _vettore(math.cos(ang), math.sin(ang))))
    rep = build_matrix(campioni, soglia=0.78)
    righe = rep.per_coppia_di_voci()
    require(len(righe) == 1, f"una coppia di voci, risultano {len(righe)}")
    r = righe[0]
    require((r.a, r.b) == ("GLOBAL_004", "GLOBAL_018"),
            f"coppia sbagliata: {r.a} x {r.b}")
    require(r.n == 9, f"9 confronti fra sessioni diverse, risultano {r.n}")
    require(r.sopra == 6, f"sopra 0,78 stanno 6 confronti, risultano {r.sopra}")
    require(abs(r.massimo - math.cos(0.55)) < 1e-6, f"massimo sbagliato: {r.massimo}")
    require(r.insieme == 0, "non compaiono mai nella stessa sessione")


def centroide_e_il_numero_del_sistema() -> None:
    """Il numero che decide e' centroide contro centroide.

    Se il DB delle voci da' i centroidi, si usano quelli: e' lo stesso
    confronto di `SpeakerDB._best_match`, e il report non puo' dire una
    cosa diversa da quella che il sistema ha fatto. Qui il caso di
    GLOBAL_028: un campione fortunato sta sopra la soglia, il centroide
    no — e il report deve mostrare il centroide.
    """
    import math
    campioni = [
        VoiceSample("GLOBAL_001", "SPEAKER_00", "2026-10-01_10-00-00", 600,
                    _vettore(1.0, 0.0)),
        VoiceSample("GLOBAL_028", "SPEAKER_00", "2026-10-02_10-00-00", 60,
                    _vettore(math.cos(0.5), math.sin(0.5))),   # 0.878
    ]
    centroidi = {"GLOBAL_001": _vettore(1.0, 0.0),
                 "GLOBAL_028": _vettore(math.cos(0.75), math.sin(0.75))}
    rep = build_matrix(campioni, soglia=0.78, centroidi=centroidi)
    r = rep.per_coppia_di_voci()[0]
    require(abs(r.centroide - math.cos(0.75)) < 1e-6,
            f"il centroide deve venire dal DB: {r.centroide:.4f}")
    require(r.massimo > 0.78 > r.centroide,
            "il caso e' costruito con il campione sopra e il centroide sotto")
    require(rep.coppie_da_decidere() == [r],
            "un campione sopra la soglia basta a renderla da decidere")

    # Senza centroidi dal DB, si ricavano dai campioni: con un campione
    # per voce coincidono con il campione stesso.
    rep2 = build_matrix(campioni, soglia=0.78)
    r2 = rep2.per_coppia_di_voci()[0]
    require(abs(r2.centroide - math.cos(0.5)) < 1e-6,
            f"centroide ricavato dai campioni sbagliato: {r2.centroide:.4f}")


def coppie_lontane_non_sono_da_decidere() -> None:
    """Due voci chiaramente diverse non devono comparire nell'elenco."""
    campioni = [
        VoiceSample("GLOBAL_001", "SPEAKER_00", "2026-10-01_10-00-00", 600,
                    _vettore(1.0, 0.0)),
        VoiceSample("GLOBAL_002", "SPEAKER_00", "2026-10-02_10-00-00", 600,
                    _vettore(0.0, 1.0)),
    ]
    rep = build_matrix(campioni, soglia=0.78)
    require(len(rep.per_coppia_di_voci()) == 1, "una coppia nel totale")
    require(rep.coppie_da_decidere() == [], "coseno 0: niente da decidere")


def parlano_insieme_e_un_indizio() -> None:
    """Due voci nella stessa registrazione: contate in `insieme`.

    I confronti della stessa sessione non entrano nelle statistiche
    (la diarizzazione le ha gia' separate) ma il report deve dire che
    hanno parlato insieme, perche' e' l'indizio piu' forte che siano
    due persone.
    """
    import math
    v = _vettore(math.cos(0.4), math.sin(0.4))
    campioni = [
        VoiceSample("GLOBAL_031", "SPEAKER_00", "2026-10-05_10-41-30", 600,
                    _vettore(1.0, 0.0)),
        VoiceSample("GLOBAL_035", "SPEAKER_01", "2026-10-05_10-41-30", 600, v),
        VoiceSample("GLOBAL_035", "SPEAKER_00", "2026-10-05_11-43-50", 600, v),
    ]
    rep = build_matrix(campioni, soglia=0.78)
    r = rep.per_coppia_di_voci()[0]
    require(r.insieme == 1, f"insieme in una sessione, risulta {r.insieme}")
    require(r.n == 1, f"un solo confronto fra sessioni diverse, risultano {r.n}")
    testo = format_report(rep)
    require("parlano insieme" in testo,
            "il report deve dire che le due voci hanno parlato insieme")
    d = rep.to_dict()
    require(d["voice_pairs_to_decide"][0]["sessions_together"] == 1,
            "il JSON deve portare sessions_together")
    require("embedding" not in json.dumps(d["voice_pairs"]),
            "nessun embedding nel JSON delle coppie")


CHECKS = [
    ("una persona in tre giornate resta una persona",
     raggruppa_una_voce_fra_sessioni),
    ("si confrontano solo voci diverse, senza ripetizioni",
     confronta_sole_voci_diverse),
    ("un campione non viene confrontato con se stesso",
     non_confronta_lo_stesso_campione),
    ("la zona grigia è quella che resta aperta", zona_grigia),
    ("un campione senza identità non viene confrontato",
     campione_senza_identita_non_confrontato),
    ("i campioni si leggono dai checkpoint", legge_dai_checkpoint),
    ("i checkpoint incompleti non bloccano la matrice",
     checkpoint_incompleti_ignorati),
    ("una cartella vuota dà un report vuoto", cartella_vuota),
    ("il report è leggibile e serializzabile", report_leggibile),
    ("il report dichiara quale confronto ha fatto", report_dichiara_qual_confronto_e),
    ("i confronti si aggregano per coppia di voci", aggrega_per_coppia_di_voci),
    ("decide il centroide, come il sistema", centroide_e_il_numero_del_sistema),
    ("le coppie lontane non sono da decidere",
     coppie_lontane_non_sono_da_decidere),
    ("parlare insieme e' un indizio, e si vede", parlano_insieme_e_un_indizio),
]


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
