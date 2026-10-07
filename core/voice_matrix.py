"""
Matrice di somiglianza fra le voci, voce per voce e sessione per sessione.

Il problema che risolve. Il DB delle voci tiene un solo numero per
persona: il centroide, la media di tutte le sessioni in cui ha
parlato. E' il numero giusto per riconoscere, e il numero sbagliato per
capire. Perche' non si puo' chiedere «sono la stessa persona?» guardando
lui: e' la media, e la media non e' una voce.

Quello che serve, e che il DB non ha, e' la domanda opposta: **due voci
che coesistono in giorni diversi quanto si somigliano?** Non per
riconoscerle — quello e' gia' fatto — ma per vedere se la macchina ha
deciso bene. Una coppia a 0,90 e' la stessa persona. Una a 0,13 sono due
persone. Una a 0,65 e' il caso in cui non sa, ed e' l'unica che merita
una decisione umana.

Le embedding vive sono gia' nei checkpoint di ogni sessione: non dentro
il DB delle voci, che tiene solo la media, ma li', uno per voce locale.
Questo modulo le rilegge e le mette in relazione.

Perche' la matrice e' quadrata e non una lista. Per ogni voce globale
si prendono tutte le sue voci locali, una per sessione in cui ha
parlato. Poi si confrontano tutte le coppie che **non** vengono da
quella stessa voce: due voci che coesistono davvero, o che si sono
sentite in due giorni diversi. Ogni coppia porta con se' i due
riscontri, la somiglianza fra loro e da quale sessione vengono.

Il risultato si legge cosi': una coppia sopra `db.threshold` e' la stessa
persona e il matching ha funzionato; una coppia sotto e' gente
diversa; e una coppia in mezzo, quella da guardare con attenzione, e'
l'unica che questa matrice serve a far emergere. Senza, quei numeri
esistono gia' dentro un file che nessuno apre.
"""

from __future__ import annotations

import json
import logging
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.speakers_merge import cosine  # noqa: E402

logger = logging.getLogger("audio-to-text.voices")


@dataclass
class VoiceSample:
    """Una voce come e' stata udita in una sessione precisa."""

    gid: str                 # GLOBAL_00x
    locale: str              # SPEAKER_00, come l'ha chiamata pyannote
    sessione: str            # stem della sessione
    secondi: float
    embedding: list[float]

    @property
    def chiave(self) -> str:
        return f"{self.sessione}|{self.locale}"


@dataclass
class Pair:
    """Due campioni di due voci diverse, confrontati fra loro."""

    a: VoiceSample
    b: VoiceSample
    simiglianza: float

    def same_session(self) -> bool:
        return self.a.sessione == self.b.sessione

    def to_dict(self) -> dict[str, Any]:
        return {
            "a": {"gid": self.a.gid, "session": self.a.sessione,
                  "seconds": round(self.a.secondi, 1)},
            "b": {"gid": self.b.gid, "session": self.b.sessione,
                  "seconds": round(self.b.secondi, 1)},
            "similarity": round(self.simiglianza, 4),
            "same_session": self.same_session(),
        }


@dataclass
class VoicePairSummary:
    """Due **voci** messe a confronto, non due campioni.

    Perche' esiste (APERTI 10). La matrice pone la domanda «questi due
    campioni sono la stessa persona?» una volta per ogni coppia di
    sessioni: sulle voci vere del 4 ottobre la poneva 14 volte per la
    stessa coppia `GLOBAL_004 × GLOBAL_018`, e la risposta andava da 0,661
    a 0,784 — 1 sopra la soglia e 13 sotto. Nessuno decide sui campioni:
    si decide sulle voci. Aggregando, le 34 indecisioni diventavano 4.

    Il numero che decide e' `centroide`: il coseno fra i due centroidi,
    cioe' **lo stesso confronto che fa il sistema** quando assegna le
    identita' (`SpeakerDB._best_match`). Cosi' il report e il sistema non
    possono contraddirsi per costruzione. Media, massimo e quante volte
    un campione ha superato la soglia restano accanto, come contesto:
    dicono quanto la coppia e' stabile da una sessione all'altra.

    `insieme` conta le sessioni in cui le due voci compaiono **nella
    stessa registrazione**. E' l'indizio piu' forte che esista che siano
    due persone diverse: la diarizzazione le ha separate mentre parlavano
    nello stesso file, probabilmente l'una all'altra. Non e' una prova
    (una persona puo' essere spezzata in due dentro un file), ma sposta
    il giudizio.
    """

    a: str
    b: str
    n: int                 # confronti campione-campione fra sessioni diverse
    media: float | None
    massimo: float | None
    sopra: int             # quanti di quei confronti superano la soglia
    centroide: float       # coseno centroide-centroide: il numero del sistema
    insieme: int           # sessioni in cui compaiono tutte e due

    def to_dict(self) -> dict[str, Any]:
        def r(x: float | None) -> float | None:
            return None if x is None else round(x, 4)
        return {
            "a": self.a, "b": self.b,
            "centroid_similarity": r(self.centroide),
            "n_cross_session": self.n,
            "mean": r(self.media), "max": r(self.massimo),
            "above_threshold": self.sopra,
            "sessions_together": self.insieme,
        }


