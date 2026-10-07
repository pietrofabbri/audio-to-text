"""
Revisione delle voci: ascoltarle, dare un nome, sapere quali sono nuove.

Il problema che risolve (ROADMAP, Fase 1). Il DB delle voci sa unire,
separare e nominare (`review_speakers.py name/merge/split`), ma non c'era
il **momento** in cui il sistema chiede «chi e' questa voce?», e non
c'era modo di **sentirla** prima di rispondere. Un nome dato a orecchio
su un `GLOBAL_035` letto in una tabella e' un nome tirato a indovinare; un
nome dato dopo aver sentito tre frasi e' una decisione.

Tre pezzi:

  - `prepara_ascolto(gid)` sceglie 3-5 estratti brevi in cui parla solo
    quella voce, dalle sessioni piu' diverse possibile, e li taglia
    dall'audio originale con ffmpeg. Ogni estratto ha accanto il testo
    trascritto, cosi' si sente e si legge insieme.
  - `voci_da_rivedere(db)` dice quali voci non hai ancora guardato:
    senza nome, mai segnate come viste, con abbastanza parlato da
    contare. Le voci sotto la soglia di parlato non si propongono:
    chiedere un nome a ogni frase raccolta per strada e' rumore.
  - `prepara_revisione_notturna(...)` lo fa dopo il giro di notte, e
    taglia subito gli estratti delle voci nuove. **Il perche' del
    subito:** l'audio originale vive nell'archivio solo 7 giorni
    (`sync_device.py purge`), e il 7 ottobre la sessione
    2026-10-05_09-39-09 non aveva gia' piu' l'originale da nessuna parte,
    a due giorni dalla registrazione. Un estratto tagliato
    la notte stessa resta; uno chiesto fra tre settimane no.

Dove vanno gli estratti. In `data/ascolto/`, accanto al DB delle voci, e
per la stessa ragione: sono audio di persone, e `data/` e' fuori dal
repo (`.gitignore`) e fuori dal corpus pubblicato. Il nome del file e'
`<sessione>_<decimi di secondo>.mp3` e la cartella e' piatta, senza
sottocartelle per voce: dopo un `merge` gli estratti della voce assorbita
diventano della voce che resta senza spostare niente, perche' quale voce
parla si ricalcola ogni volta dalla trascrizione.

Come si sceglie un estratto. Le parole di `transcript.json` portano
ciascuna l'etichetta locale del parlante (`SPEAKER_02`) com'era **prima**
della fusione dei frammenti (`speaker_merge.json`): l'etichetta si
risolve lungo la catena delle fusioni e poi si traduce nella voce
globale tramite i segmenti. Un estratto e' una corsa di parole
consecutive della stessa voce, senza buchi lunghi, da 3 a 12 secondi,
in un segmento di qualita' `ok`. Fra le corse possibili vince quella
lunga e sentita bene (durata per probabilita' media delle parole), e si
pesca a giro fra le sessioni: tre estratti dalla stessa ora di
registrazione dicono meno di tre estratti da tre giorni diversi.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.config import INPUT_DIR, OUTPUT_DIR, ROOT_DIR, config  # noqa: E402

logger = logging.getLogger("audio-to-text.voice_review")

CLIP_DIR = ROOT_DIR / "data" / "ascolto"
ARCHIVE_DIR = ROOT_DIR / "archive"

# Sotto questo parlato totale una voce non viene proposta per un nome.
# Un minuto e' circa una decina di frasi: abbastanza per riconoscere
# qualcuno ascoltandolo, e sopra le voci di passaggio (delle 23 voci
# nate il 5 ottobre, 4 avevano meno di un minuto in tutto e altre 4 meno
# di due).
MIN_SECONDI_DA_RIVEDERE = 60.0

ESTRATTO_MIN_SEC = 3.0
ESTRATTO_MAX_SEC = 12.0
# Una pausa piu' lunga di cosi' fra due parole spezza la corsa: dentro
# potrebbe esserci qualcun altro che Whisper non ha trascritto.
BUCO_MAX_SEC = 0.8


@dataclass
class Estratto:
    """Un pezzo di audio in cui parla una sola voce, con il suo testo."""

    gid: str
    sessione: str
    inizio: float
    fine: float
    testo: str
    prob: float

    @property
    def durata(self) -> float:
        return self.fine - self.inizio

    @property
    def punteggio(self) -> float:
        return self.durata * self.prob

    @property
    def nome_file(self) -> str:
        return f"{self.sessione}_{int(round(self.inizio * 10)):06d}.mp3"


# ----------------------------------------------------------------------
# Scegliere gli estratti
# ----------------------------------------------------------------------

def _risolvi(locale: str | None, fusioni: dict[str, str]) -> str | None:
    """Segue la catena delle fusioni: SPEAKER_04 -> 05 -> 03."""
    visti = set()
    while locale in fusioni and locale not in visti:
        visti.add(locale)
        locale = fusioni[locale]
    return locale


def _leggi_sessione(sessione_dir: Path) -> tuple[list[dict], dict[str, str]]:
    try:
        dati = json.loads((sessione_dir / "transcript.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return [], {}
    fusioni: dict[str, str] = {}
    try:
        fusioni = json.loads((sessione_dir / "speaker_merge.json").read_text(
            encoding="utf-8")).get("merged") or {}
    except (OSError, json.JSONDecodeError):
        pass
    return dati.get("segments") or [], fusioni


def candidati_per_voce(
    gid: str,
    output_dir: Path = OUTPUT_DIR,
    min_sec: float = ESTRATTO_MIN_SEC,
    max_sec: float = ESTRATTO_MAX_SEC,
) -> list[Estratto]:
    """Tutte le corse di parole di `gid` utilizzabili come estratto."""
    out: list[Estratto] = []
    base = Path(output_dir)
    cartelle = sorted(p for p in base.iterdir() if p.is_dir()) if base.is_dir() else []
    for sdir in cartelle:
        segmenti, fusioni = _leggi_sessione(sdir)
        locali = {s.get("speaker_local") for s in segmenti
                  if s.get("speaker") == gid and s.get("speaker_local")}
        if not locali:
            continue
        sessione = sdir.name
        for seg in segmenti:
            if seg.get("speaker") != gid or seg.get("quality", "ok") != "ok":
                continue
            corsa: list[dict] = []

            def chiudi() -> None:
                if not corsa:
                    return
                parole = list(corsa)
                # Taglia a max_sec dall'inizio: l'estratto deve restare
                # breve, e le prime parole di una corsa sono le piu' pulite
                # (la fine e' dove arriva l'interruzione).
                while parole and parole[-1]["end"] - parole[0]["start"] > max_sec:
                    parole.pop()
                if not parole:
                    return
                dur = parole[-1]["end"] - parole[0]["start"]
                if dur < min_sec:
                    return
                probs = [float(w.get("prob") or 0.0) for w in parole]
                out.append(Estratto(
                    gid=gid, sessione=sessione,
                    inizio=float(parole[0]["start"]),
                    fine=float(parole[-1]["end"]),
                    testo="".join(w.get("word", "") for w in parole).strip(),
                    prob=sum(probs) / len(probs),
                ))

            for w in seg.get("words") or []:
                if w.get("start") is None or w.get("end") is None:
                    continue
                mia = _risolvi(w.get("speaker"), fusioni) in locali
                if mia and corsa and w["start"] - corsa[-1]["end"] > BUCO_MAX_SEC:
                    chiudi()
                    corsa = []
                if mia:
                    corsa.append(w)
                else:
                    chiudi()
                    corsa = []
            chiudi()
    return out


def ordina_per_varieta(candidati: Iterable[Estratto]) -> list[Estratto]:
    """Il migliore di ogni sessione, poi il secondo di ogni sessione...

    Le sessioni si visitano dalla piu' promettente. Cosi' i primi tre
    estratti vengono da tre registrazioni diverse quando ce ne sono tre.
    """
    per_sessione: dict[str, list[Estratto]] = {}
    for c in candidati:
        per_sessione.setdefault(c.sessione, []).append(c)
    for v in per_sessione.values():
        v.sort(key=lambda c: -c.punteggio)
    ordine = sorted(per_sessione, key=lambda s: -per_sessione[s][0].punteggio)
    out: list[Estratto] = []
    giro = 0
    while True:
        presi = False
        for s in ordine:
            if giro < len(per_sessione[s]):
                out.append(per_sessione[s][giro])
                presi = True
        if not presi:
            return out
        giro += 1


# ----------------------------------------------------------------------
# Tagliare l'audio
# ----------------------------------------------------------------------

def trova_audio(sessione: str, cartelle: Iterable[Path] | None = None) -> Path | None:
    """Il file originale di una sessione, dove che sia.

    Sul Mac il 7 ottobre gli originali stavano in tre posti diversi:
    `archive/`, `input/` e `input/today/`. Si cercano tutti, in
    sottocartelle comprese, e vince il primo con un'estensione audio.
    """
    estensioni = {e.lower() for e in config.accepted_extensions}
    for base in (cartelle if cartelle is not None else (ARCHIVE_DIR, INPUT_DIR)):
        base = Path(base)
        if not base.is_dir():
            continue
        for f in sorted(base.rglob(f"{sessione}.*")):
            if f.is_file() and f.suffix.lower() in estensioni:
                return f
    return None


def taglia(estratto: Estratto, sorgente: Path, dest: Path,
           ffmpeg: str | None = None) -> bool:
    """Taglia l'estratto con un margine di 0,2 s per lato, mono, MP3."""
    ffmpeg = ffmpeg or shutil.which("ffmpeg")
    if not ffmpeg:
        logger.warning("ffmpeg non trovato: estratti non tagliati")
        return False
    dest.parent.mkdir(parents=True, exist_ok=True)
    inizio = max(0.0, estratto.inizio - 0.2)
    durata = estratto.durata + 0.4
    tmp = dest.with_suffix(".tmp.mp3")
    cmd = [ffmpeg, "-nostdin", "-loglevel", "error", "-y",
           "-ss", f"{inizio:.2f}", "-t", f"{durata:.2f}",
           "-i", str(sorgente), "-ac", "1", "-b:a", "64k", str(tmp)]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.warning("taglio fallito per %s: %s", dest.name, exc)
        return False
    if r.returncode != 0 or not tmp.exists() or tmp.stat().st_size == 0:
        logger.warning("taglio fallito per %s: %s", dest.name, r.stderr.strip()[-200:])
        tmp.unlink(missing_ok=True)
        return False
    tmp.replace(dest)
    return True


