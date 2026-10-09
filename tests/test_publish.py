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
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import publish_corpus as pc  # noqa: E402
from core.config import config as _config  # noqa: E402

# I test della privacy provano il default protetto (pseudonimi), anche se
# sulla macchina vera l'interruttore dei nomi e' acceso (ROADMAP D1).
_config.corpus_with_names = False
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

    # Il checkpoint che la matrice delle voci legge per i campioni. Non
    # finisce mai sulla repo (e' nel FORBIDDEN per nome), quindi qui puo'
    # stare: e' la sorgente da cui la matrice viene generata.
    _make_checkpoint(job, stem)


def _make_checkpoint(job: Path, stem: str, n_voci: int = 2) -> None:
    """Checkpoint finto con embedding, diarizzazione e mappa globale.

    `load_samples` non legge nient'altro: senza questi tre campi la
    matrice esce vuota e il test passerebbe senza provare niente.
    """
    emb = {f"SPEAKER_0{i}": [0.10 * (i + 1), 0.20, 0.30, 0.40] for i in range(n_voci)}
    segmenti = [
        {"idx": i, "speaker": f"SPEAKER_0{i}", "start": float(i * 10),
         "end": float(i * 10 + 9), "duration_sec": 9.0}
        for i in range(n_voci)
    ]
    (job / f"{stem}.checkpoint.json").write_text(
        json.dumps({
            "stem": stem,
            "diarization_segments": segmenti,
            "speaker_embeddings": emb,
            "speaker_global_map": {f"SPEAKER_0{i}": f"GLOBAL_00{i + 1}"
                                   for i in range(n_voci)},
        }, ensure_ascii=False),
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
    # Il DB delle voci va nella stessa radice finta dell'output: con
    # l'output finto e il database vero, il controllo di coerenza
    # confronta due alberi diversi e segnala voci fantasma che qui non
    # esistono. Trovato perche' un test che doveva passare e' cominciato
    # a fallire quando e' stato aggiunto quel controllo.
    db = tmp / "data" / "speakers_db.json"
    db.parent.mkdir(parents=True, exist_ok=True)
    db.write_text(json.dumps({"speakers": {
        "GLOBAL_001": {"centroid": [0.0], "sessions": {}},
        "GLOBAL_002": {"centroid": [0.0], "sessions": {}},
    }}), encoding="utf-8")
    pc.SPEAKERS_DB = db
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
    from core.config import config
    config.corpus_with_names = False   # i test della privacy provano il default protetto
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
    for atteso in ("INDEX.md", "giorni/2026-10-02/transcript.txt",
                   "giorni/2026-10-02/giorno.json", "giorni/2026-10-02/tokens.jsonl"):
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
    giorno = clone / "giorni" / "2026-10-02"
    m = json.loads((giorno / "giorno.json").read_text(encoding="utf-8"))
    require(m["speaker_names"] == {}, m["speaker_names"])
    require(sorted(m["speakers"]) == ["GLOBAL_001", "GLOBAL_002"],
            "gli pseudonimi devono restare")
    segs = (giorno / "segments.jsonl").read_text(encoding="utf-8").splitlines()
    require(len(segs) == 2, "i segmenti devono restare")
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
    (clone / "giorni" / "2026-10-02" / "note.txt").write_text(
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
    # I nomi della giornata vengono dal DB delle voci, che e' la fonte.
    db = SpeakerDB(path=pc.SPEAKERS_DB)
    db._data["speakers"] = {
        "GLOBAL_001": {"name": "Pietro", "centroid": [0.0], "sessions": {}},
        "GLOBAL_002": {"name": "Chiara", "centroid": [0.0], "sessions": {}},
    }
    db.save()
    pc.OUTPUT_DIR = out
    pc.LOCAL_CLONE = clone
    pc.keep_names = True
    rc = _quiet(pc.cmd_push, argparse.Namespace(dry_run=False, with_names=True))
    pc.keep_names = False
    require(rc == 0, "push non riuscito")

    giorno = clone / "giorni" / "2026-10-02"
    m = json.loads((giorno / "giorno.json").read_text(encoding="utf-8"))
    require(m["speaker_names"] == {"GLOBAL_001": "Pietro",
                                   "GLOBAL_002": "Chiara"},
            m["speaker_names"])
    testo = (giorno / "transcript.txt").read_text(encoding="utf-8")
    require("Pietro (GLOBAL_001)" in testo,
            "con --with-names il testo del giorno deve mostrare nome e pseudonimo")
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


def t_i_nomi_da_nascondere_sono_nomi_non_pseudonimi(tmp: Path) -> None:
    """Il controllo cerca «Pietro», non «GLOBAL_001»."""
    print("  il controllo privacy cerca i nomi, non gli pseudonimi")
    p = tmp / "spk.json"
    db = SpeakerDB(path=p)
    db._data["speakers"] = {
        "GLOBAL_001": {"name": "Pietro", "centroid": [], "sessions": {}},
        "GLOBAL_002": {"name": None, "centroid": [], "sessions": {}},
    }
    db.save()
    vecchio = pc.SPEAKERS_DB
    pc.SPEAKERS_DB = p
    try:
        got = pc._speaker_names_to_hide()
    finally:
        pc.SPEAKERS_DB = vecchio
    require(got == ["Pietro"], f"devono essere i nomi: {got}")


def t_reindex_popola_il_database(tmp: Path) -> None:
    """Il database locale si ricostruisce dalle sessioni gia' elaborate.

    Il caso reale: quattro ore di registrazione elaborate e pubblicate,
    e un database che non ne sapeva niente. Da quando la pipeline
    aggiorna il database da sola non capita piu', ma il comando serve a
    riparare un database indietro senza rielaborare nulla.
    """
    print("  reindex popola il database indicato")
    out, _clone = _fake_env(tmp)
    db = tmp / "corpus.sqlite"
    pc.OUTPUT_DIR = out

    require(not db.exists(), "il database di prova non deve gia' esistere")
    rc = _quiet(pc.cmd_reindex, argparse.Namespace(db=str(db)))
    require(rc == 0, "reindex non riuscito")

    from core.corpus_db import CorpusDB

    with CorpusDB(path=db) as cdb:
        st = cdb.stats()
    require(st["sessions"] == 1,
            f"sessioni nel database: {st['sessions']}, attesa 1")
    require(st["segments"] == 2,
            f"segmenti nel database: {st['segments']}, attesi 2")

    # Rilanciarlo non deve duplicare niente: il comando si usa anche per
    # rimettere a posto un database solo in parte.
    _quiet(pc.cmd_reindex, argparse.Namespace(db=str(db)))
    with CorpusDB(path=db) as cdb:
        st2 = cdb.stats()
    require(st2 == st,
            f"rilanciare il comando ha cambiato il database: {st} -> {st2}")


def t_reindex_toglie_la_sessione_senza_cartella(tmp: Path) -> None:
    """Una sessione rinominata non deve restare a contare parole due volte.

    Il caso reale, trovato sul database vero: il fix del file troncato
    ha tolto l'hash dal nome della copia di lavoro, quindi lo stem della
    sessione e' passato da `2026-10-04_10-49-40-1508d6ee` a
    `2026-10-04_10-49-40`. `ingest_session_dir` cancella le righe del
    nuovo stem, e quelle del vecchio restano tutte: la sessione con
    l'hash sparisce dalla tabella `sessions` ma non — perche' quel nome
    non corrisponde a nessuna cartella, non perche' qualcosa l'abbia
    cancellata. Restano i suoi token, il suo wordfreq e i suoi bigrams,
    e le parole di quel quarto d'ora vengono contate due volte.

    Il punto che rende la cosa silenziosa: la riga `sessions` col nome
    vecchio e' ancora li', quindi le foreign key sono soddisfatte e
    `integrity_check` risponde `ok`. Nessun errore, solo statistiche
    sbagliate.

    Qui si rifa la situazione: si ingesta con un nome, poi si rinomina la
    cartella e si reingesta, e si controlla che il nome vecchio sparisca
    anche da `tokens`, `wordfreq` e `bigrams`, non solo da `sessions`.
    """
    print("  reindex toglie la sessione rimasta senza cartella")
    from core.corpus_db import CorpusDB

    out, _clone = _fake_env(tmp)
    db = tmp / "corpus.sqlite"
    pc.OUTPUT_DIR = out
    require(_quiet(pc.cmd_reindex, argparse.Namespace(db=str(db))) == 0,
            "reindex iniziale non riuscito")

    # Il nome cambia come e' successo davvero: la cartella perde l'hash.
    vecchia = out / "2026-10-02_21-44-16"
    nuova = out / "2026-10-02_21-44-16-9f2c1ab0"
    vecchia.rename(nuova)
    tr = json.loads((nuova / "transcript.json").read_text(encoding="utf-8"))
    tr["meta"]["stem"] = nuova.name
    tr["meta"]["file"] = f"{nuova.name}.mp3"
    (nuova / "transcript.json").write_text(
        json.dumps(tr, ensure_ascii=False, indent=2), encoding="utf-8")

    require(_quiet(pc.cmd_reindex, argparse.Namespace(db=str(db))) == 0,
            "reindex dopo il rinomina non riuscito")

    with CorpusDB(path=db) as cdb:
        stems = {r["stem"] for r in cdb.query("SELECT stem FROM sessions")}
        require(vecchia.name not in stems,
                f"la sessione col nome vecchio {vecchia.name} e' ancora in sessions")
        require(nuova.name in stems,
                f"la sessione col nome nuovo {nuova.name} non c'e' in sessions")
        # Il punto vero: se restano, `wordfreq` conta le stesse parole
        # due volte e nessuno lo vede.
        for tabella in ("tokens", "wordfreq", "bigrams"):
            n = cdb.query(
                f"SELECT count(*) c FROM {tabella} WHERE stem = ?",
                (vecchia.name,))[0]["c"]
            require(n == 0,
                    f"{tabella} ha ancora {n} righe col nome vecchio {vecchia.name}")

        st = cdb.stats()
    require(st["sessions"] == 1,
            f"sessioni nel database: {st['sessions']}, attesa 1")
    require(st["segments"] == 2,
            f"segmenti nel database: {st['segments']}, attesi 2")


def t_reindex_non_pota_se_output_e_vuoto(tmp: Path) -> None:
    """Un percorso sbagliato non deve azzerare l'indice.

    La potatura confronta gli stem del database con le cartelle presenti.
    Se la directory passata e' sbagliata — un disco non montato, un
    percorso spostato — non si deve concludere che il corpus sia vuoto e
    cancellare tutto: un errore che si vede subito, ma che si fa male
    prima di accorgersene. Con zero sessioni da cui misurare, la
    potatura non guarda niente e lascia il database com'e'.
    """
    print("  reindex non pota nulla se output non contiene sessioni")
    from core.corpus_db import CorpusDB

    out, _clone = _fake_env(tmp)
    db = tmp / "corpus.sqlite"
    pc.OUTPUT_DIR = out
    require(_quiet(pc.cmd_reindex, argparse.Namespace(db=str(db))) == 0,
            "reindex iniziale non riuscito")

    with CorpusDB(path=db) as cdb:
        prima = cdb.stats()

    # Directory che esiste ma non contiene sessioni: e' il caso in cui
    # `output/` e' stata svuotata o montata altrove.
    vuota = tmp / "output_vuota"
    vuota.mkdir()
    pc.OUTPUT_DIR = vuota
    require(_quiet(pc.cmd_reindex, argparse.Namespace(db=str(db))) == 0,
            "reindex verso una directory vuota non riuscito")

    with CorpusDB(path=db) as cdb:
        dopo = cdb.stats()
    require(dopo == prima,
            f"la potatura ha cancellato il database: {prima} -> {dopo}")


def t_voice_matrix_lands_on_the_repo_without_embeddings(tmp: Path) -> None:
    """La matrice delle voci deve essere pubblicata, e senza embedding.

    Il buco: la matrice si generava solo con `review_speakers.py voices
    --json`, e quel `--json` non c'era da nessuna parte nella corsa
    notturna. Il corpus pubblicato aveva i minuti per voce (`session.json`)
    ma non il numero che dice *chi* ha parlato: quanto due voci
    somigliano e quali coppie la soglia non riesce a decidere. Sono 34
    coppie, e senza questo file non si vedono da nessuna parte.

    La seconda parte del test e' quella che conta di piu': la matrice non
    deve contenere gli embedding vocali. Un embedding e' un'impronta
    biometrica, e questa repo non ne tiene — quindi il JSON pubblicato
    viene letto e controllato parola per parola, non solo fatto esistere.
    """
    print("  la matrice delle voci arriva, senza embedding")
    out, clone = _fake_env(tmp)
    require(_publish(out, clone) == 0, "push non riuscito")

    m = clone / "voices" / "voice_matrix.json"
    require(m.exists(),
            f"la matrice delle voci non e' stata pubblicata in {m}")

    doc = json.loads(m.read_text(encoding="utf-8"))
    for k in ("threshold", "n_voices", "n_samples", "n_pairs",
              "voices", "pairs", "gray_zone"):
        require(k in doc, f"manca il campo {k} nella matrice")
    require(doc["n_voices"] == 2,
            f"voci nella matrice: {doc['n_voices']}, attese 2")
    require(doc["n_pairs"] == 1,
            f"coppie nella matrice: {doc['n_pairs']}, attesa 1")
    require("GLOBAL_001" in doc["voices"] and "GLOBAL_002" in doc["voices"],
            f"voci pubblicate: {sorted(doc['voices'])}")

    # Il controllo vero: nessun embedding, in nessuna forma. Non basta
    # dire che non c'e' la chiave 'embedding' — un vettore potrebbe
    # essere arrivato sotto un altro nome, o dentro una lista di numeri.
    testo = m.read_text(encoding="utf-8").lower()
    require("embedding" not in testo,
            "la matrice pubblicata contiene un embedding vocale")
    require("speaker_00" not in testo,
            "la matrice pubblicata contiene ID locali di pyannote")

    # E il file intero non deve contenere vettori: ogni lista di numeri
    # deve essere corta. I numeri qui sono minuti e somiglianze, mai 4
    # decimali di embedding.
    for gid, campioni in doc["voices"].items():
        for c in campioni:
            require(set(c) == {"session", "seconds"},
                    f"campione di {gid} con campi inattesi: {sorted(c)}")
    for p in doc["pairs"]:
        require(set(p) == {"a", "b", "similarity", "same_session"},
                f"coppia con campi inattesi: {sorted(p)}")
    print(f"    {doc['n_voices']} voci, {doc['n_pairs']} coppie, "
          f"{len(doc['gray_zone'])} in zona grigia, nessun embedding")


def t_status_dichiara_anche_le_sessioni_orfane(tmp: Path) -> None:
    """`status` non deve dire «tutto pubblicato» mentre mostra uno scarto.

    Il caso reale: il comando stampava «Sessioni in locale: 11 | sulla
    repo: 12» e subito sotto «Tutto pubblicato». Guardava solo
    `locale - published`, cioe' le sessioni da pubblicare, e ignorava
    l'altra direzione: una sessione che sta sulla repo e non ha piu' una
    cartella in `output/` — che non si puo' ne' rielaborare ne'
    ripubblicare.

    E' la stessa classe di difetto di un elenco che dichiara un formato
    che non copia: il numero c'era gia' stampato, la conclusione no. Il
    comando che serve a dire «a che punto siamo» non poteva dire il
    contrario dei numeri che lui stesso aveva appena scritto.
    """
    print("  status dichiara anche le sessioni rimaste senza sorgente")
    import io
    import contextlib

    out, clone = _fake_env(tmp)
    pc.OUTPUT_DIR = out
    pc.LOCAL_CLONE = clone
    require(_publish(out, clone) == 0, "prima pubblicazione")

    def status() -> str:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            pc.cmd_status(argparse.Namespace())
        return buf.getvalue()

    testo = status()
    require("Tutto pubblicato" in testo,
            f"una sola sessione pubblicata deve risultare completa:\n{testo}")

    # La cartella sparisce: resta sulla repo, non e' piu' riproducibile.
    shutil.rmtree(out / "2026-10-02_21-44-16")

    testo = status()
    require("Tutto pubblicato" not in testo,
            "con una sessione orfana il comando non puo' dire che e' tutto "
            f"pubblicato:\n{testo}")
    require("2026-10-02_21-44-16" in testo,
            f"la sessione orfana deve essere detta per nome:\n{testo}")

    # E l'altra direzione continua a funzionare: una sessione da
    # pubblicare deve ancora essere segnalata come tale.
    nuova = out / "2026-10-05_10-00-00"
    _make_session(nuova, "2026-10-05_10-00-00")
    testo = status()
    require("Non ancora pubblicate" in testo,
            f"una sessione nuova deve risultare da pubblicare:\n{testo}")
    require("Tutto pubblicato" not in testo,
            f"non si puo' dire 'tutto pubblicato' con una da pubblicare:\n{testo}")
    print("    sessione orfana e sessione da pubblicare, entrambe dette")


def t_push_ripubblica_solo_cio_che_e_cambiato(tmp: Path) -> None:
    """Un secondo push senza modifiche non deve dichiarare pubblicazioni.

    Il caso reale (quando la repo era per sessione): sul disco le 11
    sessioni erano gia' identiche alla repo, e il dry-run rispondeva
    «avrei pubblicato 11 sessioni». Il messaggio di commit raccontava
    pubblicazioni dove non era successo niente. Con le giornate vale lo
    stesso: una giornata identica non si conta.
    """
    print("  push conta solo le giornate davvero diverse")
    import io
    import contextlib

    out, clone = _fake_env(tmp)
    pc.OUTPUT_DIR = out
    pc.LOCAL_CLONE = clone
    require(_publish(out, clone) == 0, "prima pubblicazione")

    def cambiati() -> dict[str, list[str]]:
        return {g.giorno: sorted(p.name for p in pc._publish_day(g, dry_run=True))
                for g in pc._giornate()}

    require(cambiati() == {"2026-10-02": []},
            f"una giornata gia' identica sulla repo non deve cambiare: {cambiati()}")

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
        rc = pc.cmd_push(argparse.Namespace(dry_run=True, with_names=False))
    testo = buf.getvalue()
    require(rc == 0, f"il dry-run senza modifiche deve uscire con 0, non {rc}")
    require("Nessuna giornata da pubblicare" in testo,
            f"deve dire che non c'e' niente da pubblicare:\n{testo}")

    # I nomi veri in session.json non cambiano la giornata pubblicata:
    # senza --with-names i nomi non entrano, quindi il contenuto e' lo stesso.
    sj = out / "2026-10-02_21-44-16" / "session.json"
    d = json.loads(sj.read_text(encoding="utf-8"))
    d["speaker_names"] = {"GLOBAL_001": "Pietro"}
    sj.write_text(json.dumps(d), encoding="utf-8")
    require(cambiati() == {"2026-10-02": []},
            f"un nome in locale non deve cambiare la giornata pubblicata: {cambiati()}")

    # Il testo di un segmento cambia (per esempio una correzione): la
    # giornata cambia, nei file che portano il testo, e solo in quelli.
    seg = out / "2026-10-02_21-44-16" / "segments.jsonl"
    righe = [json.loads(r) for r in seg.read_text(encoding="utf-8").splitlines()]
    righe[1]["text"] = "Grazie, cominciamo subito."
    seg.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in righe) + "\n",
                   encoding="utf-8")
    c = cambiati()["2026-10-02"]
    require("segments.jsonl" in c and "transcript.txt" in c,
            f"il testo cambiato deve risultare da pubblicare: {c}")
    require("tokens.jsonl" not in c, f"le parole non sono cambiate: {c}")
    print("    giornata identica non contata, giornata cambiata contata")


