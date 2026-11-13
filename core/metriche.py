"""
Metriche aggregate di una giornata, per il pannello.

Il pannello (documento di progetto `analisi-corpus.md`, sezioni 3–8)
non legge il testo delle conversazioni: legge numeri. Questo modulo li
calcola sul Mac, a partire dai file di una giornata del corpus
(`giorni/AAAA-MM-GG/`: `giorno.json`, `segments.jsonl`), e li scrive in
`metriche/AAAA-MM-GG.json`. GitHub Actions disegna soltanto.

Regole che il calcolo rispetta (sezione 5 del documento):

- **Maschera di copertura.** Ogni minuto e' *registrato* o no, dalle
  ore di inizio e fine dei file. Le metriche audio si danno per ora
  registrata o per 1000 parole, mai come totali del giorno.
- **Giorno valido** solo con almeno `ORE_MINIME_VALIDO` ore registrate.
- **Soggetto delle analisi psicologiche: solo Pietro** (`SOGGETTO`).
  Delle altre voci si misurano solo tempi e turni.
- **Versione 2 (28/10).** Ogni numero che il pannello presenta come
  «tuo» si calcola solo sulla voce di Pietro: quota di parlato sul
  registrato, turni presi per ora di conversazione, conversazioni a cui
  partecipa, confidenza dell'ASR e segmenti affidabili sulle sue parole.
  I valori su tutte le voci restano nel file, con il loro nome, come
  contesto dell'ambiente.
- **Versione 3 (29/10).** Le parole di Pietro sono le parole che la
  diarizzazione attribuisce a lui una per una (`tokens.jsonl`,
  `speaker_global` per parola), non tutto il testo dei segmenti con la
  sua etichetta: un segmento dura fino a 29 s e dentro parlano spesso
  in due. La prosodia si prende solo dai segmenti dove almeno
  `QUOTA_PROSODIA` delle parole sono sue. I segmenti `unreliable`
  (allucinazioni, prompt nel testo, loop) restano fuori dalle parole.
  Il costo: le parole per parola sono il testo grezzo, non quello
  corretto, e `limiti.attribuzione` lo dichiara.

Cosa NON c'e' ancora, e il file lo dichiara in `limiti`: risate (serve un
classificatore audio), saturazione (sta nell'audio, non nel corpus),
temi, tono, impegni e storie (servono un modello linguistico), voto
serale. L'Helio (`core.helio`) entra solo come `corpo`: il battito nei
minuti registrati; le sue misure sulle 24 ore stanno in `helio/`. Le metriche sulle parole sono calcolate sul testo corretto
quando esiste, altrimenti su quello grezzo, e il file dice quale.

Versione del formato: `VERSIONE`. Un cambio di significato di un campo
alza la versione.
"""

from __future__ import annotations

import json
import re
import statistics
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

VERSIONE = 3

# La voce di Pietro. GLOBAL_001 e' presente in 18 sessioni su 19 ed e'
# stata nominata «Pietro» l'8 ottobre.
SOGGETTO = "GLOBAL_001"

# Voce non attribuita dalla diarizzazione: conta come parlato, non come
# persona.
SCONOSCIUTA = "UNKNOWN"

ORE_MINIME_VALIDO = 3.0

# Sotto questa quota di parole sue, la prosodia di un segmento e' di
# qualcun altro almeno quanto di Pietro, e non la si conta come sua.
QUOTA_PROSODIA = 0.8

# Una conversazione finisce dopo un minuto senza parlato: serve a dire chi
# apre e chi chiude.
PAUSA_CONVERSAZIONE_SEC = 60.0

# Finestra del type-token ratio a media mobile (MATTR): 50 parole e' il
# valore usuale, ed e' indipendente dalla lunghezza del testo, che qui
# cambia molto da un giorno all'altro.
FINESTRA_MATTR = 50

INTERCALARI = (
    "cioè", "tipo", "praticamente", "insomma", "boh", "diciamo", "ecco",
    "comunque", "vabbè", "allora", "quindi", "niente", "senti", "guarda",
    "appunto", "magari",
)
IO = frozenset({"io", "me", "mio", "mia", "miei", "mie"})
NOI = frozenset({"noi", "nostro", "nostra", "nostri", "nostre"})
NEGAZIONI = frozenset({"non", "no", "mai", "niente", "nulla", "nessuno",
                       "nessuna"})

