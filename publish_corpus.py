#!/usr/bin/env python
"""
publish_corpus.py — pubblica il materiale testuale sulla repo privata.

Cosa va sulla repo e cosa no. La regola è semplice e non negoziabile:
**la repo contiene il materiale che un LLM deve potere leggere, e nient'altro.**

Va:
  - transcript, segmenti, token, frequenze, markdown di analisi
  - un indice per giorno, con i link ai file
  - le analisi giornaliere in chiaro

Non va, mai:
  - audio di qualsiasi tipo
  - embedding vocali (identificatori biometrici)
  - i database locali
  - i log grezzi

I nomi reali dei parlanti esistono, e stanno in `transcript.json` e
`session.json` in locale: è una scelta, non un limite. Di default non
vengono pubblicati, e `push --with-names` serve per pubblicarli
volutamente.

Il perché della protezione è il motivo per cui la repo esiste: un corpus
di voci e comportamento di una persona è un profilo, e un profilo
ricostruibile in un colpo da testo, prosodia e statistiche parlarie è un
rischio di re-identificazione che nessun singolo file rivela da solo.
Tenendo i nomi fuori dalla repo, un accesso alla repo non dà l'identità.

Il `--with-names` esiste perché la protezione di default e la comodità
sono in tensione: i nomi rendono il corpus leggibile voce per voce, e una
repo privata è già, per definizione, sotto il controllo di una sola
persona. Il default resta quello che non espone nulla; accettare
l'esposizione deve essere una decisione presa ogni volta, non una
impostazione dimenticata.

Dal 7 ottobre la repo e' organizzata **per giorno**: `giorni/AAAA-MM-GG/`
con un file per tipo che contiene tutte le sessioni del giorno in fila,
con l'ora vera (core/giorno.py). La vecchia cartella `sessions/` viene
migrata al primo `push`: le sessioni che stanno solo sulla repo vengono
prima riportate in `output/`, cosi' nessuna si perde.

    python publish_corpus.py init      # clona la repo privata in locale
    python publish_corpus.py push      # pubblica le sessioni nuove
    python publish_corpus.py push --with-names   # pubblica anche i nomi
    python publish_corpus.py status    # cosa c'è dentro, cosa manca
"""

from __future__ import annotations

import argparse
import filecmp
import json
import logging
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from core.config import OUTPUT_DIR  # noqa: E402

logger = logging.getLogger("publish")

# Se True, i nomi reali dei parlanti vengono pubblicati. Non è un
# dettaglio: cambia chi può dare un nome alle voci di un corpus. Default
# False — vedere il docstring in cima.
keep_names = False

REPO_SLUG = "pietrofabbri/corpus"
LOCAL_CLONE = ROOT / "corpus_repo"
# Il DB delle voci sta qui e non dentro `core.config` perche' i controlli
# che lo leggono devono poter girare su un albero finto insieme
# all'output: con `OUTPUT_DIR` finto e il database vero, il confronto
# fra le due cose non vuole niente. E' la stessa leva di `LOCAL_CLONE`.
SPEAKERS_DB = ROOT / "data" / "speakers_db.json"

# Fino al 7 ottobre si pubblicava una cartella per sessione con 13 file
# (PUBLISHABLE). Ora si pubblica una cartella per giorno: l'elenco dei
# file di una giornata sta in core/giorno.py (FILE_GIORNO,
# FILE_GIORNO_CORRETTI). Due lezioni di quell'elenco restano valide:
# `tokens.jsonl` e' il file che rende il corpus interrogabile parola per
# parola, ed era rimasto fuori per mesi perche' l'indice lo dichiarava
# senza che nessuno lo copiasse; e un formato dichiarato va controllato
# contro quello che c'e' davvero (lo fa nightly._sessioni_non_pubblicate).

# Artefatti che stanno a livello di corpus e non dentro una cartella di
# sessione. Il confronto file per file non li vede per costruzione, quindi
# hanno bisogno di un controllo proprio: e' cosi' che `tokens.jsonl` e la
# matrice delle voci sono rimasti fuori senza che nessuno se ne accorgesse.
ARTEFATTI_CORPUS = ("voices/voice_matrix.json",)

# La repo per giorno (dal 7 ottobre). `sessions/` e' la struttura di prima,
# che il primo push migra e toglie.
GIORNI = "giorni"
SESSIONI_VECCHIE = "sessions"

# File che non devono MAI essere copiati, per nome. La lista è volutamente
# conservativa: più è restrittiva, meglio è.
FORBIDDEN = (
    "speakers_db.json", "corpus.db", "checkpoint.json", ".wav", ".mp3", ".m4a",
)

# Scritto nel clone prima di `git add -A`. Serve perche' il clone e' una
# cartella che l'utente puo' aprire nel Finder, e il Finder ci lascia
# dentro `.DS_Store`: `git add -A` mette in stage tutto quello che trova,
# e cosi' la spazzatura del desktop finisce sulla repo pubblicata. Non
# e' un file che la pubblicazione scrive — e' entrato da un'altra parte,
# ed e' per questo che la lista dei vietati non lo intercettava.
GITIGNORE = """\
# Scritto da publish_corpus.py. La repo pubblicata contiene solo i
# file del corpus: questo clone e' una working copy come un'altra.
.DS_Store
*.swp
*~
"""

