#!/usr/bin/env python
"""
nightly.py — la notte, in un comando.

    scarico dal registratore (se collegato) → elaborazione della coda
    entro un budget di tempo → pubblicazione del corpus → voci da
    rivedere → bilancio

Dal 7 ottobre l'elaborazione legge dalla coda locale (`input/coda/`),
non dal registratore: il registratore si scarica in pochi minuti a ogni
inserimento (`sync_device.py scarica`, lanciato da launchd) e si stacca.
Se e' ancora collegato quando parte la notte, lo scarico si fa qui per
primo.

È questo che il job launchd esegue. Esiste perché i tre passi hanno
vincoli diversi e falliscono in modi diversi: l'import cancella file,
l'elaborazione può andare oltre il tempo disponibile, la pubblicazione
non deve mai bloccare nessuno dei due. Metterli in sequenza in un
unico punto rende l'ordine esplicito e il fallimento parziale visibile.

Il budget di tempo è la cosa più importante qui. Le misure reali su
M1 Pro danno circa 3,3x realtime per l'ASR: 18 file da un'ora sono
~8 ore di elaborazione, e la finestra notturna è di 3. Il sistema
non può finire tutto in una notte, e non è un errore: è il punto in
cui la coda deve poter avanzare di un pezzo alla volta, in ordine,
senza perdere niente. I file non elaborati restano in coda e
ripartono dalla stessa identica condizione la notte dopo.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from core.config import (  # noqa: E402
    NIGHT_COOLDOWN_SEC,
    NIGHT_NICE,
    NIGHT_THREADS,
    NIGHT_WINDOW_SEC,
    ROOT_DIR,
)
from core.cost import estimate_seconds  # noqa: E402

# Due radici distinte, per due scopi distinti. ROOT e' dove stanno gli
# script: non si sposta, perché è lì che devono essere eseguiti. ROOT_DIR
# è dove finiscono output, log e bilanci, e segue A2T_ROOT_DIR: è ciò che
# permette di provare il ciclo notturno completo senza scrivere sopra la
# produzione.
DATA_ROOT = ROOT_DIR

logger = logging.getLogger("nightly")

# Finestra notturna. Il numero vive in core/config.py, letto sia da questo
# file sia da setup_launchd.py: prima era scritto in due posti con due
# valori diversi, e lanciando nightly.py a mano la finestra era piu'
# corta di quella del job launchd — la stessa notte, due piani diversi.
DEFAULT_WINDOW_SEC = NIGHT_WINDOW_SEC

# Anche l'intensita' e' centralizzata: thread, priorita' e pausa di
# respiro fra un file e il successo, cosi' il job launchd e la passata
# manuale fanno lo stesso lavoro.
DEFAULT_THREADS = NIGHT_THREADS
DEFAULT_NICE = NIGHT_NICE
DEFAULT_COOLDOWN_SEC = NIGHT_COOLDOWN_SEC

# Il modello di costo vive in core/cost.py: separa i costi fissi per
# file dai costi per secondo di parlato, ed e' tarato sulle registrazioni
# vere (prevede entro l'1% della misura). Qui la media per secondo di
# audio serve solo come riferimento veloce: un file da un'ora con il
# 65% di parlato costa circa 16 minuti, cioe' 0,27.
#
# La costante che c'era prima, 0,76, veniva da novanta secondi di audio
# di prova e sbagliava di un fattore due sul materiale vero.
MEASURED_RTF = 0.27

# Quanti secondi di cpu si lasciano accendere insieme. Non e' una
# concessione alla macchina: 4 thread sono risultati PIU' veloci di 8
# sulla M1 Pro (0,26 contro 0,32 sul parlato), perche' gli altri 4 core
# sono efficiency e insieme ai primi non fanno lavoro, fanno contesa e
# consumo. Vedere la tabella in core/cost.py.


# L'elenco degli artefatti di corpus vive in `publish_corpus`: e' la stessa
# lista che usa `status`, e tenere due copie di un elenco che elenca i file
# da controllare significa che uno dei due prima o poi non viene aggiornato.
from publish_corpus import ARTEFATTI_CORPUS  # noqa: E402,F401


def _artefatti_mancanti() -> list[str]:
    """Artefatti di corpus che non sono arrivati nella repo."""
    return [rel for rel in ARTEFATTI_CORPUS
            if not (ROOT / "corpus_repo" / rel).exists()]


def _coda_dir() -> Path:
    from core.scarico import CODA_DIR
    # La coda segue DATA_ROOT, come output e log: un test che sposta la
    # radice non deve leggere la coda di produzione.
    return DATA_ROOT / CODA_DIR.relative_to(ROOT_DIR)


def _file_in_coda() -> list[Path]:
    from core.device import AUDIO_EXTENSIONS, MIN_AUDIO_BYTES
    coda = _coda_dir()
    if not coda.is_dir():
        return []
    return sorted(f for f in coda.rglob("*")
                  if f.is_file() and not f.name.startswith(".")
                  and f.suffix.lower() in AUDIO_EXTENSIONS
                  and f.stat().st_size >= MIN_AUDIO_BYTES)


def _scarica_se_collegato(dry_run: bool) -> None:
    """Se il registratore e' collegato, lo si scarica prima di tutto.

    Di solito l'ha gia' fatto launchd al momento dell'inserimento; questo
    copre il caso in cui il job all'inserimento non sia partito (Mac in
    stop, job non installato). `--no-elabora`: l'elaborazione la fa
    questo stesso ciclo, subito dopo.
    """
    cmd = [sys.executable, str(ROOT / "sync_device.py"), "scarica",
           "--auto", "--no-elabora"]
    if dry_run:
        cmd.append("--dry-run")
    code, out = _run(cmd)
    if code != 0:
        _log_uscita_male("sync_device scarica", code, out)
    elif out:
        righe = [r for r in out.splitlines() if "Scarico:" in r]
        if righe:
            logger.info(righe[-1].split("sync_device: ", 1)[-1])


def _publish_cmd() -> list[str]:
    """Il comando di pubblicazione, con i nomi solo se la config lo dice.

    `corpus_with_names` (core/config.py) e' la decisione D1 della ROADMAP.
    Sta in config e non in un argomento di nightly perche' launchd lancia
    sempre lo stesso comando: un interruttore che vive nel plist si
    dimentica, uno che vive in config si legge accanto al suo perche'.
    """
    from core.config import config
    cmd = [sys.executable, str(ROOT / "publish_corpus.py"), "push"]
    if config.corpus_with_names:
        cmd.append("--with-names")
    return cmd


def _correzione_cmd() -> list[str] | None:
    """Il comando di correzione del testo, o None se e' spento.

    E' un passo separato fra la trascrizione e la pubblicazione: cosi'
    le varianti `*.corrected.*` sono gia' nelle sessioni quando
    `publish_corpus` ricompone le giornate. Un fallimento (rete, chiave,
    quota) non ferma il giro: si pubblica il testo grezzo e la notte dopo
    `correct_text` riprende dai segmenti non ancora corretti.
    """
    from core.config import config
    if not config.correzione_notturna:
        return None
    cmd = [sys.executable, str(ROOT / "correct_text.py"), "--consent",
           "--sintetico", "--max-seconds", str(config.correzione_budget_sec)]
    for giorno in config.correzione_giorni_esclusi:
        cmd += ["--escludi-giorno", giorno]
    return cmd


def _voci_da_rivedere(dry_run: bool) -> dict | None:
    """Ultimo passo: promemoria delle voci nuove ed estratti da ascoltare.

    Gli estratti si tagliano adesso perche' l'audio originale resta
    nell'archivio solo 7 giorni: dopo, una voce non si puo' piu' sentire
    e quindi nemmeno nominare a ragion veduta. Il promemoria finisce in
    output/voci_da_rivedere.md, in locale: ha estratti di testo e nomi
    di file audio, e non va nel corpus.

    Non solleva: l'elaborazione della notte e' gia' finita e pubblicata,
    e un difetto qui non deve farla sembrare fallita.
    """
    if dry_run:
        logger.info("Voci da rivedere: saltato (--dry-run)")
        return None
    try:
        from core.config import config
        from core.speaker_db import SpeakerDB
        from core.voice_review import prepara_revisione_notturna

        db = SpeakerDB(config.speaker_id.db_path,
                       threshold=config.speaker_id.match_threshold)
        r = prepara_revisione_notturna(
            db, DATA_ROOT / "output" / "voci_da_rivedere.md",
            output_dir=DATA_ROOT / "output")
    except Exception as exc:  # noqa: BLE001
        logger.warning("Voci da rivedere non preparate: %s: %s",
                       type(exc).__name__, exc)
        return None
    if r["voci"]:
        logger.info(
            "Voci da rivedere: %d, %d estratti pronti in data/ascolto%s. "
            "Elenco: %s — oppure: python review_speakers.py nuove",
            r["voci"], r["estratti"],
            f" ({r['senza_audio']} senza audio originale)" if r["senza_audio"] else "",
            r["file"],
        )
    else:
        logger.info("Voci da rivedere: nessuna")
    return r


def _timeout_correzione() -> int:
    """Il budget della correzione, piu' un margine per l'ultima sessione.

    `correct_text` si ferma fra una sessione e l'altra, quindi puo'
    sforare il budget di una sessione intera (~200 segmenti): il timeout
    duro serve solo a non restare appesi a una rete morta.
    """
    from core.config import config
    return int(config.correzione_budget_sec) + 1800


def _run(cmd: list[str], timeout: int | None = None) -> tuple[int, str]:
    proc = subprocess.run(
        cmd, cwd=str(ROOT), capture_output=True, text=True,
        timeout=timeout,
    )
    return proc.returncode, (proc.stdout + proc.stderr).strip()


def _log_uscita_male(nome: str, code: int, out: str) -> None:
    """Scrive perche' un passo e' fallito, non solo che e' fallito.

    `subprocess.run` cattura l'output, quindi il messaggio d'errore
    vero — l'eccezione, il modulo che manca, la riga che spiega il
    guasto — esiste solo in quella variabile. Senza questo, un ciclo
    notturno finito con «pull ha restituito 1» non lascia nessuna
    traccia del motivo: il giorno dopo la domanda e' «perche' non ha
    lavorato?» e la risposta non esiste da nessuna parte.

    Non si stampa tutto: il pull emette una riga per chunk trascritto e
    possono essere centinaia. Bastano le ultime righe, che sono dove
    sta l'errore.
    """
    logger.warning("%s ha restituito %d", nome, code)
    if not out:
        logger.warning("%s non ha scritto nulla: nessun output da guardare", nome)
        return
    for riga in out.splitlines()[-20:]:
        logger.warning("  | %s", riga)


def _sessioni_non_pubblicate() -> dict[str, list[str]]:
    """Quali sessioni complete non sono arrivate nella loro giornata.

    Dal 7 ottobre la repo e' per giorno: una sessione e' pubblicata se il
    manifesto della sua giornata (`giorni/<giorno>/giorno.json`) la elenca
    e la giornata ha tutti i suoi file. Il controllo esiste perche' il
    push puo' uscire 0 senza aver pubblicato tutto — `tokens.jsonl` era
    rimasto fuori per mesi cosi'.

    Restituisce {sessione: [cosa manca]}, vuoto se tutto e' a posto.
    """
    from core.giorno import FILE_GIORNO, inizio_sessione

    output_dir = ROOT / "output"
    giorni_dir = ROOT / "corpus_repo" / "giorni"
    if not output_dir.is_dir() or not giorni_dir.is_dir():
        return {}
    elencate: dict[str, set[str]] = {}
    for d in giorni_dir.iterdir():
        try:
            m = json.loads((d / "giorno.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        elencate[d.name] = {x.get("stem") for x in m.get("sessions") or []}
    dove = {stem: g for g, stems in elencate.items() for stem in stems}
    out: dict[str, list[str]] = {}
    for d in sorted(x for x in output_dir.iterdir()
                    if x.is_dir() and not x.name.startswith(".")):
        if not (d / "transcript.json").exists():
            continue
        giorno = dove.get(d.name)
        if giorno is None:
            inizio = inizio_sessione(d)
            atteso = inizio.strftime("%Y-%m-%d") if inizio else "?"
            out[d.name] = [f"giorni/{atteso}/giorno.json non la elenca"]
            continue
        mancanti = [n for n in FILE_GIORNO
                    if not (giorni_dir / giorno / n).exists()]
        if mancanti:
            out[d.name] = [f"giorni/{giorno}/{n}" for n in mancanti]
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="Ciclo notturno completo")
    ap.add_argument(
        "--max-seconds", type=int, default=DEFAULT_WINDOW_SEC,
        help=f"budget di elaborazione (default {DEFAULT_WINDOW_SEC//3600}h)",
    )
    ap.add_argument("--source", help="percorso del device (default: rilevamento)")
    ap.add_argument("--limit", type=int, help="massimo file da prendere")
    ap.add_argument(
        "--threads", type=int, default=DEFAULT_THREADS,
        help=f"thread CPU per l'elaborazione (default {DEFAULT_THREADS}: "
             "non tutto il processore, per non scaldare la macchina)",
    )
    ap.add_argument(
        "--nice", type=int, default=0,
        help="abbassa la priorita' del processo (0 = lascia stare launchd)",
    )
    ap.add_argument(
        "--cooldown-sec", type=float, default=0.0,
        help=f"pausa di respiro fra un file e il successivo in secondi "
             f"(0 = usa il default di {DEFAULT_COOLDOWN_SEC})",
    )
    ap.add_argument(
        "--no-publish", action="store_true",
        help="non pubblicare il corpus (utile per un test a secco)",
    )
    ap.add_argument(
        "--dry-run", action="store_true",
        help="mostra il piano senza elaborare né cancellare",
    )
    args = ap.parse_args()

    if args.threads and args.threads > 0:
        # Vale per faster-whisper (CTranslate2) e per i worker della
        # prosodia: limitarli qui evita che una passata si appropri dei
        # core tutti e lasci la macchina bollente per il resto della
        # giornata. Non e' una prestazione che si ottiene per free — si
        # paga in tempo di elaborazione, e il tempo si misura in code
        # che avanzano piu' lentamente.
        os.environ["OMP_NUM_THREADS"] = str(args.threads)
        os.environ["CT2_NUM_THREADS"] = str(args.threads)
        logger.info("Thread CPU limitati a %d", args.threads)

    # La priorita' la si abbassa anche quando si lancia a mano: il
    # termico non deve dipendere da chi ha lanciato il comando.
    nice_target = args.nice or DEFAULT_NICE
    if nice_target > 0:
        try:
            os.nice(nice_target)
            logger.info("Priorita' abbassata di %d (nice)", nice_target)
        except (OSError, PermissionError) as exc:
            # nice() su un processo non alleviato e' un errore, non un
            # motivo per non elaborare nulla.
            logger.debug("nice(%d) non applicato: %s", nice_target, exc)

    cooldown = args.cooldown_sec if args.cooldown_sec > 0 else DEFAULT_COOLDOWN_SEC

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[logging.StreamHandler(sys.stdout)],
        force=True,
    )

    started = time.time()
    started_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    logger.info("=" * 60)
    logger.info("Ciclo notturno — budget %s", _hms(args.max_seconds))
    logger.info("=" * 60)

    # ------------------------------------------------------------------
    # 0. Il registratore, se e' collegato, si scarica in coda per primo
    # ------------------------------------------------------------------
    if not args.source:
        _scarica_se_collegato(args.dry_run)

    # ------------------------------------------------------------------
    # 1. Stato di partenza: cosa c'è da fare e cosa manca
    # ------------------------------------------------------------------
    plan = _plan(args)
    logger.info("Dispositivo: %s", plan.get("device") or "non trovato")
    logger.info("File in attesa: %d", plan.get("pending", 0))
    if plan.get("est_hours"):
        logger.info(
            "Stima: %.1f ore di audio grezza, ~%.1f ore di elaborazione",
            plan["est_hours"], plan["est_process_hours"],
        )
    if plan.get("speech_ratio") is not None:
        logger.info(
            "Parlato effettivo stimato: %.0f%% delle registrazioni già "
            "elaborate (l'ASR lavora sul parlato, non sui silenzi: è questa "
            "frazione che decide se la coda cresce)",
            100 * plan["speech_ratio"],
        )
    if plan.get("fits"):
        logger.info(
            "Nella finestra entrano ~%d file; gli altri aspettano la "
            "prossima passata", plan["fits"],
        )

    # ------------------------------------------------------------------
    # 2. Import ed elaborazione
    # ------------------------------------------------------------------
    # L'interprete e' quello che sta girando adesso, non il primo
    # `python` che capita nel PATH: l'import e la pubblicazione
    # dipendono da faster-whisper, numpy e torch, che stanno
    # nell'ambiente che ha lanciato nightly. Sotto launchd le due cose
    # coincidevano perche' il plist mette l'ambiente davanti nel PATH,
    # quindi l'errore non si e' mai visto lanciandolo a mano: l'import
    # partiva con l'interprete sbagliato, falliva dopo minuti di
    # apparentemente lavoro, e il ciclo notturno registrava solo un
    # codice di uscita. Fuori da launchd l'ambiente non e' nel PATH e
    # il fallimento e' immediato ma silenzioso.
    pull = [sys.executable, str(ROOT / "sync_device.py"), "pull",
            "--max-seconds", str(args.max_seconds),
            "--source", args.source or str(_coda_dir())]
    if args.limit:
        pull += ["--limit", str(args.limit)]
    if args.dry_run:
        pull.append("--dry-run")
    # La pausa di respiro la applica sync_device, che e' dove i file
    # vengono presi uno alla volta: qui non ha un file fra le mani.
    os.environ["A2T_COOLDOWN_SEC"] = str(cooldown)
    if cooldown > 0:
        pull += ["--cooldown-sec", str(cooldown)]

    logger.info("--- 1/4 elaborazione della coda ---")
    if not args.source and not _file_in_coda():
        logger.info("Coda vuota: niente da trascrivere")
        code_pull, out_pull = 0, ""
    else:
        code_pull, out_pull = _run(pull)
    if code_pull != 0:
        # L'import può uscire non-zero per un file fallito, non per un
        # errore generale: si prosegue comunque a pubblicare quello che
        # è stato prodotto. Il motivo del fallimento va scritto,
        # altrimenti il ciclo finisce senza lasciare traccia.
        _log_uscita_male("sync_device pull", code_pull, out_pull)

    # ------------------------------------------------------------------
    # 3. Pubblicazione del corpus
    # ------------------------------------------------------------------
    # ------------------------------------------------------------------
    # 2b. Correzione del testo (Gemini), se accesa in config
    # ------------------------------------------------------------------
    logger.info("--- 2/4 correzione del testo ---")
    cmd_corr = _correzione_cmd()
    if cmd_corr is None:
        logger.info("Spenta (correzione_notturna = False in core/config.py)")
    elif args.dry_run:
        logger.info("Saltata (--dry-run): %s", " ".join(cmd_corr[1:]))
    else:
        try:
            code_corr, out_corr = _run(cmd_corr, timeout=_timeout_correzione())
        except subprocess.TimeoutExpired:
            # Il lavoro gia' fatto e' salvato sessione per sessione: si
            # pubblica quello che c'e' e si riprende la notte dopo.
            code_corr, out_corr = 1, "timeout della correzione"
        if code_corr != 0:
            _log_uscita_male("correct_text", code_corr, out_corr)
        else:
            for riga in out_corr.splitlines():
                if "parole corrette" in riga or "Tempo esaurito" in riga:
                    logger.info(riga.strip())

    logger.info("--- 3/4 pubblicazione del corpus ---")
    if args.no_publish:
        logger.info("Saltata (--no-publish)")
    else:
        code_pub, out_pub = _run(_publish_cmd())
        if code_pub != 0:
            _log_uscita_male("publish_corpus push", code_pub, out_pub)
        else:
            logger.info("Pubblicazione: %s", out_pub.splitlines()[-1] if out_pub else "ok")
        # Il push puo' uscire 0 senza aver pubblicato tutto: per esempio
        # se un file di una sessione non e' nell'elenco dei pubblicabili,
        # e' successo. Il codice di uscita non racconta tutto, quindi
        # dopo si confronta quello che c'e' in output/ con quello che c'e'
        # nella repo e quello che manca viene detto per nome.
        for nome, mancanti in _sessioni_non_pubblicate().items():
            logger.warning(
                "Pubblicazione incompleta: %s non ha nella repo %s",
                nome, ", ".join(mancanti),
            )
        for artefatto in _artefatti_mancanti():
            logger.warning(
                "Pubblicazione incompleta: %s non e' nella repo", artefatto,
            )

    # ------------------------------------------------------------------
    # 4. Voci da rivedere
    # ------------------------------------------------------------------
    logger.info("--- 4/4 voci da rivedere ---")
    _voci_da_rivedere(args.dry_run)

    # ------------------------------------------------------------------
    # 5. Bilancio
    # ------------------------------------------------------------------
    elapsed = time.time() - started
    leftover = _leftover()
    logger.info("=" * 60)
    logger.info("Ciclo terminato in %s", _hms(elapsed))
    if leftover:
        logger.info(
            "Restano %d file da trascrivere: verranno presi alla prossima "
            "passata, dal più vecchio", leftover,
        )
    else:
        logger.info("Coda vuota: niente rimane da elaborare")
    logger.info("=" * 60)

    _write_report(started_iso, elapsed, plan, leftover, code_pull)
    return 0


def _hms(seconds: float) -> str:
    m, s = divmod(int(seconds), 60)
    return f"{m // 60}h {m % 60:02d}m" if m >= 60 else f"{m}m {s:02d}s"


def _plan(args) -> dict:
    """Stima il lavoro della notte prima di iniziare a farlo."""
    sys.path.insert(0, str(ROOT))
    from core.device import AUDIO_EXTENSIONS, parse_recording_time

    files: list[Path] = []
    device = None
    if not args.source:
        # Il percorso normale: la coda locale, gia' riempita dallo scarico.
        files = _file_in_coda()
        device = f"coda locale ({_coda_dir()})"
    else:
        src = Path(args.source)
        device = str(src)
        if src.is_dir():
            files = [f for f in sorted(src.rglob("*"))
                     if f.is_file() and f.suffix.lower() in AUDIO_EXTENSIONS]

    # I file già elaborati non si contano: il ciclo notturno è
    # incrementale, non ripete la coda da capo ogni sera.
    done = {d.name for d in (DATA_ROOT / "output").iterdir()
            if d.is_dir() and (d / "transcript.json").exists()} \
        if (DATA_ROOT / "output").is_dir() else set()

    pending = []
    for f in files:
        dt, _ = parse_recording_time(f.name)
        stem = dt.strftime("%Y-%m-%d_%H-%M-%S") if dt else None
        if stem and stem in done:
            continue
        pending.append(f)
    pending.sort(key=lambda f: f.name)   # ordine cronologico dal nome

    if args.limit:
        pending = pending[:args.limit]

    durations = [(_duration_sec(f) or 0.0) for f in pending]  # gia" in secondi
    est_hours = sum(durations) / 3600

    # Il costo non e' una costante per secondo di audio: i costi fissi
    # (caricamento modelli, campione denoise) si pagano una volta per
    # file, e l'ASR paga solo sul parlato. La frazione di parlato si
    # misura sulle sessioni gia' elaborate; senza dati si assume il
    # 100%, che e' la stima peggiore e quindi quella giusta quando non
    # si sa.
    #
    # Il modello in core/cost.py e' tarato sulle registrazioni vere e
    # prevede il tempo di elaborazione entro l'1% della misura.
    ratio = _measured_speech_ratio()
    est_process_sec = sum(
        estimate_seconds(a, a * ratio if ratio else None) for a in durations
    )
    est_process_hours = est_process_sec / 3600
    budget_h = args.max_seconds / 3600
    per_file_h = estimate_seconds(3600, 3600 * ratio if ratio else None) / 3600

    return {
        "device": device,
        "pending": len(pending),
        "files": [f.name for f in pending],
        "est_hours": round(est_hours, 2),
        "est_process_hours": round(est_process_hours, 2),
        "speech_ratio": round(ratio, 3) if ratio else None,
        "fits": int(budget_h / (per_file_h * 1.15)) if est_hours and per_file_h else 0,
        "per_file_hours": round(per_file_h, 3),
        "budget_hours": round(budget_h, 2),
    }


def _measured_speech_ratio() -> float | None:
    """Frazione di parlato media delle sessioni gia' elaborate.

    Nessun dato -> None, e la stima resta quella conservativa sul
    campione peggiore invece di indovinare una frazione ottimistica.
    """
    out_dir = DATA_ROOT / "output"
    if not out_dir.is_dir():
        return None
    ratios = []
    for d in out_dir.iterdir():
        p = d / "transcript.json"
        if not p.is_dir() and p.exists():
            try:
                m = json.loads(p.read_text(encoding="utf-8")).get("meta", {})
            except (json.JSONDecodeError, OSError):
                continue
            r = m.get("speech_ratio")
            if isinstance(r, (int, float)) and 0 < r <= 1:
                ratios.append(float(r))
    return (sum(ratios) / len(ratios)) if ratios else None


def _duration_sec(path: Path) -> float | None:
    """Durata in secondi via ffprobe: legge i metadati senza decodificare."""
    import shutil
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        return None
    try:
        out = subprocess.run(
            [ffprobe, "-v", "quiet", "-show_entries", "format=duration",
             "-of", "csv=p=0", str(path)],
            capture_output=True, text=True, timeout=60,
        ).stdout.strip()
        return float(out)
    except (ValueError, OSError, subprocess.SubprocessError):
        return None


def _leftover() -> int | None:
    """Quanti file aspettano ancora: in coda e, se collegato, sul registratore.

    È il numero che dice se la coda avanza o è ferma. Dal 7 ottobre il
    grosso sta nella coda locale; il registratore conta solo se e' rimasto
    collegato con file non ancora scaricati.
    """
    from core.device import AUDIO_EXTENSIONS, discover, pick_recorder, parse_recording_time
    in_coda = len(_file_in_coda())
    vol = pick_recorder(discover())
    if not vol:
        return in_coda
    done = {d.name for d in (DATA_ROOT / "output").iterdir()
            if d.is_dir() and (d / "transcript.json").exists()} \
        if (DATA_ROOT / "output").is_dir() else set()
    n = 0
    for f in vol.audio_files:
        dt, _ = parse_recording_time(f.name)
        stem = dt.strftime("%Y-%m-%d_%H-%M-%S") if dt else None
        if stem and stem in done:
            continue
        n += 1
    return n + in_coda


def _write_report(started: str, elapsed: float, plan: dict,
                  leftover: int | None, pull_code: int) -> None:
    """Bilancio su disco: una riga per notte, confrontabile nel tempo.
    È il primo segnale di se la coda tiene il passo con la produzione."""
    path = DATA_ROOT / "logs" / "nightly_runs.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "started_at": started,
        "elapsed_sec": round(elapsed, 1),
        "device": plan.get("device"),
        "pending_at_start": plan.get("pending", 0),
        "est_hours": plan.get("est_hours"),
        "est_process_hours": plan.get("est_process_hours"),
        "speech_ratio": plan.get("speech_ratio"),
        "left_on_device": leftover,
        "pull_exit": pull_code,
    }
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


if __name__ == "__main__":
    raise SystemExit(main())
