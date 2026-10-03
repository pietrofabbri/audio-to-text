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

    python publish_corpus.py init      # clona la repo privata in locale
    python publish_corpus.py push      # pubblica le sessioni nuove
    python publish_corpus.py push --with-names   # pubblica anche i nomi
    python publish_corpus.py status    # cosa c'è dentro, cosa manca
"""

from __future__ import annotations

import argparse
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

# File pubblicati per ogni sessione.
PUBLISHABLE = (
    "transcript.json", "transcript.txt", "transcript.srt",
    "prosody.csv", "session.json", "segments.jsonl",
    "wordfreq.csv", "analysis_ready.md", "speaker_profiles.json",
    "denoise_decision.json",
)

# File che non devono MAI essere copiati, per nome. La lista è volutamente
# conservativa: più è restrittiva, meglio è.
FORBIDDEN = (
    "speakers_db.json", "corpus.db", "checkpoint.json", ".wav", ".mp3", ".m4a",
)

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


def _session_dir(stem: str) -> Path:
    return LOCAL_CLONE / "sessions" / stem


def _publish_session(stem: str, dry_run: bool = False) -> list[Path]:
    """Copia gli output di una sessione nella repo, ripuliti."""
    src = OUTPUT_DIR / stem
    if not src.is_dir():
        return []

    dest = _session_dir(stem)
    written: list[Path] = []

    for name in PUBLISHABLE:
        s = src / name
        if not s.exists():
            continue
        if any(name.endswith(x) for x in (".wav", ".mp3", ".m4a", ".db")):
            continue

        if name.endswith(".json"):
            try:
                doc = json.loads(s.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError) as exc:
                logger.warning("%s: %s non leggibile, saltato (%s)", stem, name, exc)
                continue
            content = json.dumps(_scrub(doc), ensure_ascii=False, indent=2)
            target = dest / name
            if not dry_run:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content, encoding="utf-8")
            written.append(target)
        else:
            target = dest / name
            if not dry_run:
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(s, target)
            written.append(target)

    return written


def _write_index(dry_run: bool = False) -> Path:
    """Indice per data: il punto di ingresso di un LLM nel corpus.

    Un indice unico in cima, ordinato per data, vale più di mille file
    in una cartella: un LLM non sa cosa cercare se non gli dici prima
    che cosa c'è.
    """
    sessions_dir = LOCAL_CLONE / "sessions"
    entries = []
    if sessions_dir.is_dir():
        for d in sorted(sessions_dir.iterdir()):
            if not d.is_dir():
                continue
            info = {"stem": d.name, "files": sorted(f.name for f in d.iterdir() if f.is_file())}
            # I metadati si leggono da transcript.json, che è il formato
            # canonico: session.json è una vista derivata e ha una
            # struttura diversa (duration/speakers/stats piatti).
            try:
                m = json.loads((d / "transcript.json").read_text(encoding="utf-8")).get("meta", {})
                info.update({
                    "recorded_at": m.get("session_start_wall"),
                    "duration_sec": m.get("total_duration_sec"),
                    "speech_sec": m.get("speech_duration_sec"),
                    "speakers": m.get("speakers"),
                    "n_segments": m.get("segments_count"),
                    "n_words": m.get("total_words"),
                    "denoise": m.get("denoise_winner"),
                })
            except (json.JSONDecodeError, OSError):
                pass
            entries.append(info)

    entries.sort(key=lambda e: (e.get("recorded_at") or "", e["stem"]), reverse=True)

    lines = [
        "# Corpus — indice",
        "",
        "> Generato automaticamente. Un file per sessione in `sessions/`.",
        "> Gli speaker compaiono come pseudonimi `GLOBAL_00x`" + (
            ": la mappa con i nomi reali e' pubblicata accanto a questi file."
            if keep_names else
            ": la mappa con i nomi reali sta solo in locale e non viene pubblicata."
        ),
        "",
        f"Sessioni: **{len(entries)}**",
        "",
        "| Data | Sessione | Parlato | Speaker | Segmenti | Parole | Denoise |",
        "|---|---|---|---|---|---|---|",
    ]
    for e in entries:
        rec = (e.get("recorded_at") or "")[:16].replace("T", " ")
        lines.append(
            f"| {rec or '—'} | `{e['stem']}` | "
            f"{_hms(e.get('speech_sec'))} | {len(e.get('speakers') or [])} | "
            f"{e.get('n_segments') or 0} | {e.get('n_words') or 0} | "
            f"{e.get('denoise') or '—'} |"
        )
    lines += [
        "",
        "## Formati",
        "",
        "- `transcript.txt` — testo leggibile con etichette speaker",
        "- `segments.jsonl` — un segmento per riga (testo, tempi, prosodia)",
        "- `tokens.jsonl` — una parola per riga con timestamp (KWIC, n-grammi)",
        "- `wordfreq.csv` — frequenze per parola e speaker",
        "- `prosody.csv` — F0, intensità, ritmo per segmento",
        "- `session.json` — durate, statistiche per speaker",
        "- `analysis_ready.md` — testo pronto per un LLM",
        "",
    ]
    p = LOCAL_CLONE / "INDEX.md"
    if not dry_run:
        LOCAL_CLONE.mkdir(parents=True, exist_ok=True)
        p.write_text("\n".join(lines), encoding="utf-8")
    return p


def _hms(seconds) -> str:
    if not seconds:
        return "—"
    m, s = divmod(int(seconds), 60)
    return f"{m // 60}h {m % 60:02d}m" if m >= 60 else f"{m}m {s:02d}s"


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
        return sorted(set(names_from_db(SpeakerDB())))
    except Exception:  # noqa: BLE001
        return []


def cmd_push(args) -> int:
    if not LOCAL_CLONE.exists():
        print("Repo non clonata. Esegui prima: python publish_corpus.py init")
        return 1

    global keep_names
    keep_names = bool(getattr(args, "with_names", False))
    if keep_names:
        logger.warning(
            "--with-names: i nomi reali dei parlanti verranno pubblicati. "
            "Chi legge la repo potra' dare un nome alle voci."
        )

    _, before_sha = _run(["git", "rev-parse", "HEAD"], cwd=LOCAL_CLONE)
    pushed = []

    for d in sorted(OUTPUT_DIR.iterdir()) if OUTPUT_DIR.is_dir() else []:
        if not d.is_dir():
            continue
        # Solo sessioni con transcript: un output a metà non si pubblica
        if not (d / "transcript.json").exists():
            continue
        written = _publish_session(d.name, dry_run=args.dry_run)
        if written:
            pushed.append(d.name)
            logger.info("Pubblicata %s (%d file)", d.name, len(written))

    if not pushed and not args.dry_run:
        print("Nessuna sessione nuova da pubblicare.")
        return 0

    idx = _write_index(dry_run=args.dry_run)

    if args.dry_run:
        print(f"\n[dry-run] avrei pubblicato {len(pushed)} sessioni e aggiornato {idx.name}")
        for s in pushed:
            print(f"  {s}")
        return 0

    if not _guard_repo(_speaker_names_to_hide()):
        return 1

    _run(["git", "add", "-A"], cwd=LOCAL_CLONE)
    status, out = _run(["git", "status", "--porcelain"], cwd=LOCAL_CLONE)
    if not out:
        print("Nessuna modifica da pubblicare.")
        return 0

    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
    commit_msg = f"corpus: {len(pushed)} sessioni ({stamp})" if pushed else f"corpus: indice ({stamp})"
    code, out = _run(["git", "commit", "-m", commit_msg], cwd=LOCAL_CLONE)
    if code != 0:
        print("Commit fallito:\n" + out, file=sys.stderr)
        return 1

    code, out = _run(["git", "push", "origin", "HEAD"], cwd=LOCAL_CLONE)
    if code != 0:
        print("Push fallito:\n" + out, file=sys.stderr)
        return 1

    _, after_sha = _run(["git", "rev-parse", "HEAD"], cwd=LOCAL_CLONE)
    print(f"Pubblicato: {len(pushed)} sessioni, indice aggiornato.")
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

    print(f"\nDatabase ricostruito: {n_ok} sessioni ingestate"
          f"{f', {n_skipped} saltate' if n_skipped else ''}.")
    print(f"  sessioni={st['sessions']} segmenti={st['segments']} "
          f"parole_distinte={st['distinct_words']} "
          f"parlato={st['total_speech_hours']:.1f} h")
    return 0


def cmd_status(args) -> int:
    if not LOCAL_CLONE.exists():
        print(f"Repo non clonata ({LOCAL_CLONE}).")
        print(f"Esegui: python publish_corpus.py init")
        return 1

    code, out = _run(["git", "log", "--oneline", "-10"], cwd=LOCAL_CLONE)
    print(f"Ultimi commit su {REPO_SLUG}:\n{out or '(nessuno)'}\n")

    local = {d.name for d in OUTPUT_DIR.iterdir() if (d / "transcript.json").exists()} \
        if OUTPUT_DIR.is_dir() else set()
    published = set()
    sd = LOCAL_CLONE / "sessions"
    if sd.is_dir():
        published = {d.name for d in sd.iterdir() if d.is_dir()}

    missing = sorted(local - published)
    print(f"Sessioni in locale: {len(local)} | sulla repo: {len(published)}")
    if missing:
        print(f"\nNon ancora pubblicate ({len(missing)}):")
        for m in missing[:20]:
            print(f"  {m}")
    else:
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