# Chiavi da rimuovere o sostituire prima di pubblicare: sono gli unici
# punti in cui può comparire un nome reale.
def _scrub(obj):
    """Sostituisce ogni nome reale con lo pseudonimo, ricorsivamente.

    Non ci fidiamo del fatto che oggi i nomi non ci siano: se domani
    assegni un nome a una voce, non deve finire qui per sbaglio.

    Con `keep_names=True` non viene toccato nulla. E' quello che fa
    `push --with-names`, e la differenza è una riga: per questo il
    default resta lo scrubbing, e la scelta va ripetuta a ogni push.
    """
    if keep_names:
        return obj
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if k in ("speaker_names", "names"):
                out[k] = {}
                continue
            if k in ("name", "speaker_name"):
                out[k] = None
                continue
            out[k] = _scrub(v)
        return out
    if isinstance(obj, list):
        return [_scrub(x) for x in obj]
    return obj


def _run(cmd: list[str], cwd: Path | None = None) -> tuple[int, str]:
    proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=120)
    return proc.returncode, (proc.stdout + proc.stderr).strip()


def cmd_init(args) -> int:
    if LOCAL_CLONE.exists():
        print(f"Clone già presente: {LOCAL_CLONE}")
        return 0
    print(f"Clono {REPO_SLUG} in {LOCAL_CLONE}...")
    code, out = _run(["gh", "repo", "clone", REPO_SLUG, str(LOCAL_CLONE)])
    if code != 0:
        print("Clonazione fallita:\n" + out, file=sys.stderr)
        return 1
    # Il token di gh è già autenticato per l'utente: si usa per lo push
    # senza dover mettere credenziali in chiaro nel clone.
    _run(["gh", "auth", "setup-git"])
    print("Fatto. La repo è pronta per ricevere il corpus.")
    return 0


def _gia_uguale(target: Path, content: str | None = None,
                sorgente: Path | None = None) -> bool:
    """Il file sulla repo è già identico a quello che scriverei?

    Confronto i byte: un file assente è sempre diverso. Per i JSON il
    confronto è sul testo che andrebbe scritto (non sul sorgente, che è
    lo stesso identico su disco e su repo, ma senza lo scrub — ed è lo
    scrub a poter cambiare il contenuto).
    """
    if not target.exists():
        return False
    try:
        if content is not None:
            return target.read_text(encoding="utf-8") == content
        assert sorgente is not None
        return filecmp.cmp(target, sorgente, shallow=False)
    except OSError:
        # Illeggibile: meglio riscriverlo che fingere che sia a posto.
        return False

def _cartelle_sessioni() -> list[Path]:
    """Le sessioni locali complete: un output a meta' non si pubblica."""
    if not OUTPUT_DIR.is_dir():
        return []
    return sorted(d for d in OUTPUT_DIR.iterdir()
                  if d.is_dir() and not d.name.startswith(".")
                  and (d / "transcript.json").exists())


def _nomi_da_pubblicare() -> dict[str, str]:
    """I nomi da mettere nelle giornate: nessuno, salvo `--with-names`."""
    if not keep_names:
        return {}
    try:
        from core.speaker_db import SpeakerDB
        from core.speaker_sync import names_from_db
        return dict(names_from_db(SpeakerDB(SPEAKERS_DB)))
    except Exception as exc:  # noqa: BLE001
        logger.warning("Nomi non disponibili, pubblico gli pseudonimi: %s", exc)
        return {}


def _giornate():
    from core.giorno import giornate
    return giornate(_cartelle_sessioni(), nomi=_nomi_da_pubblicare())


def _publish_day(g, dry_run: bool = False) -> list[Path]:
    """Scrive i file di una giornata; restituisce solo quelli cambiati.

    Il confronto e' sul contenuto che andrebbe scritto, come per le
    sessioni prima: una giornata identica sulla repo non si conta, e il
    messaggio di commit non racconta pubblicazioni che non ci sono state.
    Un file che la giornata non produce piu' (per esempio il testo
    corretto ritirato) viene tolto.
    """
    from core.giorno import FILE_GIORNO, FILE_GIORNO_CORRETTI

    dest = LOCAL_CLONE / GIORNI / g.giorno
    cambiati: list[Path] = []
    contenuti = g.file()
    for nome, testo in contenuti.items():
        if nome.endswith(".json"):
            testo = json.dumps(_scrub(json.loads(testo)), ensure_ascii=False,
                               indent=2) + "\n"
        target = dest / nome
        if _gia_uguale(target, testo):
            continue
        if not dry_run:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(testo, encoding="utf-8")
        cambiati.append(target)
    for nome in FILE_GIORNO + FILE_GIORNO_CORRETTI:
        vecchio = dest / nome
        if nome not in contenuti and vecchio.exists():
            if not dry_run:
                vecchio.unlink()
            cambiati.append(vecchio)
    return cambiati


