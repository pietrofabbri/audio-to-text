"""
Test del piano notturno: quanto lavoro entra in una finestra, e se la
coda tiene il passo con la produzione.

    python tests/test_nightly.py

Non carica modelli e non elabora nulla: qui si prova la *decisione*, cioe'
se la stima regge e se quello che il sistema promette corrisponde a
quello che fa.

Il punto non e' accademico. Con 18 file da un'ora al giorno e una finestra
notturna di poche ore, la domanda non e' "funziona?" ma "la coda cresce?".
Una risposta sbagliata qui significa un registratore che si riempie e
registrazioni che nessuno trascrive, senza che nulla fallisca.

I numeri usati qui (RTF, rapporto di parlato) sono quelli misurati, non
quelli che farebbero comodo: il test verifica che il piano sia coerente
con le costanti che il codice usa davvero.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(HERE))

import make_fake_device as fake  # noqa: E402


class Failure(Exception):
    pass


def require(cond: bool, msg: str) -> None:
    if not cond:
        raise Failure(msg)


# ---------------------------------------------------------------------------

def t_budget_fits_known_files(tmp: Path) -> None:
    """Il piano deve contare i file reali, non indovinare."""
    print("  budget e stima sul piano")
    import nightly

    dev = tmp / "untitled"
    made = fake.build(dev, 3, 0.5, __import__("datetime").datetime(2026, 10, 3, 21),
                      "Alice", -25.0, edge=False, name_pattern="REC_%Y%m%d_%H%M%S.mp3")
    require(len(made) == 3, f"device finto non creato: {len(made)}")

    plan = nightly._plan(argparse.Namespace(
        source=str(dev), limit=None, max_seconds=4 * 3600,
    ))
    require(plan["pending"] == 3, f"pending={plan['pending']}, attesi 3")
    require(plan["est_hours"] > 0, "est_hours non calcolata")
    require(plan["est_process_hours"] > 0, "est_process_hours non calcolata")
    print(f"    {plan['pending']} file, {plan['est_hours']:.2f} h audio, "
          f"{plan['est_process_hours']:.2f} h di elaborazione stimata")


def t_limit_is_after_sorting(tmp: Path) -> None:
    """--limit deve prendere i piu' vecchi, non i primi per nome."""
    print("  --limit prende la coda piu' vecchia")
    dev = tmp / "untitled"
    import nightly

    made = fake.build(dev, 3, 0.5, __import__("datetime").datetime(2026, 10, 3, 21),
                      "Alice", -25.0, edge=False, name_pattern="REC_%Y%m%d_%H%M%S.mp3")
    names = sorted(f.name for f in made)
    plan = nightly._plan(argparse.Namespace(
        source=str(dev), limit=1, max_seconds=4 * 3600,
    ))
    require(plan["files"] == names[:1],
            f"il limite non ha preso il file piu' vecchio: {plan['files']} "
            f"invece di {names[0]}")
    print(f"    con --limit 1 prende {plan['files'][0]}")