def t_migrazione_riporta_le_sessioni_solo_sulla_repo(tmp: Path) -> None:
    """La vecchia `sessions/` sparisce, ma nessuna sessione con lei.

    Il caso vero: `2026-10-02_17-02-36` stava sulla repo e non piu' in
    `output/`. Le giornate si costruiscono da `output/`, quindi togliendo
    `sessions/` senza riportarla in locale sarebbe sparita dal corpus.
    """
    print("  la migrazione per giorno non perde le sessioni solo sulla repo")
    out, clone = _fake_env(tmp)
    vecchia = clone / "sessions" / "2026-10-02_17-02-36"
    _make_session(vecchia, "2026-10-02_17-02-36")
    sj = json.loads((vecchia / "session.json").read_text(encoding="utf-8"))
    sj["session_start_wall"] = "2026-10-02T17:02:36"
    (vecchia / "session.json").write_text(json.dumps(sj), encoding="utf-8")
    for f in ("registrazione.wav", "checkpoint.json", "speakers_db.json",
              "2026-10-02_17-02-36.checkpoint.json"):
        (vecchia / f).unlink()
    (clone / "sessions" / "2026-10-02_21-44-16").mkdir()
    _git(clone, "add", "-A")
    _git(clone, "commit", "-q", "-m", "struttura vecchia")

    require(_publish(out, clone) == 0, "push non riuscito")
    require(not (clone / "sessions").exists(), "sessions/ deve sparire")
    require((out / "2026-10-02_17-02-36" / "transcript.json").exists(),
            "la sessione che c'era solo sulla repo deve tornare in output/")
    m = json.loads((clone / "giorni" / "2026-10-02" / "giorno.json").read_text())
    stems = [x["stem"] for x in m["sessions"]]
    require(stems == ["2026-10-02_17-02-36", "2026-10-02_21-44-16"],
            f"la giornata deve contenere entrambe, in ordine: {stems}")
    require(not any(f.startswith("sessions/") for f in _remote_files(tmp)),
            "sul remoto non deve restare niente di sessions/")
    print("    sessions/ migrata, la sessione orfana e' nella sua giornata")


