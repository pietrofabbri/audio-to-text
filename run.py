"""
run.py — Entrypoint CLI della pipeline audio-to-text

Uso:
    python run.py input/registrazione.mp3
    python run.py input/ --all
    python run.py input/registrazione.mp3 --backend faster --model large-v3
    python run.py input/registrazione.mp3 --no-diarization --no-prosody
    python run.py --status output/registrazione/

Modalità schedulata (chiamata da launchd di notte):
    python run.py --scheduled
"""

from __future__ import annotations

import os
# Workaround: PyTorch e mlx caricano entrambi libomp.dylib su macOS.
# KMP_DUPLICATE_LIB_OK sopprime l'abort — sicuro per inferenza single-process.
os.environ.setdefault("KMP_DUPLICATE_LIB_OK", "TRUE")

import argparse
import json
import logging
import signal
import sys
import time
from pathlib import Path

# ---------------------------------------------------------------------------
# Logging configurato prima di tutto
# ---------------------------------------------------------------------------

def _setup_logging(log_level: str, log_file: Path | None = None) -> None:
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    if log_file:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(str(log_file), encoding="utf-8"))

    logging.basicConfig(
        level=getattr(logging, log_level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=handlers,
        force=True,
    )

logger = logging.getLogger("audio-to-text")


# ---------------------------------------------------------------------------
# Gestione segnali (SIGTERM da launchd, SIGINT da Ctrl+C)
# ---------------------------------------------------------------------------

_shutdown_requested = False

def _handle_signal(signum, frame):  # noqa: ANN001
    global _shutdown_requested
    logger.warning("Segnale %d ricevuto — chiusura ordinata dopo il chunk corrente...", signum)
    _shutdown_requested = True

signal.signal(signal.SIGTERM, _handle_signal)
signal.signal(signal.SIGINT,  _handle_signal)


# ---------------------------------------------------------------------------
# Denoise: la variante migliore la sceglie il software
# ---------------------------------------------------------------------------

def _decide_denoise(
    cfg,
    ck,
    audio_path: Path,
    wav_path: Path,
    asr_chunks: list[dict],
    speech_segments,
) -> tuple[list[dict], str]:
    """
    Confronta audio originale e audio ripulito, e restituisce i chunk
    della variante migliore più il nome del vincitore.

    Il confronto usa le STESSE finestre temporali per entrambe le varianti
    (quelle prodotte dal VAD sull'originale). È la condizione che rende il
    confronto valido: con segmentazioni diverse ogni differenza fra le due
    trascrizioni sarebbe inseparabile da dove sono stati tagliati i chunk.

    Il VAD viene invece rilanciato sulla variante ripulita, ma solo per
    misurare quanto parlato è sopravvissuto al denoise: è l'unico controllo
    che intercetta il guasto tipico, cioè una pulizia che si mangia le
    consonanti e lascia il VAD con le tasche vuote.
    """
    import tempfile

    from core.config import OUTPUT_DIR
    from pipeline.denoise import (
        compare, denoise_afftdn, denoised_path_for, score_variant, write_decision,
    )
    from pipeline.vad import VoiceActivityDetector
    from pipeline.transcriber import Transcriber

    logger.info("Stadio 2b/5: Denoise afftdn + confronto automatico")

    # I segmenti VAD servono per costruire le finestre del confronto. Se
    # il VAD è stato ripreso dal checkpoint non ci sono in memoria: senza
    # questo, la variante ripulita veniva "trascritta" con i chunk
    # dell'originale e il confronto non confrontava niente.
    if speech_segments is None:
        try:
            vad = VoiceActivityDetector(cfg.vad, cfg.asr)
            _, speech_segments, _ = vad.process(wav_path)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Segmenti VAD non disponibili (%s): niente confronto, si usa l'originale",
                exc,
            )
            ck.complete_stage("denoise", winner="original", reason="segmenti VAD non disponibili")
            return asr_chunks, "original"

    all_chunks = VoiceActivityDetector.split_into_chunks(
        speech_segments, max_chunk_sec=cfg.asr.chunk_max_sec
    )

    try:
        denoised = denoise_afftdn(
            wav_path, denoised_path_for(wav_path),
            nr=cfg.denoise.nr, nf=cfg.denoise.nf,
        )
    except (RuntimeError, OSError) as exc:
        # Nessuna variante ripulita: si prosegue con l'originale. Un
        # denoise fallito non è motivo per perdere una notte di registrazioni.
        logger.warning("Denoise non riuscito (%s): si usa l'originale", exc)
        ck.complete_stage("denoise", winner="original", reason="denoise fallito")
        return asr_chunks, "original"

    # --- VAD sulla variante ripulita: quanto parlato è sopravvissuto? ---
    denoised_vad_stats: dict = {}
    probe_wav: Path | None = None
    try:
        vad = VoiceActivityDetector(cfg.vad, cfg.asr)
        probe_wav, _, denoised_vad_stats = vad.process(denoised)
    except Exception as exc:  # noqa: BLE001
        logger.warning("VAD sulla variante ripulita fallito: %s", exc)

    def _butta_via_derivati() -> None:
        """Cancella i WAV prodotti dal denoise: non servono a nessuno.

        Non basta cancellare quello prodotto da afftdn: il VAD puo'
        averne creato un altro (una copia, se l'ingresso non era gia'
        nel formato giusto), e quello non e' citato da nessun
        checkpoint, quindi nessuno lo recupererebbe mai. Sono 115 MB
        per ora di audio, e senza questa riga restano in cache per
        sempre.
        """
        for p in {denoised, probe_wav}:
            if p is None:
                continue
            try:
                Path(p).unlink()
            except OSError as exc:
                logger.warning(
                    "Non riesco a cancellare %s: %s", p, exc,
                )

    original_vad_stats = ck._data.get("vad_stats", {}) or {}

    if not cfg.denoise.compare:
        logger.info("Confronto disattivato: si usa la variante ripulita")
        decision = {
            "winner": "denoised",
            "reasons": ["confronto disattivato in configurazione"],
            "original": None,
            "denoised": None,
        }
        denoised_chunks = _transcribe_windows(
            cfg, audio_path, denoised, all_chunks
        )
    else:
        # Finestre di confronto: un campione, non tutto il file.
        #
        # Con 18 ore di registrazione a notte una seconda passata ASR
        # completa costerebbe oltre un'ora, e il confronto da solo farebbe
        # sballare il budget. Il campione costa ~un minuto e basta: la
        # decisione prende in giro tutto il file, e su un file da un'ora
        # la qualita dell'audio non cambia di minuto in minuto.
        windows = _sample_windows(all_chunks, cfg.denoise.sample_sec)
        sample_sec = sum(w["end"] - w["start"] for w in windows)
        total_sec = sum(c["end"] - c["start"] for c in all_chunks)
        logger.info(
            "  denoise: confronto su %d finestre (%.0fs su %.0fs, campione %s)",
            len(windows), sample_sec, total_sec,
            "completo" if len(windows) == len(all_chunks) else "parziale",
        )

        # L'originale è già trascritto: campionarlo è gratis.
        idx = {w["idx"] for w in windows}
        orig_sample = [c for c in asr_chunks if c.get("idx") in idx]
        dn_sample = _transcribe_windows(cfg, audio_path, denoised, windows)

        # Le statistiche VAD vanno ristrette al campione, altrimenti il
        # rapporto di parlato verrebbe calcolato su un file intero
        # confrontato con parole di una porzione: numeri senza senso.
        orig_stats = _stats_for_window(
            original_vad_stats, windows, all_chunks
        )
        dn_stats = _stats_for_window(denoised_vad_stats, windows, all_chunks)

        original_score = score_variant("original", orig_sample, orig_stats)
        denoised_score = score_variant("denoised", dn_sample, dn_stats)
        decision = compare(original_score, denoised_score)
        decision["sample"] = {
            "windows": len(windows),
            "total_windows": len(all_chunks),
            "sample_sec": round(sample_sec, 1),
            "total_sec": round(total_sec, 1),
        }

    for r in decision.get("reasons", []):
        logger.info("  denoise: %s", r)
    logger.info(
        "  denoise: originale conf=%.3f parole/s=%.2f | ripulita conf=%.3f parole/s=%.2f -> %s",
        (decision.get("original") or {}).get("asr_confidence", 0.0),
        (decision.get("original") or {}).get("words_per_sec", 0.0),
        (decision.get("denoised") or {}).get("asr_confidence", 0.0),
        (decision.get("denoised") or {}).get("words_per_sec", 0.0),
        decision["winner"],
    )

    if cfg.denoise.write_decision_json:
        write_decision(decision, OUTPUT_DIR / ck.stem / "denoise_decision.json")

    if decision["winner"] == "denoised":
        # Il campione ha vinto: ora serve davvero tutto il file nella
        # variante ripulita. È il percorso costoso, ma si attiva solo
        # quando il confronto ha trovato una differenza reale.
        denoised_chunks = _transcribe_windows(
            cfg, audio_path, denoised, all_chunks
        )
        ck._data["chunks"] = denoised_chunks
        ck._data["vad_stats"] = denoised_vad_stats or ck._data.get("vad_stats", {})
        ck.complete_stage("denoise", winner="denoised", reason="; ".join(decision["reasons"]))
        # Il WAV ripulito è derivabile (lo si rifà con una riga di
        # ffmpeg) e i chunk sono ormai nel checkpoint: tenerlo occuperebbe
        # 115 MB per ogni ora registrata senza servire a nulla.
        _butta_via_derivati()
        return denoised_chunks, "denoised"

    # L'originale ha vinto: la variante ripulita non serve più e occupa
    # disco (~115 MB per ora di audio a 16 kHz mono).
    _butta_via_derivati()
    ck.complete_stage("denoise", winner="original", reason="; ".join(decision["reasons"]))
    return asr_chunks, "original"