@dataclass
class VoiceReport:
    """Tutto quello che la matrice dice, in forma leggibile."""

    soglia: float
    campioni: list[VoiceSample] = field(default_factory=list)
    coppie: list[Pair] = field(default_factory=list)
    # Centroidi del DB delle voci, se il chiamante li ha. Senza, si
    # ricavano dai campioni come media pesata per i secondi, che e' come
    # il DB li costruisce: misurato sulle voci vere, il coseno fra il
    # centroide salvato e la media dei campioni vale 1,0000 per le voci
    # con un solo campione e 0,934-0,989 per le altre (APERTI 10).
    centroidi: dict[str, list[float]] = field(default_factory=dict)

    def _centroide(self, gid: str) -> list[float]:
        if self.centroidi.get(gid):
            return list(self.centroidi[gid])
        voci = [c for c in self.campioni if c.gid == gid and c.embedding]
        if not voci:
            return []
        dim = len(voci[0].embedding)
        somma = [0.0] * dim
        peso_tot = 0.0
        for c in voci:
            if len(c.embedding) != dim:
                continue
            p = max(c.secondi, 1.0)
            peso_tot += p
            for i, x in enumerate(c.embedding):
                somma[i] += x * p
        return [x / peso_tot for x in somma] if peso_tot else []

    def per_coppia_di_voci(self) -> list[VoicePairSummary]:
        """Una riga per coppia di voci, dalla piu' somigliante.

        I confronti fra campioni della **stessa sessione** non entrano
        nelle statistiche: due voci nello stesso file le ha gia' separate
        la diarizzazione, e non e' la domanda che qui si fa. Entrano pero'
        nel conteggio `insieme`, perche' sono un indizio.
        """
        gruppi: dict[tuple[str, str], list[Pair]] = {}
        insieme: dict[tuple[str, str], set[str]] = {}
        for p in self.coppie:
            k = tuple(sorted((p.a.gid, p.b.gid)))
            if p.same_session():
                insieme.setdefault(k, set()).add(p.a.sessione)
            else:
                gruppi.setdefault(k, []).append(p)

        out = []
        for k in sorted(set(gruppi) | set(insieme)):
            ps = gruppi.get(k, [])
            sims = [p.simiglianza for p in ps]
            out.append(VoicePairSummary(
                a=k[0], b=k[1], n=len(sims),
                media=(sum(sims) / len(sims)) if sims else None,
                massimo=max(sims) if sims else None,
                sopra=sum(1 for s in sims if s >= self.soglia),
                centroide=cosine(self._centroide(k[0]), self._centroide(k[1])),
                insieme=len(insieme.get(k, ())),
            ))
        return sorted(out, key=lambda s: -s.centroide)

    def coppie_da_decidere(self, margine: float = 0.06) -> list[VoicePairSummary]:
        """Le coppie di voci che la soglia non chiude da sola.

        Una coppia e' da decidere se il numero del sistema (centroide
        contro centroide) cade entro `margine` dalla soglia o la supera,
        oppure se almeno un campione l'ha superata. Le coppie che parlano
        nella stessa registrazione restano nell'elenco ma marcate: sono le
        piu' probabilmente due persone diverse.
        """
        return [s for s in self.per_coppia_di_voci()
                if s.centroide >= self.soglia - margine
                or (s.massimo is not None and s.massimo >= self.soglia)]

    def per_voce(self) -> dict[str, list[VoiceSample]]:
        out: dict[str, list[VoiceSample]] = {}
        for c in self.campioni:
            out.setdefault(c.gid, []).append(c)
        return {g: sorted(v, key=lambda c: c.sessione)
                for g, v in sorted(out.items())}

    def zona_grigia(self, margine: float = 0.06) -> list[Pair]:
        """Coppie troppo vicine alla soglia per essere decise da lei.

        La soglia non e' una verita' e' una soglia: quello che sta
        entro `margine` da lei potrebbe essere la stessa persona o due
        persone, e la differenza fra le due letture non e' conoscibile
        dal numero. Sono le coppie che il giudizio umano deve chiudere,
        e sono le uniche che questa matrice serve a far emergere.
        """
        return [p for p in self.coppie
                if abs(p.simiglianza - self.soglia) <= margine]

    def sicure_stessa_persona(self) -> list[Pair]:
        return [p for p in self.coppie if p.simiglianza >= self.soglia]

    def sicure_persone_diverse(self) -> list[Pair]:
        return [p for p in self.coppie if p.simiglianza < self.soglia]

    def to_dict(self) -> dict[str, Any]:
        return {
            "threshold": self.soglia,
            "n_voices": len(self.per_voce()),
            "n_samples": len(self.campioni),
            "n_pairs": len(self.coppie),
            "voices": {
                g: [{"session": c.sessione, "seconds": round(c.secondi, 1)}
                    for c in v]
                for g, v in self.per_voce().items()
            },
            "pairs": sorted(
                (p.to_dict() for p in self.coppie),
                key=lambda d: -d["similarity"],
            ),
            "gray_zone": [p.to_dict() for p in self.zona_grigia()],
            # Per coppia di voci, con il numero del sistema. Nessun
            # embedding: solo pseudonimi e coseni, come il resto.
            "voice_pairs_to_decide": [
                s.to_dict() for s in self.coppie_da_decidere()
            ],
            "voice_pairs": [s.to_dict() for s in self.per_coppia_di_voci()],
        }