_PAROLA = re.compile(r"[^\W\d_]+(?:[’'][^\W\d_]+)?", re.UNICODE)
_FRASE = re.compile(r"[^.?!…]+[.?!…]*")

LIMITI_FISSI = {
    "risate": "non ancora misurate: serve un classificatore di eventi sonori",
    "saturazione": "sta nell'audio, non nel corpus: non calcolata qui",
    "temi_tono_impegni_storie": "servono un modello linguistico locale (non ancora attivo)",
    "helio": ("collegato dall'export di Zepp (circa mensile): battito al minuto, "
              "passi, sonno con le fasi, attivita'; HRV, stress e SpO2 non sono "
              "nell'export. Il confronto battito/parlato vale solo nei minuti "
              "registrati e usa l'ora del registratore"),
    "voto_serale": "meccanismo di raccolta da definire",
    "sovrapposizioni": ("i segmenti di Whisper sono in fila e non si "
                        "sovrappongono: servono i tempi della diarizzazione"),
}


# ----------------------------------------------------------------------
# Lettura
# ----------------------------------------------------------------------

def _jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    for riga in path.read_text(encoding="utf-8").splitlines():
        if riga.strip():
            out.append(json.loads(riga))
    return out


def _sec_del_giorno(iso: str, giorno: str) -> float:
    """Secondi dalla mezzanotte del giorno, oltre 86400 dopo mezzanotte."""
    t = datetime.fromisoformat(iso)
    base = datetime.fromisoformat(giorno)
    return (t - base).total_seconds()


def _parole(testo: str) -> list[str]:
    """Le parole di un testo, minuscole, con l'apostrofo che separa.

    «c'è» -> «c», «è»; «dell'altro» -> «dell», «altro». E' la divisione
    che conta per i pronomi e gli intercalari.
    """
    out = []
    for m in _PAROLA.finditer(testo or ""):
        for pezzo in re.split(r"[’']", m.group(0)):
            if pezzo:
                out.append(pezzo.casefold())
    return out


def _mediana(valori: Iterable[float | None]) -> float | None:
    v = [x for x in valori if isinstance(x, (int, float))]
    return round(statistics.median(v), 3) if v else None


def mattr(parole: list[str], finestra: int = FINESTRA_MATTR) -> float | None:
    """Type-token ratio a media mobile; None se il testo e' troppo corto."""
    if len(parole) < finestra:
        return None
    conteggi = Counter(parole[:finestra])
    somma = len(conteggi)
    for i in range(finestra, len(parole)):
        esce, entra = parole[i - finestra], parole[i]
        conteggi[esce] -= 1
        if conteggi[esce] == 0:
            del conteggi[esce]
        conteggi[entra] += 1
        somma += len(conteggi)
    n = len(parole) - finestra + 1
    return round(somma / n / finestra, 4)


# ----------------------------------------------------------------------
# Calcolo
# ----------------------------------------------------------------------

def _intervalli_registrati(manifesto: dict, giorno: str) -> list[tuple[float, float]]:
    out = []
    for s in manifesto.get("sessions", []):
        if s.get("start") and s.get("end"):
            out.append((_sec_del_giorno(s["start"], giorno),
                        _sec_del_giorno(s["end"], giorno)))
    return sorted(out)


