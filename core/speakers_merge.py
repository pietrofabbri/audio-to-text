"""
Fusione dei cluster di voce troppo deboli per essere una persona.

Perche' questo file esiste. La diarizzazione restituisce cluster, e il
loro numero non e' il numero delle persone: quattro ore di
registrazione di una stessa conversazione hanno prodotto 28 cluster
locali per 16 voci reali, e undici di quei cluster parlavano meno di
novanta secondi in due ore e un quarto di parlato. Un essere umano che
entra, dice due frasi e esiste per tredici secondi non e' un
interlocutore: e' un frammento di qualcuno che il modello ha tagliato
male.

La regola e' volutamente stretta, e la strettezza e' il punto:

1. **Non si fondono mai due voci grandi.** Due persone vere che si
   somigliano restano due persone. Se la fusione sbaglia, sbaglia solo
   sui frammenti, che sono rumore comunque.
2. **Un cluster debole si scioglie solo nel piu' simile**, e solo se la
   somiglianza supera la soglia. Non nel secondo piu' simile, non nel
   piu' vicino nel tempo: nel piu' simile, che e' l'unica misura che
   riguarda la voce e non l'occasione.
3. **Si itera.** Due frammenti si fondono fra loro e il risultato, se
   ancora debole, cerca un altro partner. Senza l'iterazione restano
   gruppetti di due-tre frammenti che nessuno ha fuso.

La soglia (0.45) e' volutamente piu' bassa della soglia con cui si
uniscono le voci fra file diversi (0.78). Non e' un errore: qui la
decisione e' «questa voce e' troppo piccola per essere qualcuno», e un
frammento anche medio-simile a qualcuno e' quasi sempre di qualcuno.
L'altra soglia decide «sono la stessa persona attraverso sessioni
diverse», che e' una domanda molto piu' difficile e che lascia stare.

Dove va chiamata, e perche' l'ordine conta piu' della soglia. La fusione
deve avvenire **prima** che le voci locali vengano risolte in identita'
globali. Sulla stessa conversazione, fusesi prima i frammenti, le voci
globali passano da 21 a 9 e tutte e 9 parlano almeno 160 secondi; fusesi
dopo, ne restano 21 di cui dodici sotto i novanta secondi. Il motivo e'
che il DB delle voci registra ogni cluster come una persona a se: se il
frammento arriva al DB, il DB gli assegna un'identita' e quella
identita' resta per sempre, perche' il DB non torna mai indietro a
riconoscere che aveva contato due volte la stessa voce.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Iterable

logger = logging.getLogger("audio-to-text.merge")


@dataclass
class MergePolicy:
    # Sotto quanti secondi parlati un cluster non e' considerato una
    # persona. Novanta secondi sono un metroquinto d'ora: chi parla di
    # meno, in una conversazione che dura, e' un frammento.
    min_seconds: float = 90.0

    # Somiglianza minima fra centroide del frammento e centroide di chi
    # lo assorbe. Verificata sui dati veri: i frammenti hanno similarita'
    # 0.49-0.70 verso la voce che li spiega, e sotto 0.45 verso le altre.
    # La scelta di 0.45 e' verificata contro i dati e non per taste:
    # ogni soglia fra 0.30 e 0.45 dà lo stesso risultato sulle quattro
    # sessioni reali, e 0.45 e' la piu' alta del pianoro, cioe' la piu'
    # conservatrice che ancora cattura tutta la fusione utile. Sale a
    # 0.50 e comincia a lasciare fuori un frammento.
    threshold: float = 0.45

    # Tetto sui cicli di fusione: una rete di assorbimenti non deve
    # poter girare all'infinito.
    max_rounds: int = 20


@dataclass
class MergeReport:
    """Cosa ha fatto la fusione, in parole che si possono leggere."""

    clusters_before: int = 0
    clusters_after: int = 0
    merged: dict[str, str] = field(default_factory=dict)
    seconds_before: dict[str, float] = field(default_factory=dict)
    orphans: list[str] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return bool(self.merged)

    def to_dict(self) -> dict[str, Any]:
        return {
            "clusters_before": self.clusters_before,
            "clusters_after": self.clusters_after,
            "merged": dict(self.merged),
            "seconds_before": {k: round(v, 1) for k, v in
                               sorted(self.seconds_before.items())},
            "orphans": sorted(self.orphans),
        }


def speaking_seconds(segments: Iterable[dict[str, Any]]) -> dict[str, float]:
    """Secondi parlati per etichetta di voce."""
    out: dict[str, float] = {}
    for seg in segments:
        sp = seg.get("speaker")
        if sp is None:
            continue
        out[sp] = out.get(sp, 0.0) + max(
            0.0, float(seg.get("end", 0.0)) - float(seg.get("start", 0.0))
        )
    return out


def cosine(a: Any, b: Any) -> float:
    """Similarita' del coseno fra due vettori.

    Nessuna dipendenza da numpy: qui i vettori sono 256 numeri e si
    confrontano a decine per volta, e il costo di importare numpy per
    questo e' piu' alto del conto. Ritorna 0.0 se un vettore e' nullo,
    perche' un embedding assente non dice "nessuna somiglianza", dice
    "non lo so", e in questo file non si tratta di non sapere.
    """
    if not a or not b:
        return 0.0
    # Vettori di lunghezza diversa non sono confrontabili: senza questo
    # controllo `zip` li tronca in silenzio e produce un numero che
    # sembra una somiglianza ed e' un confronto fra due pezzi diversi.
    if len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


def merge_weak_clusters(
    segments: list[dict[str, Any]],
    embeddings: dict[str, list[float]],
    policy: MergePolicy | None = None,
) -> tuple[list[dict[str, Any]], dict[str, list[float]], MergeReport]:
    """Restituisce (segmenti, embedding, relazione di fusione).

    I segmenti tornano con l'etichetta del cluster che li assorbe; la
    relazione dice da quale a quale, cosi' chi guarda sa che cosa e'
    successo e non vede solo un numero cambiato.

    Gli embedding dei cluster fusi diventano la media pesata per i
    secondi parlati: il frammento che si aggiunge sposta poco il
    centroide, il grosso sposta molto, che e' il comportamento giusto
    per una media pesata per quantita' di voce.
    """
    policy = policy or MergePolicy()

    segmenti = [dict(s) for s in segments]
    emb = {k: list(v) for k, v in (embeddings or {}).items()}

    sec = speaking_seconds(segmenti)
    report = MergeReport(
        clusters_before=len(sec),
        clusters_after=len(sec),
        seconds_before=dict(sec),
    )

    # chi e' finito dentro chi: da -> verso
    verso: dict[str, str] = {}

    def _risolvi(label: str) -> str:
        """Segue la catena fino alla voce che non e' stata assorbita."""
        while label in verso:
            label = verso[label]
        return label

    for _ in range(policy.max_rounds):
        # Secondi aggregati per gruppo: due frammenti uniti contano
        # insieme, altrimenti il ciclo non finisce.
        aggregati: dict[str, float] = {}
        for sp, s in sec.items():
            aggregati[_risolvi(sp)] = aggregati.get(_risolvi(sp), 0.0) + s

        # Solo i gruppi ancora piccoli si muovono. Un gruppo che ha
        # raccolto 200 secondi non e' piu' un frammento, e trattarlo
        # come tale e' il modo per fondere due persone vere.
        deboli = [k for k in aggregati if aggregati[k] < policy.min_seconds]
        if not deboli:
            break

        fatti = 0
        for debole in sorted(deboli, key=lambda k: aggregati[k]):
            if debole not in aggregati:
                continue
            # Il partner migliore fra tutti gli altri gruppi. I gruppi
            # grandi restano candidati, ma non si fondono fra loro:
            # sotto, la regola lo impedisce a coppia.
            candidati = []
            for altro, sec_altro in aggregati.items():
                if altro == debole:
                    continue
                if (aggregati[debole] >= policy.min_seconds
                        and sec_altro >= policy.min_seconds):
                    continue
                s = cosine(emb.get(debole), emb.get(altro))
                if s >= policy.threshold:
                    candidati.append((s, sec_altro, altro))
            if not candidati:
                report.orphans.append(debole)
                continue

            # A parita' di somiglianza vince il gruppo che parla di
            # piu': e' il destinatario piu' probabile, e l'etichetta
            # che resta e' quella di chi si sentiva di piu'.
            candidati.sort(key=lambda x: (-x[0], -x[1]))
            sim, sec_altro, scelto = candidati[0]

            # Il centroide del gruppo piu' pesante vince: si unisce il
            # piccolo al grande, non il contrario, cosi' l'etichetta
            # continua a identificare la voce che c'era prima.
            if aggregati[debole] > sec_altro:
                aggregati[debole], aggregati[scelto] = (
                    aggregati[scelto], aggregati[debole],
                )
                emb[debole], emb[scelto] = emb.get(scelto), emb.get(debole)
                debole, scelto = scelto, debole

            verso[debole] = scelto
            aggregati.pop(debole, None)
            aggregati[scelto] = aggregati.get(scelto, 0.0) + sec.get(debole, 0.0)
            report.merged[debole] = scelto
            fatti += 1

        if not fatti:
            break

    if not verso:
        return segmenti, emb, report

    # Applica la relazione ai segmenti e ricostruisce gli embedding.
    nuovi_segmenti = []
    for seg in segmenti:
        nuovo = dict(seg)
        sp = nuovo.get("speaker")
        if sp in verso:
            nuovo["speaker"] = _risolvi(sp)
        nuovi_segmenti.append(nuovo)

    nuovi_emb: dict[str, list[float]] = {}
    pesi: dict[str, float] = {}
    for sp, v in emb.items():
        if v is None:
            continue
        destinazione = _risolvi(sp)
        peso = sec.get(sp, 0.0) + 1e-9      # il peso non puo' essere zero
        acc = nuovi_emb.setdefault(destinazione, [0.0] * len(v))
        for i, x in enumerate(v):
            acc[i] += x * peso
        pesi[destinazione] = pesi.get(destinazione, 0.0) + peso
    nuovi_emb = {k: [x / pesi[k] for x in v] for k, v in nuovi_emb.items()}

    finali = speaking_seconds(nuovi_segmenti)
    report.clusters_after = len(finali)
    report.orphans = sorted(set(report.orphans) - set(verso))

    logger.info(
        "Voci: %d -> %d (%d fusioni%s)",
        report.clusters_before, report.clusters_after, len(report.merged),
        f", {len(report.orphans)} senza partner" if report.orphans else "",
    )
    for da, a in sorted(report.merged.items()):
        logger.info(
            "  %s (%.0fs) fuso in %s (%.0fs)",
            da, report.seconds_before.get(da, 0.0), a,
            report.seconds_before.get(a, 0.0),
        )

    return nuovi_segmenti, nuovi_emb, report