def _samples_from_session(
    stem: str,
    segmenti: list[dict[str, Any]],
    emb: dict[str, list[float]],
    global_map: dict[str, str],
) -> list[VoiceSample]:
    """I campioni di una sessione, uno per voce locale."""
    from core.speakers_merge import speaking_seconds

    secondi = speaking_seconds(segmenti)
    out = []
    for locale, vettore in (emb or {}).items():
        gid = (global_map or {}).get(locale)
        if not gid:
            continue
        out.append(VoiceSample(
            gid=gid, locale=locale, sessione=stem,
            secondi=float(secondi.get(locale, 0.0)),
            embedding=list(vettore),
        ))
    return out


def load_samples(output_dir: Path) -> list[VoiceSample]:
    """Legge i checkpoint sotto `output_dir` e ricava i campioni vocali.

    I checkpoint stanno dentro `output/<sessione>/`, non direttamente in
    `output/`: il glob li cerca li' dentro, perche' una sessione e' una
    cartella e il nome del checkpoint e' il suo.

    Solo `speaker_global_map`: un campione senza identita' globale non
    si puo' confrontare con nessuno, e finirebbe in una matrice con un
    buco che sembrerebbe una voce che non parla.
    """
    out: list[VoiceSample] = []
    for ck_file in sorted(output_dir.glob("*/*.checkpoint.json")):
        try:
            dati = json.loads(ck_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("%s non leggibile, saltato: %s", ck_file.name, exc)
            continue
        segmenti = dati.get("diarization_segments") or []
        emb = dati.get("speaker_embeddings") or {}
        global_map = dati.get("speaker_global_map") or {}
        if not segmenti or not emb or not global_map:
            continue
        out.extend(_samples_from_session(
            dati.get("stem") or ck_file.parent.name,
            segmenti, emb, global_map,
        ))
    return out


def build_matrix(
    campioni: Iterable[VoiceSample],
    soglia: float = 0.78,
    centroidi: dict[str, list[float]] | None = None,
) -> VoiceReport:
    """Confronta tutte le coppie che vengono da voci diverse.

    Le coppie della stessa voce globale non si confrontano: sono la
    stessa persona per costruzione, e il numero che ne uscirebbe
    misurerebbe quanto l' embedding di una persona cambia fra un giorno
    e l'altro — che e' una domanda interessante, ma non e' questa, e
    mischiarla qui nasconderebbe le coppie che contano.
    """
    campioni = [c for c in campioni if c.embedding]
    rep = VoiceReport(soglia=soglia, campioni=campioni,
                      centroidi=dict(centroidi or {}))
    for i, a in enumerate(campioni):
        for b in campioni[i + 1:]:
            if a.gid == b.gid:
                continue
            if a.chiave == b.chiave:
                continue
            rep.coppie.append(Pair(a, b, cosine(a.embedding, b.embedding)))
    return rep


def format_report(rep: VoiceReport, mostra_tutto: bool = False) -> str:
    """Il report in forma di testo, con la zona grigia in evidenza."""
    righe = [
        f"\nMatrice delle voci — soglia {rep.soglia:.2f}, "
        f"{len(rep.per_voce())} voci, {len(rep.campioni)} campioni, "
        f"{len(rep.coppie)} coppie\n",
    ]

    # Quale operazione e' questa, detto subito e non in fondo. La matrice
    # confronta campione con campione, mentre l'assegnazione delle voci
    # confronta l'embedding della sessione contro i centroidi memorizzati:
    # sono due numeri diversi per la stessa domanda, e possono dare
    # risposte diverse. Senza questa riga uno legge 0,848, conclude che il
    # sistema abbia sbagliato a tenere aperta una voce, e corregge a mano
    # un merge che era giusto — il caso reale di GLOBAL_028.
    righe.append(
        "Questi numeri confrontano un campione con l'altro, e i due "
        "campioni\nprovengono da sessioni diverse (le coppie della stessa "
        "sessione non si\ndecidono qui). L'assegnazione delle voci fa un "
        "confronto diverso:\nl'embedding della sessione contro i "
        "centroidi salvati. Non sono lo stesso numero.\n"
    )

    righe.append("Ogni voce, e dove l'hai sentita:")
    for gid, voci in rep.per_voce().items():
        dove = "  ".join(f"{c.sessione[11:16]}" for c in voci)
        totale = sum(c.secondi for c in voci)
        righe.append(f"  {gid}  {totale/60:6.1f} min  in {dove}")

    # Prima la decisione, per coppia di voci e con il numero del sistema;
    # poi, sotto, il dettaglio campione per campione da cui viene.
    da_decidere = rep.coppie_da_decidere()
    if da_decidere:
        righe.append(
            f"\nCoppie di VOCI da decidere ({len(da_decidere)}) — il numero "
            f"e' centroide contro centroide,\ncioe' lo stesso confronto che "
            f"fa il sistema quando assegna le voci:"
        )
        righe.append("  centroide  media  max   sopra/confronti  coppia")
        for s in da_decidere:
            media = f"{s.media:.3f}" if s.media is not None else "  -  "
            massimo = f"{s.massimo:.3f}" if s.massimo is not None else "  -  "
            nota = (f"   parlano insieme in {s.insieme} sessioni: "
                    "probabilmente due persone" if s.insieme else "")
            righe.append(
                f"  {s.centroide:.3f}     {media}  {massimo}  "
                f"{s.sopra:>3d}/{s.n:<3d}          {s.a} x {s.b}{nota}"
            )
        righe.append(
            "  Stessa persona: review_speakers.py merge <tenere> <unire>. "
            "Due persone: lascia cosi'."
        )
    else:
        righe.append("\nNessuna coppia di voci da decidere.")

    zona = rep.zona_grigia()
    if zona:
        righe.append(
            f"\nDettaglio: coppie di CAMPIONI entro 0,06 dalla soglia "
            f"({len(zona)}):"
        )
        for p in sorted(zona, key=lambda x: -x.simiglianza):
            righe.append(
                f"  {p.simiglianza:.3f}  {p.a.gid}[{p.a.sessione[11:16]}] "
                f"x {p.b.gid}[{p.b.sessione[11:16]}]"
            )
    else:
        righe.append("\nNessuna coppia nella zona grigia.")

    if mostra_tutto:
        righe.append("\nTutte le coppie, dalla piu' somigliante:")
        for p in sorted(rep.coppie, key=lambda x: -x.simiglianza):
            segno = "=" if p.simiglianza >= rep.soglia else " "
            righe.append(
                f"  {p.simiglianza:.3f}{segno} {p.a.gid}[{p.a.sessione[11:16]}] "
                f"x {p.b.gid}[{p.b.sessione[11:16]}]"
            )
    else:
        righe.append(
            "\nLe coppie estreme: "
            f"{len(rep.sicure_stessa_persona())} sopra soglia, "
            f"{len(rep.sicure_persone_diverse())} sotto. "
            "Usa --tutto per vederle tutte."
        )
    return "\n".join(righe)