"""
La giornata: tutte le sessioni di un giorno in un file per tipo.

Il problema che risolve (ROADMAP, Fase 2). Il registratore spezza le
registrazioni in file da un'ora, e il corpus le pubblicava cosi': una
cartella per file, dieci-dodici file ciascuna. Un giorno erano sette
cartelle e ottanta file, e una conversazione che attraversava le 10:39
era divisa a meta' fra due cartelle con due orologi che ripartivano
entrambi da 00:00:00. Per leggere una giornata — che e' l'unita' che
interessa — bisognava ricucirla a mano.

Qui la giornata diventa una cartella sola, `giorni/AAAA-MM-GG/`, con un
file per tipo in cui le sessioni sono gia' in fila, con l'**ora vera**
dell'orologio del registratore al posto del tempo dall'inizio del file:

    giorno.json          manifesto: sessioni, orari, blocchi continui, voci,
                         decisioni di denoise e fusioni, una riga per file
    transcript.txt       il testo del giorno, leggibile
    transcript.srt       sottotitoli con l'ora del giorno
    segments.jsonl       un segmento per riga, con sessione e ora vera
    tokens.jsonl         una parola per riga, con sessione, ora vera e voce
    prosody.csv          un segmento per riga, con sessione e ora vera
    wordfreq.csv         frequenze sommate sul giorno, per voce
    analysis_ready.md    il giorno intero, pronto per un LLM
    *.corrected.*        le stesse viste col testo corretto, dove esiste

Cosa NON si unisce e perche'. L'elaborazione resta per file: checkpoint,
giro notturno, limiti termici e recupero dei file troncati funzionano
cosi' e non si toccano. L'unione e' una vista costruita a valle, alla
pubblicazione. `transcript.json` (circa 800 KB a sessione) non si pubblica
piu': contiene parola per parola le stesse cose di `segments.jsonl` piu'
`tokens.jsonl`. `speaker_profiles.json` e' una fotografia del DB delle voci
presa a fine sessione, e la vista aggiornata e' `voices/voice_matrix.json`.
`denoise_decision.json` e `speaker_merge.json` entrano nel manifesto.

Le regole (ROADMAP D3, D4):

  - **Blocco continuo**: file consecutivi separati da meno di 5 minuti.
    Il TileRec spezza ogni ora e fra un file e il successivo perde da
    pochi secondi a ~3 minuti (misurato il 2 e il 5 ottobre); le pause
    vere del 4 ottobre andavano da 4 a 23 minuti.
  - **Un blocco appartiene al giorno in cui comincia**, anche se passa la
    mezzanotte: una conversazione serale non si taglia in due.
  - **L'ora vera** e' l'inizio del file (dal nome che gli da' il
    registratore) piu' la posizione nel file. La deriva dell'orologio del
    TileRec non e' ancora misurata: gli orari valgono al minuto.
  - **I campi originali restano.** `start`/`end` di un segmento restano
    i secondi dall'inizio del suo file, accanto a `session` e all'ora
    vera: chi deve tornare all'audio di quel file sa ancora dove.
"""

from __future__ import annotations

import csv
import io
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

logger = logging.getLogger("audio-to-text.giorno")

SOGLIA_CONTINUITA_SEC = 300.0

# I file che una giornata pubblica sempre, e quelli che compaiono solo se
# almeno una sessione del giorno ha il testo corretto.
FILE_GIORNO = (
    "giorno.json", "transcript.txt", "transcript.srt", "segments.jsonl",
    "tokens.jsonl", "prosody.csv", "wordfreq.csv", "analysis_ready.md",
)
FILE_GIORNO_CORRETTI = (
    "transcript.corrected.txt", "transcript.corrected.srt",
    "segments.corrected.jsonl", "text_correction.json",
)


# ----------------------------------------------------------------------
# Lettura di una sessione
# ----------------------------------------------------------------------

def _json(p: Path) -> Any:
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _jsonl(p: Path) -> list[dict]:
    out = []
    try:
        for riga in p.read_text(encoding="utf-8").splitlines():
            riga = riga.strip()
            if riga:
                try:
                    out.append(json.loads(riga))
                except json.JSONDecodeError:
                    continue
    except OSError:
        pass
    return out