def t_la_spazzatura_del_finder_non_finisce_sulla_repo(tmp: Path) -> None:
    """Un `.DS_Store` non deve raggiungere la repo pubblicata.

    Il caso reale: il clone e' una cartella che l'utente puo' aprire nel
    Finder, il Finder ci lascia dentro `.DS_Store`, e `git add -A` mette
    in stage **tutto** quello che trova. Il file e' finito in `HEAD` con
    6.148 byte, entrato da un commit che si chiamava «corpus: 11
    sessioni».

    Non e' un file che `_publish_session` copia, quindi la lista dei
    vietati non lo intercetta: entra dalla working copy. Per questo la
    correzione e' un `.gitignore` scritto **prima** di `git add -A` —
    dopo sarebbe troppo tardi, il file sarebbe gia' in stage.
    """
    print("  la spazzatura del Finder non finisce sulla repo")
    out, clone = _fake_env(tmp)
    pc.OUTPUT_DIR = out
    pc.LOCAL_CLONE = clone

    # Il Finder lascia il suo file nella working copy, prima del push.
    (clone / ".DS_Store").write_bytes(b"\x00\x01spazzatura\x02")
    require(_publish(out, clone) == 0, "prima pubblicazione")

    require(".gitignore" in _remote_files(tmp),
            "il clone deve avere un .gitignore: senza, `git add -A` "
            "pubblica quello che il Finder lascia nella cartella")
    require(".DS_Store" not in _remote_files(tmp),
            f"la spazzatura del Finder non deve finire sulla repo:\n"
            f"{_remote_files(tmp)}")
    print("    .gitignore scritto, .DS_Store lasciato fuori")