def _minuti(intervalli: list[tuple[float, float]]) -> dict[int, float]:
    """Per ogni minuto del giorno, i secondi coperti dagli intervalli."""
    out: dict[int, float] = defaultdict(float)
    for a, b in intervalli:
        m = int(a // 60)
        while m * 60 < b:
            lo, hi = max(a, m * 60), min(b, (m + 1) * 60)
            if hi > lo:
                out[m] += hi - lo
            m += 1
    return out


def _turni(segmenti: list[dict]) -> list[dict]:
    """Sequenze consecutive della stessa voce, nell'ordine dell'ora vera."""
    turni: list[dict] = []
    for s in segmenti:
        voce = s.get("speaker") or SCONOSCIUTA
        a, b = s["day_sec_start"], s["day_sec_end"]
        if (turni and turni[-1]["voce"] == voce
                and a - turni[-1]["fine"] < PAUSA_CONVERSAZIONE_SEC):
            turni[-1]["fine"] = max(turni[-1]["fine"], b)
            turni[-1]["segmenti"] += 1
        else:
            turni.append({"voce": voce, "inizio": a, "fine": b, "segmenti": 1})
    return turni


def _conversazioni(segmenti: list[dict]) -> list[list[dict]]:
    """Gruppi di segmenti separati da almeno un minuto senza parlato."""
    gruppi: list[list[dict]] = []
    fine = None
    for s in segmenti:
        if fine is None or s["day_sec_start"] - fine >= PAUSA_CONVERSAZIONE_SEC:
            gruppi.append([])
        gruppi[-1].append(s)
        fine = s["day_sec_end"] if fine is None else max(fine, s["day_sec_end"])
    return gruppi


def _marcatori(testi: list[str]) -> dict[str, Any]:
    """Marcatori linguistici di un insieme di frasi (solo del soggetto)."""
    parole: list[str] = []
    frasi = domande = 0
    for t in testi:
        parole.extend(_parole(t))
        for f in _FRASE.findall(t or ""):
            if _parole(f):
                frasi += 1
                if f.rstrip().endswith("?"):
                    domande += 1
    n = len(parole)
    conta = Counter(parole)
    per_mille = (lambda k: round(1000 * k / n, 2)) if n else (lambda k: None)
    io = sum(conta[w] for w in IO)
    noi = sum(conta[w] for w in NOI)
    intercalari = {w: conta[w] for w in INTERCALARI if conta[w]}
    return {
        "parole": n,
        "frasi": frasi,
        "mattr": mattr(parole),
        "domande_quota": round(domande / frasi, 4) if frasi else None,
        "io_per_1000": per_mille(io),
        "noi_per_1000": per_mille(noi),
        "io_su_io_noi": round(io / (io + noi), 4) if io + noi else None,
        "negazioni_per_1000": per_mille(sum(conta[w] for w in NEGAZIONI)),
        "intercalari_per_1000": per_mille(sum(intercalari.values())),
        "intercalari": dict(sorted(intercalari.items(),
                                   key=lambda kv: -kv[1])[:8]),
    }


def _prosodia(segmenti: list[dict]) -> dict[str, Any]:
    """Mediane della prosodia, pesate per segmento (solo il soggetto)."""
    def campo(nome):
        return _mediana((s.get("prosody") or {}).get(nome) for s in segmenti
                        if s.get("quality") != "unreliable")
    return {
        "f0_media_hz": campo("f0_mean_hz"),
        "f0_variabilita_hz": campo("f0_std_hz"),
        "intensita_db": campo("intensity_mean_db"),
        "velocita_sill_sec": campo("speech_rate_syl_per_sec"),
        "pause_quota": campo("pause_ratio"),
        "segmenti": len(segmenti),
    }


def _parole_e_prosodia_di_pietro(cartella: Path, segmenti: list[dict],
                                 di_pietro: list[dict]):
    """(testi con l'ora, segmenti per la prosodia, se e' parola per parola).

    Con `tokens.jsonl` che porta la voce di ogni parola (`speaker_segment`
    accanto a `speaker_global`), le parole di Pietro sono le sue corse di
    parole consecutive; altrimenti, come prima, il testo dei segmenti con
    la sua etichetta.
    """
    def affidabile(s: dict) -> bool:
        return s.get("quality") != "unreliable"

    tok = _jsonl(cartella / "tokens.jsonl")
    if not tok or not any("speaker_segment" in x for x in tok[:200]):
        testi = [(s["day_sec_start"], s.get("text", "")) for s in di_pietro
                 if affidabile(s)]
        return testi, di_pietro, False

    per_giorno = {s.get("day_idx"): s for s in segmenti}
    tot: Counter = Counter()
    sue: Counter = Counter()
    corse: list[tuple[float, list[str]]] = []
    prec = None
    for x in tok:
        i = x.get("day_segment_idx")
        tot[i] += 1
        mia = x.get("speaker_global") == SOGGETTO
        seg = per_giorno.get(i)
        if not mia or seg is None or not affidabile(seg):
            prec = None
            continue
        sue[i] += 1
        if prec != i:
            corse.append((x.get("day_sec") or seg["day_sec_start"], []))
        corse[-1][1].append(x.get("word", ""))
        prec = i
    testi = [(sec, " ".join(p)) for sec, p in corse if p]
    prosodia = [s for i, s in per_giorno.items()
                if tot[i] and sue[i] / tot[i] >= QUOTA_PROSODIA and affidabile(s)]
    return testi, prosodia, True


def calcola_giorno(cartella: Path, helio: Any = None) -> dict[str, Any]:
    """Le metriche di una giornata del corpus.

    `helio`: le misure del braccialetto (`core.helio.carica()`), o None.
    Con le misure, il campo `corpo` confronta il battito nei minuti
    registrati in cui Pietro parla, parlano altri o c'e' silenzio
    (`core.helio.battito_e_parlato`). Senza, `corpo` e' None.
    """
    cartella = Path(cartella)
    manifesto = json.loads((cartella / "giorno.json").read_text(encoding="utf-8"))
    giorno = manifesto["day"]
    corretti = (cartella / "segments.corrected.jsonl").exists()
    segmenti = _jsonl(cartella / ("segments.corrected.jsonl" if corretti
                                  else "segments.jsonl"))
    segmenti = [s for s in segmenti
                if s.get("day_sec_start") is not None
                and s.get("day_sec_end") is not None]
    segmenti.sort(key=lambda s: (s["day_sec_start"], s["day_sec_end"]))
    nomi = manifesto.get("speaker_names") or {}

    # --- copertura (direttrice 1) ------------------------------------
    registrati = _intervalli_registrati(manifesto, giorno)
    min_reg = _minuti(registrati)
    reg_sec = sum(b - a for a, b in registrati)
    parlato_per_voce: dict[str, dict[int, float]] = defaultdict(lambda: defaultdict(float))
    for s in segmenti:
        voce = s.get("speaker") or SCONOSCIUTA
        for m, sec in _minuti([(s["day_sec_start"], s["day_sec_end"])]).items():
            parlato_per_voce[voce][m] += sec
    min_parlato: dict[int, set[str]] = defaultdict(set)
    for voce, mm in parlato_per_voce.items():
        for m, sec in mm.items():
            if sec >= 1.0:
                min_parlato[m].add(voce)

    # --- corpo (direttrice 5, scala dei minuti) -------------------------
    corpo = None
    if helio is not None:
        from core.helio import battito_e_parlato
        corpo = battito_e_parlato(helio, giorno, min_reg, min_parlato, SOGGETTO)

    per_ora: dict[int, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for m, sec in min_reg.items():
        ora = (m // 60) % 24
        per_ora[ora]["registrato_min"] += sec / 60
        voci = min_parlato.get(m, set())
        if not voci:
            per_ora[ora]["silenzio_min"] += sec / 60
        elif voci == {SOGGETTO}:
            per_ora[ora]["solo_pietro_min"] += sec / 60
        else:
            per_ora[ora]["con_altri_min"] += sec / 60
    for voce, mm in parlato_per_voce.items():
        for m, sec in mm.items():
            ora = (m // 60) % 24
            per_ora[ora]["parlato_min"] += sec / 60
            if voce == SOGGETTO:
                per_ora[ora]["parlato_pietro_min"] += sec / 60

    # --- voci, turni, conversazioni (direttrici 2 e 12) ---------------
    turni = _turni(segmenti)
    per_voce: dict[str, dict[str, Any]] = {}
    for voce, info in (manifesto.get("speakers") or {}).items():
        mie = [t for t in turni if t["voce"] == voce]
        per_voce[voce] = {
            "id": voce,
            "nome": nomi.get(voce),
            "minuti": round((info.get("seconds") or 0) / 60, 1),
            "parole": info.get("words") or 0,
            "turni": len(mie),
            "turno_medio_sec": round(statistics.mean(
                t["fine"] - t["inizio"] for t in mie), 1) if mie else None,
        }
    voci = sorted(per_voce.values(), key=lambda v: -v["minuti"])
    parlato_persone = sum(v["minuti"] for v in voci if v["id"] != SCONOSCIUTA)
    pietro_min = per_voce.get(SOGGETTO, {}).get("minuti", 0.0)

    conversazioni = []
    for gruppo in _conversazioni(segmenti):
        voci_g = {s.get("speaker") for s in gruppo} - {SCONOSCIUTA, None}
        if len(voci_g) < 2:
            continue
        sec_voce: Counter = Counter()
        for s in gruppo:
            sec_voce[s.get("speaker")] += s["day_sec_end"] - s["day_sec_start"]
        tot = sum(v for k, v in sec_voce.items() if k != SCONOSCIUTA)
        conversazioni.append({
            "inizio_sec": round(gruppo[0]["day_sec_start"]),
            "durata_min": round((max(s["day_sec_end"] for s in gruppo)
                                 - gruppo[0]["day_sec_start"]) / 60, 1),
            "voci": sorted(voci_g),
            "quota_pietro": round(sec_voce[SOGGETTO] / tot, 3) if tot else None,
            "apre": gruppo[0].get("speaker"),
            "chiude": gruppo[-1].get("speaker"),
        })
    n_conv = len(conversazioni)
    con_pietro = [c for c in conversazioni if SOGGETTO in c["voci"]]
    # Quante volte Pietro prende la parola, per ora di conversazione a cui
    # partecipa: il ritmo dei suoi interventi, non quello della stanza.
    finestre = [(c["inizio_sec"], c["inizio_sec"] + c["durata_min"] * 60)
                for c in con_pietro]
    turni_pietro = sum(1 for t in turni if t["voce"] == SOGGETTO and any(
        a <= t["inizio"] < b + 1 for a, b in finestre))
    ore_conv_pietro = sum(b - a for a, b in finestre) / 3600

    # --- Pietro: voce e parole (direttrici 4, 10, 12) ------------------
    di_pietro = [s for s in segmenti if s.get("speaker") == SOGGETTO]
    testi_pietro, prosodia_pietro, per_parola = _parole_e_prosodia_di_pietro(
        cartella, segmenti, di_pietro)

    # --- legami fra parlato e corpo (direttrice 5 con 2 e 4) -----------
    if corpo is not None:
        from core.helio import legami
        corpo["legami"] = legami(helio, giorno, min_reg, min_parlato, SOGGETTO,
                                 conversazioni, prosodia_pietro, SCONOSCIUTA)
    prosodia_per_ora: dict[int, list[dict]] = defaultdict(list)
    testi_per_ora: dict[int, list[str]] = defaultdict(list)
    for s in prosodia_pietro:
        prosodia_per_ora[int(s["day_sec_start"] // 3600) % 24].append(s)
    for sec, testo in testi_pietro:
        testi_per_ora[int(sec // 3600) % 24].append(testo)

    ore = []
    for ora in sorted(set(per_ora) | set(prosodia_per_ora)):
        r = {k: round(v, 1) for k, v in per_ora.get(ora, {}).items()}
        pro = _prosodia(prosodia_per_ora.get(ora, []))
        mar = _marcatori(testi_per_ora.get(ora, []))
        reg = r.get("registrato_min", 0.0)
        ore.append({
            "ora": ora,
            **r,
            "pietro_parole": mar["parole"],
            "pietro_parole_per_ora_reg": (round(mar["parole"] * 60 / reg, 1)
                                          if reg >= 5 else None),
            "pietro_velocita_sill_sec": pro["velocita_sill_sec"],
            "pietro_f0_media_hz": pro["f0_media_hz"],
            "pietro_f0_variabilita_hz": pro["f0_variabilita_hz"],
            "pietro_domande_quota": mar["domande_quota"],
        })

    # --- qualita' (direttrice 6) -------------------------------------
    qualita = (manifesto.get("totals") or {}).get("quality") or {}
    n_seg = sum(qualita.values()) or len(segmenti)
    # Confidenza dell'ASR: la probabilita' media delle parole. Si legge
    # da `tokens.jsonl` riga per riga, senza tenere il file in memoria.
    somma_p = n_p = 0
    bassa = 0
    # Le stesse due misure sulle sole parole di Pietro: sono quelle che
    # dicono quanto fidarsi delle sue metriche sulle parole.
    somma_pp = n_pp = bassa_p = 0
    tok = cartella / "tokens.jsonl"
    if tok.exists():
        with tok.open(encoding="utf-8") as f:
            for riga in f:
                t = json.loads(riga)
                p = t.get("asr_prob")
                if isinstance(p, (int, float)):
                    somma_p += p
                    n_p += 1
                    bassa += p < 0.5
                    if t.get("speaker_global") == SOGGETTO:
                        somma_pp += p
                        n_pp += 1
                        bassa_p += p < 0.5
    q_pietro: Counter = Counter(s.get("quality") or "ok" for s in di_pietro)

    totali = manifesto.get("totals") or {}
    parlato_sec = totali.get("speech_sec") or 0.0
    silenzio = sum(r.get("silenzio_min", 0) for r in per_ora.values())
    solo_pietro = sum(r.get("solo_pietro_min", 0) for r in per_ora.values())
    con_altri = sum(r.get("con_altri_min", 0) for r in per_ora.values())

    return {
        "versione": VERSIONE,
        "giorno": giorno,
        "giorno_settimana": datetime.fromisoformat(giorno).isoweekday(),
        "valido": reg_sec / 3600 >= ORE_MINIME_VALIDO,
        "limiti": {
            "testo": ("grezzo per le parole di Pietro (attribuite parola per parola)"
                      if per_parola else
                      "corretto" if corretti else "grezzo (non corretto)"),
            "attribuzione": "parola per parola" if per_parola else "per segmento",
            **LIMITI_FISSI,
        },
        "copertura": {
            "registrato_ore": round(reg_sec / 3600, 2),
            "parlato_ore": round(parlato_sec / 3600, 2),
            "parlato_quota": round(parlato_sec / reg_sec, 3) if reg_sec else None,
            "pietro_parlato_quota": (round(pietro_min * 60 / reg_sec, 3)
                                     if reg_sec else None),
            "intervalli": [[round(a), round(b)] for a, b in registrati],
            "blocchi": len(manifesto.get("blocks") or []),
            "prima": (totali.get("first_start") or "")[11:16],
            "ultima": (totali.get("last_end") or "")[11:16],
        },
        "silenzio": {
            "silenzio_min": round(silenzio, 1),
            "solo_pietro_min": round(solo_pietro, 1),
            "con_altri_min": round(con_altri, 1),
        },
        "voci": voci,
        "relazioni": {
            "persone": len([v for v in voci if v["id"] != SCONOSCIUTA]) - (
                1 if SOGGETTO in per_voce else 0),
            "quota_parola_pietro": (round(pietro_min / parlato_persone, 3)
                                    if parlato_persone else None),
            "conversazioni": n_conv,
            "conversazioni_con_pietro": len(con_pietro),
            "quota_parola_pietro_mediana": _mediana(
                c["quota_pietro"] for c in con_pietro),
            "apre_pietro_quota": (round(sum(c["apre"] == SOGGETTO for c in con_pietro)
                                        / len(con_pietro), 3) if con_pietro else None),
            "chiude_pietro_quota": (round(sum(c["chiude"] == SOGGETTO for c in con_pietro)
                                          / len(con_pietro), 3) if con_pietro else None),
            "cambi_turno_per_ora_parlato": (round((len(turni) - 1) * 3600 / parlato_sec, 1)
                                            if parlato_sec and turni else None),
            "pietro_turni_per_ora_conversazione": (
                round(turni_pietro / ore_conv_pietro, 1)
                if ore_conv_pietro >= 1 / 60 else None),
            "elenco": conversazioni,
        },
        "pietro": {
            "minuti": pietro_min,
            "prosodia": _prosodia(prosodia_pietro),
            "parole": _marcatori([testo for _, testo in testi_pietro]),
        },
        "qualita": {
            "segmenti": n_seg,
            "ok_quota": round(qualita.get("ok", 0) / n_seg, 3) if n_seg else None,
            "incerti_quota": (round(qualita.get("unreliable", 0) / n_seg, 3)
                              if n_seg else None),
            "asr_prob_media": round(somma_p / n_p, 3) if n_p else None,
            "parole_incerte_quota": round(bassa / n_p, 3) if n_p else None,
            "testo_corretto": corretti,
            "pietro_ok_quota": (round(q_pietro.get("ok", 0) / len(di_pietro), 3)
                                if di_pietro else None),
            "pietro_asr_prob_media": round(somma_pp / n_pp, 3) if n_pp else None,
            "pietro_parole_incerte_quota": (round(bassa_p / n_pp, 3)
                                            if n_pp else None),
        },
        "ore": ore,
        "corpo": corpo,
    }


def scrivi(cartella_giorni: Path, cartella_metriche: Path,
           solo: Iterable[str] | None = None) -> list[Path]:
    """Scrive `metriche/AAAA-MM-GG.json` per le giornate, se cambiate."""
    cartella_metriche = Path(cartella_metriche)
    scelti = set(solo) if solo is not None else None
    scritti = []
    for d in sorted(Path(cartella_giorni).iterdir()):
        if not (d / "giorno.json").exists():
            continue
        if scelti is not None and d.name not in scelti:
            continue
        testo = json.dumps(calcola_giorno(d), ensure_ascii=False, indent=1) + "\n"
        dest = cartella_metriche / f"{d.name}.json"
        if dest.exists() and dest.read_text(encoding="utf-8") == testo:
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(testo, encoding="utf-8")
        scritti.append(dest)
    return scritti


if __name__ == "__main__":
    import sys
    for p in scrivi(Path(sys.argv[1]), Path(sys.argv[2])):
        print(p)