@dataclass
class Ascolto:
    estratto: Estratto
    file: Path


def prepara_ascolto(
    gid: str,
    n: int = 4,
    output_dir: Path = OUTPUT_DIR,
    clip_dir: Path = CLIP_DIR,
    cartelle_audio: Iterable[Path] | None = None,
    rifai: bool = False,
) -> tuple[list[Ascolto], int]:
    """Fino a `n` estratti pronti da ascoltare per `gid`.

    Un estratto gia' tagliato si riusa: e' l'unico modo di sentire una
    voce dopo che l'archivio ha cancellato l'originale. Se un candidato
    non ha piu' l'audio si passa al successivo. Ritorna gli estratti e
    quanti candidati sono stati saltati perche' l'audio non c'e' piu'.
    """
    cartelle_audio = list(cartelle_audio) if cartelle_audio is not None else None
    pronti: list[Ascolto] = []
    senza_audio = 0
    sorgenti: dict[str, Path | None] = {}
    for c in ordina_per_varieta(candidati_per_voce(gid, output_dir)):
        if len(pronti) >= n:
            break
        dest = Path(clip_dir) / c.nome_file
        if dest.exists() and not rifai:
            pronti.append(Ascolto(c, dest))
            continue
        if c.sessione not in sorgenti:
            sorgenti[c.sessione] = trova_audio(c.sessione, cartelle_audio)
        src = sorgenti[c.sessione]
        if src is None:
            senza_audio += 1
            continue
        if taglia(c, src, dest):
            pronti.append(Ascolto(c, dest))
    return pronti, senza_audio