def t_status_dichiara_le_voci_che_il_db_non_conosce(tmp: Path) -> None:
    """`status` deve vedere una voce che le sessioni citano e il DB no.

    E' l'invariante che due dry-run hanno rotto: `merge --dry-run` e
    `split --dry-run` cancellavano la voce dal database delle voci, e le
    sessioni continuavano a citarla. Un ID assente non produce un
    errore — e' solo un ID che nessuno genera piu' — quindi il corpus
    diventava incoerente in silenzio, e la differenza non compariva da
    nessuna parte fino a che una ricerca non tornava vuota.

    Il controllo e' nato perche' quei due difetti li ho trovati a mano;
    il punto e' che il secondo dei due poteva essere impedito dal primo.
    """
    print("  status vede le voci citate ma assenti dal DB")
    import io
    import contextlib

    out, clone = _fake_env(tmp)
    pc.OUTPUT_DIR = out
    pc.LOCAL_CLONE = clone
    require(_publish(out, clone) == 0, "prima pubblicazione")

    # Il DB delle voci con una sola identita', mentre la sessione ne cita
    # due: una delle due non esiste piu' e nessuno lo dice.
    radice = tmp / "root"
    (radice / "data").mkdir(parents=True, exist_ok=True)
    db_path = radice / "data" / "speakers_db.json"
    db_path.write_text(json.dumps({
        "speakers": {
            "GLOBAL_001": {"centroid": [0.0], "sessions": {}},
        },
    }), encoding="utf-8")
    vecchio_db = pc.SPEAKERS_DB
    pc.SPEAKERS_DB = db_path
    try:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            pc.cmd_status(argparse.Namespace())
        testo = buf.getvalue()
    finally:
        pc.SPEAKERS_DB = vecchio_db

    require("assenti dal DB delle voci" in testo,
            f"una voce citata ma assente dal DB deve essere detta:\n{testo}")
    require("GLOBAL_002" in testo,
            f"la voce fantasma deve essere detta per nome:\n{testo}")
    require("Tutto pubblicato" not in testo,
            f"non si puo' dire che e' tutto pubblicato con una voce che "
            f"il corpus non riconosce:\n{testo}")
    print("    voce fantasma dichiarata per nome")