def inizio_sessione(d: Path) -> datetime | None:
    """L'ora di inizio della registrazione, dalla fonte piu' affidabile.

    `session.json` (la scrive `sync_device.py` dal nome del file), poi
    `transcript.json`, poi il nome della cartella, che il TileRec compone
    come `AAAA-MM-GG_hh-mm-ss`. Stesso ordine dell'indice.
    """
    for doc in (_json(d / "session.json") or {},
                (_json(d / "transcript.json") or {}).get("meta") or {}):
        wall = doc.get("session_start_wall")
        if wall:
            try:
                return datetime.fromisoformat(str(wall)).replace(tzinfo=None)
            except ValueError:
                pass
    try:
        return datetime.strptime(d.name[:19], "%Y-%m-%d_%H-%M-%S")
    except ValueError:
        return None


@dataclass
class Sessione:
    stem: str
    cartella: Path
    inizio: datetime
    durata: float
    parlato: float
    meta: dict
    segmenti: list[dict]
    gap_prima: float | None = None   # secondi dalla fine della precedente
    blocco: int = 0

    @property
    def fine(self) -> datetime:
        return self.inizio + timedelta(seconds=self.durata)

    def ora(self, secondi: float) -> datetime:
        return self.inizio + timedelta(seconds=float(secondi or 0.0))


def leggi_sessione(d: Path) -> Sessione | None:
    """Una sessione pronta da unire, o None se manca qualcosa che serve."""
    inizio = inizio_sessione(d)
    tr = _json(d / "transcript.json") or {}
    meta = tr.get("meta") or {}
    sj = _json(d / "session.json") or {}
    if inizio is None:
        logger.warning("%s: nessuna ora di inizio, esclusa dalla giornata", d.name)
        return None
    segmenti = _jsonl(d / "segments.jsonl") or list(tr.get("segments") or [])
    for s in segmenti:
        s.pop("words", None)       # le parole stanno in tokens.jsonl
    durata = ((sj.get("duration") or {}).get("total_sec")
              or meta.get("total_duration_sec")
              or (max((s.get("end") or 0) for s in segmenti) if segmenti else 0.0))
    parlato = ((sj.get("duration") or {}).get("speech_sec")
               or meta.get("speech_duration_sec") or 0.0)
    return Sessione(stem=d.name, cartella=d, inizio=inizio, durata=float(durata),
                    parlato=float(parlato), meta=meta, segmenti=segmenti)


# ----------------------------------------------------------------------
# Raggruppamento
# ----------------------------------------------------------------------

def raggruppa(sessioni: Iterable[Sessione],
              soglia: float = SOGLIA_CONTINUITA_SEC) -> dict[str, list[Sessione]]:
    """{AAAA-MM-GG: sessioni in ordine}, con blocchi e buchi calcolati.

    Un blocco continuo e' una catena di file con buchi non oltre `soglia`;
    il giorno di un blocco e' quello in cui comincia, quindi un file che
    continua dopo la mezzanotte resta nel giorno prima.
    """
    giorni: dict[str, list[Sessione]] = {}
    giorno = ""
    prec: Sessione | None = None
    for s in sorted(sessioni, key=lambda x: (x.inizio, x.stem)):
        continua = prec is not None and (s.inizio - prec.fine).total_seconds() <= soglia
        if not continua:
            giorno = s.inizio.strftime("%Y-%m-%d")
        lista = giorni.setdefault(giorno, [])
        if continua:
            s.blocco = lista[-1].blocco
        else:
            s.blocco = lista[-1].blocco + 1 if lista else 0
        lista.append(s)
        prec = s
    for lista in giorni.values():
        lista[0].gap_prima = None
        for a, b in zip(lista, lista[1:]):
            b.gap_prima = (b.inizio - a.fine).total_seconds()
    return giorni


# ----------------------------------------------------------------------
# Formattazione
# ----------------------------------------------------------------------

def _hms(dt: datetime) -> str:
    return dt.strftime("%H:%M:%S")


def _secondi_del_giorno(dt: datetime, giorno: str) -> float:
    base = datetime.strptime(giorno, "%Y-%m-%d")
    return round((dt - base).total_seconds(), 3)


def _durata(sec: float | None) -> str:
    if not sec or sec < 1:
        return "0 s"
    sec = int(round(sec))
    h, r = divmod(sec, 3600)
    m, s = divmod(r, 60)
    if h:
        return f"{h} h {m:02d} min"
    if m:
        return f"{m} min {s:02d} s" if m < 10 else f"{m} min"
    return f"{s} s"


def _n(x: int) -> str:
    return f"{x:,}".replace(",", ".")