def _sample_windows(chunks: list[dict], sample_sec: float) -> list[dict]:
    """
    Sceglie le finestre su cui fare il confronto.

    Prende una fetta contigua di centro, non un campione sparso: la
    contiguità mantiene le condizioni acustiche costanti dentro la
    porzione, che è il punto di un confronto. Il centro e non l'inizio
    perche i primi secondi di una registrazione sono spesso silenzio o
    parole isolate, e un giudizio su quelli non dice niente sul resto.
    """
    if not chunks:
        return []
    total = sum(c["end"] - c["start"] for c in chunks)
    if sample_sec <= 0 or total <= sample_sec:
        return list(chunks)

    # Parte dal centro e cammina all'indietro finché copre il campione
    start_at = len(chunks) // 2
    picked: list[dict] = []
    acc = 0.0
    i = start_at
    while i >= 0 and acc < sample_sec / 2:
        picked.insert(0, chunks[i])
        acc += chunks[i]["end"] - chunks[i]["start"]
        i -= 1
    acc = 0.0
    i = start_at
    while i < len(chunks) and acc < sample_sec / 2:
        if i >= start_at:
            picked.append(chunks[i])
        acc += chunks[i]["end"] - chunks[i]["start"]
        i += 1
    return picked


def _stats_for_window(
    vad_stats: dict, windows: list[dict], all_chunks: list[dict]
) -> dict:
    """
    Ristringe le statistiche VAD alla porzione di confronto.

    Le feature prosodiche e di parlato sono per-unità di tempo: mescolare
    il rapporto di parlato di un'ora con il numero di parole di tre
    minuti produce un metro al secondo che non esiste.
    """
    total = sum(c["end"] - c["start"] for c in all_chunks) or 1.0
    win = sum(w["end"] - w["start"] for w in windows)
    if not vad_stats:
        return {}
    frac = win / total
    speech = float(vad_stats.get("speech_duration_sec", 0.0) or 0.0) * frac
    return {
        "speech_duration_sec": speech,
        "total_duration_sec": win,
        "speech_ratio": float(vad_stats.get("speech_ratio", 0.0) or 0.0),
    }