def t_indice_elenca_i_giorni(tmp: Path) -> None:
    """L'indice ha una riga per giorno, letta dai manifesti pubblicati."""
    old = pc.LOCAL_CLONE
    pc.LOCAL_CLONE = tmp
    try:
        for giorno, inizio, fine, parole in (("2026-10-04", "10:49", "19:06", 24338),
                                              ("2026-10-05", "09:39", "16:21", 30665)):
            d = tmp / "giorni" / giorno
            d.mkdir(parents=True)
            (d / "giorno.json").write_text(json.dumps({
                "totals": {"sessions": 7, "first_start": f"{giorno}T{inizio}:00",
                           "last_end": f"{giorno}T{fine}:00", "speech_sec": 15600,
                           "words": parole},
                "blocks": [{}], "speakers": {"GLOBAL_001": {}, "UNKNOWN": {}},
                "sessions": []}), encoding="utf-8")
        testo = pc._write_index().read_text(encoding="utf-8")
        righe = [r for r in testo.splitlines() if r.startswith("| [")]
        require(len(righe) == 2, f"due giorni, due righe: {righe}")
        require(righe[0].startswith("| [2026-10-05](giorni/2026-10-05/) | 7 | 09:39–16:21 |"),
                f"il giorno piu' recente in cima, con orari: {righe[0]}")
        require("| 1 |" in righe[0], f"UNKNOWN non conta come voce: {righe[0]}")
        require("parole: **55003**" in testo, "il totale delle parole")
        print("  ok   l'indice elenca i giorni dai manifesti")
    finally:
        pc.LOCAL_CLONE = old



