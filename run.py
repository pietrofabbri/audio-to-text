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
# Pipeline per singolo file
# ---------------------------------------------------------------------------

def process_file(audio_path: Path, cfg, args) -> bool:
    """
    Esegue la pipeline completa su un singolo file audio.
    Restituisce True se completato, False se saltato o fallito.
    """
    from core.checkpoint import Checkpoint
    from core.config import OUTPUT_DIR, LOGS_DIR
    from pipeline.vad import VoiceActivityDetector
    from pipeline.transcriber import Transcriber
    from pipeline.diarizer import Diarizer
    from pipeline.prosody import ProsodyAnalyzer
    from pipeline.assembler import Assembler

    output_dir = OUTPUT_DIR / audio_path.stem
    ck = Checkpoint(audio_path, OUTPUT_DIR)

    # Salta se già completato
    if cfg.skip_completed and ck.all_done():
        logger.info("Già completato, saltato: %s", audio_path.name)
        return False

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
    # Stadio 3: Diarizzazione speaker
    # ------------------------------------------------------------------
    if not args.no_diarization and not ck.stage_done("diarization"):
        logger.info("Stadio 3/5: Diarizzazione speaker [pyannote]")
        diarizer = Diarizer(cfg.diarization)
        diar_segments = diarizer.diarize(wav_path)

        # Arricchisci ASR con speaker label
        asr_chunks = diarizer.assign_speakers_word_level(asr_chunks, diar_segments)

        ck.save_diarization(diar_segments)
        # Aggiorna i chunk con i speaker nel checkpoint
        ck._data["chunks"] = asr_chunks
        ck.save()
    else:
        if args.no_diarization:
            logger.info("Stadio 3/5: Diarizzazione disabilitata (--no-diarization)")
            diar_segments = []
        else:
            logger.info("Stadio 3/5: Diarizzazione già completata, carico da checkpoint")
            diar_segments = ck.get_diarization()
            asr_chunks = ck.get_all_chunks()

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
        )
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

    return True


# ---------------------------------------------------------------------------
# Raccolta file da processare
# ---------------------------------------------------------------------------

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

    for i, audio_path in enumerate(files):
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
        try:
            ok = process_file(audio_path, cfg, args)
            if ok:
                completed += 1
        except Exception as exc:
            logger.error("Errore su %s: %s", audio_path.name, exc, exc_info=True)
            failed += 1

    elapsed_total = time.time() - t_global
    logger.info(
        "Sessione terminata: %d completati, %d falliti | Tempo totale: %.1f min",
        completed, failed, elapsed_total / 60,
    )

    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