def _transcribe_windows(
    cfg,
    audio_path: Path,
    wav_path: Path,
    windows: list[dict],
) -> list[dict]:
    """
    Trascrive un insieme di finestre con un checkpoint usa-e-getta.

    Il checkpoint temporaneo serve a non inquinare i chunk canonici con i
    risultati della passata di confronto: se la variante ripulita perde,
    i suoi chunk non devono restare da qualche parte e finire nell'output.
    """
    import tempfile

    from core.checkpoint import Checkpoint
    from pipeline.transcriber import Transcriber

    if not windows:
        return []

    with tempfile.TemporaryDirectory(prefix="a2t_variant_") as tmp:
        throwaway = Checkpoint(audio_path, Path(tmp))
        transcriber = Transcriber(cfg.asr)
        return transcriber.transcribe_chunks(
            windows, wav_path, throwaway, save_every=25
        )


# ---------------------------------------------------------------------------
# Identità speaker cross-file
# ---------------------------------------------------------------------------

def _resolve_global_speakers(
    cfg, session_stem: str, diar_result
) -> tuple[dict[str, str], dict[str, str]]:
    """
    Trasforma i label locali SPEAKER_xx in ID globali persistenti.

    Ritorna (speaker_global_map, speaker_names): il primo serve
    all'assembler per rietichettare i segmenti, il secondo per
    stampare i nomi umani dove esistono.

    Fallisce in silenzio (mapping vuoto) se il DB non è scrivibile o
    gli embedding mancano: la diarizzazione locale resta valida e la
    pipeline va avanti. Il matching non deve mai essere un blocco.
    """
    from core.speaker_db import SpeakerDB

    if not cfg.speaker_id.enabled or not diar_result.embeddings:
        return {}, {}

    try:
        db = SpeakerDB(
            path=cfg.speaker_id.db_path,
            threshold=cfg.speaker_id.match_threshold,
            update_centroid=cfg.speaker_id.update_centroid,
        )
        local = {
            sp: {
                "embedding": emb,
                "seconds": diar_result.speaker_seconds.get(sp, 0.0),
            }
            for sp, emb in diar_result.embeddings.items()
        }
        mapping = db.resolve(session_stem, local)
        if mapping:
            readable = {local: db.get_name(gid) for local, gid in mapping.items()}
            logger.info("Identità vocali: %s", readable)
        # Solo le voci con un nome vero: `GLOBAL_004: "GLOBAL_004"` nella
        # mappa dei nomi è rumore che sembra un'informazione. Chi non ha
        # nome resta pseudonimo, ed è la scelta giusta finché non gli si
        # dà un nome.
        names = {
            gid: rec.get("name")
            for gid, rec in db._data.get("speakers", {}).items()
            if gid in set(mapping.values()) and rec.get("name")
        }
        return mapping, names
    except OSError as exc:
        logger.warning(
            "SpeakerDB non scrivibile (%s): continuo con label locali", exc
        )
        return {}, {}


