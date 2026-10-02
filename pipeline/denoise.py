"""
Denoise — pulizia del fruscio e scelta automatica della variante migliore.

Due problemi distinti, spesso confusi:

1. PRODUZIONE della variante ripulita. Si usa `afftdn` di ffmpeg: è già
   nella build di sistema (verificato: ffmpeg 7 con afftdn, anlmdn,
   arnndn), quindi nessuna dipendenza nuova e nessun costo di modello.
   L'originale non viene mai sovrascritto: la variante ripulita è un
   file separato, e i due convivono finché non si è deciso.

2. SCELTA di quale usare per la trascrizione. Qui sta il punto, e non
   si può risolvere ascoltando: nessuno è disponibile per farlo di notte.

   Non esiste un ground truth (non c'è un testo di riferimento da
   confrontare, quindi niente WER). Si usano quindi proxy, e la cosa
   importante è che siano *misurabili* e che la scelta sia
   *conservativa*:

   - confidenza ASR media (logprob): l'ASR è più sicuro quando
     l'audio è pulito. Un guasto tipico del denoise eccessivo è
     l'ASR che inventa parole con bassa confidenza.
   - rapporto di parlato VAD: se il denoise mangia le parole deboli
     (consonanti, finali) il VAD vede meno parlato. Cioè: troppo poco
     parlato è un sintomo, non un successo.
   - plausibilità del ritmo di parola: parole/secondo fuori da un
     intervallo umano indica allineamento rotto o segmenti corrotti.
   - segmenti degeneri: chunk vuoti o composti dalla stessa parola
     ripetuta sono il modo tipico in cui l'ASR "si arrende" su audio
     degradato.

   Regola: si cambia variante solo se il guadagno supera una soglia di
   margine. In caso di parità si tiene l'originale, perché è il
   minormale e non richiede giustificazioni. Ogni decisione viene
   scritta con i numeri che l'hanno prodotta, così è verificabile e
   riproducibile — e si può tuningare dopo aver visto i dati reali.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Produzione della variante ripulita
# ---------------------------------------------------------------------------

def denoise_afftdn(
    src: Path,
    dst: Path,
    nr: int = 12,
    nf: int = -25,
    sample_rate: int = 16000,
) -> Path:
    """
    Produce una copia ripulita con afftdn (FFT denoise).

    afftdn è scelto su arnndn perché non serve un file di pesi da
    scaricare e gestire: con una registrazione vocale ravvicinata il
    guadagno è marginale rispetto al costo operativo di un modello
    neuronale. Se i frusci fossero cattivi (ventola, fruscio severo)
    il passo successivo è DeepFilterNet, non un modello dentro ffmpeg.

    Args:
        nr: riduzione del rumore in dB (0-97). 12 è aggressivo ma
            sicuro sulla voce; 6-8 se la voce risulta ovattata.
        nf: soglia di soppressione in dB. -25 tiene il rumore di fondo
            senza cancellare le consonanti.
    """
    dst.parent.mkdir(parents=True, exist_ok=True)
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("ffmpeg non trovato nel PATH")

    cmd = [
        ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
        "-i", str(src),
        "-af", f"afftdn=nr={nr}:nf={nf}",
        "-ar", str(sample_rate), "-ac", "1",
        str(dst),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
    if proc.returncode != 0 or not dst.exists() or dst.stat().st_size == 0:
        raise RuntimeError(
            f"afftdn fallito (exit {proc.returncode}): {proc.stderr.strip()[:400]}"
        )
    return dst


# ---------------------------------------------------------------------------
# Valutazione di una variante
# ---------------------------------------------------------------------------

@dataclass
class QualityScore:
    """
    Punteggio di una variante audio, su segnali osservabili a posteriori.

    Tutti i campi sono derivati dall'elaborazione, non da giudizi: il
    punto è che la decisione sia riproducibile e ispezionabile.
    """
    name: str
    # Confidenza ASR media sui token (probabilità 0-1 da faster-whisper,
    # via la chiave "prob" del Transcriber). Più alta = l'ASR è più
    # sicuro di quello che sente.
    asr_confidence: float = 0.0
    # Frazione di audio classificata come parlato dal VAD.
    speech_ratio: float = 0.0
    # Parole per secondo di parlato effettivo.
    words_per_sec: float = 0.0
    # Segmenti con testo degenere (vuoto o ciclo di parole ripetute).
    degenerate_segments: int = 0
    # Segmenti totali della variante (serve per il rapporto sotto)
    segments_count: int = 0
    # Quota di segmenti degeneri. Un singolo segmento strano è normale;
    # una maggioranza di segmenti ripetuti è un guasto. Oltre questa
    # soglia la variante viene considerata rotta: prima si flaggava
    # anche un solo segmento, e chi parla per davvero ripetendo ("parlare,
    # dire, fare") si trovava condannato da un linguaggio legittimo.
    degenerate_ratio: float = 0.0
    # Token totali trascritti.
    token_count: int = 0
    # Frammenti di testo: valori altissimi segnalano allineamento rotto.
    repetition_ratio: float = 0.0

    # True se le statistiche VAD erano disponibili per questa variante.
    # Senza questo, uno speech_ratio mancante vale 0.0 e fa passare
    # l'audio integro per "quasi nessun parlato": la variante sana
    # verrebbe scartata perché non si sa, non perché sia rotta.
    has_vad_stats: bool = False

    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


# Quota di segmenti ripetuti oltre la quale una variante è considerata
# rotta. Va qui fuori dal dataclass: dentro, insieme ai campi, non è un
# campo ma un'istruzione che spezza la classe.
DEGENERATE_RATIO_LIMIT = 0.3


def score_variant(
    name: str,
    asr_chunks: list[dict[str, Any]],
    vad_stats: dict[str, Any],
) -> QualityScore:
    """
    Calcola il punteggio di una variante a partire dai suoi risultati.

    asr_chunks: chunk ASR come li produce la pipeline, con "words"
        (word-level) e/o "text".
    vad_stats: statistiche VAD (speech_duration_sec, total_duration_sec).
    """
    s = QualityScore(name=name)

    # --- confidenza ASR -------------------------------------------------
    # Il Transcriber salva la chiave "prob" (probabilità 0-1 di
    # faster-whisper). Sono previsti anche i formati in log-probabilità
    # perché cambiano fra versioni: senza questa distinzione, ognuna
    # delle due chiavi letta con l'altra formula restituisce numeri
    # plausibili ma sbagliati — il caso peggiore, perché un punteggio
    # falso non si vede.
    confs: list[float] = []
    for c in asr_chunks:
        for w in c.get("words") or []:
            if w.get("prob") is not None:
                confs.append(float(w["prob"]))
            elif w.get("probability") is not None:
                confs.append(float(w["probability"]))
            elif w.get("avg_logprob") is not None or w.get("logprob") is not None:
                import math
                lp = w.get("avg_logprob")
                lp = float(lp if lp is not None else w["logprob"])
                confs.append(math.exp(max(-1.0, min(0.0, lp))))
    s.asr_confidence = sum(confs) / len(confs) if confs else 0.0

    # --- rapporto di parlato --------------------------------------------
    speech = float(vad_stats.get("speech_duration_sec", 0.0) or 0.0)
    total = float(vad_stats.get("total_duration_sec", 0.0) or 0.0)
    s.speech_ratio = (speech / total) if total > 0 else 0.0
    s.has_vad_stats = total > 0

    # --- ritmo di parola -------------------------------------------------
    words = [
        w.get("word", "").strip()
        for c in asr_chunks for w in (c.get("words") or [])
        if w.get("word", "").strip()
    ]
    s.token_count = len(words)
    s.words_per_sec = (len(words) / speech) if speech > 0 else 0.0

    # --- segmenti degeneri e ripetizioni --------------------------------
    s.segments_count = len(asr_chunks)
    for c in asr_chunks:
        text = (c.get("text") or "").strip()
        if not text:
            s.degenerate_segments += 1
            continue
        toks = text.lower().split()
        # Poche parole distinte su un segmento lungo è il modo tipico in
        # cui l'ASR "si arrende" su audio degradato: ripete un nucleo
        # invece di trascrivere. La soglia è sul rapporto, non sul numero
        # assoluto di parole distinte, altrimenti un ciclo di due soli
        # ("dire fare" ripetuto) passerebbe spesso.
        if len(toks) >= 8 and (len(set(toks)) / len(toks)) <= 0.4:
            s.degenerate_segments += 1
    if words:
        s.repetition_ratio = 1.0 - (len(set(words)) / len(words))

    if s.segments_count:
        s.degenerate_ratio = s.degenerate_segments / s.segments_count

    return s


# ---------------------------------------------------------------------------
# Scelta
# ---------------------------------------------------------------------------

# Soglia di confidenza sotto la quale l'audio è considerato degradato:
# sotto questo valore l'ASR sta indovinando, non trascrivendo.
ASR_CONF_FLOOR = 0.55

# Frazione di parlato sotto la quale il VAD sta perdendo voce.
SPEECH_RATIO_FLOOR = 0.08

# Ritmo plausibile per l'italiano parlato (sillabe~parole al secondo).
WPS_MIN, WPS_MAX = 0.8, 4.0

# Margine minimo perché si cambi variante. Senza un margine, la scelta
# oscillerebbe di run in run per puro rumore numerico.
SWITCH_MARGIN = 0.04


def compare(original: QualityScore, denoised: QualityScore) -> dict[str, Any]:
    """
    Confronta due varianti e decide, motivando la scelta.

    Il confronto non è "il punteggio maggiore vince": è una sequenza di
    controlli di sanità, dal più grave allo meno grave. Un audio con
    troppe parole al secondo non vince per avere confidenza alta, è
    rotto. Questo è il punto in cui un punteggio unico darebbe il
    risultato sbagliato.
    """
    reasons: list[str] = []
    broken: dict[str, list[str]] = {"original": [], "denoised": []}

    for s in (original, denoised):
        label = s.name
        if s.token_count == 0:
            broken[label].append("nessun testo trascritto")
            continue
        if s.words_per_sec > WPS_MAX:
            broken[label].append(f"ritmo assurdo ({s.words_per_sec:.1f} parole/s)")
        elif s.words_per_sec > 0 and s.words_per_sec < WPS_MIN:
            broken[label].append(f"ritmo troppo lento ({s.words_per_sec:.2f} parole/s)")
        if s.asr_confidence < ASR_CONF_FLOOR:
            broken[label].append(f"confidenza ASR bassa ({s.asr_confidence:.2f})")
        # La soglia su speech_ratio vale solo se il dato c'era: assente
        # non è uguale a zero.
        if s.has_vad_stats and s.speech_ratio < SPEECH_RATIO_FLOOR:
            broken[label].append(f"quasi nessun parlato ({s.speech_ratio:.1%})")
        if s.degenerate_ratio > DEGENERATE_RATIO_LIMIT:
            broken[label].append(
                f"{s.degenerate_segments}/{s.segments_count} segmenti ripetuti"
            )

    if broken["original"] and not broken["denoised"]:
        winner = "denoised"
        reasons.append("l'originale è degradato, la variante ripulita è sana")
    elif broken["denoised"] and not broken["original"]:
        winner = "original"
        reasons.append("la variante ripulita è degradata, l'originale è sano")
    elif broken["original"] and broken["denoised"]:
        winner = "original"
        reasons.append("entrambe degradate: si tiene l'originale per non peggiorare")
    else:
        # Entrambe sane: decide il margine di confidenza, con soglia
        # di commutazione per non oscillare.
        gain = denoised.asr_confidence - original.asr_confidence
        if gain > SWITCH_MARGIN:
            winner = "denoised"
            reasons.append(f"confidenza ASR migliore di {gain:+.3f}")
        elif gain < -SWITCH_MARGIN:
            winner = "original"
            reasons.append(f"confidenza ASR peggiore di {gain:+.3f}")
        else:
            winner = "original"
            reasons.append(
                f"differenza non significativa ({gain:+.3f}): si tiene l'originale"
            )

    return {
        "winner": winner,
        "reasons": reasons,
        "broken": broken,
        "original": original.as_dict(),
        "denoised": denoised.as_dict(),
        "switch_margin": SWITCH_MARGIN,
    }


def write_decision(decision: dict[str, Any], path: Path) -> None:
    """Scrive la decisione accanto agli output, con i numeri che l'hanno
    prodotta. Serve a non dover ricostruire a posteriori perché è stata
    scelta una variante, e a poter cambiare soglia e ricalcolare."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(decision, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    logger.info("Decisione denoise: %s → %s", path.name, decision["winner"])


def denoised_path_for(wav_path: Path) -> Path:
    """Nome del file ripulito derivato da un WAV: si tiene il nome
    dell'originale così i checkpoint e i log restano leggibili."""
    return wav_path.with_name(f"{wav_path.stem}_dn.wav")