def _migra_sessioni_vecchie(dry_run: bool = False) -> list[str]:
    """Prima di togliere `sessions/`, riporta in locale cio' che c'e' solo li'.

    Il caso vero: `2026-10-02_17-02-36` stava sulla repo e non piu' in
    `output/`. Le giornate si costruiscono da `output/`: senza questo
    passo, togliere `sessions/` l'avrebbe cancellata dal corpus. Si copia
    la cartella pubblicata (gia' senza nomi) in `output/`, senza toccare
    nulla che esista gia'. Restituisce le sessioni riportate.
    """
    vecchie = LOCAL_CLONE / SESSIONI_VECCHIE
    if not vecchie.is_dir():
        return []
    riportate = []
    for d in sorted(vecchie.iterdir()):
        if not d.is_dir() or (OUTPUT_DIR / d.name / "transcript.json").exists():
            continue
        riportate.append(d.name)
        if not dry_run:
            shutil.copytree(d, OUTPUT_DIR / d.name, dirs_exist_ok=True)
            logger.info("Riportata in output/ dalla repo: %s", d.name)
    return riportate



def _write_voice_matrix(dry_run: bool = False) -> Path | None:
    """Genera la matrice di somiglianza fra le voci e la scrive in `voices/`.

    Va pubblicata perche' e' l'unica cosa che dice *chi* ha parlato:
    `session.json` dice quanti minuti per voce, la matrice dice quanto due
    voci somigliano e quali coppie la soglia non riesce a decidere. E il
    punto in cui la diarizzazione si vede incrinata — le 34 coppie in
    zona grigia non sono un dettaglio di una sessione, sono il buco da
    chiudere con una decisione.

    `VoiceReport.to_dict()` mette fuori solo pseudonimo, sessione,
    secondi e somiglianza: **nessun embedding**. E' una scelta che va
    tenuta, perche' un embedding vocale e' un'impronta biometrica e questa
    repo non ne tiene.

    Ritorna il percorso scritto, o None se non c'e' nessun campione
    vocale da confrontare (una sessione senza diarizzazione non e' un
    errore).
    """
    try:
        from core.speaker_db import SpeakerDB
        from core.voice_matrix import build_matrix, load_samples
    except Exception as exc:  # noqa: BLE001
        logger.warning("Matrice delle voci non disponibile: %s", exc)
        return None

    campioni = load_samples(OUTPUT_DIR)
    if not campioni:
        logger.info("Matrice delle voci: nessun campione, non scritta")
        return None

    sdb = SpeakerDB()
    rep = build_matrix(campioni, soglia=sdb.threshold, centroidi=sdb.centroids())
    dest = LOCAL_CLONE / "voices" / "voice_matrix.json"
    if not dry_run:
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(
            json.dumps(rep.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    logger.info(
        "Matrice delle voci: %d voci, %d coppie, %d in zona grigia, "
        "%d coppie di voci da decidere",
        rep.to_dict()["n_voices"], rep.to_dict()["n_pairs"],
        len(rep.zona_grigia()), len(rep.coppie_da_decidere()),
    )
    return dest


def _recorded_at(session_dir: Path, meta: dict) -> str | None:
    """Ora di inizio della registrazione, dalla fonte piu' affidabile.

    In `transcript.json` il campo `session_start_wall` resta `None`:
    l'assembler non conosce l'orologio del registratore, e il valore lo
    scrive `sync_device.py` al primo livello di `session.json`. L'indice
    leggeva solo il primo dei due, ed e' per questo che la colonna Data
    usciva vuota su tutte le sessioni.

    Ordine: `session.json`, poi `transcript.json`, poi il nome della
    sessione, che il registratore compone come `AAAA-MM-GG_hh-mm-ss`.
    L'ultimo e' lo stesso orologio letto da un'altra parte, non una
    stima: se anche quello manca, la data resta vuota e lo si vede.
    """
    try:
        wall = json.loads((session_dir / "session.json").read_text(
            encoding="utf-8")).get("session_start_wall")
        if wall:
            return wall
    except (json.JSONDecodeError, OSError):
        pass
    if meta.get("session_start_wall"):
        return meta["session_start_wall"]
    try:
        return datetime.strptime(session_dir.name[:19], "%Y-%m-%d_%H-%M-%S").isoformat()
    except ValueError:
        return None


def _write_index(dry_run: bool = False) -> Path:
    """Indice per giorno: il punto di ingresso di un LLM nel corpus.

    Si legge dai manifesti `giorni/*/giorno.json` della repo, cioe' da
    quello che e' davvero pubblicato, non da quello che c'e' in locale.
    """
    righe_giorni = []
    tot_parole = tot_sess = 0
    gdir = LOCAL_CLONE / GIORNI
    for d in sorted((gdir.iterdir() if gdir.is_dir() else []), reverse=True):
        m = _leggi_json(d / "giorno.json")
        if not m:
            continue
        t = m.get("totals") or {}
        voci = [v for v in (m.get("speakers") or {}) if v != "UNKNOWN"]
        tot_parole += t.get("words") or 0
        tot_sess += t.get("sessions") or 0
        righe_giorni.append(
            f"| [{d.name}]({GIORNI}/{d.name}/) | {t.get('sessions', 0)} | "
            f"{(t.get('first_start') or '')[11:16]}–{(t.get('last_end') or '')[11:16]} | "
            f"{len(m.get('blocks') or [])} | {_hms(t.get('speech_sec'))} | "
            f"{t.get('words', 0)} | {len(voci)} |")

    lines = [
        "# Corpus — indice",
        "",
        f"> Generato automaticamente. Una cartella per giorno in `{GIORNI}/`, "
        "con un file per tipo che contiene tutte le registrazioni del giorno "
        "in fila, all'ora dell'orologio del registratore.",
        "> Le voci compaiono come pseudonimi `GLOBAL_00x`" + (
            ": dove una voce ha un nome, il nome e' accanto (`giorno.json`, "
            "`speaker_names`)." if keep_names else
            ": la mappa con i nomi reali sta solo in locale e non viene pubblicata."
        ),
        "",
        f"Giorni: **{len(righe_giorni)}** · registrazioni: **{tot_sess}** · "
        f"parole: **{tot_parole}**",
        "",
        "| Giorno | Registrazioni | Dalle–alle | Blocchi | Parlato | Parole | Voci |",
        "|---|---|---|---|---|---|---|",
        *righe_giorni,
        "",
        "## Dentro ogni giorno",
        "",
        "- `giorno.json` — il manifesto: registrazioni con ora di inizio e fine, "
        "buchi fra un file e l'altro, blocchi continui (buchi sotto 5 minuti), "
        "voci con minuti e parole, decisioni di denoise e fusioni delle voci",
        "- `transcript.txt` — il testo del giorno, leggibile, con l'ora vera",
        "- `transcript.srt` — sottotitoli con l'ora del giorno",
        "- `segments.jsonl` — un segmento per riga: testo, voce, qualità, "
        "prosodia; `session` + `start`/`end` (secondi dall'inizio del file) e "
        "`clock_*`/`day_sec_*` (ora vera)",
        "- `tokens.jsonl` — una parola per riga con tempi, probabilità ASR, voce "
        "e `day_segment_idx` (la prosodia sta nel segmento)",
        "- `prosody.csv` — F0, intensità, ritmo per segmento, con l'ora vera",
        "- `wordfreq.csv` — frequenze del giorno, per voce",
        "- `analysis_ready.md` — il giorno intero, pronto per un LLM",
        "- `*.corrected.*`, `text_correction.json` — le stesse viste col testo "
        "corretto dal modello di lingua, dove esiste",
        "- `../../voices/voice_matrix.json` — somiglianza fra le voci e coppie "
        "di voci da decidere",
        "",
        "Gli orari sono quelli dell'orologio del registratore, la cui deriva "
        "non è ancora misurata: valgono al minuto.",
        "",
    ]
    p = LOCAL_CLONE / "INDEX.md"
    if not dry_run:
        LOCAL_CLONE.mkdir(parents=True, exist_ok=True)
        p.write_text("\n".join(lines), encoding="utf-8")
    return p


def _leggi_json(p: Path):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _hms(seconds) -> str:
    if not seconds:
        return "—"
    m, s = divmod(int(seconds), 60)
    return f"{m // 60}h {m % 60:02d}m" if m >= 60 else f"{m}m {s:02d}s"


METRICHE = "metriche"


def _write_metriche(dry_run: bool = False) -> list[Path]:
    """Le metriche aggregate di ogni giornata, per il pannello cifrato.

    Si calcolano dalle giornate **della repo** (come l'indice), cosi' le
    metriche descrivono esattamente quello che e' pubblicato. Un file
    cambia solo se cambiano i numeri; il push di `metriche/` fa partire
    il workflow che costruisce e cifra il pannello (vedi
    `pannello/pannello.yml` e il documento `analisi-corpus.md`).
    """
    from core.metriche import calcola_giorno

    giorni = LOCAL_CLONE / GIORNI
    dest = LOCAL_CLONE / METRICHE
    cambiati: list[Path] = []
    if not giorni.is_dir():
        return cambiati
    presenti = set()
    for d in sorted(giorni.iterdir()):
        if not (d / "giorno.json").exists():
            continue
        presenti.add(f"{d.name}.json")
        testo = json.dumps(_scrub(calcola_giorno(d)), ensure_ascii=False,
                           indent=1) + "\n"
        target = dest / f"{d.name}.json"
        if _gia_uguale(target, testo):
            continue
        if not dry_run:
            dest.mkdir(parents=True, exist_ok=True)
            target.write_text(testo, encoding="utf-8")
        cambiati.append(target)
    if dest.is_dir():
        for vecchio in dest.glob("????-??-??.json"):
            if vecchio.name not in presenti:
                if not dry_run:
                    vecchio.unlink()
                cambiati.append(vecchio)
    return cambiati


WORKFLOW_PANNELLO = Path(__file__).resolve().parent / "pannello" / "pannello.yml"
WORKFLOW_DEST = Path(".github") / "workflows" / "pannello.yml"


def _write_workflow(dry_run: bool = False) -> list[Path]:
    """Installa nel corpus il workflow che costruisce il pannello cifrato.

    La copia ufficiale sta in questa repo (`pannello/pannello.yml`) e la
    porta il Mac, come ogni altro file del corpus: cosi' la repo del
    corpus riceve commit da una parte sola e il workflow e' versionato
    accanto al codice che lancia.
    """
    if not WORKFLOW_PANNELLO.exists():
        return []
    testo = WORKFLOW_PANNELLO.read_text(encoding="utf-8")
    target = LOCAL_CLONE / WORKFLOW_DEST
    if _gia_uguale(target, testo):
        return []
    if not dry_run:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(testo, encoding="utf-8")
    return [target]


def _allinea_al_remoto() -> None:
    """Porta il clone allo stato del remoto prima di scrivere.

    Dall'8 ottobre la repo del corpus riceve commit anche da fuori del
    Mac: il workflow del pannello (`.github/workflows/pannello.yml`) e'
    stato aggiunto da GitHub. Senza questo passo il primo push successivo
    verrebbe rifiutato come non fast-forward, e il giro notturno non
    pubblicherebbe piu' niente.

    Solo fast-forward: il clone del Mac non ha commit suoi non pubblicati
    (ogni push committa e spinge subito). Se un giorno li avesse, non si
    fonde niente a caso: si avvisa e il push dira' perche' fallisce.
    """
    code, out = _run(["git", "fetch", "-q", "origin"], cwd=LOCAL_CLONE)
    if code != 0:
        logger.warning("fetch del corpus non riuscito, proseguo: %s", out[-200:])
        return
    code, ramo = _run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=LOCAL_CLONE)
    code, _ = _run(["git", "rev-parse", "--verify", "-q", f"origin/{ramo}"],
                   cwd=LOCAL_CLONE)
    if code != 0:
        return  # il remoto non ha ancora il ramo: primo push
    code, out = _run(["git", "merge", "--ff-only", "-q", f"origin/{ramo}"],
                     cwd=LOCAL_CLONE)
    if code != 0:
        logger.warning("Il clone del corpus non si allinea al remoto con un "
                       "fast-forward: %s", out[-300:])


def _write_gitignore() -> Path:
    """Scrive il `.gitignore` nel clone, se non c'e' gia' quello giusto.

    Va scritto **prima** di `git add -A`, altrimenti e' troppo tardi: il
    file e' gia' in stage e la pubblicazione lo porta sulla repo.
    """
    p = LOCAL_CLONE / ".gitignore"
    try:
        if p.exists() and p.read_text(encoding="utf-8") == GITIGNORE:
            return p
    except OSError:
        pass
    LOCAL_CLONE.mkdir(parents=True, exist_ok=True)
    p.write_text(GITIGNORE, encoding="utf-8")
    return p


def _guard_repo(names_to_hide: Iterable[str] = ()) -> bool:
    """Controllo finale: niente file vietati, niente nomi reali.

    Due controlli distinti perché falliscono in modi distinti. Il primo
    guarda le estensioni e i nomi di file noti. Il secondo cerca nel
    contenuto i nomi che il DB delle voci conosce: è l'unico che può
    dire «il file pubblicato contiene "Pietro"», che è il rischio
    reale e non quello che si puo' vedere dalla lista dei file.

    Il controllo sui nomi è la difesa che conta quando i nomi NON
    devono uscire. Non è una scansione generica per capire "parole
    che sembrano nomi": usa l'elenco esatto delle etichette che hai
    assegnato tu, che è l'unica informazione disponibile e che non
    produce falsi positivi. Con `--with-names` il controllo salta,
    perché in quel caso l'esposizione è stata chiesta.
    """
    bad = []
    for p in LOCAL_CLONE.rglob("*"):
        if p.is_file():
            if p.name in FORBIDDEN or p.suffix.lower() in (".wav", ".mp3", ".m4a", ".aac", ".db"):
                bad.append(("file vietato", p))

    # I nomi si cercano solo nei testi: in un .wav non ci sono stringhe
    # e in un .png non c'è nulla da leggere.
    if names_to_hide and not keep_names:
        for p in LOCAL_CLONE.rglob("*"):
            if not p.is_file() or p.suffix.lower() not in (
                ".json", ".md", ".txt", ".csv", ".srt", ".jsonl"
            ):
                continue
            try:
                testo = p.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for nome in names_to_hide:
                if nome and nome in testo:
                    bad.append((f"nome reale {nome!r}", p))
                    break

    if bad:
        logger.error("Controllo privacy fallito, push annullato:")
        for motivo, p in bad[:10]:
            logger.error("  %s — %s", motivo, p.relative_to(LOCAL_CLONE))
        if any(m.startswith("nome reale") for m, _ in bad):
            logger.error(
                "Qualcuno dei nomi che hai assegnato è finito in un file "
                "pubblicato. Non viene pubblicato niente finche' non si "
                "capisce come ci sia arrivato."
            )
        return False
    return True


def _speaker_names_to_hide() -> list[str]:
    """I nomi reali noti, letti dal DB delle voci locale.

    Non li mette in logging: sono il dato che il controllo serve a
    trovare, e scriverli nel log li metterebbe in un secondo posto da
    cui escono.
    """
    try:
        from core.speaker_db import SpeakerDB
        from core.speaker_sync import names_from_db
        # I valori, cioe' i nomi. Fino all'8 ottobre qui c'erano le
        # chiavi (gli pseudonimi GLOBAL_xxx): con nessun nome assegnato non
        # cambiava niente, al primo nome il controllo ha bloccato il push
        # perche' trovava «GLOBAL_001» nei file — cioe' proprio lo
        # pseudonimo che doveva restare.
        return sorted(set(names_from_db(SpeakerDB(SPEAKERS_DB)).values()))
    except Exception:  # noqa: BLE001
        return []


def cmd_push(args) -> int:
    if not LOCAL_CLONE.exists():
        print("Repo non clonata. Esegui prima: python publish_corpus.py init")
        return 1

    global keep_names
    # `--with-names` oppure la scelta salvata in config (ROADMAP D1): un
    # `push` lanciato a mano deve pubblicare come quello della notte.
    from core.config import config as _cfg
    keep_names = bool(getattr(args, "with_names", False)) or bool(
        getattr(_cfg, "corpus_with_names", False))
    if keep_names:
        logger.warning(
            "--with-names: i nomi reali dei parlanti verranno pubblicati. "
            "Chi legge la repo potra' dare un nome alle voci."
        )

    if not args.dry_run:
        _allinea_al_remoto()
    _, before_sha = _run(["git", "rev-parse", "HEAD"], cwd=LOCAL_CLONE)

    # La struttura di prima (una cartella per file): prima di toglierla si
    # riportano in locale le sessioni che esistono solo sulla repo.
    vecchie = LOCAL_CLONE / SESSIONI_VECCHIE
    da_migrare = vecchie.is_dir()
    riportate = _migra_sessioni_vecchie(dry_run=args.dry_run)
    if da_migrare:
        logger.info("%s alla struttura per giorno: %s sparisce%s",
                    "Migrerei" if args.dry_run else "Migrazione",
                    SESSIONI_VECCHIE + "/",
                    f", {len(riportate)} sessioni riportate in output/ "
                    f"({', '.join(riportate)})" if riportate else "")

    pushed = []
    for g in _giornate():
        cambiati = _publish_day(g, dry_run=args.dry_run)
        if cambiati:
            pushed.append(g.giorno)
            verb = "Avrei aggiornato" if args.dry_run else "Aggiornato"
            logger.info("%s %s: %d registrazioni, %d file cambiati",
                        verb, g.giorno, len(g.sessioni), len(cambiati))

    # Le metriche si calcolano anche quando nessuna giornata e' cambiata:
    # alla prima pubblicazione dopo l'aggiornamento mancano del tutto, e
    # un cambio del calcolo deve arrivare al pannello senza aspettare un
    # giorno nuovo.
    metriche = _write_metriche(dry_run=args.dry_run)
    metriche += _write_workflow(dry_run=args.dry_run)
    if metriche:
        logger.info("%s metriche e pannello: %d file", "Aggiornerei" if args.dry_run
                    else "Aggiornati", len(metriche))

    if not pushed and not da_migrare and not metriche:
        # L'indice e la matrice delle voci sono derivati dalle giornate
        # gia' presenti sulla repo: con nessuna giornata cambiata
        # riscriverebbero lo stesso contenuto, e in un push vero
        # produrrebbero un commit vuoto. Si esce qui in entrambi i casi.
        if args.dry_run:
            print("Nessuna giornata da pubblicare: quello che c'e' in "
                  "output/ e' gia' identico sulla repo.")
        else:
            print("Nessuna giornata nuova da pubblicare.")
        return 0

    if da_migrare and not args.dry_run:
        shutil.rmtree(vecchie)

    idx = _write_index(dry_run=args.dry_run)
    matrice = _write_voice_matrix(dry_run=args.dry_run)

    if args.dry_run:
        print(f"\n[dry-run] avrei pubblicato {len(pushed)} giorni "
              f"e aggiornato {idx.name}. Niente scritto.")
        for s in pushed:
            print(f"  {s}")
        return 0

    if not _guard_repo(_speaker_names_to_hide()):
        return 1

    _write_gitignore()
    _run(["git", "add", "-A"], cwd=LOCAL_CLONE)
    status, out = _run(["git", "status", "--porcelain"], cwd=LOCAL_CLONE)
    if not out:
        print("Nessuna modifica da pubblicare.")
        return 0

    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
    if da_migrare:
        commit_msg = (f"corpus: una cartella per giorno, {len(pushed)} giorni "
                      f"({stamp})")
    elif pushed:
        commit_msg = f"corpus: {len(pushed)} giorni aggiornati ({stamp})"
    elif metriche:
        commit_msg = f"corpus: metriche aggiornate ({stamp})"
    else:
        commit_msg = f"corpus: indice ({stamp})"
    code, out = _run(["git", "commit", "-m", commit_msg], cwd=LOCAL_CLONE)
    if code != 0:
        print("Commit fallito:\n" + out, file=sys.stderr)
        return 1

    code, out = _run(["git", "push", "origin", "HEAD"], cwd=LOCAL_CLONE)
    if code != 0:
        print("Push fallito:\n" + out, file=sys.stderr)
        return 1

    _, after_sha = _run(["git", "rev-parse", "HEAD"], cwd=LOCAL_CLONE)
    print(f"Pubblicato: {len(pushed)} giorni, indice aggiornato.")
    if before_sha != after_sha:
        print(f"https://github.com/{REPO_SLUG}/commit/{after_sha[:12]}")
    return 0


def cmd_reindex(args) -> int:
    """Ricostruisce il database locale a partire da output/.

    Il database e' la copia interrogabile del corpus: la repo privata
    serve a leggerlo da un altro posto, il database serve a
    chiedergli qualcosa. Le due cose possono divergere, e quando
    divergono il database e' quella che non si vede: una sessione
    elaborata e pubblicata che il database non conosce e' una sessione
    che non si trova con nessuna ricerca.

    Non serve per il caso normale (la pipeline aggiorna il database
    appena finisce una sessione). Serve dopo un rilascio che cambia
    come si scrive l'output, dopo un restore, e per riparare un
    database indietro senza rielaborare niente.
    """
    from core.corpus_db import CorpusDB

    if not OUTPUT_DIR.is_dir():
        print(f"Nessuna cartella di output ({OUTPUT_DIR}).")
        return 1

    db_arg = getattr(args, "db", None)
    # CorpusDB(path=None) non e' un "usa il default": None non e' un
    # percorso e lo costruttore lo rifiuta. Il default si ottiene non
    # passando niente.
    db = CorpusDB(path=Path(db_arg)) if db_arg else CorpusDB()
    with db as cdb:
        n_ok = 0
        n_skipped = 0
        for d in sorted(OUTPUT_DIR.iterdir()):
            if not d.is_dir() or not (d / "transcript.json").exists():
                continue
            if cdb.ingest_session_dir(d):
                n_ok += 1
                print(f"  {d.name}")
            else:
                n_skipped += 1
                print(f"  {d.name}: non leggibile, saltata")

        st = cdb.stats()

        # I nomi vengono dai DB delle voci, che e' la fonte: senza
        # questo allineamento la tabella `speakers` del database tiene i
        # nomi di un tempo, anche per voci che non esistono piu'.
        try:
            from core.speaker_db import SpeakerDB
            from core.speaker_sync import names_from_db
            cdb.sync_speaker_names(names_from_db(SpeakerDB()))
        except Exception as exc:  # noqa: BLE001
            logger.warning("Allineamento dei nomi saltato: %s", exc)

        # Poi la potatura: senza, un merge delle identita' lascia le voci
        # assorbite nella tabella e le query per parlante contano anche
        # quelle.
        potate = cdb.prune_speakers()

        # E poi le sessioni che non hanno piu' una cartella: senza,
        # un cambio di nome lascia dentro token, wordfreq e bigrams
        # che contano due volte lo stesso testo.
        persi = cdb.prune_missing_sessions(OUTPUT_DIR)
        st = cdb.stats()

    print(f"\nDatabase ricostruito: {n_ok} sessioni ingestate"
          f"{f', {n_skipped} saltate' if n_skipped else ''}.")
    if potate:
        print(f"  {potate} voci obsolete rimosse dalla tabella speakers")
    if persi:
        print(f"  {persi} sessioni obsolete rimosse (nessuna cartella corrispondente)")
    print(f"  sessioni={st['sessions']} segmenti={st['segments']} "
          f"parole_distinte={st['distinct_words']} "
          f"parlato={st['total_speech_hours']:.1f} h")
    return 0


def _voci_senza_identita() -> tuple[list[str], dict[str, list[str]]]:
    """Le voci che le sessioni citano e il DB delle voci non conosce.

    E' l'invariante che `review_speakers.py merge --dry-run` e `split
    --dry-run` hanno rotto cancellando una voce dal database: le sessioni
    hanno continuato a citarla, e niente lo segnalava, perche' un ID
    assente non produce un errore — e' solo un ID che nessuno genera
    piu'.

    Non e' un controllo teorico: senza, il corpus puo' avere una voce in
    piu' sessioni di quanti siano i(DB) e la differenza non appare da
    nessuna parte fino a che una ricerca non torna vuota.

    Ritorna `([], {})` quando il DB non c'e': e' una condizione normale
    prima della prima sessione, non un errore.
    """
    db_path = SPEAKERS_DB
    if not db_path.exists() or not OUTPUT_DIR.is_dir():
        return [], {}
    try:
        note = json.loads(db_path.read_text(encoding="utf-8")).get("speakers") or {}
    except (json.JSONDecodeError, OSError):
        return [], {}
    esistono = set(note)

    dove: dict[str, list[str]] = {}
    for d in sorted(OUTPUT_DIR.iterdir()):
        if not d.is_dir():
            continue
        sj = d / "session.json"
        if not sj.exists():
            continue
        try:
            voci = json.loads(sj.read_text(encoding="utf-8")).get("speakers") or []
        except (json.JSONDecodeError, OSError):
            continue
        for v in voci:
            if v and v != "UNKNOWN" and v not in esistono:
                dove.setdefault(v, []).append(d.name)
    return sorted(dove), dove


def cmd_status(args) -> int:
    if not LOCAL_CLONE.exists():
        print(f"Repo non clonata ({LOCAL_CLONE}).")
        print(f"Esegui: python publish_corpus.py init")
        return 1

    code, out = _run(["git", "log", "--oneline", "-10"], cwd=LOCAL_CLONE)
    print(f"Ultimi commit su {REPO_SLUG}:\n{out or '(nessuno)'}\n")

    local = {d.name for d in _cartelle_sessioni()}
    # Pubblicate = quelle elencate nei manifesti delle giornate sulla repo.
    published: set[str] = set()
    gdir = LOCAL_CLONE / GIORNI
    for d in (gdir.iterdir() if gdir.is_dir() else []):
        m = _leggi_json(d / "giorno.json") or {}
        published |= {x.get("stem") for x in m.get("sessions") or [] if x.get("stem")}
    vecchie = LOCAL_CLONE / SESSIONI_VECCHIE
    if vecchie.is_dir():
        published |= {d.name for d in vecchie.iterdir() if d.is_dir()}
        print(f"La repo ha ancora la struttura per file ({SESSIONI_VECCHIE}/): "
              "il prossimo push la migra a una cartella per giorno.\n")

    missing = sorted(local - published)
    # La direzione opposta: una sessione che sta sulla repo e non ha piu'
    # una cartella in `output/`. Prima non veniva guardata, e il risultato
    # era che il comando stampava «11 in locale | 12 sulla repo» e subito
    # sotto «Tutto pubblicato» — una contraddizione enunciata e ignorata.
    orfane = sorted(published - local)
    mancanti_artefatti = [rel for rel in ARTEFATTI_CORPUS
                          if not (LOCAL_CLONE / rel).exists()]
    incoerenti, dove_voci = _voci_senza_identita()

    print(f"Sessioni in locale: {len(local)} | sulla repo: {len(published)}")
    if missing:
        print(f"\nNon ancora pubblicate ({len(missing)}):")
        for m in missing[:20]:
            print(f"  {m}")
    if orfane:
        print(f"\nSulla repo ma non piu' in locale ({len(orfane)}):")
        for o in orfane[:20]:
            print(f"  {o}")
        print("  Non sono riproducibili: senza la cartella non si possono")
        print("  rielaborare ne' ripubblicare. La loro giornata le perderebbe")
        print("  alla prossima pubblicazione di quel giorno: decidi tu se tenerle")
        print("  (riportale in output/ dalla repo) o lasciarle andare.")
    if mancanti_artefatti:
        print(f"\nArtefatti di corpus mancanti ({len(mancanti_artefatti)}):")
        for rel in mancanti_artefatti:
            print(f"  {rel}")
    if incoerenti:
        print(f"\nIdentita' citate ma assenti dal DB delle voci "
              f"({len(incoerenti)}):")
        for g in incoerenti[:20]:
            sessioni = ", ".join(dove_voci[g][:3])
            print(f"  {g} — citata da {sessioni}")
        print("  Una sessione che cita una voce inesistente parla di una")
        print("  persona che il DB non conosce piu': il corpus e' incoerente")
        print("  e nienti lo segnala, perche' un ID assente e' solo un ID")
        print("  che nessuno genera piu'.")

    if missing or orfane or mancanti_artefatti or incoerenti or vecchie.is_dir():
        return 0  # c'e' roba da decidere, ma non e' un errore del comando
    print("\nTutto pubblicato.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Pubblica il corpus sulla repo privata")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init", help="clona la repo privata in locale").set_defaults(func=cmd_init)
    p = sub.add_parser("push", help="pubblica le sessioni nuove")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument(
        "--with-names", action="store_true",
        help="pubblica anche i nomi reali dei parlanti (default: no, "
             "vengono sostituiti dagli pseudonimi)",
    )
    p.set_defaults(func=cmd_push)
    r = sub.add_parser(
        "reindex",
        help="ricostruisce il database locale a partire da output/",
    )
    r.add_argument(
        "--db", default=None,
        help="percorso del database (default: data/corpus.db)",
    )
    r.set_defaults(func=cmd_reindex)
    sub.add_parser("status", help="cosa c'è sulla repo e cosa manca").set_defaults(func=cmd_status)

    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s", force=True)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