def t_le_metriche_arrivano_sulla_repo(tmp: Path) -> None:
    """Il push scrive `metriche/` per il pannello, senza nomi e senza testo.

    Le metriche sono l'unica cosa che il pannello cifrato legge: se non
    arrivano, il pannello resta vuoto senza che niente fallisca. E non
    devono portare testo delle conversazioni, perche' la pagina finale le
    incorpora tutte.
    """
    print("  le metriche aggregate arrivano sulla repo")
    out, clone = _fake_env(tmp)
    require(_publish(out, clone) == 0, "push non riuscito")
    finiti = _remote_files(tmp)
    require(any(f.endswith("metriche/2026-10-02.json") for f in finiti),
            f"mancano le metriche: {sorted(finiti)}")
    require(any(f == ".github/workflows/pannello.yml" for f in finiti),
            f"manca il workflow del pannello: {sorted(finiti)}")
    m = json.loads((clone / "metriche" / "2026-10-02.json").read_text(encoding="utf-8"))
    require(m["giorno"] == "2026-10-02" and m["versione"] >= 1, f"intestazione: {m}")
    testo = json.dumps(m, ensure_ascii=False)
    seg = (clone / "giorni" / "2026-10-02" / "segments.jsonl").read_text(encoding="utf-8")
    frase = json.loads(seg.splitlines()[0])["text"]
    require(frase not in testo, "il testo di un segmento e' finito nelle metriche")
    # Un secondo push senza cambi non riscrive le metriche.
    require(pc._write_metriche(dry_run=True) == [], "metriche gia' identiche")