# ----------------------------------------------------------------------
# Quali voci rivedere
# ----------------------------------------------------------------------

def voci_da_rivedere(db, min_secondi: float = MIN_SECONDI_DA_RIVEDERE) -> list[dict[str, Any]]:
    """Le voci senza nome, mai viste, con almeno `min_secondi` di parlato.

    Per ognuna, la voce piu' somigliante fra tutte le altre, con il
    coseno fra centroidi (lo stesso numero con cui il sistema decide):
    se e' vicina alla soglia, la domanda giusta e' «e' la stessa di
    quella?» prima di «come si chiama?».
    """
    from core.speakers_merge import cosine

    prof = db.profiles()
    cent = db.centroids()
    out = []
    for gid, p in prof.items():
        if p.get("reviewed") or p.get("total_seconds", 0) < min_secondi:
            continue
        vicina, sim = None, -1.0
        for altra, v in cent.items():
            if altra == gid or gid not in cent:
                continue
            s = cosine(cent[gid], v)
            if s > sim:
                vicina, sim = altra, s
        out.append({
            "gid": gid,
            "secondi": p.get("total_seconds", 0.0),
            "sessioni": p.get("sessions", []),
            "vicina": vicina,
            "vicina_nome": prof.get(vicina, {}).get("name") if vicina else None,
            "somiglianza": round(sim, 3) if vicina else None,
        })
    return sorted(out, key=lambda v: -v["secondi"])


