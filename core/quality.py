"""
Qualità della trascrizione, segmento per segmento.

Il problema che indirizza. In un estratto reale i primi 40 secondi erano
audio non intellegibile, e il modello ci ha messo dentro testo
plausibile e sbagliato — non parole a caso, ma frasi che si possono
leggere e che somigliano a italiano. Niente nel corpus diceva "qui non
c'era parlato": una frase inventata è indistinguibile da una frase vera
una volta scritta, e un'analisi costruita sopra non lo sa.

Il flag non è un certificato di verità. È un segnale che dice "guarda
qui prima di fidarti", e serve soprattutto in due casi:

  - quando guardi i risultati di notte e vuoi sapere dove guardare;
  - quando costruisci un'analisi e vuoi poter escludere un insieme noto
    di segmenti sospetti, invece di scoprire fra sei mesi che metà del
    corpus era illeggibile.

Il rischio di questi flag è noto e dichiarato: se scattano troppo spesso
diventano rumore e vengono ignorati. Per questo le regole sono strette e
poche, e ognuna guarda un modo diverso in cui Whisper sbaglia:

| Segnale | Cosa cattura |
|---|---|
| `no_speech_prob` alto | il modello stesso dice "qui non parlavi" |
| parole al secondo implausibili | allineamento rotto, dettatura assurda |
| loop di n-gramma | il caso tipico: "va va va va" per 22 volte |
| testo degenere | segmenti troppo brevi o troppo vuoti per essere utili |
| probabilità media delle parole bassa | il modello indovina, e lo sa |

Le soglie sono in `QualityThresholds` e sono **segnali deboli**: non
scelgono niente da soli. Un solo segnale che scatta dà `low`, due o più
danno `unreliable`. La differenza è deliberata: un segmento con una
parola a probabilità 0,4 è ancora informazione; un segmento con loop e
probabilità bassa è spazzatura, e va saputo.

Perché qui e non dentro `assembler.py`. L'assembler unisce pezzi e non
sa niente di soglie; e una funzione che decide se un testo è rumore
merita un modulo proprio, con i suoi test, perché è la funzione che
protegge tutte le altre.
"""

from __future__ import annotations

import logging
import re
from collections import Counter
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

# Sotto questa soglia un segmento non dice niente: o è vuoto, o è un
#pezzo troppo corto perché l'analisi linguistica ci trovi qualcosa.
MIN_WORDS_USEFUL = 3

# Sotto questa soglia la punteggiatura è rumore, non informazione: non si
# misura la qualità di una frase senza segni di frase.
MIN_PUNCT_CHARS = 40

# Soglia sotto cui il modello sta tirando a indovinare. Su Whisper
# large-v3-turbo la massa dei token sta sopra 0,6; sotto 0,35 il testo
# è tipicamente fratture, sillabe mozzate o audio non parlato.
WORD_PROB_LOW = 0.35

# Quota di parole sotto quella soglia che fa scattare il flag. Un
# quarto è il punto in cui una frase smette di essere una frase con
# qualche passaggio incerto e diventa una sequenza di pezzi che il
# modello ha costruito per riempire il silenzio.
LOW_WORD_SHARE = 0.25

# Soglia di no-speech che il transcriber già calcola per chunk. Sopra
# questa il modello afferma che nel chunk non c'era parlato.
NO_SPEECH_HIGH = 0.60

# Ripetizioni: una parola o un gruppo di parole che si ripete identico
# più di N volte di fila è il segnale del loop di faster-whisper.
REPEAT_NGRAM = 3
REPEAT_TIMES = 4

# Ritmo plausibile per il parlato. Fuori da questa fascia il chunk non
# è parlato: o non è niente, o è un difetto di allineamento.
WORDS_PER_SEC_MIN = 0.3
WORDS_PER_SEC_MAX = 6.0


@dataclass(frozen=True)
class QualityThresholds:
    """Soglie dei flag. Raggruppate perché sono una scelta, non un dettaglio:
    cambiarle tutte insieme è un'altra politica sulla qualità."""
    min_words: int = MIN_WORDS_USEFUL
    word_prob_low: float = WORD_PROB_LOW
    low_word_share: float = LOW_WORD_SHARE
    no_speech_high: float = NO_SPEECH_HIGH
    repeat_ngram: int = REPEAT_NGRAM
    repeat_times: int = REPEAT_TIMES
    wps_min: float = WORDS_PER_SEC_MIN
    wps_max: float = WORDS_PER_SEC_MAX
    min_punct_chars: int = MIN_PUNCT_CHARS


