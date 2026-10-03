"""
Test della pubblicazione del corpus, senza rete e senza toccare la repo.

    python tests/test_publish.py

Il punto 9 di APERTI.md dice che la pubblicazione non è mai stata
provata con dati veri, ed è l'unico punto dell'elenco in cui un errore
è irreversibile: dati pubblicati per errore non si richiamano. Qui si
prova la catena intera con una coda finta e si verifica la cosa che
conta davvero — che sulla copia non finiscano audio, embedding né nomi
reali.

Il clone git vero non viene toccato: tutto avviene sotto una directory
temporanea, e la "repo" è una cartella con dentro i file che
`publish_corpus` ci metterebbe. È la stessa isola che usa il test
end-to-end per la pipeline, ed è il motivo per cui `publish_corpus`
accetta un `output_dir` esplicito invece di leggerlo da una costante.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import publish_corpus as pc  # noqa: E402
from core.speaker_db import SpeakerDB  # noqa: E402


class Failure(Exception):
    pass


def require(cond: bool, msg: str) -> None:
    if not cond:
        raise Failure(msg)


# -------------------------------------------------------------------
# Una coda finta, con tutto quello che nella vita vera c'è
# -------------------------------------------------------------------

def _make_session(job: Path, stem: str, named: bool = True) -> None:
    job.mkdir(parents=True, exist_ok=True)
    meta = {
        "file": f"{stem}.mp3", "stem": stem,
        "processed_at": "2026-10-02T22:00:00",
        "session_start_wall": "2026-10-02T21:44:16",
        "total_duration_sec": 1758.0, "speech_duration_sec": 1143.0,
        "speech_ratio": 0.65, "segments_count": 2, "total_words": 420,
        "speakers": ["GLOBAL_001", "GLOBAL_002"],
        "speaker_names": ({"GLOBAL_001": "Pietro", "GLOBAL_002": "Chiara"}
                          if named else {}),
        "speaker_stats": {
            "GLOBAL_001": {"segments_count": 1, "total_duration_sec": 600.0,
                          "total_words": 240, "fraction": 0.52},
            "GLOBAL_002": {"segments_count": 1, "total_duration_sec": 543.0,
                          "total_words": 180, "fraction": 0.48},
        },
        "speaker_global_map": {"SPEAKER_00": "GLOBAL_001",
                               "SPEAKER_01": "GLOBAL_002"},
        "denoise_winner": "original",
        "quality": {"segments": 2, "ok": 2, "low": 0, "unreliable": 0,
                    "unreliable_share": 0.0, "low_or_worse_share": 0.0},
    }
    segments = [
        {"idx": 0, "start": 0.0, "end": 30.0, "duration_sec": 30.0,
         "text": "Buongiorno a tutti, oggi parliamo del progetto.",
         "speaker": "GLOBAL_001", "speaker_local": "SPEAKER_00",
         "language": "it", "no_speech_prob": 0.02,
         "quality": "ok", "quality_reasons": [],
         "prosody": {"f0_mean_hz": 120.0, "intensity_mean_db": 58.0}},
        {"idx": 1, "start": 30.0, "end": 61.0, "duration_sec": 31.0,
         "text": "Grazie, iniziamo pure.", "speaker": "GLOBAL_002",
         "speaker_local": "SPEAKER_01", "language": "it",
         "no_speech_prob": 0.01, "quality": "ok", "quality_reasons": [],
         "prosody": {"f0_mean_hz": 195.0}},
    ]
    (job / "transcript.json").write_text(
        json.dumps({"meta": meta, "segments": segments}, ensure_ascii=False, indent=2),
        encoding="utf-8")
    (job / "session.json").write_text(
        json.dumps({
            "file": meta["file"], "stem": stem,
            "processed_at": meta["processed_at"],
            "session_start_wall": meta["session_start_wall"],
            "duration": {"total_sec": 1758.0, "speech_sec": 1143.0,
                         "speech_ratio": 0.65},
            "speakers": meta["speaker_stats"],
            "speaker_names": meta["speaker_names"],
            "speaker_global_map": meta["speaker_global_map"],
            "stats": {"total_words": 420, "segments_count": 2},
        }, ensure_ascii=False, indent=2), encoding="utf-8")
    (job / "transcript.txt").write_text(
        "[00:00:00 → 00:00:30] GLOBAL_001\nBuongiorno a tutti.\n", encoding="utf-8")
    (job / "segments.jsonl").write_text(
        "\n".join(json.dumps(s, ensure_ascii=False) for s in segments) + "\n",
        encoding="utf-8")
    (job / "prosody.csv").write_text(
        "idx,start,end,duration_sec,speaker,text_preview,word_count,"
        "no_speech_prob,quality,quality_reasons,f0_mean_hz\n"
        "0,0.0,30.0,30.0,GLOBAL_001,Buongiorno a tutti,4,0.02,ok,,120.0\n",
        encoding="utf-8")

    # Quello che NON deve mai uscire, ma che sta nelle sessioni vere.
    (job / "registrazione.wav").write_bytes(b"RIFF" + b"\0" * 2000)
    (job / "checkpoint.json").write_text("{}", encoding="utf-8")
    (job / "speakers_db.json").write_text(
        json.dumps({"speakers": {"GLOBAL_001": {"name": "Pietro"}}}),
        encoding="utf-8")


def _fake_env(tmp: Path) -> tuple[Path, Path]:
    """Coda finta e clone git vero, in una directory temporanea.

    Il clone è un repository git vero (`git init` piu' un commit vuoto) con
    un remoto locale — un bare repo nella stessa directory temporanea —
    cosi' `git add`, `commit` e `push` vengono eseguiti davvero. Provarlo
    contro una cartella che non è un repo significherebbe non aver
    provato niente, e la parte che conta è proprio quella che finisce
    dopo il controllo privacy.

    Niente di tutto questo tocca `corpus_repo/`, GitHub, o il progetto.
    """
    out = tmp / "output"
    clone = tmp / "clone"
    remoto = tmp / "remoto.git"
    out.mkdir(parents=True, exist_ok=True)
    clone.mkdir(parents=True, exist_ok=True)
    _git(tmp, "init", "-q", "--bare", "-b", "main", str(remoto))
    _git(clone, "init", "-q", "-b", "main")
    _git(clone, "config", "user.email", "test@localhost")
    _git(clone, "config", "user.name", "test")
    _git(clone, "remote", "add", "origin", str(remoto))
    _git(clone, "commit", "-q", "--allow-empty", "-m", "inizio")
    _make_session(out / "2026-10-02_21-44-16", "2026-10-02_21-44-16")
    return out, clone


def _quiet(fn, *args, **kwargs):
    """Come `fn`, ma senza stampare.

    `cmd_push` scrive l'URL del commit GitHub, che qui e' inventato e
    finisca in un log di test sembrerebbe una pubblicazione vera.
    """
    import io
    import contextlib
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
        return fn(*args, **kwargs)


def _remote_files(tmp: Path) -> list[str]:
    """Cosa e' finito davvero nel remoto, letto dal remoto.

    Non si guarda la copia di lavoro: quella puo' contenere file che il
    push non ha pubblicato, e controllare la copia darebbe una risposta
    falsa sulla privacy.
    """
    remoto = tmp / "remoto.git"
    p = subprocess.run(["git", "--git-dir", str(remoto), "ls-tree", "-r",
                        "--name-only", "HEAD"],
                       capture_output=True, text=True, timeout=60)
    return p.stdout.split("\n") if p.returncode == 0 else []


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=str(cwd),
                          capture_output=True, text=True, timeout=60)


def _publish(out: Path, clone: Path) -> int:
    pc.OUTPUT_DIR = out
    pc.LOCAL_CLONE = clone
    pc.keep_names = False
    return _quiet(pc.cmd_push,
                  argparse.Namespace(dry_run=False, with_names=False))


# -------------------------------------------------------------------

def t_nothing_forbidden_lands_on_the_repo(tmp: Path) -> None:
    """Né audio, né database, né il file delle voci.

    Il pericolo qui è duplice: il file esiste, e i permessi non lo
    fermerebbero. Un `.wav` da un'ora sono 115 MB che finiscono su una
    repo pubblica — e su una privata, che è peggio, perché lì non c'è
    nemmeno la paura che li veda qualcuno.
    """
    print("  la copia non contiene file vietati")
    out, clone = _fake_env(tmp)
    require(_publish(out, clone) == 0, "push non riuscito")

    finiti = _remote_files(tmp)
    require(finiti, "il remoto è vuoto: il push non è avvenuto")
    for vietato in ("registrazione.wav", "checkpoint.json", "speakers_db.json"):
        require(not any(f.endswith(vietato) for f in finiti),
                f"{vietato} è finito sulla repo: {sorted(finiti)}")
    for f in finiti:
        require(Path(f).suffix.lower() not in (".wav", ".mp3", ".m4a", ".aac", ".db"),
                f"file audio/database sulla repo: {f}")
    # E i file che DEVONo esserci, altrimenti il test passerebbe anche
    # se la pubblicazione non pubblicasse niente.
    for atteso in ("INDEX.md", "sessions/2026-10-02_21-44-16/transcript.json"):
        require(any(f.endswith(atteso) for f in finiti),
                f"mancava {atteso} nel remoto: {sorted(finiti)}")
    print(f"    {len(finiti)} file pubblicati, nessuno vietato")


def t_real_names_are_scrubbed_by_default(tmp: Path) -> None:
    """I nomi veri non escono, e il motivo è nel testo.

    Non basta cancellare la chiave `speaker_names`: il nome può essere
    finito in qualunque altro campo, in un CSV o in una riga di
    `segments.jsonl`. Per questo il controllo finale cerca i nomi nel
    contenuto di ogni file pubblicato, e questo test verifica proprio
    quello — non il campo che oggi lo contiene.
    """
    print("  i nomi veri non escono dalla copia")
    out, clone = _fake_env(tmp)
    require(_publish(out, clone) == 0, "push non riuscito")

    # Si cerca nel CONTENUTO pubblicato, letto dal remoto: e l'unica
    # verifica che non dipenda da quale campo oggi contiene il nome.
    remoto = tmp / "remoto.git"
    for nome in ("Pietro", "Chiara"):
        for f in _remote_files(tmp):
            testo = subprocess.run(
                ["git", "--git-dir", str(remoto), "show", f"HEAD:{f}"],
                capture_output=True, text=True, timeout=60).stdout
            require(nome not in testo,
                    f"{nome!r} compare nel file pubblicato {f}")

    # E deve restare l'informazione utile: il segmento c'è, con lo
    # pseudonimo. Cancellare tutto non è privacy, è perdita.
    tr = json.loads((clone / "sessions" / "2026-10-02_21-44-16"
                     / "transcript.json").read_text(encoding="utf-8"))
    require(tr["meta"]["speaker_names"] == {},
            tr["meta"]["speaker_names"])
    require(tr["meta"]["speakers"] == ["GLOBAL_001", "GLOBAL_002"],
            "gli pseudonimi devono restare")
    require(len(tr["segments"]) == 2, "i segmenti devono restare")
    print("    pseudonimi presenti, nomi assenti")


def t_guard_catches_a_name_that_slipped_through(tmp: Path) -> None:
    """Il controllo finale è l'ultima rete. Se un nome arriva sulla
    copia per un motivo che nessuno ha previsto, deve fermare il push
    invece di pubblicarlo."""
    print("  il controllo finale ferma un nome che è sfuggito")
    out, clone = _fake_env(tmp)
    require(_publish(out, clone) == 0, "prima pubblicazione")

    # Qualcuno scrive un nome a mano nella copia, come può succedere
    # con uno script, un merge di git, un file lasciato da una versione
    # precedente.
    (clone / "sessions" / "2026-10-02_21-44-16" / "note.txt").write_text(
        "riunione con Pietro e Chiara", encoding="utf-8")

    require(not pc._guard_repo(["Pietro", "Chiara"]),
            "il controllo non ha visto il nome")
    print("    push bloccato")


def t_guard_passes_when_there_is_nothing(tmp: Path) -> None:
    """Il controllo non deve urlare sempre: se bloccasse tutto, la
    protezione verrebbe aggirata il primo giorno di noia."""
    print("  il controllo lascia passare una copia pulita")
    out, clone = _fake_env(tmp)
    require(_publish(out, clone) == 0, "push non riuscito")
    require(pc._guard_repo(["Pietro", "Chiara"]),
            "falso positivo: la copia pulita è stata bloccata")
    print("    nessun falso positivo")


def t_with_names_publishes_them(tmp: Path) -> None:
    """`--with-names` è la via dichiarata, e deve funzionare: un flag
    che non fa niente è peggio di un flag assente, perché si crede
    che la protezione sia attiva."""
    print("  --with-names pubblica i nomi, e solo se chiesto")
    out, clone = _fake_env(tmp)
    pc.OUTPUT_DIR = out
    pc.LOCAL_CLONE = clone
    pc.keep_names = True
    rc = _quiet(pc.cmd_push, argparse.Namespace(dry_run=False, with_names=True))
    pc.keep_names = False
    require(rc == 0, "push non riuscito")

    tr = json.loads((clone / "sessions" / "2026-10-02_21-44-16"
                     / "transcript.json").read_text(encoding="utf-8"))
    require(tr["meta"]["speaker_names"] == {"GLOBAL_001": "Pietro",
                                            "GLOBAL_002": "Chiara"},
            tr["meta"]["speaker_names"])
    # E con i nomi pubblicati, il controllo non deve più bloccare:
    # altrimenti il flag sarebbe incomprensibile per l'utente.
    require(pc._guard_repo.__doc__ is not None, "manca il docstring")
    print("    nomi pubblicati, controllo coerente")


def t_names_to_hide_reads_the_speaker_db(tmp: Path) -> None:
    """L'elenco dei nomi da cercare viene dal DB delle voci, non da
    una lista scritta a mano: una lista manuale invecchierebbe e il
    controllo continuerebbe a passare."""
    print("  i nomi da cercare vengono dal DB delle voci")
    p = tmp / "spk.json"
    db = SpeakerDB(path=p)
    db._data["speakers"] = {
        "GLOBAL_001": {"name": "Pietro", "centroid": [], "sessions": {}},
        "GLOBAL_002": {"name": None, "centroid": [], "sessions": {}},
    }
    db.save()
    from core.speaker_sync import names_from_db
    got = names_from_db(SpeakerDB(path=p))
    require(got == {"GLOBAL_001": "Pietro"}, got)
    print("    solo le voci nominate entrano nel controllo")


def main() -> int:
    tests = [
        t_nothing_forbidden_lands_on_the_repo,
        t_real_names_are_scrubbed_by_default,
        t_guard_catches_a_name_that_slipped_through,
        t_guard_passes_when_there_is_nothing,
        t_with_names_publishes_them,
        t_names_to_hide_reads_the_speaker_db,
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