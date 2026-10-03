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
class VoiceReport:
    """Tutto quello che la matrice dice, in forma leggibile."""

    soglia: float
    campioni: list[VoiceSample] = field(default_factory=list)
    coppie: list[Pair] = field(default_factory=list)

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
) -> VoiceReport:
    """Confronta tutte le coppie che vengono da voci diverse.

    Le coppie della stessa voce globale non si confrontano: sono la
    stessa persona per costruzione, e il numero che ne uscirebbe
    misurerebbe quanto l' embedding di una persona cambia fra un giorno
    e l'altro — che e' una domanda interessante, ma non e' questa, e
    mischiarla qui nasconderebbe le coppie che contano.
    """
    campioni = [c for c in campioni if c.embedding]
    rep = VoiceReport(soglia=soglia, campioni=campioni)
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

    righe.append("Ogni voce, e dove l'hai sentita:")
    for gid, voci in rep.per_voce().items():
        dove = "  ".join(f"{c.sessione[11:16]}" for c in voci)
        totale = sum(c.secondi for c in voci)
        righe.append(f"  {gid}  {totale/60:6.1f} min  in {dove}")

    zona = rep.zona_grigia()
    if zona:
        righe.append(
            f"\nCoppie entro 0,06 dalla soglia — quelle che la macchina "
            f"non puo' decidere ({len(zona)}):"
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
            "\nLe coppi estreme: "
            f"{len(rep.sicure_stessa_persona())} sopra soglia, "
            f"{len(rep.sicure_persone_diverse())} sotto. "
            "Usa --tutto per vederle tutte."
        )
    return "\n".join(righe)