DEFAULT = QualityThresholds()

# I tre valori, in ordine di peggioramento.
OK, LOW, UNRELIABLE = "ok", "low", "unreliable"

_RANK = {OK: 0, LOW: 1, UNRELIABLE: 2}


def worst(*levels: str) -> str:
    return max(levels, key=lambda l: _RANK.get(l, 0))


def _words(text: str) -> list[str]:
    return re.findall(r"[^\W\d_]+", (text or "").lower(), flags=re.UNICODE)


def has_repetition(text: str, n: int = REPEAT_NGRAM,
                   times: int = REPEAT_TIMES) -> bool:
    """True se una sequenza di `n` parole consecutive si ripete identica
    `times` volte.

    Non è "una parola ripetuta": "no no no" è italiano. È la stessa
    sequenza di più parole che si ripete, che è la firma del loop.

    Le occorrenze sono sovrapposte — in "va va va va va" il trigramma
    "va va va" compare tre volte, non una — quindi il numero minimo di
    parole è `n + times - 1` e non `n * times`. Con la guardia sbagliata
    il caso più tipico in assoluto, il loop di una sola parola, non
    veniva mai intercettato: sotto, il test che lo verifica è
    `test_loop_of_one_word_is_caught`.
    """
    w = _words(text)
    if len(w) < n + times - 1:
        return False
    grams = Counter(tuple(w[i:i + n]) for i in range(len(w) - n + 1))
    return any(count >= times for count in grams.values())


def mean_word_prob(segment: dict[str, Any]) -> float | None:
    """Probabilità media delle parole, se ci sono i timestamp.

    Il dato c'è solo se la pipeline è andata con `word_timestamps` (che
    di proposito ricade indietro quando il modello va in crisi su un
    chunk: meglio un chunk senza timestamp che un chunk perso).
    """
    probs = _word_probs(segment)
    if not probs:
        return None
    return sum(probs) / len(probs)


def low_word_share(segment: dict[str, Any], below: float = WORD_PROB_LOW) -> float | None:
    """Quota di parole che il modello ha indovinato.

    Serve insieme alla media, non al posto suo. Con quattro parole a
    0,9 e una a 0,2 la media è 0,74 e sembra ottima: il modello ha
    indovinato una parola su cinque e la media lo copre. La quota no.
    """
    probs = _word_probs(segment)
    if not probs:
        return None
    return sum(1 for p in probs if p < below) / len(probs)


def _word_probs(segment: dict[str, Any]) -> list[float]:
    words = segment.get("words") or []
    return [float(w["prob"]) for w in words
            if isinstance(w.get("prob"), (int, float))]