def _srt_ts(dt: datetime, giorno: str) -> str:
    """Ora del giorno in formato SRT; oltre la mezzanotte continua a contare."""
    tot = max(0.0, _secondi_del_giorno(dt, giorno))
    h, r = divmod(int(tot), 3600)
    m, s = divmod(r, 60)
    ms = int(round((tot - int(tot)) * 1000))
    if ms == 1000:
        s, ms = s + 1, 0
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def _etichetta(gid: str, nomi: dict[str, str]) -> str:
    nome = nomi.get(gid)
    return f"{nome} ({gid})" if nome else gid


def _parole(testo: str | None) -> int:
    return len((testo or "").split())


# ----------------------------------------------------------------------
# Composizione
# ----------------------------------------------------------------------

@dataclass
class Giornata:
    giorno: str
    sessioni: list[Sessione]
    nomi: dict[str, str] = field(default_factory=dict)

    # --- riepiloghi ---------------------------------------------------

    def voci(self) -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        for s in self.sessioni:
            for seg in s.segmenti:
                g = seg.get("speaker") or "UNKNOWN"
                v = out.setdefault(g, {"seconds": 0.0, "words": 0,
                                       "segments": 0, "sessions": []})
                v["seconds"] += float(seg.get("duration_sec")
                                      or (seg.get("end", 0) - seg.get("start", 0)) or 0)
                v["words"] += _parole(seg.get("text"))
                v["segments"] += 1
                if s.stem not in v["sessions"]:
                    v["sessions"].append(s.stem)
        for v in out.values():
            v["seconds"] = round(v["seconds"], 1)
        return dict(sorted(out.items(), key=lambda kv: -kv[1]["seconds"]))

    def blocchi(self) -> list[dict[str, Any]]:
        out: dict[int, list[Sessione]] = {}
        for s in self.sessioni:
            out.setdefault(s.blocco, []).append(s)
        return [{
            "block": b,
            "start": ss[0].inizio.isoformat(),
            "end": ss[-1].fine.isoformat(),
            "sessions": [s.stem for s in ss],
            "speech_sec": round(sum(s.parlato for s in ss), 1),
        } for b, ss in sorted(out.items())]

    def totali(self) -> dict[str, Any]:
        segs = [g for s in self.sessioni for g in s.segmenti]
        q = {"ok": 0, "low": 0, "unreliable": 0}
        for g in segs:
            q[g.get("quality", "ok")] = q.get(g.get("quality", "ok"), 0) + 1
        return {
            "sessions": len(self.sessioni),
            "first_start": self.sessioni[0].inizio.isoformat(),
            "last_end": self.sessioni[-1].fine.isoformat(),
            "recorded_sec": round(sum(s.durata for s in self.sessioni), 1),
            "speech_sec": round(sum(s.parlato for s in self.sessioni), 1),
            "segments": len(segs),
            "words": sum(_parole(g.get("text")) for g in segs),
            "quality": q,
        }

    # --- file -----------------------------------------------------------

    def manifesto(self) -> dict[str, Any]:
        sess = []
        for s in self.sessioni:
            sj = _json(s.cartella / "session.json") or {}
            den = _json(s.cartella / "denoise_decision.json") or {}
            mrg = _json(s.cartella / "speaker_merge.json") or {}
            sess.append({
                "stem": s.stem,
                "file": s.meta.get("file") or sj.get("file"),
                "start": s.inizio.isoformat(),
                "end": s.fine.isoformat(),
                "block": s.blocco,
                "gap_before_sec": None if s.gap_prima is None else round(s.gap_prima, 1),
                "duration_sec": round(s.durata, 1),
                "speech_sec": round(s.parlato, 1),
                "segments": len(s.segmenti),
                "words": sum(_parole(g.get("text")) for g in s.segmenti),
                "speakers": sorted({g.get("speaker") or "UNKNOWN" for g in s.segmenti}),
                "partial": bool(sj.get("parziale") or s.meta.get("parziale")),
                "processed_at": s.meta.get("processed_at") or sj.get("processed_at"),
                "denoise": {"winner": den.get("winner") or s.meta.get("denoise_winner"),
                            "reasons": den.get("reasons") or []},
                "speaker_merge": mrg.get("merged") or {},
                "corrected": (s.cartella / "segments.corrected.jsonl").exists(),
            })
        doc = {
            "day": self.giorno,
            "continuity_threshold_sec": SOGLIA_CONTINUITA_SEC,
            "totals": self.totali(),
            "blocks": self.blocchi(),
            "sessions": sess,
            "speakers": self.voci(),
            "speaker_names": dict(self.nomi),
            "notes": [
                "start/end dei segmenti sono secondi dall'inizio del file "
                "della loro sessione; clock_* e day_sec_* sono l'ora vera "
                "(orologio del registratore, deriva non misurata).",
                "Un blocco e' una catena di file con buchi sotto "
                f"{int(SOGLIA_CONTINUITA_SEC)} s; un blocco appartiene al "
                "giorno in cui comincia.",
            ],
        }
        return doc

    def _segmenti_con_ora(self, corretti: bool = False) -> list[dict]:
        out = []
        n = 0
        for s in self.sessioni:
            segs = s.segmenti
            if corretti:
                c = _jsonl(s.cartella / "segments.corrected.jsonl")
                if not c:
                    continue
                segs = c
            for seg in segs:
                seg = {k: v for k, v in seg.items() if k != "words"}
                a, b = s.ora(seg.get("start", 0)), s.ora(seg.get("end", 0))
                seg.update({
                    "session": s.stem,
                    "day_idx": n,
                    "clock_start": _hms(a), "clock_end": _hms(b),
                    "day_sec_start": _secondi_del_giorno(a, self.giorno),
                    "day_sec_end": _secondi_del_giorno(b, self.giorno),
                })
                out.append(seg)
                n += 1
        return out

    def segments_jsonl(self, corretti: bool = False) -> str:
        return "".join(json.dumps(s, ensure_ascii=False) + "\n"
                       for s in self._segmenti_con_ora(corretti))

    def tokens_jsonl(self) -> str:
        """Una parola per riga, con sessione, ora vera, voce e segmento.

        Per sessione, ogni parola ripeteva tutta la prosodia del suo
        segmento (F0, intensita', jitter... sedici campi uguali per ogni
        parola del segmento): sul 5 ottobre faceva 20 MB per un giorno.
        Qui quei campi si tolgono, perche' stanno una volta sola in
        `segments.jsonl`, e la parola porta `day_segment_idx` per
        ritrovarli. Restano i campi della parola: testo, tempi, durata,
        probabilita' ASR, etichetta locale e voce globale.
        """
        righe = []
        n_seg = 0
        for s in self.sessioni:
            voce: dict[Any, str] = {}
            giorno_idx: dict[Any, int] = {}
            prosodia: dict[Any, set] = {}
            for seg in s.segmenti:
                voce[seg.get("idx")] = seg.get("speaker") or "UNKNOWN"
                giorno_idx[seg.get("idx")] = n_seg
                prosodia[seg.get("idx")] = set(seg.get("prosody") or {}) | {"duration_sec"}
                n_seg += 1
            for t in _jsonl(s.cartella / "tokens.jsonl"):
                i = t.get("segment_idx")
                via = prosodia.get(i, {"duration_sec"})
                t = {k: v for k, v in t.items() if k not in via}
                a = s.ora(t.get("start", 0))
                t.update({
                    "session": s.stem,
                    "day_segment_idx": giorno_idx.get(i),
                    "speaker_global": voce.get(i, "UNKNOWN"),
                    "clock": _hms(a),
                    "day_sec": _secondi_del_giorno(a, self.giorno),
                })
                righe.append(json.dumps(t, ensure_ascii=False) + "\n")
        return "".join(righe)

    def prosody_csv(self) -> str:
        segs = self._segmenti_con_ora()
        chiavi: list[str] = []
        for g in segs:
            for k in (g.get("prosody") or {}):
                if k not in chiavi:
                    chiavi.append(k)
        base = ["session", "clock_start", "clock_end", "day_sec_start", "idx",
                "start", "end", "duration_sec", "speaker", "word_count",
                "no_speech_prob", "quality", "quality_reasons", "text_preview"]
        buf = io.StringIO()
        w = csv.writer(buf, lineterminator="\n")
        w.writerow(base + [f"p_{k}" if k in base else k for k in chiavi])
        for g in segs:
            p = g.get("prosody") or {}
            w.writerow([
                g["session"], g["clock_start"], g["clock_end"], g["day_sec_start"],
                g.get("idx"), g.get("start"), g.get("end"), g.get("duration_sec"),
                g.get("speaker"), _parole(g.get("text")), g.get("no_speech_prob"),
                g.get("quality"), ";".join(g.get("quality_reasons") or []),
                (g.get("text") or "")[:200],
            ] + [p.get(k) for k in chiavi])
        return buf.getvalue()

    def wordfreq_csv(self) -> str:
        """Frequenze del giorno: la somma, parola per parola e voce per voce."""
        tot: dict[str, dict[str, int]] = {}
        voci: dict[str, int] = {}
        for s in self.sessioni:
            p = s.cartella / "wordfreq.csv"
            if not p.exists():
                continue
            with p.open(encoding="utf-8", newline="") as f:
                for r in csv.DictReader(f):
                    parola = r.get("word")
                    if not parola:
                        continue
                    riga = tot.setdefault(parola, {})
                    for k, v in r.items():
                        if not k or not k.startswith("freq_"):
                            continue
                        try:
                            n = int(float(v or 0))
                        except ValueError:
                            continue
                        riga[k] = riga.get(k, 0) + n
                        if k != "freq_global":
                            voci[k] = voci.get(k, 0) + n
        colonne = ["freq_global"] + sorted(voci, key=lambda k: -voci[k])
        buf = io.StringIO()
        w = csv.writer(buf, lineterminator="\n")
        w.writerow(["word"] + colonne)
        for parola, r in sorted(tot.items(), key=lambda kv: (-kv[1].get("freq_global", 0), kv[0])):
            w.writerow([parola] + [r.get(c, 0) for c in colonne])
        return buf.getvalue()

    def _intestazione(self) -> list[str]:
        t = self.totali()
        blocchi = self.blocchi()
        etich = "pseudonimi `GLOBAL_xxx`" if not self.nomi else \
            "nome e pseudonimo, dove il nome e' stato dato"
        return [
            f"Giornata {self.giorno}: {t['sessions']} registrazioni dalle "
            f"{_hms(self.sessioni[0].inizio)} alle {_hms(self.sessioni[-1].fine)}, "
            f"{_durata(t['speech_sec'])} di parlato, {_n(t['words'])} parole.",
            f"Blocchi continui: {len(blocchi)} ("
            + ", ".join(f"{b['start'][11:16]}–{b['end'][11:16]}" for b in blocchi) + ").",
            f"Le voci compaiono come {etich}. Gli orari sono quelli "
            "dell'orologio del registratore.",
        ]

    def _riga_sessione(self, s: Sessione, i: int) -> str:
        if i == 0:
            come = "inizio della giornata"
        elif s.gap_prima is not None and s.gap_prima <= SOGLIA_CONTINUITA_SEC:
            come = f"continua, {_durata(max(0.0, s.gap_prima))} dopo la fine del file precedente"
        else:
            come = f"dopo una pausa di {_durata(s.gap_prima)}"
        return f"════ {_hms(s.inizio)} · {s.stem} · {come} ════"

    def transcript_txt(self, corretti: bool = False) -> str:
        righe = [f"# {self.giorno}", ""] + self._intestazione()
        if corretti:
            con = [s.stem for s in self.sessioni
                   if (s.cartella / "segments.corrected.jsonl").exists()]
            righe.append(f"Testo corretto dal modello di lingua per {len(con)} "
                         f"sessioni su {len(self.sessioni)}; le altre col testo originale.")
        righe.append("")
        for i, s in enumerate(self.sessioni):
            segs = s.segmenti
            if corretti:
                c = _jsonl(s.cartella / "segments.corrected.jsonl")
                segs = c or segs
            righe += [self._riga_sessione(s, i), ""]
            gruppo: list[dict] = []

            def chiudi() -> None:
                if not gruppo:
                    return
                a = s.ora(gruppo[0].get("start", 0))
                b = s.ora(gruppo[-1].get("end", 0))
                righe.append(f"[{_hms(a)} → {_hms(b)}] "
                             f"{_etichetta(gruppo[0].get('speaker') or 'UNKNOWN', self.nomi)}")
                righe.extend((g.get("text") or "").strip() for g in gruppo)
                righe.append("")

            for g in segs:
                if gruppo and g.get("speaker") != gruppo[-1].get("speaker"):
                    chiudi()
                    gruppo = []
                gruppo.append(g)
            chiudi()
        return "\n".join(righe).rstrip() + "\n"

    def transcript_srt(self, corretti: bool = False) -> str:
        blocchi = []
        n = 1
        for s in self.sessioni:
            segs = s.segmenti
            if corretti:
                segs = _jsonl(s.cartella / "segments.corrected.jsonl") or segs
            for g in segs:
                a, b = s.ora(g.get("start", 0)), s.ora(g.get("end", 0))
                blocchi.append(
                    f"{n}\n{_srt_ts(a, self.giorno)} --> {_srt_ts(b, self.giorno)}\n"
                    f"[{_etichetta(g.get('speaker') or 'UNKNOWN', self.nomi)}] "
                    f"{(g.get('text') or '').strip()}\n")
                n += 1
        return "\n".join(blocchi)

    def analysis_md(self) -> str:
        t = self.totali()
        q = t["quality"]
        righe = [f"# Giornata: {self.giorno}", "", "## Metadata", ""]
        righe += [f"- {r}" for r in self._intestazione()]
        righe += [
            f"- **Registrato:** {_durata(t['recorded_sec'])}; **parlato:** "
            f"{_durata(t['speech_sec'])}",
            f"- **Segmenti:** {_n(t['segments'])}; **qualità:** {q.get('ok', 0)} "
            f"affidabili, {q.get('low', 0)} scarsi, {q.get('unreliable', 0)} inaffidabili",
            "",
            "> I segmenti marcati `low` o `unreliable` in `segments.jsonl` contengono "
            "testo che il modello ha indovinato, ripetuto o attribuito ad audio "
            "senza parlato. Vanno letti, non pesati come dati.",
            "", "### Voci", "",
        ]
        tot_parole = max(1, t["words"])
        for g, v in self.voci().items():
            righe.append(f"- **{_etichetta(g, self.nomi)}** — {_durata(v['seconds'])} di "
                         f"parlato, {_n(v['words'])} parole "
                         f"({100 * v['words'] / tot_parole:.0f}%), in "
                         f"{len(v['sessions'])} registrazioni")
        righe += ["", "### Registrazioni", "",
                  "| Ora | Sessione | Parlato | Parole | Voci |", "|---|---|---|---|---|"]
        for s in self.sessioni:
            righe.append(
                f"| {_hms(s.inizio)}–{_hms(s.fine)} | `{s.stem}` | {_durata(s.parlato)} | "
                f"{_n(sum(_parole(g.get('text')) for g in s.segmenti))} | "
                f"{len({g.get('speaker') for g in s.segmenti})} |")
        righe += ["", "---", "", "## Trascrizione", ""]
        for i, s in enumerate(self.sessioni):
            righe += [f"### {self._riga_sessione(s, i).strip('═ ')}", ""]
            for g in s.segmenti:
                righe.append(f"**[{_etichetta(g.get('speaker') or 'UNKNOWN', self.nomi)}]** "
                             f"[{_hms(s.ora(g.get('start', 0)))}] {(g.get('text') or '').strip()}")
            righe.append("")
        return "\n".join(righe).rstrip() + "\n"

    def text_correction_json(self) -> str | None:
        doc = {}
        for s in self.sessioni:
            c = _json(s.cartella / "text_correction.json")
            if c is not None:
                doc[s.stem] = c
        if not doc:
            return None
        return json.dumps({"day": self.giorno, "sessions": doc},
                          ensure_ascii=False, indent=2)

    def file(self) -> dict[str, str]:
        """Tutti i file della giornata: {nome: contenuto}."""
        out = {
            "giorno.json": json.dumps(self.manifesto(), ensure_ascii=False, indent=2) + "\n",
            "transcript.txt": self.transcript_txt(),
            "transcript.srt": self.transcript_srt(),
            "segments.jsonl": self.segments_jsonl(),
            "tokens.jsonl": self.tokens_jsonl(),
            "prosody.csv": self.prosody_csv(),
            "wordfreq.csv": self.wordfreq_csv(),
            "analysis_ready.md": self.analysis_md(),
        }
        if any((s.cartella / "segments.corrected.jsonl").exists() for s in self.sessioni):
            out["transcript.corrected.txt"] = self.transcript_txt(corretti=True)
            out["transcript.corrected.srt"] = self.transcript_srt(corretti=True)
            out["segments.corrected.jsonl"] = self.segments_jsonl(corretti=True)
        tc = self.text_correction_json()
        if tc is not None:
            out["text_correction.json"] = tc + "\n"
        return out


def giornate(cartelle: Iterable[Path], nomi: dict[str, str] | None = None,
             soglia: float = SOGLIA_CONTINUITA_SEC) -> list[Giornata]:
    """Dalle cartelle di sessione alle giornate, in ordine di data."""
    sessioni = [s for s in (leggi_sessione(d) for d in cartelle) if s is not None]
    return [Giornata(g, ss, dict(nomi or {}))
            for g, ss in sorted(raggruppa(sessioni, soglia).items())]