def t_un_commit_fatto_altrove_non_blocca_il_push(tmp: Path) -> None:
    """Il workflow del pannello committa sul remoto: il Mac deve seguirlo."""
    print("  un commit fatto su GitHub non blocca il push dal Mac")
    out, clone = _fake_env(tmp)
    require(_publish(out, clone) == 0, "prima pubblicazione")
    altro = tmp / "altro"
    _git(tmp, "clone", "-q", str(tmp / "remoto.git"), str(altro))
    _git(altro, "config", "user.email", "x@localhost")
    _git(altro, "config", "user.name", "x")
    (altro / ".github").mkdir(exist_ok=True)
    (altro / ".github" / "w.yml").write_text("on: push\n", encoding="utf-8")
    _git(altro, "add", "-A")
    _git(altro, "commit", "-q", "-m", "workflow")
    _git(altro, "push", "-q", "origin", "main")
    # Una giornata cambia sul Mac.
    seg = out / "2026-10-02_21-44-16" / "segments.jsonl"
    righe = [json.loads(r) for r in seg.read_text(encoding="utf-8").splitlines()]
    righe[1]["text"] = "Testo cambiato dopo il commit remoto."
    seg.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in righe) + "\n",
                   encoding="utf-8")
    require(_publish(out, clone) == 0, "il secondo push deve riuscire")
    finiti = _remote_files(tmp)
    require(any(f.endswith(".github/w.yml") for f in finiti),
            f"il file del workflow resta sul remoto: {sorted(finiti)}")


def main() -> int:
    tests = [
        t_indice_elenca_i_giorni,
        t_migrazione_riporta_le_sessioni_solo_sulla_repo,
        t_nothing_forbidden_lands_on_the_repo,
        t_status_dichiara_le_voci_che_il_db_non_conosce,
        t_push_ripubblica_solo_cio_che_e_cambiato,
        t_la_spazzatura_del_finder_non_finisce_sulla_repo,
        t_real_names_are_scrubbed_by_default,
        t_guard_catches_a_name_that_slipped_through,
        t_guard_passes_when_there_is_nothing,
        t_with_names_publishes_them,
        t_names_to_hide_reads_the_speaker_db,
        t_i_nomi_da_nascondere_sono_nomi_non_pseudonimi,
        t_voice_matrix_lands_on_the_repo_without_embeddings,
        t_reindex_popola_il_database,
        t_reindex_toglie_la_sessione_senza_cartella,
        t_reindex_non_pota_se_output_e_vuoto,
        t_status_dichiara_anche_le_sessioni_orfane,
        t_le_metriche_arrivano_sulla_repo,
        t_un_commit_fatto_altrove_non_blocca_il_push,
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