def score_segment(
    segment: dict[str, Any],
    thresholds: QualityThresholds = DEFAULT,
) -> dict[str, Any]:
    """Valuta un segmento e restituisce i numeri e i motivi.

    Il dizionario restituito finisce nel corpus: `level` è il verdetto,
    `reasons` è perché, e i numeri restano così che una soglia cambiata
    si possa rivalutare senza rileggere l'audio.
    """
    text = segment.get("text") or ""
    words = _words(text)
    n_words = len(words)
    dur = float(segment.get("duration_sec") or 0.0)
    if dur <= 0 and segment.get("start") is not None and segment.get("end") is not None:
        dur = float(segment["end"]) - float(segment["start"])

    reasons: list[str] = []
    level = OK

    # 1. Vuoto o troppo corto per dire qualcosa.
    if n_words == 0:
        reasons.append("testo_vuoto")
        level = worst(level, UNRELIABLE)
    elif n_words < thresholds.min_words:
        reasons.append(f"pochi_parole:{n_words}")
        level = worst(level, LOW)

    # 2. Il modello stesso dice che non c'era parlato.
    nsp = segment.get("no_speech_prob")
    if isinstance(nsp, (int, float)) and nsp >= thresholds.no_speech_high:
        reasons.append(f"no_speech:{nsp:.2f}")
        level = worst(level, UNRELIABLE)

    # 3. Loop. Il caso più comune e il più rumoroso se non intercettato.
    if has_repetition(text, thresholds.repeat_ngram, thresholds.repeat_times):
        reasons.append(f"ripetizione_n{thresholds.repeat_ngram}")
        level = worst(level, UNRELIABLE)

    # 4. Ritmo implausibile. Solo se il segmento ha una durata: senza
    #    durata il rapporto è inventato e il flag non significherebbe niente.
    wps = (n_words / dur) if dur > 0 and n_words else None
    if wps is not None and not (thresholds.wps_min <= wps <= thresholds.wps_max):
        reasons.append(f"ritmo_fuori_scala:{wps:.1f}wps")
        level = worst(level, UNRELIABLE)

    # 5. Il modello indovina, e lo sa. Due letture, perché una sola
    #    non basta: la media copre una parola indovinata fra quattro,
    #    la quota copre il caso opposto.
    mp = mean_word_prob(segment)
    if mp is not None and mp < thresholds.word_prob_low:
        reasons.append(f"prob_parole_bassa:{mp:.2f}")
        level = worst(level, LOW)

    lws = low_word_share(segment, thresholds.word_prob_low)
    if lws is not None and lws >= thresholds.low_word_share:
        reasons.append(f"troppe_parole_insicure:{lws:.0%}")
        level = worst(level, LOW)

    return {
        "level": level,
        "reasons": reasons,
        "n_words": n_words,
        "words_per_sec": round(wps, 2) if wps is not None else None,
        "mean_word_prob": round(mp, 3) if mp is not None else None,
        "low_word_share": round(lws, 3) if lws is not None else None,
        "no_speech_prob": nsp,
        "loop": any(r.startswith("ripetizione_n") for r in reasons),
    }


def score_segments(
    segments: list[dict[str, Any]],
    thresholds: QualityThresholds = DEFAULT,
) -> list[dict[str, Any]]:
    return [score_segment(s, thresholds) for s in segments]


def summarize(
    segments: list[dict[str, Any]],
    thresholds: QualityThresholds = DEFAULT,
) -> dict[str, Any]:
    """Riassunto di una sessione: quanto materiale è poco affidabile.

    Accetta i **segmenti**, non i risultati di `score_segment`: è la
    forma che l'assembler ha in mano e non ha bisogno di tenere da parte
    un secondo elenco parallelo. Se il segmento ha già il verdetto
    (perché è stato ricalcolato o rietichettato) lo usa; altrimenti lo
    calcola adesso, così la funzione è giusta anche chiamata da sola.

    La domanda che questo numero risponde è "posso analizzare questa
    sessione?", non "quanti segmenti hanno un flag". Sono numeri
    diversi, e il secondo è quello che non dice niente.
    """
    n = len(segments)
    if not n:
        return {"segments": 0, "ok": 0, "low": 0, "unreliable": 0,
                "unreliable_share": 0.0, "low_or_worse_share": 0.0}

    quals: list[dict[str, Any]] = []
    for seg in segments:
        # Un segmento già giudicato viene accettato per quello che
        # dice: ricalcolarlo qui ignorerebbe un giudizio cambiato a
        # mano, che è una cosa che capita quando si rivede a mano un
        # segmento sospetto.
        if "level" in seg and "n_words" in seg:
            quals.append(seg)
        else:
            quals.append(score_segment(seg, thresholds))

    counts = Counter(q["level"] for q in quals)
    ok = counts.get(OK, 0)
    low = counts.get(LOW, 0)
    unrel = counts.get(UNRELIABLE, 0)
    # La quota di segmenti sospetti è pesata sulle parole, non sui
    # segmenti: venti segmenti da una parola e un segmento di trenta
    # secondi contano per la stessa durata, e contare i segmenti farebbe
    # sembrare una sessione peggiore (o migliore) di quello che è.
    w_ok = sum(q.get("n_words", 0) for q in quals if q["level"] == OK)
    w_bad = sum(q.get("n_words", 0) for q in quals if q["level"] != OK)
    tot_w = w_ok + w_bad
    return {
        "segments": n,
        "ok": ok,
        "low": low,
        "unreliable": unrel,
        "unreliable_share": round(unrel / n, 3),
        "low_or_worse_share": round(w_bad / tot_w, 3) if tot_w else 0.0,
    }