def t_queue_growth_is_visible(tmp: Path) -> None:
    """Con 18 file al giorno, quanto regge? Il numero va mostrato.

    Non è un test che "deve passare" o "deve fallire": è un test che
    deve mostrare il divario, cosi la decisione (cambiare modello,
    cambiare macchina, accettare il ritardo) si prende su un dato e non
    su una speranza. Se un giorno la situazione cambia, il numero
    cambia da solo.
    """
    print("  la coda regge con 18 file da 1h al giorno?")
    from core.cost import estimate_seconds

    NIGHT_SEC = 4 * 3600
    DAY_SEC = 40 * 60 * 3        # tre passate diurne da 40 minuti

    for ratio in (0.95, 0.80, 0.65, 0.50, 0.35):
        per_file = estimate_seconds(3600, 3600 * ratio)
        night = int(NIGHT_SEC // per_file)
        day = int(DAY_SEC // per_file)
        gap = 18 - (night + day)
        verdict = f"in pari ({night + day}/18)" if gap <= 0 else f"scopre {gap}/18"
        print(f"    parlato {ratio:.0%}: {per_file/60:4.1f} min/file -> "
              f"notte {night} + diurno {day} = {night + day:2d}/18  {verdict}")

    # Con il 65% di parlato (misurato sulle registrazioni vere) notte e
    # diurno insieme devono coprire i 18 file che arrivano ogni giorno:
    # è il motivo per cui il modello di costo esiste.
    per_file = estimate_seconds(3600, 3600 * 0.65)
    capacity = int((NIGHT_SEC + DAY_SEC) // per_file)
    require(capacity >= 18,
            f"con il 65% di parlato si coprono {capacity} file al giorno, "
            f"ne arrivano 18")


def t_dry_run_writes_nothing(tmp: Path) -> None:
    """Il piano a secco non deve toccare output, log o device."""
    print("  il piano a secco non scrive nulla")
    import os

    root = tmp / "root"
    dev = tmp / "untitled"
    root.mkdir(parents=True, exist_ok=True)
    fake.build(dev, 2, 0.5, __import__("datetime").datetime(2026, 10, 3, 21),
               "Alice", -25.0, edge=False, name_pattern="REC_%Y%m%d_%H%M%S.mp3")

    before = sorted((str(p.relative_to(dev)), p.stat().st_size)
                    for p in dev.rglob("*") if p.is_file())
    env = {**os.environ, "A2T_ROOT_DIR": str(root)}
    r = subprocess.run(
        [sys.executable, str(ROOT / "nightly.py"), "--dry-run",
         "--source", str(dev), "--no-publish"],
        capture_output=True, text=True, env=env, cwd=str(ROOT), timeout=600,
    )
    after = sorted((str(p.relative_to(dev)), p.stat().st_size)
                   for p in dev.rglob("*") if p.is_file())
    require(before == after, "il piano a secco ha toccato il device")
    out_dir = root / "output"
    created = list(out_dir.iterdir()) if out_dir.is_dir() else []
    require(not created, f"il piano a secco ha scritto output: {created}")
    print(f"    device e output intatti (exit {r.returncode})")


def t_i_passaggi_usano_l_interprete_di_adesso(tmp: Path) -> None:
    """I sotto-passi devono girare con l'ambiente che ha lanciato nightly.

    Il pericolo e' `python` scritto alla lettera: funziona finche' il
    primo `python` nel PATH e' quello giusto, e sotto launchd lo e' perche'
    il plist mette l'ambiente davanti. Lanciato a mano da una shell
    normale non lo e': l'import parte con un interprete che non ha
    faster-whisper, e l'unica traccia del fallimento era un codice di
    uscita.

    Qui il PATH e' sabotato apposta: `python` e' uno script che fallisce
    e grida. Se nightly usa l'interprete di adesso, il piano a secco
    passa lo stesso.
    """
    print("  i sotto-passi usano l'interprete che sta girando adesso")
    import os

    root = tmp / "root"
    dev = tmp / "untitled"
    root.mkdir(parents=True, exist_ok=True)
    fake.build(dev, 2, 0.5, __import__("datetime").datetime(2026, 10, 3, 21),
               "Alice", -25.0, edge=False, name_pattern="REC_%Y%m%d_%H%M%S.mp3")

    poison = tmp / "bin"
    poison.mkdir(parents=True, exist_ok=True)
    (poison / "python").write_text(
        "#!/bin/sh\necho PYTHON_SABOTATO \"$@\" >&2\nexit 9\n")
    (poison / "python").chmod(0o755)

    env = {**os.environ, "A2T_ROOT_DIR": str(root),
           "PATH": f"{poison}:{os.environ.get('PATH', '')}"}
    r = subprocess.run(
        [sys.executable, str(ROOT / "nightly.py"), "--dry-run",
         "--source", str(dev), "--no-publish"],
        capture_output=True, text=True, env=env, cwd=str(ROOT), timeout=600,
    )
    out = r.stdout + r.stderr
    require("PYTHON_SABOTATO" not in out,
            "un sotto-passo e' partito con il python del PATH, non con "
            f"l'ambiente di nightly:\n{out[-1500:]}")
    require("sync_device pull ha restituito" not in out,
            f"l'import e' fallito:\n{out[-1500:]}")
    require("ha restituito" not in out,
            f"qualche passo e' uscito non-zero:\n{out[-1500:]}")
    print(f"    python sabotato nel PATH, ciclo intatto (exit {r.returncode})")


def t_il_motivo_del_fallimento_arriva_nel_log(tmp: Path) -> None:
    """Un passo che fallisce deve dire perche', non solo che ha fallito.

    L'output dei sotto-passi e' catturato: senza scriverlo, il log del
    ciclo finisce con «pull ha restituito 1» e il motivo non e' da
    nessuna parte. Il giorno dopo la domanda «perche' non ha lavorato?»
    non ha risposta.
    """
    print("  il motivo di un fallimento finisce nel log")
    import logging
    import os

    import nightly

    righe: list[str] = []

    class _Cattura(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            righe.append(record.getMessage())

    h = _Cattura()
    h.setLevel(logging.WARNING)
    vecchio = logging.getLogger("nightly")
    livello = vecchio.level
    vecchio.setLevel(logging.WARNING)
    vecchio.addHandler(h)
    try:
        nightly._log_uscita_male("sync_device pull", 1, (
            "riga inutile 1\nriga inutile 2\n"
            "Traceback (most recent call last):\n"
            "ModuleNotFoundError: No module named 'faster_whisper'"
        ))

        testo = "\n".join(righe)
        require("ModuleNotFoundError" in testo,
                f"il motivo del fallimento non e' nel log:\n{testo}")
        require("ha restituito 1" in testo,
                f"il log non dice quale passo e' fallito:\n{testo}")

        # Il pull scrive una riga per chunk: scaricare tutto significherebbe
        # che il log della notte e' il log dell'ASR, e il motivo (l'ultima
        # riga) sparirebbe dentro il rumore.
        rumoroso = "\n".join(f"faster_whisper: chunk {i}" for i in range(5000))
        righe.clear()
        nightly._log_uscita_male("sync_device pull", 1, rumoroso + "\nERRORE_FINALE")
        testo = "\n".join(righe)
        require("ERRORE_FINALE" in testo,
                "la riga dell'errore non arriva nel log")
        righe_strappate = [r for r in righe if "chunk " in r]
        require(len(righe_strappate) <= 20,
                f"il log ha riversato {len(righe_strappate)} righe di rumore")

        # E se non c'e' niente da dire, si dice anche quello: un codice di
        # uscita senza una parola e' il caso piu' difficile da diagnosticare.
        righe.clear()
        nightly._log_uscita_male("sync_device pull", 1, "")
        require(any("non ha scritto nulla" in r for r in righe),
                "un fallimento senza output non lascia traccia")
    finally:
        vecchio.removeHandler(h)
        vecchio.setLevel(livello)
    print("    motivo presente, rumore tagliato, silenzio dichiarato")


def t_un_passo_che_fallisce_scrive_il_motivo(tmp: Path) -> None:
    """Il ramo di fallimento del ciclo deve scrivere il motivo, non solo il codice.

    Provare la funzione non basta: se il ramo non la chiama, il motivo
    resta nella variabile catturata da subprocess e muore li'. Qui si fa
    fallire l'import per davvero — `_run` restituisce l'errore che
    faster-whisper avrebbe dato — e si guarda cosa finisce nel log.

    Si cattura stdout e non un handler perche' `main()` chiama
    `logging.basicConfig(force=True)`: un handler agganciato a mano verrebbe
    staccato e il log finirebbe a schermo, dove il test non lo vede.
    """
    print("  un import che fallisce dice perche' nel log")
    import contextlib
    import io
    import os

    import nightly

    root = tmp / "root"
    dev = tmp / "untitled"
    root.mkdir(parents=True, exist_ok=True)
    fake.build(dev, 2, 0.5, __import__("datetime").datetime(2026, 10, 3, 21),
               "Alice", -25.0, edge=False, name_pattern="REC_%Y%m%d_%H%M%S.mp3")

    run_originale = nightly._run
    argv_originale = sys.argv
    root_originale = os.environ.get("A2T_ROOT_DIR")
    os.environ["A2T_ROOT_DIR"] = str(root)
    sys.argv = ["nightly.py", "--dry-run", "--source", str(dev), "--no-publish"]

    def _run_fallito(cmd, timeout=None):
        if "sync_device.py" in " ".join(cmd):
            return 1, ("faster_whisper: chunk 0\n"
                       "ModuleNotFoundError: No module named 'faster_whisper'")
        return run_originale(cmd, timeout=timeout)

    buf = io.StringIO()
    nightly._run = _run_fallito
    try:
        with contextlib.redirect_stdout(buf):
            nightly.main()
    finally:
        nightly._run = run_originale
        sys.argv = argv_originale
        if root_originale is None:
            os.environ.pop("A2T_ROOT_DIR", None)
        else:
            os.environ["A2T_ROOT_DIR"] = root_originale

    testo = buf.getvalue()
    require("ModuleNotFoundError" in testo,
            f"l'import e' fallito ma il log non dice perche':\n{testo}")
    require("sync_device pull" in testo,
            f"il log non dice quale passo e' fallito:\n{testo}")
    print("    il motivo di un import fallito finisce nel log")


def t_speech_ratio_learns_from_sessions(tmp: Path) -> None:
    """Se esistono sessioni, il rapporto di parlato smette di essere un'ipotesi."""
    print("  il rapporto di parlato si impara dalle sessioni fatte")
    import nightly

    root = tmp / "ratio-root"
    out = root / "output"
    sess = out / "2026-10-01_22-00-00"
    sess.mkdir(parents=True, exist_ok=True)
    # speech_ratio sta in transcript.json sotto "meta": e' da li' che la
    # stima notturna lo legge, quindi è li' che va scritto.
    (sess / "transcript.json").write_text(
        json.dumps({
            "meta": {"speech_ratio": 0.5, "duration_sec": 100.0},
            "segments": [{"text": "una frase", "start": 0, "end": 1}],
        }),
        encoding="utf-8",
    )

    import os
    os.environ["A2T_ROOT_DIR"] = str(root)
    for mod in ("core.config", "core.corpus_db", "core.speaker_db", "nightly"):
        sys.modules.pop(mod, None)
    import nightly as fresh  # noqa: F811

    ratio = fresh._measured_speech_ratio()
    require(ratio is not None, "nessun rapporto di parlato ricavato dalle sessioni")
    require(abs(ratio - 0.5) < 0.01, f"rapporto di parlato inatteso: {ratio}")
    print(f"    rapporto misurato: {ratio}")


# ---------------------------------------------------------------------------

def t_cost_model_matches_measurements(tmp: Path) -> None:
    """Il modello di costo deve riprodurre le misure reali.

    Non serve che preveda il futuro: serve che dica la verita' sul
    passato. Un modello che sbaglia del 30% sulle misure non merita
    fiducia sulle stime, per quanto ben ragionate siano le formule.
    """
    print("  il modello di costo riproduce le misure")
    from core.cost import estimate_seconds, estimate_rtf

    # (audio_s, rapporto parlato, secondi realmente misurati)
    measured = [
        (600, 0.96, 255),
        (600, 0.78, 229),
    ]
    for audio_s, ratio, real in measured:
        pred = estimate_seconds(audio_s, audio_s * ratio)
        err = abs(pred - real) / real
        require(err < 0.15,
                f"su {audio_s}s al {ratio:.0%}: predetto {pred:.0f}s, "
                f"reale {real}s (errore {err:.0%})")

    # Su un file da un'ora il rapporto deve essere piu' basso che su uno
    # da dieci minuti: i costi fissi si ammortizzano. E' la ragione per
    # cui una costante unica sbaglia.
    short = estimate_rtf(600, 600 * 0.8)
    hour = estimate_rtf(3600, 3600 * 0.8)
    require(hour < short,
            f"su un'ora il RTF dovrebbe scendere: {hour:.2f} vs {short:.2f}")
    require(0.2 < hour < 0.4,
            f"RTF su un'ora fuori dall'intervallo plausibile: {hour:.2f}")
    print(f"    misure entro il 15% | RTF 10 min {short:.2f} -> 1h {hour:.2f}")


def t_chunks_respect_the_clock_limit(tmp: Path) -> None:
    """Un chunk non puo' durare piu' del limite, silenzi compresi.

    Il chunker sommava le durate di PARLATO ma produceva uno span che
    includeva i silenzi: un chunk con 29 s di parole si diluiva su 188 s
    di clock e sembrava rispettare il limite senza rispettarlo.
    """
    print("  i chunk rispettano il limite sul tempo trascorso")
    from pipeline.vad import SpeechSegment, VoiceActivityDetector
    from core.config import config

    vad = VoiceActivityDetector.__new__(VoiceActivityDetector)
    # 30 segmenti da 1 s, distanziati di 2 s di silenzio: 30 s di
    # parlato sparsi su 90 s di clock. E' la situazione che faceva
    # traboccare i chunk: sommando solo il parlato, venti segmenti
    # entravano in un chunk che copriva 58 s di orologio.
    segs = [
        SpeechSegment(idx=i, start=i * 3.0, end=i * 3.0 + 1.0)
        for i in range(30)
    ]
    chunks = vad.split_into_chunks(segs, 29.0)

    # Il limite e' sul tempo TRASCORSO: start/end del chunk.
    for c in chunks:
        span = c["end"] - c["start"]
        require(span <= 29.01,
                f"chunk {c['idx']} copre {span:.1f}s di clock, oltre il limite")
    require(len(chunks) >= 3,
            f"su 90 s di clock servono almeno 3 chunk, trovati {len(chunks)}")

    # E nessun segmento di parlato puo' andare perso: la somma delle
    # durate dei chunk deve tornare con il parlato totale.
    covered = sum(c["duration"] for c in chunks)
    total = sum(s.duration for s in segs)
    require(abs(covered - total) < 0.01,
            f"parlato coperto {covered:.1f}s su {total:.1f}s: qualcosa e' perso")
    print(f"    {len(chunks)} chunk, span max "
          f"{max(c['end']-c['start'] for c in chunks):.1f}s, "
          f"parlato {covered:.0f}/{total:.0f}s")


def t_window_lives_in_one_place(tmp: Path) -> None:
    """La finestra notturna deve essere un numero solo.

    Prima stava in tre file e due non concordavano: lanciando
    nightly.py a mano si otteneva una notte di 3 ore, mentre il job
    launchd ne dava 4. Non rompeva niente — finche' un giorno ha
    significato "la coda non si chiude" in due sensi diversi a seconda
    di chi chiedeva.
    """
    print("  la finestra notturna e' un numero solo")
    import nightly
    import setup_launchd
    from core.config import NIGHT_START_HOUR, NIGHT_WINDOW_SEC

    require(nightly.DEFAULT_WINDOW_SEC == NIGHT_WINDOW_SEC,
            f"nightly: {nightly.DEFAULT_WINDOW_SEC}s, config: {NIGHT_WINDOW_SEC}s")
    require(setup_launchd.MAX_RUNTIME_SEC == NIGHT_WINDOW_SEC,
            f"launchd: {setup_launchd.MAX_RUNTIME_SEC}s, config: {NIGHT_WINDOW_SEC}s")
    require(setup_launchd.START_HOUR == NIGHT_START_HOUR,
            "l'ora di avvio non coincide")

    # E soprattutto: quello che il plist passa a nightly.py e' quello
    # che nightly.py usa se lo lanci a mano senza argomenti.
    argv = setup_launchd.build_plist()["ProgramArguments"]
    i = argv.index("--max-seconds")
    require(int(argv[i + 1]) == nightly.DEFAULT_WINDOW_SEC,
            f"il plist passa {argv[i+1]}s, nightly usa {nightly.DEFAULT_WINDOW_SEC}s")
    print(f"    {NIGHT_WINDOW_SEC // 3600}h dalle {NIGHT_START_HOUR:02d}:00, "
          f"un posto solo")


def t_thermal_budget_is_actually_passed_on(tmp: Path) -> None:
    """Le impostazioni che limitano il calore devono arrivare alla
    pipeline vera, non restare scritte in un plist che nessuno legge.

    Un `--threads` che non arriva a faster-whisper e' un numero che
    rassicura: la macchina si scalda lo stesso.
    """
    print("  il carico termico e' passato fino alla pipeline")
    import setup_launchd
    from core.config import (
        DAYTIME_BUDGET_SEC, DAYTIME_THREADS, NIGHT_THREADS,
    )

    night = setup_launchd.build_plist()["ProgramArguments"]
    day = setup_launchd.build_plist_daytime()["ProgramArguments"]

    for argv, threads, label in ((night, NIGHT_THREADS, "notte"),
                                 (day, DAYTIME_THREADS, "giorno")):
        require("--threads" in argv, f"{label}: nessun --threads nel plist")
        require(int(argv[argv.index("--threads") + 1]) == threads,
                f"{label}: thread nel plist diverso dalla config")
        require("--cooldown-sec" in argv, f"{label}: nessuna pausa di respiro")
        require(threads < 8,
                f"{label}: {threads} thread su 8 core, la macchina resta bollente")

    # Il worker della prosodia e' un secondo carico parallelo: conta
    # quanto ne mette insieme all'ASR.
    from core.config import config as cfg
    require(cfg.prosody.num_workers <= 2,
            f"prosodia con {cfg.prosody.num_workers} worker: insieme all'ASR "
            "fa più carico di quanto dichiarato")
    require(int(day[day.index("--max-seconds") + 1]) == DAYTIME_BUDGET_SEC,
            "il budget diurno nel plist non è quello della config")
    print(f"    notte {NIGHT_THREADS} thread, giorno {DAYTIME_THREADS} thread, "
          f"prosodia {cfg.prosody.num_workers} worker")


def t_thread_cap_is_measured_not_guessed(tmp: Path) -> None:
    """Il tetto di thread è una misura, non una scelta di buon senso.

    Su questa macchina 4 thread danno un ASR più VELOCE di 8: gli altri
    4 core sono efficiency e insieme ai primi fanno contesa, non lavoro.
    Il test non può ripetere la misura (ci vorrerebbero minuti di CPU),
    ma può impedire che il numero venga alzato "perché tanto la macchina
    è libera": è esattamente il cambiamento che peggiorerebbe tempo e
    temperatura insieme.
    """
    print("  il tetto di thread e' quello misurato")
    from core.config import (
        DAYTIME_THREADS, MAX_THREADS, NIGHT_THREADS, config as cfg,
    )

    require(NIGHT_THREADS <= MAX_THREADS,
            f"notte: {NIGHT_THREADS} thread oltre il tetto misurato {MAX_THREADS}")
    require(DAYTIME_THREADS <= MAX_THREADS,
            f"giorno: {DAYTIME_THREADS} thread oltre il tetto {MAX_THREADS}")

    # L'ASRConfig deve poter ricevere il limite dalla pipeline: un tetto
    # che non arriva dove si usa non è un tetto.
    import os
    os.environ["A2T_ASR_THREADS"] = str(NIGHT_THREADS)
    for mod in [m for m in sys.modules if m.split(".")[0] == "core"]:
        sys.modules.pop(mod, None)
    from core.config import ASRConfig  # noqa: PLC0415
    try:
        require(ASRConfig().cpu_threads == NIGHT_THREADS,
                "A2T_ASR_THREADS non arriva a faster-whisper")
    finally:
        os.environ.pop("A2T_ASR_THREADS", None)
        for mod in [m for m in sys.modules if m.split(".")[0] == "core"]:
            sys.modules.pop(mod, None)
    print(f"    tetto {MAX_THREADS} thread, notte {NIGHT_THREADS}, "
          f"giorno {DAYTIME_THREADS}, prosodia {cfg.prosody.num_workers}")


def t_una_pubblicazione_incompleta_viene_detettata(tmp: Path) -> None:
    """Il push che esce 0 non vuol dire che tutto sia pubblicato.

    Il caso reale: `tokens.jsonl` era nell'INDEX.md come formato
    dichiarato e non nella lista dei file da copiare. La notte passava,
    il push usciva 0, e il file — quello che rende il corpus
    interrogabile parola per parola, con KWIC e sincronizzazione al
    secondo — non era mai arrivato da nessuna parte. Nessuno se ne
    accorse perche' nessuno guardava: l'elenco dei formati diceva che
    c'era.

    Qui si verifica che il controllo notturno trovi il buco. Il file
    mancante viene tolto dalla copia pubblicata di una sessione, e ci si
    aspetta che venga detto per nome: un avviso generico o un exit 0
    farebbero passare la cosa due volte.
    """
    print("  una pubblicazione incompleta viene detta per nome")
    import nightly

    out = tmp / "output"
    repo = tmp / "corpus_repo" / "sessions"
    stem = "2026-10-04_14-43-16"
    src = out / stem
    dst = repo / stem
    src.mkdir(parents=True)
    dst.mkdir(parents=True)

    # Una sessione completa: transcript.json la rende completa, e i
    # file pubblicabili ci sono tutti.
    from publish_corpus import PUBLISHABLE
    (src / "transcript.json").write_text("{}", encoding="utf-8")
    (dst / "transcript.json").write_text("{}", encoding="utf-8")
    for nome in PUBLISHABLE:
        if nome == "transcript.json":
            continue
        (src / nome).write_text("x", encoding="utf-8")
        (dst / nome).write_text("x", encoding="utf-8")

    root_reale = nightly.ROOT
    try:
        nightly.ROOT = tmp
        require(nightly._sessioni_non_pubblicate() == {},
                "una sessione completa e tutta pubblicata non deve "
                f"essere segnalata: {nightly._sessioni_non_pubblicate()}")

        # Il buco vero: il file che porta i timestamp parola per parola
        # resta in output/ e non arriva nella repo.
        (dst / "tokens.jsonl").unlink()
        mancanti = nightly._sessioni_non_pubblicate()
        require(mancanti.get(stem) == ["tokens.jsonl"],
                f"il file mancante deve essere detto per nome, risulta {mancanti}")

        # Rimesso a posto: da qui in poi la sessione e' interamente
        # pubblicata e non deve piu' essere segnalata, altrimenti i casi
        # sotto misurerebbero il buco di prima e non quello che provano.
        (dst / "tokens.jsonl").write_text("x", encoding="utf-8")
        require(nightly._sessioni_non_pubblicate() == {},
                "rimesso il file, la sessione non deve piu' essere segnalata")

        # Un file che non si pubblica non e' un buco: altrimenti il
        # controllo urlerebbe sempre e diventarebbe rumore.
        (src / "nota_privata.txt").write_text("x", encoding="utf-8")
        require(nightly._sessioni_non_pubblicate() == {},
                "un file non pubblicabile non deve essere segnalato")
        (src / "nota_privata.txt").unlink()

        # Una sessione non finita non si controlla.
        (src / "2026-10-04_13-30-39").mkdir()
        (src / "2026-10-04_13-30-39" / "tokens.jsonl").write_text("x", encoding="utf-8")
        require(nightly._sessioni_non_pubblicate() == {},
                "una sessione senza transcript.json non va controllata")
    finally:
        nightly.ROOT = root_reale
    print(f"    {len(PUBLISHABLE)} formati controllati per sessione")


def t_una_matrice_delle_voci_mancante_viene_detettata(tmp: Path) -> None:
    """Senza la matrice, la repo non dice chi ha parlato.

    Il buco vero, diverso dagli altri: i file per sessione mancavano di
    uno, quindi il confronto file per file l'avrebbe trovato. La matrice
    e' un file solo per tutto il corpus e non sta in nessuna cartella di
    sessione, quindi nessun confronto per sessione la vede. Serve un
    controllo suo, o il buco si ripete.
    """
    print("  la matrice delle voci mancante viene detta")
    import nightly

    out = tmp / "output"
    repo = tmp / "corpus_repo" / "sessions"
    stem = "2026-10-04_14-43-16"
    src, dst = out / stem, repo / stem
    src.mkdir(parents=True)
    dst.mkdir(parents=True)

    from publish_corpus import PUBLISHABLE
    (src / "transcript.json").write_text("{}", encoding="utf-8")
    (dst / "transcript.json").write_text("{}", encoding="utf-8")
    for nome in PUBLISHABLE:
        if nome == "transcript.json":
            continue
        (src / nome).write_text("x", encoding="utf-8")
        (dst / nome).write_text("x", encoding="utf-8")

    # La matrice sta a livello di corpus, non dentro la sessione.
    matrice = tmp / "corpus_repo" / "voices" / "voice_matrix.json"
    matrice.parent.mkdir(parents=True)
    matrice.write_text("{}", encoding="utf-8")

    root_reale = nightly.ROOT
    try:
        nightly.ROOT = tmp
        # Tutte le sessioni complete e pubblicate: nessun buco per
        # sessione, eppure la matrice potrebbe mancare lo stesso.
        require(nightly._sessioni_non_pubblicate() == {},
                "le sessioni sono a posto, non devono essere segnalate")

        matrice.unlink()
        require(not (tmp / "corpus_repo" / "voices" / "voice_matrix.json").exists(),
                "la matrice non e' stata tolta")
        # Il controllo per sessione, da solo, non la vede: e' un file
        # che non sta in nessuna cartella di sessione.
        require(nightly._sessioni_non_pubblicate() == {},
                "il confronto per sessione non deve accorgersi della matrice")

        # Il controllo degli artefatti, invece, sì: ed è quello che
        # durante la notte mette a verbale il buco per nome.
        require(nightly._artefatti_mancanti() == ["voices/voice_matrix.json"],
                "la matrice mancante deve essere detta per nome")
    finally:
        nightly.ROOT = root_reale

    # Rimessa a posto, il controllo non deve più segnalare nulla: se
    # urlasse sempre, l'avviso diventa rumore che nessuno legge.
    root_reale = nightly.ROOT
    try:
        nightly.ROOT = tmp
        matrice.write_text("{}", encoding="utf-8")
        require(nightly._artefatti_mancanti() == [],
                "con la matrice a posto non deve essere segnalata")
    finally:
        nightly.ROOT = root_reale


def main() -> int:
    tests = [
        t_budget_fits_known_files,
        t_limit_is_after_sorting,
        t_queue_growth_is_visible,
        t_dry_run_writes_nothing,
        t_i_passaggi_usano_l_interprete_di_adesso,
        t_il_motivo_del_fallimento_arriva_nel_log,
        t_un_passo_che_fallisce_scrive_il_motivo,
        t_speech_ratio_learns_from_sessions,
        t_cost_model_matches_measurements,
        t_chunks_respect_the_clock_limit,
        t_window_lives_in_one_place,
        t_thermal_budget_is_actually_passed_on,
        t_thread_cap_is_measured_not_guessed,
        t_una_pubblicazione_incompleta_viene_detettata,
        t_una_matrice_delle_voci_mancante_viene_detettata,
    ]
    failed = 0
    for fn in tests:
        with tempfile.TemporaryDirectory() as d:
            try:
                fn(Path(d))
            except Failure as exc:
                print(f"  KO   {fn.__name__}: {exc}")
                failed += 1
            except Exception as exc:  # noqa: BLE001
                print(f"  ERRORE {fn.__name__}: {type(exc).__name__}: {exc}")
                failed += 1
        print()
    print(f"{len(tests) - failed}/{len(tests)} test superati")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())