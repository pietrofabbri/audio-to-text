"""
Protezione termica: quanto riposare, e come capire che il chip sta soffrendo.

Perché esiste questo file. Il cooldown finora era un numero fisso (90
secondi) scritto in `core/config.py`: uguale dopo un file da 20 secondi e
dopo un file da 15 minuti di lavoro pieno. È una regola che non guarda
niente, quindi non può essere giusta. Il vero problema non è «un file
dopo l'altro» ma «quattro ore di fila»: la temperatura non dipende da
quanto lavori in un momento, dipende da quanto tempo resti acceso.

Le due leve qui, in ordine di quanto rendono:

**1. Riposo proporzionale al lavoro.** Se un file ha occupato 15 minuti,
   il giusto non è 90 secondi di pausa ma qualche minuto: il rapporto
   giusto non è una costante, è una frazione del lavoro svolto. Con
   `work_ratio=0.25` si lavora tre quarti del tempo e si riposa un
   quarto, e la pausa cresce da sola con la durata della sessione.

**2. Rallentamento come sensore.** Non abbiamo un termometro: lo
   strumento di misura della potenza chiede i privilegi di amministratore
   e il contatore termico di sistema su Apple Silicon non restituisce
   nulla (verificato: «No thermal warning level has been recorded»).
   Ma non serve un termometro, perché il riscaldamento ha una
   conseguenza che si misura gratis: quando il chip è caldo la
   frequenza scende e **il lavoro ci mette di più**. Il tempo per secondo
   di audio è quindi un termometro indiretto, e l'unico che si può avere
   senza permessi. Se il tempo per unità di audio peggiora rispetto alle
   sessioni precedenti, il governor non si accontenta della pausa
   normale: la raddoppia, perché il segnale che ha letto è «sto già
   rallentando».

Cosa NON promette: di raffreddare la macchina. Il riposo serve a dare al
chip occasioni di scaricare il calore prima che il pacchetto termico
entri in gioco, e a tenere bassa la temperatura di picco, che è quella
che fa invecchiare e che si sente sotto le mani. L'energia totale resta
quella che è: il risparmio vero viene da altrove (4 thread invece di 8,
e il VAD che scarta il 26% di silenzio prima di trascriverlo).

Uso tipico fra due file:

    gov = ThermalGovernor(ThermalPolicy())
    t0 = time.monotonic()
    process_file(...)
    gov.note_work(elapsed=time.monotonic() - t0, audio_sec=3600)
    gov.rest(should_stop=lambda: shutdown_requested)
"""

from __future__ import annotations

import logging
import statistics
import time
from dataclasses import dataclass, field

logger = logging.getLogger("audio-to-text.thermal")


@dataclass
class ThermalPolicy:
    # Pausa minima fra un file e il successivo, in secondi. Sotto questo
    # valore il chip non ha tempo di smaltire nulla.
    min_sec: float = 90.0

    # Pausa massima: oltre, il riposo costa più di quanto protegga.
    max_sec: float = 600.0

    # Frazione del tempo lavorato da usare come pausa. 0.25 = si lavora
    # tre quarti del tempo. Sale se la macchina è calda o sta in una
    # stanza calda, scende se è ben ventilata.
    work_ratio: float = 0.25

    # Frazione di rallentamento oltre la quale si sospetta il throttling:
    # se un secondo di audio costa il 25% in più che prima, il chip non
    # sta più al suo passo.
    slowdown_threshold: float = 1.25

    # Quando si sospetta il throttling, la pausa viene moltiplicata per
    # questo fattore. Serve a rientrare nel pacchetto termico, non a
    # fingere di misurarlo.
    slowdown_multiplier: float = 2.0

    # Sotto questo valore la sessione è troppo corta perché la regola
    # proporzionale abbia senso: si usa solo min_sec.
    short_job_sec: float = 120.0

    def cooldown_for(self, worked_sec: float) -> float:
        if worked_sec <= self.short_job_sec:
            return self.min_sec
        return min(
            self.max_sec, max(self.min_sec, worked_sec * self.work_ratio)
        )


@dataclass
class ThermalGovernor:
    """Accumula il lavoro svolto e decide quanto riposare."""

    policy: ThermalPolicy = field(default_factory=ThermalPolicy)

    # Secondi di elaborazione per secondo di audio, una voce per sessione
    # finita. È il campione con cui confrontare la sessione successiva.
    history: list[float] = field(default_factory=list)

    # Pausa decisa dopo l'ultimo lavoro registrato.
    last_cooldown_sec: float = 0.0
    throttled: bool = False

    def note_work(self, elapsed_sec: float, audio_sec: float = 0.0) -> None:
        """Registra una sessione finita e calcola la pausa dovuta."""
        cost = (elapsed_sec / audio_sec) if audio_sec > 0 else 0.0

        self.throttled = False
        if cost > 0 and self.history:
            # Mediana e non media: un file con un paio di chunk difficili
            # non deve far sembrare che la macchina stia rallentando.
            baseline = statistics.median(self.history)
            if baseline > 0 and cost > baseline * self.policy.slowdown_threshold:
                self.throttled = True
                logger.warning(
                    "Rallentamento: %.1fs per secondo di audio contro una "
                    "mediana di %.1fs (+%.0f%%) — il chip sta probabilmente "
                    "abbassando la frequenza, si allunga la pausa.",
                    cost, baseline, (cost / baseline - 1) * 100,
                )

        if cost > 0:
            self.history.append(cost)

        pause = self.policy.cooldown_for(elapsed_sec)
        if self.throttled:
            pause = min(
                self.policy.max_sec * 2, pause * self.policy.slowdown_multiplier
            )

        self.last_cooldown_sec = pause

    def rest(
        self, *, should_stop=lambda: False, sleeper=time.sleep
    ) -> float:
        """Dorme per la pausa decisa. Ritorna i secondi effettivamente dormiti.

        Dormire a pezzi invece che tutto d'un colpo serve a una cosa
        sola, ma serve: se arriva un segnale di chiusura durante la
        pausa, non si deve aspettare minuti prima di accorgersene. Si
        guarda ogni secondo, e si esce subito.
        """
        remaining = self.last_cooldown_sec
        if remaining <= 0:
            return 0.0

        logger.info(
            "Pausa di raffreddamento: %.0fs%s",
            remaining,
            " (allungata: rallentamento rilevato)" if self.throttled else "",
        )
        slept = 0.0
        while remaining > 0 and not should_stop():
            step = min(1.0, remaining)
            sleeper(step)
            slept += step
            remaining -= step

        if should_stop():
            logger.info("Pausa interrotta dopo %.0fs: chiusura richiesta.", slept)
        else:
            logger.info("Pausa finita dopo %.0fs.", slept)
        return slept


def format_cooldown(sec: float) -> str:
    """'4m30s' — per i log, che si leggono a occhio."""
    m, s = divmod(int(round(sec)), 60)
    return f"{m}m{s:02d}s" if m else f"{s}s"
