"""
Diarizer — Speaker Diarization via pyannote.audio

Risponde alla domanda "chi parla quando?" su tutto il file audio.
Usa pyannote/speaker-diarization-3.1 (MIT license, gratuito, locale).

Requisiti:
  - Token Hugging Face gratuito (vedi README — serve una volta sola per
    accettare i termini d'uso; poi il modello gira offline)
  - pyannote-audio >= 3.0 (già nel venv whisperx_env: 4.0.7)
  - PyTorch con MPS (già disponibile: torch 2.8.0 su M1 Pro)

Output per segmento:
    {"speaker": "SPEAKER_00", "start": 12.4, "end": 18.7}

Strategia per file lunghi (18-20 ore):
  pyannote lavora sull'intero file in batch interni; su M1 Pro con MPS
  processa ~5-8x realtime. Su 10 ore di parlato effettivo: ~75-120 min.
  Per rientrare nel budget notturno, la diarizzazione gira in parallelo
  con la fase di prosodia (dopo l'ASR), non con l'ASR stesso.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class DiarizationResult:
    """Esito della diarizzazione: segmenti + embedding delle voci.

    Gli embedding sono quelli che pyannote calcola già per il clustering
    agglomerativo: non costa tempo extra chiederli. Servono al
    SpeakerDB per riconoscere la stessa persona tra sessioni diverse.
    """
    segments: list[dict[str, Any]] = field(default_factory=list)
    # {"SPEAKER_00": [0.31, -0.02, ...]} — serializzabile in JSON
    embeddings: dict[str, list[float]] = field(default_factory=dict)

    @property
    def speaker_seconds(self) -> dict[str, float]:
        """Secondi di parlato per speaker, per pesare i contributi."""
        out: dict[str, float] = {}
        for seg in self.segments:
            sp = seg.get("speaker", "UNKNOWN")
            out[sp] = out.get(sp, 0.0) + (seg["end"] - seg["start"])
        return out


class Diarizer:
    """
    Wrapper attorno a pyannote.audio per la diarizzazione speaker.

    Uso:
        diarizer = Diarizer(config.diarization)
        segments = diarizer.diarize(wav_path)
        # segments: [{"speaker": "SPEAKER_00", "start": 0.5, "end": 4.2}, ...]
    """

    def __init__(self, diarization_config) -> None:
        self.cfg = diarization_config
        self._pipeline = None  # lazy load

    # ------------------------------------------------------------------
    # Caricamento pipeline pyannote (lazy)
    # ------------------------------------------------------------------

    def _load_pipeline(self) -> None:
        if self._pipeline is not None:
            return

        if not self.cfg.hf_token:
            raise RuntimeError(
                "Token Hugging Face non trovato.\n"
                "Segui le istruzioni nel README per ottenerlo gratuitamente:\n"
                "  python setup_env.sh  →  sezione 'Hugging Face token'"
            )

        try:
            from pyannote.audio import Pipeline
            import torch
        except ImportError as exc:
            raise ImportError(
                f"pyannote.audio non disponibile: {exc}\n"
                "Assicurati di usare il venv whisperx_env."
            ) from exc

        logger.info(
            "Caricamento pipeline pyannote: %s ...", self.cfg.model_id
        )
        t0 = time.time()

        # pyannote-audio 4.x con speaker-diarization-3.1:
        # from_pretrained risolve automaticamente le dipendenze del config.yaml.
        # Se il caricamento fallisce per repo gated non necessari, ricade su
        # un'istanza manuale con i modelli esplicitamente specificati.
        try:
            self._pipeline = Pipeline.from_pretrained(
                self.cfg.model_id,
                token=self.cfg.hf_token,
            )
        except Exception as first_err:
            logger.warning(
                "Caricamento diretto fallito (%s), provo caricamento esplicito...",
                type(first_err).__name__,
            )
            self._pipeline = self._load_pipeline_explicit(torch)

        # Sposta su MPS (GPU Apple Silicon) se disponibile
        device = self._resolve_device()
        self._pipeline.to(torch.device(device))

        logger.info(
            "Pipeline pyannote caricata in %.1fs [device=%s]",
            time.time() - t0, device,
        )

    def _load_pipeline_explicit(self, torch):
        """
        Carica speaker-diarization-3.1 costruendo la pipeline manualmente
        con i modelli esplicitamente specificati nel config.yaml,
        evitando dipendenze da repo non autorizzati (es. community-1).
        """
        from pyannote.audio import Pipeline
        from pyannote.audio.pipelines import SpeakerDiarization
        from pyannote.audio.pipelines.utils.getter import get_model

        token = self.cfg.hf_token

        logger.info("Caricamento esplicito: segmentation + embedding separati")

        pipeline = SpeakerDiarization(
            segmentation="pyannote/segmentation-3.0",
            embedding="pyannote/wespeaker-voxceleb-resnet34-LM",
            clustering="AgglomerativeClustering",
            segmentation_batch_size=32,
            embedding_batch_size=32,
            embedding_exclude_overlap=True,
        )

        # Carica i pesi dei sotto-modelli
        pipeline._segmentation.model_ = get_model(
            "pyannote/segmentation-3.0", token=token
        )
        pipeline._embedding = get_model(
            "pyannote/wespeaker-voxceleb-resnet34-LM", token=token
        )

        # Imposta i parametri di clustering da 3.1
        pipeline.instantiate({
            "segmentation": {"min_duration_off": 0.0},
            "clustering": {
                "method": "centroid",
                "min_cluster_size": 12,
                "threshold": 0.7045654963945799,
            },
        })

        return pipeline

    def _resolve_device(self) -> str:
        """Sceglie il device: mps → cpu come fallback."""
        try:
            import torch
            if self.cfg.device == "mps" and torch.backends.mps.is_available():
                return "mps"
        except Exception:
            pass
        if self.cfg.device != "cpu":
            logger.warning(
                "Device '%s' non disponibile, fallback a CPU", self.cfg.device
            )
        return "cpu"

    # ------------------------------------------------------------------
    # Diarizzazione
    # ------------------------------------------------------------------

    def diarize(
        self,
        wav_path: Path,
        collect_embeddings: bool = True,
    ) -> DiarizationResult:
        """
        Esegue la diarizzazione sull'intero file WAV.

        Args:
            wav_path: percorso al WAV 16kHz mono prodotto dal VAD
            collect_embeddings: chiede a pyannote gli embedding vocali
                (nessun costo computazionale aggiuntivo: li calcola
                comunque per il clustering). Servono al SpeakerDB per
                l'identità cross-file.

        Returns:
            DiarizationResult con i segmenti ordinati per start
            ([{"speaker": "SPEAKER_00", "start": 0.5, "end": 4.2}, ...])
            e gli embedding per speaker.
        """
        self._load_pipeline()
        wav_path = Path(wav_path)

        logger.info("Diarizzazione: %s ...", wav_path.name)
        t0 = time.time()

        # pyannote 4.x richiede torchcodec per leggere file audio direttamente.
        # Poiché torchcodec non è disponibile su questo sistema, passiamo
        # l'audio pre-caricato come dizionario {waveform, sample_rate}.
        audio_input = self._load_audio_as_tensor(wav_path)

        # Parametri speaker
        kwargs: dict[str, Any] = {}
        if self.cfg.num_speakers is not None:
            kwargs["num_speakers"] = self.cfg.num_speakers
        else:
            kwargs["min_speakers"] = self.cfg.min_speakers
            kwargs["max_speakers"] = self.cfg.max_speakers

        if collect_embeddings:
            # Il flag NON viene passato. Nella versione di pyannote
            # installata (4.0.7) `collect_embeddings` non e' un parametro
            # di Pipeline.__call__: viene ignorato con un avviso a ogni
            # esecuzione. Gli embedding arrivano comunque, perche' la
            # diarizzazione li calcola gia' per il proprio clustering.
            #
            # Passarlo era pero' una promessa non mantenuta: se domani
            # cambiasse versione e senza flag gli embedding sparissero,
            # l'identita' vocale cross-file si sarebbe spegnuta in
            # silenzio. Meglio accorgersene con un avviso esplicito qui
            # sotto, che con un parametro che finge di funzionare.
            pass

        diarization = self._pipeline(audio_input, **kwargs)

        elapsed = time.time() - t0
        segments = self._to_segments(diarization)
        embeddings = self._to_embeddings(diarization)
        if collect_embeddings and not embeddings:
            logger.warning(
                "Nessun embedding vocale da pyannote: l'identita' cross-file "
                "non funzionera' per questa sessione (la diarizzazione e' "
                "comunque valida)"
            )

        speakers = {s["speaker"] for s in segments}
        logger.info(
            "Diarizzazione completata in %.1fs | %d segmenti | %d speaker: %s",
            elapsed,
            len(segments),
            len(speakers),
            sorted(speakers),
        )
        if embeddings:
            dims = {len(v) for v in embeddings.values()}
            logger.info(
                "Embedding vocali raccolti: %d (dim=%s)",
                len(embeddings), sorted(dims),
            )
        elif collect_embeddings:
            logger.warning(
                "Nessun embedding ottenuto da pyannote: il matching "
                "cross-file dei speaker non sarà disponibile per questa sessione"
            )

        return DiarizationResult(segments=segments, embeddings=embeddings)

    @staticmethod
    def _load_audio_as_tensor(wav_path: Path) -> dict:
        """
        Carica WAV come tensore PyTorch nel formato atteso da pyannote 4.x:
        {'waveform': (channel, time) torch.Tensor float32, 'sample_rate': int}
        """
        try:
            import torch
            import soundfile as sf
        except ImportError as exc:
            raise ImportError("torch e soundfile richiesti per diarizzazione") from exc

        audio, sr = sf.read(str(wav_path), dtype="float32", always_2d=False)
        # pyannote vuole shape (channels, time)
        waveform = torch.from_numpy(audio).unsqueeze(0)  # (1, T)
        return {"waveform": waveform, "sample_rate": sr}

    @staticmethod
    def _to_segments(diarization) -> list[dict[str, Any]]:
        """
        Converte il risultato di pyannote in lista di dict.
        Gestisce sia il vecchio Annotation (pyannote < 4) che
        il nuovo DiarizeOutput (pyannote 4.x).
        """
        # pyannote 4.x restituisce DiarizeOutput (dataclass)
        # con campo speaker_diarization che è l'Annotation
        if hasattr(diarization, "speaker_diarization"):
            annotation = diarization.speaker_diarization
        else:
            annotation = diarization  # pyannote < 4: restituisce Annotation diretta

        segments = []
        for turn, _, speaker in annotation.itertracks(yield_label=True):
            segments.append({
                "speaker": speaker,
                "start":   round(turn.start, 3),
                "end":     round(turn.end,   3),
            })
        segments.sort(key=lambda s: s["start"])
        return segments

    @staticmethod
    def _to_embeddings(diarization) -> dict[str, list[float]]:
        """
        Estrae gli embedding vocali dal risultato di pyannote.

        In pyannote 4.x DiarizeOutput.speaker_embeddings è un array
        (num_speakers, dimension) le cui righe sono allineate a
        speaker_diarization.labels() — non è un dict, e zipparlo senza
        i label assegnerebbe ogni voce alla persona sbagliata. Gestiamo
        anche il dict (forme di pyannote 3.x) per robustezza.

        Assenti in entrambi i casi: si ritorna con un dict vuoto, senza
        eccezioni — la diarizzazione resta valida, solo senza identità
        cross-file.
        """
        from core.speaker_db import to_vector

        raw = getattr(diarization, "speaker_embeddings", None)
        if raw is None:
            return {}

        # Non usare `if not raw`: su un array numpy l'operatori truth
        # solleva ValueError ("truth value of an array...").
        try:
            size = len(raw)
        except TypeError:
            return {}
        if size == 0:
            return {}

        def clean(vector) -> list[float]:
            # 4 decimali: l'embedding serve solo a confronti coseno,
            # la precisione ulteriore è spazio sprecato nel checkpoint
            # e nel DB.
            return [round(float(x), 4) for x in to_vector(vector)]

        try:
            if hasattr(raw, "items"):          # dict {label: vettore}
                pairs = list(raw.items())
            else:                                # array (n_speakers, dim)
                annotation = getattr(diarization, "speaker_diarization", None)
                labels = list(annotation.labels()) if annotation is not None else []
                if len(labels) != size:
                    logger.warning(
                        "pyannote ha restituito %d embedding per %d label: "
                        "niente identità cross-file per questa sessione",
                        size, len(labels),
                    )
                    return {}
                pairs = zip(labels, raw)
        except (TypeError, ValueError) as exc:
            logger.warning("Formato embedding non riconosciuto (%s), ignorati", exc)
            return {}

        out: dict[str, list[float]] = {}
        for speaker, vector in pairs:
            try:
                out[speaker] = clean(vector)
            except (TypeError, ValueError) as exc:
                logger.warning("Embedding non serializzabile per %s: %s", speaker, exc)
        return out

    # ------------------------------------------------------------------
    # Assegnazione speaker ai chunk ASR
    # ------------------------------------------------------------------

    @staticmethod
    def assign_speakers(
        asr_chunks: list[dict[str, Any]],
        diarization_segments: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """
        Arricchisce ogni chunk ASR con il label dello speaker dominante
        nell'intervallo temporale del chunk.

        Per ogni chunk calcola quale speaker copre più tempo nell'intervallo
        [chunk.start, chunk.end] — strategia "majority vote" temporale.

        Restituisce i chunk con chiave aggiuntiva "speaker".
        """
        enriched = []
        for chunk in asr_chunks:
            speaker = _dominant_speaker(
                chunk["start"], chunk["end"], diarization_segments
            )
            enriched.append({**chunk, "speaker": speaker})
        return enriched

    @staticmethod
    def assign_speakers_word_level(
        asr_chunks: list[dict[str, Any]],
        diarization_segments: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """
        Versione più precisa: assegna lo speaker a ogni singola parola
        (usa i word-level timestamps prodotti dal Transcriber).

        Utile per transcript con speaker change dentro lo stesso chunk.
        """
        enriched_chunks = []
        for chunk in asr_chunks:
            enriched_words = []
            for word in chunk.get("words", []):
                w_mid = (word["start"] + word["end"]) / 2.0
                speaker = _speaker_at(w_mid, diarization_segments)
                enriched_words.append({**word, "speaker": speaker})

            # Speaker dominante nel chunk = speaker più frequente nelle parole
            if enriched_words:
                from collections import Counter
                chunk_speaker = Counter(
                    w["speaker"] for w in enriched_words
                ).most_common(1)[0][0]
            else:
                chunk_speaker = _dominant_speaker(
                    chunk["start"], chunk["end"], diarization_segments
                )

            enriched_chunks.append({
                **chunk,
                "speaker": chunk_speaker,
                "words": enriched_words,
            })

        return enriched_chunks


# ---------------------------------------------------------------------------
# Utility interne
# ---------------------------------------------------------------------------

def _dominant_speaker(
    start: float,
    end: float,
    segments: list[dict[str, Any]],
) -> str:
    """
    Trova lo speaker che copre più tempo nell'intervallo [start, end].
    Restituisce "UNKNOWN" se nessun segmento si sovrappone.
    """
    overlap: dict[str, float] = {}
    for seg in segments:
        # Intersezione tra [start,end] e [seg.start, seg.end]
        ov_start = max(start, seg["start"])
        ov_end   = min(end,   seg["end"])
        ov = ov_end - ov_start
        if ov > 0:
            overlap[seg["speaker"]] = overlap.get(seg["speaker"], 0.0) + ov

    if not overlap:
        return "UNKNOWN"
    return max(overlap, key=overlap.__getitem__)


def _speaker_at(time_point: float, segments: list[dict[str, Any]]) -> str:
    """
    Trova lo speaker attivo a un dato istante temporale.
    In caso di ambiguità (overlap pyannote) prende il primo match.
    """
    for seg in segments:
        if seg["start"] <= time_point <= seg["end"]:
            return seg["speaker"]
    return "UNKNOWN"