def formatta_promemoria(voci: list[dict[str, Any]], soglia: float,
                        estratti: dict[str, list[Ascolto]] | None = None) -> str:
    """Il promemoria in Markdown: chi e' nuovo, cosa fare, cosa ascoltare."""
    estratti = estratti or {}
    righe = ["# Voci da rivedere", ""]
    if not voci:
        righe.append("Nessuna voce nuova da rivedere.")
        return "\n".join(righe) + "\n"
    righe += [
        f"{len(voci)} voci senza nome, con almeno "
        f"{MIN_SECONDI_DA_RIVEDERE/60:.0f} minuto di parlato, che non hai "
        "ancora guardato. Per ognuna:",
        "",
        "- ascoltala: `python review_speakers.py ascolta <voce> --play`",
        "- se e' qualcuno che conosci: `python review_speakers.py name <voce> <Nome>`",
        "- se e' la stessa di un'altra voce: `python review_speakers.py merge <tenere> <unire>`",
        "- se non vuoi nominarla: `python review_speakers.py ignora <voce>`",
        "",
    ]
    for v in voci:
        sess = v["sessioni"]
        giorni = sorted({s[:10] for s in sess})
        quante = "1 sessione" if len(sess) == 1 else f"{len(sess)} sessioni"
        righe.append(f"## {v['gid']} — {v['secondi']/60:.1f} min in "
                     f"{quante} ({', '.join(giorni)})")
        if v["vicina"]:
            chi = v["vicina_nome"] or v["vicina"]
            vicino = v["somiglianza"] >= soglia - 0.06
            righe.append(
                f"Voce piu' somigliante: {chi} ({v['vicina']}), coseno "
                f"{v['somiglianza']:.3f}"
                + (" — vicina alla soglia: ascoltale tutte e due prima di "
                   "darle un nome." if vicino else ".")
            )
        for a in estratti.get(v["gid"], []):
            righe.append(f"- `{a.file.name}` «{a.estratto.testo}»")
        righe.append("")
    return "\n".join(righe)


def prepara_revisione_notturna(
    db,
    dest_md: Path,
    output_dir: Path = OUTPUT_DIR,
    clip_dir: Path = CLIP_DIR,
    n_estratti: int = 3,
    taglia_estratti: bool = True,
) -> dict[str, Any]:
    """Scrive il promemoria e taglia gli estratti delle voci da rivedere.

    Non solleva: e' l'ultimo passo della notte e un difetto qui non deve
    far sembrare fallita l'elaborazione. Ogni voce che non si riesce a
    preparare viene contata e il motivo va nel log.
    """
    voci = voci_da_rivedere(db)
    estratti: dict[str, list[Ascolto]] = {}
    senza_audio = 0
    if taglia_estratti:
        for v in voci:
            try:
                pronti, mancanti = prepara_ascolto(
                    v["gid"], n=n_estratti, output_dir=output_dir,
                    clip_dir=clip_dir)
            except Exception as exc:  # noqa: BLE001
                logger.warning("estratti di %s non preparati: %s", v["gid"], exc)
                continue
            estratti[v["gid"]] = pronti
            senza_audio += mancanti
    dest_md = Path(dest_md)
    dest_md.parent.mkdir(parents=True, exist_ok=True)
    dest_md.write_text(formatta_promemoria(voci, db.threshold, estratti),
                       encoding="utf-8")
    return {
        "voci": len(voci),
        "estratti": sum(len(v) for v in estratti.values()),
        "senza_audio": senza_audio,
        "file": str(dest_md),
    }
