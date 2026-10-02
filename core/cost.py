"""
Costo della pipeline: quanto tempo ci vuole davvero.

Una costante sola non basta, perché il costo non è proporzionale alla
durata dell'audio. Mettere dentro un file da un'ora costa molto meno,
in proporzione, di mettere dentro dieci file da dieci minuti: il
caricamento del modello e il campione per il confronto denoise si
pagano una volta sola.

Il modello ha quindi due parti, entrambe misurate sulle registrazioni
vere del registratore:

    costo = fisso + per_audio_secondo * secondi_audio
                + per_parola_secondo * secondi_parlato

I termini per audio (diarizzazione, VAD, conversione) pagano sul
silenzio. Il termine per parlato (ASR) no: e' l'unica ragione per cui
una registrazione silenziosa costa meno.

Perche' esiste un modello e non una costante: con la costante 0,76,
ereditata da un campione di novanta secondi, la stima sbagliava di un
fattore due sul materiale vero e faceva prevedere una coda che cresce
invece di restare in pari. Una stima sbagliata in questa direzione
porta a comprare una macchina che non serve.

Tutte le costanti vengono da misure, non da ragionamenti. Cambiando
modello ASR o hardware vanno rimisurate: sono il punto in cui questo
modello invecchia.
"""

from __future__ import annotations

# --- costi per secondo di audio (compreso il silenzio) ---------------------

# Diarizzazione: misurata a 15,8x realtime (38 s per 10 min di audio).
Diarization_SEC_PER_AUDIO_SEC = 1.0 / 15.8

# VAD + conversione WAV: misurata a 33x realtime.
VAD_SEC_PER_AUDIO_SEC = 1.0 / 33.0

# --- costo per secondo di PARLATO ----------------------------------------

# ASR faster-whisper large-v3-turbo su CPU INT8: 142 s per 9,9 min di
# parlato = 4,18x realtime. E' la voce piu' forte del conto, e l'unica
# che premia le registrazioni silenziose.
ASR_SEC_PER_SPEECH_SEC = 1.0 / 4.18

# --- costi fissi per file -------------------------------------------------

# Caricamento modello ASR (3,1 s misurati) e pipeline pyannote (2,0 s):
# sono lontani dal costo che si suppone, perché i modelli sono gia' in
# cache su disco e il MPS li carica in fretta.
MODEL_LOAD_SEC = 10.0

# Confronto denoise: VAD sulla variante ripulita, due passate ASR sul
# campione da 180 s, e la decisione. Misurati ~44 s in tutto su un file
# da 10 minuti.
DENOISE_COMPARE_SEC = 45.0

# Prosodia e scrittura degli output.
OUTPUT_SEC = 8.0

FIXED_SEC = MODEL_LOAD_SEC + DENOISE_COMPARE_SEC + OUTPUT_SEC


def estimate_seconds(
    audio_sec: float,
    speech_sec: float | None = None,
    fixed_sec: float = FIXED_SEC,
) -> float:
    """Secondi di elaborazione attesi per un file.

    Args:
        audio_sec: durata totale del file.
        speech_sec: secondi di parlato effettivo. Se None si assume il
            100%: e' la stima peggiore, che e' la direzione giusta in
            cui sbagliare quando non si sa.
        fixed_sec: costi fissi, escludibili nei test.

    Returns:
        Secondi di elaborazione stimati.
    """
    audio_sec = max(float(audio_sec), 0.0)
    speech = audio_sec if speech_sec is None else min(float(speech_sec), audio_sec)

    return (
        fixed_sec
        + (Diarization_SEC_PER_AUDIO_SEC + VAD_SEC_PER_AUDIO_SEC) * audio_sec
        + ASR_SEC_PER_SPEECH_SEC * speech
    )


def estimate_rtf(audio_sec: float, speech_sec: float | None = None) -> float:
    """Rapporto elaborazione/audio per un file.

    Su file brevi questo rapporto è alto (i costi fissi dominano); su
    un'ora scende. E' per questo che il valore va calcolato sul file che
    si sta davvero per elaborare, non letto da una tabella.
    """
    if audio_sec <= 0:
        return 1.0
    return estimate_seconds(audio_sec, speech_sec) / audio_sec


# Coefficienti lineari per stimare senza conoscere i dettagli:
#     secondi ≈ SEC_PER_AUDIO_SEC * durata + SEC_PER_SPEECH_SEC * parlato
#     secondi ≈ FIXED_SEC                                (file brevissimi)
# Servono a sync_device, che deve stimare il file successivo mentre
# lavora e non ha ancora i segmenti del VAD.
SEC_PER_AUDIO_SEC = Diarization_SEC_PER_AUDIO_SEC + VAD_SEC_PER_AUDIO_SEC
SEC_PER_SPEECH_SEC = ASR_SEC_PER_SPEECH_SEC