def _speaker_names_for(cfg, global_ids: set[str]) -> dict[str, str]:
    """Nomi umani già assegnati nel DB delle voci per gli ID indicati.

    Il DB delle voci è l'unica fonte dei nomi. Quando una sessione viene
    rielaborata, il nome arriva da lì e non da una copia dentro il file
    vecchio: è quello che distingue una copia che si aggiorna da una che
    invecchia in silenzio.
    """
    from core.speaker_db import SpeakerDB

    if not cfg.speaker_id.enabled or not global_ids:
        return {}
    try:
        db = SpeakerDB(path=cfg.speaker_id.db_path)
    except OSError:
        return {}
    speakers = db._data.get("speakers", {})
    return {
        gid: speakers[gid]["name"]
        for gid in global_ids
        if (speakers.get(gid) or {}).get("name")
    }


def _write_speaker_profiles(cfg, output_dir: Path) -> None:
    """Scrive speaker_profiles.json accanto agli output della sessione."""
    from core.speaker_db import SpeakerDB

    if not cfg.speaker_id.enabled or not cfg.speaker_id.write_profiles_json:
        return
    try:
        db = SpeakerDB(path=cfg.speaker_id.db_path)
        profiles = db.profiles()
        if not profiles:
            return
        p = output_dir / "speaker_profiles.json"
        p.write_text(
            json.dumps(profiles, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        logger.info("Scritto: %s (%d voci globali)", p, len(profiles))
    except OSError as exc:
        logger.warning("speaker_profiles.json non scritto: %s", exc)


# ---------------------------------------------------------------------------
# Pipeline per singolo file
# ---------------------------------------------------------------------------

def process_file(audio_path: Path, cfg, args, stem: str | None = None) -> bool:
    """
    Esegue la pipeline completa su un singolo file audio.
    Restituisce True se completato, False se saltato o fallito.

    `stem` è il nome della cartella di output. Di default è il nome del
    file audio, che è quanto basta per `python run.py qualcosa.mp3`.
    sync_device lo passa esplicito perché ricava l'orario dal nome del
    file e vuole una cartella `2026-10-03_22-00-00`: senza questo, la
    pipeline scriverebbe in `REC_20261003_220000` mentre il chiamante
    cerca `2026-10-03_22-00-00`, la verifica non troverebbe nulla e il
    file non verrebbe mai cancellato dal registratore.
    """
    from core.checkpoint import Checkpoint
    from core.config import OUTPUT_DIR, LOGS_DIR
    from pipeline.vad import VoiceActivityDetector
    from pipeline.transcriber import Transcriber
    from pipeline.diarizer import Diarizer
    from pipeline.prosody import ProsodyAnalyzer
    from pipeline.assembler import Assembler

    stem = stem or audio_path.stem
    output_dir = OUTPUT_DIR / stem
    ck = Checkpoint(audio_path, OUTPUT_DIR, stem=stem)

    # Salta se già completato. Gli stadi opzionali disattivati non
    # contano: altrimenti attivare una funzione dopo mesi farebbe
    # rielaborare da capo tutte le sessioni già finite.
    required_stages = tuple(
        s for s in ck.STAGES
        if s not in ck.OPTIONAL_STAGES or getattr(cfg, s, None) and getattr(getattr(cfg, s), "enabled", False)
    )
    if cfg.skip_completed and ck.all_done(required_stages):
        logger.info("Già completato, saltato: %s", audio_path.name)
        return False

    # Rifare il testo di una sessione già elaborata, tenendo tutto il
    # resto. Serve quando è cambiato qualcosa che riguarda il testo e
    # non l'audio: un modello diverso, il prompt di contesto, una soglia.
    # Il VAD e la diarizzazione restano, perché sono fatti dell'audio e
    # rifarli darebbe lo stesso identico risultato spendendo minuti di CPU
    # — la CPU che è anche la ragione per cui questo progetto ha una
    # protezione termica.
    if getattr(args, "retranscribe", False) and ck.chunks_done_count() > 0:
        ck.invalidate_asr(reason="richiesto con --retranscribe")

    logger.info("=" * 60)
    logger.info("Inizio: %s", audio_path.name)
    logger.info("=" * 60)

    t_start = time.time()

    # ------------------------------------------------------------------
    # Stadio 1: Conversione WAV + VAD
    # ------------------------------------------------------------------
    if not ck.stage_done("ffmpeg") or not ck.stage_done("vad"):
        logger.info("Stadio 1/5: VAD + conversione WAV")
        vad = VoiceActivityDetector(cfg.vad, cfg.asr)
        wav_path, speech_segments, vad_stats = vad.process(audio_path)

        ck.complete_stage("ffmpeg",
            wav_path=str(wav_path),
            duration_sec=vad_stats["total_duration_sec"],
        )
        ck.complete_stage("vad",
            segments_count=vad_stats["segments_count"],
            speech_ratio=vad_stats["speech_ratio"],
            speech_duration_sec=vad_stats["speech_duration_sec"],
        )
        ck._data["vad_stats"] = vad_stats
        ck.save()
    else:
        logger.info("Stadio 1/5: VAD già completato, carico da checkpoint")
        wav_path = Path(ck.get_stage_data("ffmpeg")["wav_path"])
        vad_stats = ck._data.get("vad_stats", {})

        # Ricostruisce i segmenti VAD dalla lista chunk nel checkpoint
        # (già splittati e salvati)
        speech_segments = None  # segnale: usa i chunk già nel checkpoint

    if _shutdown_requested:
        logger.info("Shutdown richiesto, checkpoint salvato.")
        return False

    # ------------------------------------------------------------------
    # Stadio 2: Trascrizione ASR
    # ------------------------------------------------------------------
    if not ck.stage_done("transcription"):
        logger.info("Stadio 2/5: Trascrizione ASR [backend=%s, model=%s]",
                    cfg.asr.backend, cfg.asr.model_id)

        from pipeline.vad import VoiceActivityDetector
        if speech_segments is None:
            # Se il VAD era già fatto, ricarica i segmenti dal WAV
            vad = VoiceActivityDetector(cfg.vad, cfg.asr)
            _, speech_segments, vad_stats = vad.process(audio_path)

        chunks = VoiceActivityDetector.split_into_chunks(
            speech_segments, max_chunk_sec=cfg.asr.chunk_max_sec
        )

        transcriber = Transcriber(cfg.asr)
        asr_chunks = transcriber.transcribe_chunks(
            chunks, wav_path, ck,
            save_every=cfg.checkpoint_every_n_chunks,
        )

        if _shutdown_requested:
            logger.info("Shutdown durante ASR — checkpoint salvato al chunk %d.",
                        ck.chunks_done_count())
            return False

        ck.complete_stage("transcription", chunks_done=len(asr_chunks))
    else:
        logger.info("Stadio 2/5: ASR già completato, carico da checkpoint")
        asr_chunks = ck.get_all_chunks()

    # ------------------------------------------------------------------
    # Stadio 2b: pulizia del fruscio e scelta automatica della variante
    # ------------------------------------------------------------------
    if cfg.denoise.enabled and not ck.stage_done("denoise"):
        asr_chunks, denoise_winner = _decide_denoise(
            cfg, ck, audio_path, wav_path, asr_chunks,
            speech_segments if speech_segments is not None else None,
        )
        if denoise_winner == "denoised":
            # I chunk sono cambiati: tutto ciò che viene dopo va
            # ricalcolato, altrimenti output e diarizzazione
            # descriverebbero un audio che non è quello pubblicato.
            logger.info(
                "La variante ripulita vince: ricalcolo diarizzazione, "
                "prosodia e output su quella"
            )
            for stage in ("diarization", "prosody", "assembly"):
                ck.reset_stage(stage)
            ck.save()
    else:
        denoise_winner = ck.get_stage_data("denoise").get("winner")

    # ------------------------------------------------------------------
    # Stadio 3: Diarizzazione speaker
    # ------------------------------------------------------------------
    if not args.no_diarization and not ck.stage_done("diarization"):
        logger.info("Stadio 3/5: Diarizzazione speaker [pyannote]")
        diarizer = Diarizer(cfg.diarization)
        diar_result = diarizer.diarize(
            wav_path,
            collect_embeddings=cfg.speaker_id.enabled,
        )
        diar_segments = diar_result.segments

        # Arricchisci ASR con speaker label
        asr_chunks = diarizer.assign_speakers_word_level(asr_chunks, diar_segments)

        # Identità vocali persistenti: SPEAKER_00 di oci potrebbe essere
        # la stessa persona di SPEAKER_01 di domani.
        speaker_global_map, speaker_names = _resolve_global_speakers(
            cfg, stem, diar_result
        )

        ck.save_diarization(diar_segments, diar_result.embeddings, speaker_global_map)
        # Aggiorna i chunk con i speaker nel checkpoint
        ck._data["chunks"] = asr_chunks
        ck.save()
    else:
        if args.no_diarization:
            logger.info("Stadio 3/5: Diarizzazione disabilitata (--no-diarization)")
            diar_segments = []
            speaker_global_map = {}
            speaker_names = {}
        else:
            logger.info("Stadio 3/5: Diarizzazione già completata, carico da checkpoint")
            diar_segments = ck.get_diarization()
            asr_chunks = ck.get_all_chunks()

            # I turni di voce vanno riapplicati ai chunk, sempre, anche se
            # la diarizzazione e' gia' stata fatta in precedenza. Il
            # collegamento tra testo e voce sta dentro i chunk, e i chunk
            # possono essere stati rifatti nel frattempo: rifare la
            # trascrizione svuota i chunk, e senza questa riapplicazione
            # tornerebbero senza speaker. Il sintomo e' subdolo: il file
            # si completa senza errori e tutti i segmenti risultano
            # "UNKNOWN", cioe' una sessione intera senza interlocutori.
            asr_chunks = Diarizer.assign_speakers_word_level(
                asr_chunks, diar_segments,
            )
            speaker_global_map = ck.get_speaker_global_map()
            speaker_names = _speaker_names_for(cfg, set(speaker_global_map.values()))

    if _shutdown_requested:
        logger.info("Shutdown dopo diarizzazione — checkpoint salvato.")
        return False

    # ------------------------------------------------------------------
    # Stadio 4: Analisi prosodia
    # ------------------------------------------------------------------
    if not args.no_prosody and not ck.stage_done("prosody"):
        logger.info("Stadio 4/5: Analisi prosodia [%d worker CPU]", cfg.prosody.num_workers)
        analyzer = ProsodyAnalyzer(cfg.prosody)
        prosody_data = analyzer.analyze(asr_chunks, wav_path)
        ck.save_prosody(prosody_data)
    else:
        if args.no_prosody:
            logger.info("Stadio 4/5: Prosodia disabilitata (--no-prosody)")
            prosody_data = []
        else:
            logger.info("Stadio 4/5: Prosodia già completata, carico da checkpoint")
            prosody_data = ck.get_prosody()

    # ------------------------------------------------------------------
    # Stadio 5: Assemblaggio output
    # ------------------------------------------------------------------
    if not ck.stage_done("assembly"):
        logger.info("Stadio 5/5: Assemblaggio output")
        assembler = Assembler(cfg.output)
        written = assembler.assemble(
            audio_path=audio_path,
            asr_chunks=asr_chunks,
            diar_segments=diar_segments,
            prosody_data=prosody_data,
            vad_stats=vad_stats,
            output_dir=output_dir,
            speaker_global_map=speaker_global_map,
            speaker_names=speaker_names,
            denoise_winner=denoise_winner,
        )
        _write_speaker_profiles(cfg, output_dir)
        ck.complete_stage("assembly", files=list(str(p) for p in written.values()))
    else:
        logger.info("Stadio 5/5: Assemblaggio già completato")

    elapsed = time.time() - t_start
    audio_dur = vad_stats.get("total_duration_sec", 0)
    rtf = audio_dur / elapsed if elapsed > 0 else 0

    logger.info(
        "Completato: %s | %.1f min audio → %.1f min elaborazione | RTF=%.1fx",
        audio_path.name,
        audio_dur / 60,
        elapsed / 60,
        rtf,
    )
    logger.info("Output in: %s", output_dir)

    # La sessione è finita: il WAV derivato non serve più. Senza questo
    # la cache cresce di 115 MB per file (~4 GB al giorno con 18 file da
    # un'ora) e il giorno in cui il disco si riempie la notte si ferma a
    # metà, senza che nessuno lo venga a sapere. La pulizia è fatta qui,
    # dopo l'ultimo uso, e non prima: cancellarla prima romperebbe la
    # ripresa via checkpoint, che è l'unica difesa contro un'interruzione.
    _purge_wav_cache()

    return True


def _collect_files(args, cfg) -> list[Path]:
    from core.config import INPUT_DIR, OUTPUT_DIR
    from core.checkpoint import Checkpoint

    paths: list[Path] = []

    if args.scheduled or args.all:
        # Scansiona tutta la directory input/
        target_dir = Path(args.input) if args.input else INPUT_DIR
        for p in sorted(target_dir.iterdir()):
            if p.suffix.lower() in cfg.accepted_extensions:
                if cfg.skip_completed:
                    ck = Checkpoint(p, OUTPUT_DIR)
                    if ck.all_done():
                        logger.debug("Già completato, skip: %s", p.name)
                        continue
                paths.append(p)
    elif args.input:
        p = Path(args.input)
        if p.is_dir():
            for f in sorted(p.iterdir()):
                if f.suffix.lower() in cfg.accepted_extensions:
                    paths.append(f)
        elif p.is_file():
            paths.append(p)
        else:
            logger.error("Percorso non trovato: %s", args.input)

    return paths


# ---------------------------------------------------------------------------
# Comando --status
# ---------------------------------------------------------------------------

def _print_status(path: Path) -> None:
    from core.config import OUTPUT_DIR
    from core.checkpoint import Checkpoint

    if path.is_dir() and (path / "..").resolve() == OUTPUT_DIR.resolve():
        # È una singola directory di output
        jobs = [path]
    else:
        # Scansiona output/
        jobs = sorted(OUTPUT_DIR.iterdir()) if OUTPUT_DIR.exists() else []

    if not jobs:
        print("Nessun job trovato in output/")
        return

    for job_dir in jobs:
        if not job_dir.is_dir():
            continue
        # Cerca il checkpoint
        ck_files = list(job_dir.glob("*.checkpoint.json"))
        if not ck_files:
            continue
        import json
        data = json.loads(ck_files[0].read_text())
        stem = data.get("stem", job_dir.name)
        stages = data.get("stages", {})
        chunks = len(data.get("chunks", []))
        status = "✓ COMPLETO" if all(s.get("done") for s in stages.values()) else "⏳ IN CORSO"
        print(f"\n{stem}  [{status}]")
        for stage, info in stages.items():
            done = "✓" if info.get("done") else "·"
            print(f"  {done} {stage}")
        print(f"  chunk ASR salvati: {chunks}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="audio-to-text — trascrizione + diarizzazione + prosodia",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Esempi:
  python run.py input/registrazione.mp3
  python run.py input/ --all
  python run.py input/rec.mp3 --backend faster --model large-v3
  python run.py input/rec.mp3 --no-diarization
  python run.py --scheduled
  python run.py --status
        """,
    )

    p.add_argument(
        "input", nargs="?",
        help="File audio/video o directory da processare",
    )
    p.add_argument(
        "--all", action="store_true",
        help="Processa tutti i file nella directory input/",
    )
    p.add_argument(
        "--scheduled", action="store_true",
        help="Modalità schedulata: processa tutto input/, rispetta max_runtime_sec",
    )
    p.add_argument(
        "--status", action="store_true",
        help="Mostra lo stato dei job in output/",
    )
    p.add_argument(
        "--backend", choices=["mlx", "faster"], default=None,
        help="Backend ASR: mlx (default, più veloce su M1) o faster (fallback CPU)",
    )
    p.add_argument(
        "--model", default=None,
        help="ID modello Whisper (es: mlx-community/whisper-large-v3-turbo-q8)",
    )
    p.add_argument(
        "--language", default=None,
        help="Codice lingua (default: it). Usa 'auto' per auto-detect.",
    )
    p.add_argument(
        "--no-diarization", action="store_true",
        help="Salta la diarizzazione speaker (più veloce, nessun token HF richiesto)",
    )
    p.add_argument(
        "--no-prosody", action="store_true",
        help="Salta l'analisi prosodia",
    )
    p.add_argument(
        "--log-level", default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Livello di log (default: INFO)",
    )
    p.add_argument(
        "--max-hours", type=float, default=None,
        help="Tempo massimo di esecuzione in ore (utile per slot notturno)",
    )
    p.add_argument(
        "--retranscribe", action="store_true",
        help="Rifai la trascrizione anche se il file e' gia' stato "
             "elaborato, conservando VAD e diarizzazione. Serve dopo un "
             "cambiamento di modello, di prompt o di soglia",
    )
    p.add_argument(
        "--cooldown-sec", type=float, default=None,
        help="Pausa di respiro fra un file e il successivo. Senza questo "
             "argomento la pausa e' proporzionale a quanto si e' lavorato e "
             "si allunga da sola se il chip rallenta (core/thermal.py); "
             "un numero esplicito diventa il minimo. 0 = nessuna pausa",
    )

    return p.parse_args()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    args = _parse_args()

    # Logging
    from core.config import LOGS_DIR
    import datetime
    log_file = LOGS_DIR / f"run_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
    _setup_logging(args.log_level, log_file)

    # Carica configurazione
    from core.config import config as cfg

    # Override da argomenti CLI
    if args.backend:
        cfg.asr.backend = args.backend
    if args.model:
        cfg.asr.model_id = args.model
    if args.language:
        cfg.asr.language = None if args.language == "auto" else args.language
    if args.max_hours:
        cfg.max_runtime_sec = int(args.max_hours * 3600)

    # --status
    if args.status:
        target = Path(args.input) if args.input else Path("output")
        _print_status(target)
        return 0

    # Raccoglie file da processare
    files = _collect_files(args, cfg)

    if not files:
        logger.warning(
            "Nessun file da processare. "
            "Metti file audio in input/ o specifica un percorso."
        )
        return 0

    logger.info("File da processare: %d", len(files))

    t_global = time.time()
    completed = 0
    failed = 0

    # Pausa di raffreddamento fra un file e l'altro. Il ciclo di notte lo
    # fa gia' sync_device; questo e' il ciclo manuale (`run.py input/ --all`
    # o un file alla volta), che e' quello con cui si esaminano i file di
    # prova: anche li, quattro ore di fila non vanno bene a nessuno.
    from core.config import thermal_policy
    from core.thermal import ThermalGovernor
    gov = ThermalGovernor(thermal_policy(args.cooldown_sec))

    for i, audio_path in enumerate(files):
        if i > 0 and gov.last_cooldown_sec > 0:
            # Si controlla il tempo una volta sola, prima di iniziare: se
            # e' gia' scaduto il budget della notte, la pausa non serve a
            # niente e si torna subito al giro che lo interrompe.
            if cfg.max_runtime_sec <= 0 or (
                time.time() - t_global + gov.last_cooldown_sec < cfg.max_runtime_sec
            ):
                gov.rest(should_stop=lambda: _shutdown_requested)

        if _shutdown_requested:
            logger.info("Shutdown: interrotto prima di %s", audio_path.name)
            break

        # Controllo timeout globale (per slot notturno)
        if cfg.max_runtime_sec > 0:
            elapsed_global = time.time() - t_global
            if elapsed_global >= cfg.max_runtime_sec:
                logger.info(
                    "Timeout raggiunto (%.1f ore) — stop ordinato.",
                    elapsed_global / 3600,
                )
                break

        logger.info("File %d/%d: %s", i + 1, len(files), audio_path.name)
        t_file = time.time()
        try:
            ok = process_file(audio_path, cfg, args)
            if ok:
                completed += 1
        except Exception as exc:
            logger.error("Errore su %s: %s", audio_path.name, exc, exc_info=True)
            failed += 1

        # Quanto ha davvero costato questo file, e quanto costa rispetto
        # ai precedenti: e' l'unico segnale di riscaldamento disponibile
        # senza permessi, e decide la prossima pausa.
        from core.media import probe_duration
        gov.note_work(time.time() - t_file, audio_sec=probe_duration(audio_path) or 0.0)

    elapsed_total = time.time() - t_global
    logger.info(
        "Sessione terminata: %d completati, %d falliti | Tempo totale: %.1f min",
        completed, failed, elapsed_total / 60,
    )

    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
