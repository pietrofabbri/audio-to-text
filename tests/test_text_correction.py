"""
Test della correzione di una trascrizione con un LLM.

    python tests/test_text_correction.py

Niente rete, niente API, niente modelli: un cliente finto risponde al
posto di Gemini e il resto e' aritmetica e confronto di stringhe.

Il difetto che questo test copre non e' un errore di programma ma una
proprieta' che, se si rompe, non si vede subito. Il rischio di una
correzione fatta da un modello di lingua non e' che sbagli poche parole:
e' che ne inventi qualcuna. Una riscrittura produce un testo che sembra
piu' buono di quello che e' stato detto, e i due sono indistinguibili
a chi legge dopo. In un corpus che vuole misurare la propria voce un
testo inventato e' peggio di un testo sbagliato, perche' lo sbagliato al
meno si riconosce.

Percio' la garanzia che qui viene verificata e' che il numero di parole
non possa cambiare, e che una risposta non allineata con il testo venga
scartata invece che applicata. E' la ragione per cui la correzione e'
affiancata all'originale parola per parola e non sostitutiva: senza il
confronto fra le due forme, uno dei due errori sparisce e non sai
quale.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

from core.text_correction import (  # noqa: E402
    Correttore, SegmentResult, WordFix, _applica, _coda, _estrai_json,
    correggi_segmenti, scrivi_varianti,
)


class Failure(Exception):
    pass


def require(cond: bool, msg: str) -> None:
    if not cond:
        raise Failure(msg)


class _Risposta:
    """Il minimo che il correttore pretende da una risposta del modello."""

    def __init__(self, testo: str) -> None:
        self.text = testo


class _ModelloFinto:
    """Un cliente Gemini finto che risponde una lista di risposte.

    Serve a provare il percorso completo senza rete. Tiene il conto
    delle chiamate, e cosi' un test puo' verificare che una correzione
    scartata non abbia comunque prodotto testo nuovo.
    """

    def __init__(self, risposte) -> None:
        self._risposte = list(risposte)
        self.chiamate = 0
        self.models = self

    def generate_content(self, model=None, contents=None, config=None):
        self.chiamate += 1
        if not self._risposte:
            raise AssertionError("il modello finto ha ricevuto troppe chiamate")
        prossima = self._risposte.pop(0)
        if isinstance(prossima, Exception):
            raise prossima
        return _Risposta(prossima)


def _correttore(risposte, **kw) -> Correttore:
    """Un Correttore col cliente finto e senza toccare le chiavi reali."""
    c = Correttore(consentito=True, pausa=0, **kw)
    c._client = _ModelloFinto(risposte)
    return c


def _risposta(correzioni) -> str:
    return json.dumps({"correzioni": correzioni})


# ----------------------------------------------------------------------
# L'allineamento: la parte che decide se una correzione e' applicabile
# ----------------------------------------------------------------------

def testo_immutato() -> None:
    """Se il modello non cambia niente, il testo resta identico.

    Non scontato: il percorso ricostruisce il testo da una lista di
    parole. Se la ricostruzione cambiasse qualcosa — uno spazio, la
    punteggiatura attaccata — ogni segmento «non corretto» risulterebbe
    diverso dall'originale, e non si saprebbe mai se la differenza
    l'ha fatta il modello o il codice.
    """
    orig = "sono stato a Savot e poi siamo saliti a telefono ieri sera."
    parole = orig.split()
    correzioni = [{"i": i, "a": p.strip("."), "b": p.strip(".")}
                  for i, p in enumerate(parole)]
    out, fissate = _applica(orig, correzioni)
    require(out == orig, f"il testo non deve cambiare, risulta: {out!r}")
    require(not any(f.cambiata for f in fissate),
            "nessuna parola deve risultare cambiata")
    require(len(fissate) == len(parole),
            f"devono restare {len(parole)} parole, ne' sono {len(fissate)}")


def correzione_solo_delle_parole_giuste() -> None:
    """Le parole corrette cambiano, le altre restano esattamente come erano."""
    orig = "a stegnavano a matiala vera"
    parole = orig.split()
    correzioni = []
    for p in parole:
        nuovo = {"stegnavano": "segnavano", "matiala": "maiala"}.get(p, p)
        correzioni.append({"i": parole.index(p), "a": p, "b": nuovo})
    out, fissate = _applica(orig, correzioni)

    require(out == "a segnavano a maiala vera", f"risulta: {out!r}")
    cambiate = [f.originale for f in fissate if f.cambiata]
    require(cambiate == ["stegnavano", "matiala"],
            f"le parole cambiate devono essere due, sono {cambiate}")
    require(out.split() == ["a", "segnavano", "a", "maiala", "vera"],
            "l'ordine delle parole non deve cambiare mai")


def numero_di_parole_invariabile() -> None:
    """Il numero di parole non puo' cambiare, per costruzione.

    Questa e' la garanzia centrale del modulo, e vale anche quando il
    modello prova a fare il contrario: basta che restituisca piu'
    correzioni di quante parole ci sono, o indici che non esistono, e
    la risposta viene scartata. Il numero di parole e' quello della
    lista `parole`, quindi un modello che ne inventa una non la
    sostituisce: semplicemente non viene accettata.
    """
    orig = "una due tre"
    # Il modello vuole aggiungere parole che non ci sono.
    troppe = [{"i": i, "a": p, "b": p} for i, p in enumerate(orig.split())]
    troppe += [{"i": 3, "a": "quarta", "b": "quarta"},
               {"i": 4, "a": "quinta", "b": "quinta"}]
    require(_applica(orig, troppe) is None,
            "una risposta con piu' parole del testo deve essere scartata")

    # E se il modello cancella una parola svuotandola, la parola resta.
    out, fissate = _applica(orig, [{"i": 1, "a": "due", "b": ""}])
    require(out == orig, f"una parola svuotata non puo' sparire: {out!r}")
    require(not any(f.cambiata for f in fissate),
            "una parola svuotata non conta come correzione")


def risposta_disallineata_scartata() -> None:
    """Se il modello cita una parola che non c'e', tutto viene scartato.

    Il caso pericoloso non e' tanto l'indice sbagliato quanto la parola
    sbagliata: un modello che ha diviso diversamente il testo produce
    due elenchi simili ma allineati in modo diverso. Applicare
    comunque sposterebbe ogni parola di un segmento su quella del
    successivo, e il testo risultante sembrerebbe plausibile. Meglio
    buttare via il lavoro di una chiamata.
    """
    require(_applica("a b c", [{"i": 1, "a": "NONQUELLO", "b": "x"}]) is None,
            "una parola che non corrisponde deve far scartare la risposta")
    require(_applica("a b c", [{"i": 9, "a": "c", "b": "x"}]) is None,
            "un indice fuori range deve far scartare la risposta")
    require(_applica("a b c", [{"i": -1, "a": "a", "b": "x"}]) is None,
            "un indice negativo deve far scartare la risposta")
    require(_applica("a b c", [{"i": "non_un_numero", "a": "a", "b": "x"}]) is None,
            "un indice non numerico deve far scartare la risposta")

    # La punteggiatura non conta come differenza: «sera.» e «sera»
    # sono la stessa parola.
    out, _ = _applica("una sera.", [{"i": 1, "a": "sera", "b": "sera."}])
    require(out == "una sera.", f"la punteggiatura non deve rompere nulla: {out!r}")


def punteggiatura_conservata() -> None:
    """La correzione di una parola non mangia la sua punteggiatura.

    Il modello corregge «vera» e restituisce «vera» senza il punto. Se si
    prende anche quello, si perde un segnale che era giusto giusto e si
    introduce rumore: dopo un pomeriggio di correzioni il testo diventa
    una sequenza di parole senza frasi, e la punteggiatura era proprio
    cio' che rendeva il testo ricercabile per frase.
    """
    require(_coda("vera.") == ".", "il punto finale va tenuto")
    require(_coda("ciao?") == "?", "il punto interrogativo va tenuto")
    require(_coda("(va)") == ")", "la parentesi chiusa va tenuta")
    require(_coda("ciao") == "", "una parola senza coda non ne inventa una")

    out, _ = _applica("era vera. davvero?", [
        {"i": 1, "a": "vera", "b": "giusta"},
        {"i": 2, "a": "davvero", "b": "davvero"},
    ])
    require(out == "era giusta. davvero?", f"risulta: {out!r}")


def coerenza_interna() -> None:
    """WordFix e SegmentResult devono raccontare la stessa storia."""
    f = WordFix(indice=3, originale="matiala", proposta="maiala")
    require(f.cambiata, "una parola diversa e' cambiata")
    require(f.scelta == "maiala", "la scelta e' la parola proposta")

    vuota = WordFix(indice=0, originale="ciao", proposta="   ")
    require(not vuota.cambiata,
            "una proposta vuota non e' una correzione")
    require(vuota.scelta == "ciao", "senza proposta resta l'originale")

    r = SegmentResult(idx=7, testo_originale="a b c d",
                      testo_corretto="a b x d",
                      parole=[WordFix(0, "a", "a"), WordFix(1, "b", "b"),
                              WordFix(2, "c", "x"), WordFix(3, "d", "d")])
    require(r.n_parole == 4, f"4 parole, risulta {r.n_parole}")
    require(r.n_cambiate == 1, f"1 cambiata, risulta {r.n_cambiate}")
    require(abs(r.quota_cambiate - 0.25) < 1e-9,
            f"quota 0.25, risulta {r.quota_cambiate}")

    d = r.to_dict()
    require(d["idx"] == 7 and d["n_changed"] == 1, "il dizionario riepiloga")
    require(len(d["words"]) == 4, "il dizionario elenca ogni parola")
    require(json.dumps(d), "il dizionario deve essere serializzabile")


# ----------------------------------------------------------------------
# Il percorso completo, con un modello finto
# ----------------------------------------------------------------------

def percorso_completo() -> None:
    """Dalla risposta del modello al testo corretto, con i numeri."""
    orig = "a stegnavano a matiala vera"
    correzioni = [
        {"i": 0, "a": "a", "b": "a"},
        {"i": 1, "a": "stegnavano", "b": "segnavano"},
        {"i": 2, "a": "a", "b": "a"},
        {"i": 3, "a": "matiala", "b": "maiala"},
        {"i": 4, "a": "vera", "b": "vera"},
    ]
    c = _correttore([_risposta(correzioni)])
    r = c.correggi_segmento(0, orig)

    require(not r.scartato, f"non doveva essere scartato: {r.motivo_scarto}")
    require(r.testo_corretto == "a segnavano a maiala vera",
            f"risulta: {r.testo_corretto!r}")
    require(r.testo_originale == orig, "l'originale resta intatto")
    require(r.n_cambiate == 2, f"2 correzioni, risulta {r.n_cambiate}")
    require(c._client.chiamate == 1, "una sola chiamata per un segmento")


def risposta_illeggibile_scartata() -> None:
    """Una risposta che non e' JSON viene scartata, non interpretata a caso.

    Il modello puo' rispondere con prosa, con un JSON troncato, con un
    errore. In tutti e tre i casi la cosa giusta e' tenere il testo
    originale e dirlo: ricavare comunque qualcosa da una risposta
    malformata significa che il corpus contiene parole che nessuno ha
    scritto e nessuno ha detto.
    """
    for rotta, risposta in [
        ("niente", ""),
        ("prosa", "Mi sembra che sia meglio riscrivere la frase intera."),
        ("troncato", '{"correzioni": [{"i": 1,'),
        ("lista", "[1, 2, 3]"),
    ]:
        c = _correttore([risposta])
        r = c.correggi_segmento(0, "una due tre")
        require(r.scartato, f"{rotta}: doveva essere scartato")
        require(r.testo_corretto == "una due tre",
                f"{rotta}: il testo originale deve restare")

    # Un JSON valido ma senza la chiave attesa vale come illeggibile.
    c = _correttore([json.dumps({"risposta": "niente"})])
    r = c.correggi_segmento(0, "una due tre")
    require(r.scartato, "una chiave mancante deve far scartare")


def risposta_disallineata_ai_flesse_scartata() -> None:
    """Lo stesso scarto, ma attraverso il percorso completo.

    Il test precedente verifica la funzione, questo verifica che il
    percorso completo la usi davvero: se qualcuno collegasse le due
    cose ignorando il valore di ritorno, qui si accorgerebbe.
    """
    c = _correttore([_risposta([{"i": 0, "a": "TOTALTMENTE", "b": "x"}])])
    r = c.correggi_segmento(0, "una due tre")
    require(r.scartato, "una risposta disallineata deve essere scartata")
    require("disallineat" in r.motivo_scarto or "scartata" in r.motivo_scarto,
            f"il motivo deve dirlo, dice: {r.motivo_scarto!r}")
    require(r.testo_corretto == "una due tre", "l'originale resta")


def tentativi_e_backoff() -> None:
    """Una chiamata fallita si ritenta, e alla fine non fa perdere il testo.

    Di notte un batch lungo incontra sempre rate limit e interruzioni.
    Se la prima eccezione buttasse via il segmento, il batch si
    riempirebbe di buchi proprio nelle ore in cui la macchina e' meno
    sotto controllo. Dopo l'ultimo tentativo il testo resta quello di
    prima, dichiarato come non corretto.
    """
    c = _correttore([RuntimeError("429"), RuntimeError("503"),
                     _risposta([{"i": 0, "a": "una", "b": "una"}])],
                    tentativi=3)
    r = c.correggi_segmento(0, "una due tre")
    require(not r.scartato, f"il terzo tentativo doveva riuscire: "
                            f"{r.motivo_scarto}")
    require(c._client.chiamate == 3, "devono essere state tre chiamate")

    c = _correttore([RuntimeError("429"), RuntimeError("429")], tentativi=2)
    r = c.correggi_segmento(0, "una due tre")
    require(r.scartato, "esauriti i tentativi il segmento resta non corretto")
    require(r.testo_corretto == "una due tre", "il testo non si perde")
    require("fallita" in r.motivo_scarto,
            f"il motivo deve dire che ha fallito, dice {r.motivo_scarto!r}")


# ----------------------------------------------------------------------
# Le varianti pubblicabili: cio' che si legge su GitHub
# ----------------------------------------------------------------------

def _segmento(idx: int, testo: str) -> dict:
    return {"idx": idx, "speaker": "GLOBAL_001", "start": 0.0,
            "end": 4.0, "text": testo, "words": []}


def varianti_affiancano_l_originale(tmp) -> None:
    """Il file originale non si tocca, quello nuovo sta accanto.

    Il corpus pubblicato copia i file di sessione: se il testo corretto
    sostituisse quello di Whisper, il confronto che rende giudicabile
    il correttore sparirebbe — e insieme a esso la possibilita' di
    capire se una frase sbagliata l'ha fatta la trascrizione o la
    correzione. Non e' una scelta di formato: e' il modo in cui si
    tiene conto dei due errori insieme.
    """
    d = Path(tmp)
    segmenti = [_segmento(0, "a stegnavano a matiala vera")]
    (d / "segments.jsonl").write_text(
        "\n".join(json.dumps(s) for s in segmenti), encoding="utf-8")
    (d / "transcript.txt").write_text("testo di Whisper\n", encoding="utf-8")

    correzioni = {0: {"idx": 0, "discarded": False,
                      "corrected_text": "a segnavano a maiala vera",
                      "n_changed": 2}}
    scritti = scrivi_varianti(d, segmenti, correzioni)

    nomi = {p.name for p in scritti}
    require(nomi == {"transcript.corrected.txt", "transcript.corrected.srt",
                     "segments.corrected.jsonl"}, nomi)
    for p in scritti:
        require(p.exists(), f"{p.name} non è stato scritto")

    require((d / "transcript.txt").read_text() == "testo di Whisper\n",
            "l'originale non deve cambiare")
    corretto = (d / "transcript.corrected.txt").read_text()
    require("segnavano" in corretto, f"il nuovo deve essere corretto: {corretto!r}")

    righe = [json.loads(l) for l in
             (d / "segments.corrected.jsonl").read_text().splitlines() if l.strip()]
    require(len(righe) == 1, f"una riga per segmento, risultano {len(righe)}")
    require(righe[0]["text"] == "a segnavano a maiala vera", righe[0])
    require(righe[0]["text_raw"] == "a stegnavano a matiala vera", righe[0])
    require(righe[0]["n_words_changed"] == 2, righe[0])


def senza_correzioni_niente_varianti(tmp) -> None:
    """Nessuna correzione, nessun file: non si finge un lavoro fatto.

    Un `transcript.corrected.txt` identico all'originale suggerirebbe
    un passaggio di correzione che non e' mai avvenuto, e su GitHub
    sarebbe indistinguibile da una correzione che non ha cambiato
    niente — che sono due cose molto diverse.
    """
    d = Path(tmp)
    segmenti = [_segmento(0, "una due tre")]
    solo_scarti = {0: {"idx": 0, "discarded": True,
                       "corrected_text": "una due tre", "n_changed": 0}}
    for argomento, nome in (({}, "nessuna correzione"),
                            (solo_scarti, "solo scarti")):
        scritti = scrivi_varianti(d, segmenti, argomento)
        require(not scritti,
                f"{nome}: non doveva scrivere nulla, ha scritto {scritti}")
        require(not (d / "transcript.corrected.txt").exists(),
                f"{nome}: il file non deve esistere")


def correggere_segmenti_non_tocca_l_originale(tmp) -> None:
    """La funzione restituisce copie: la fonte resta quella che è."""
    segmenti = [_segmento(0, "a stegnavano a matiala vera")]
    correzioni = {0: {"idx": 0, "discarded": False,
                      "corrected_text": "a segnavano a maiala vera",
                      "n_changed": 2}}
    out = correggi_segmenti(segmenti, correzioni)
    require(out[0]["text"] == "a segnavano a maiala vera", out[0])
    require(segmenti[0]["text"] == "a stegnavano a matiala vera",
            "il segmento in ingresso non deve essere modificato")
    require(out is not segmenti, "devono essere copie, non lo stesso oggetto")


# ----------------------------------------------------------------------
# Il consenso: la parte che non si puo' testare a posteriori
# ----------------------------------------------------------------------

def consenso_esplicito() -> None:
    """Senza consenso esplicito non esce niente dal portatile.

    Il testo di una conversazione personale che passa a un'API esterna
    e' una scelta, non un dettaglio implementativo. Il modulo quindi non
    ha un interruttore che si possa attivare per sbaglio: il consenso
    va detto, e senza quello `pronto` spiega perche' no. Il test non
    puo' dimostrare che nessuno lo aggiri — puo' solo dimostrare che la
    porta di default e' chiusa.
    """
    c = Correttore()
    pronto, motivo = c.pronto()
    require(not pronto, "senza consenso non si deve procedere")
    require("consenso" in motivo,
            f"il motivo deve nominare il consenso, dice: {motivo!r}")

    # E il percorso completo deve rifiutarsi, non andare a chiamare e poi
    # decidere di buttare il risultato.
    try:
        c.correggi_segmento(0, "una due tre")
    except RuntimeError as exc:
        require("consenso" in str(exc),
                f"l'errore deve dire perché, dice: {exc}")
    else:
        raise Failure("senza consenso la correzione deve rifiutarsi")

    # Con consenso ma senza chiave il motivo e' un altro.
    import os
    chiavi = {k: os.environ.pop(k, None)
              for k in ("GOOGLE_API_KEY", "GEMINI_API_KEY")}
    try:
        pronto, motivo = Correttore(consentito=True).pronto()
        require(not pronto, "senza chiave non si deve procedere")
        require("GOOGLE_API_KEY" in motivo,
                f"il motivo deve nominare la chiave, dice: {motivo!r}")
    finally:
        for k, v in chiavi.items():
            if v is not None:
                os.environ[k] = v


def testo_vuoto_non_chiama_il_modello() -> None:
    """Un segmento vuoto o fatto solo di spazi non costa una chiamata.

    Nella diarizzazione ci sono segmenti brevissimi e some vuoti: sono
    la parte piu' numerosa e la meno interessante da correggere. Ogni
    chiamata inutile, di notte, e' chiamata sprecata.
    """
    c = _correttore([])
    for testo in ("", "   ", "\n"):
        r = c.correggi_segmento(0, testo)
        require(not r.scartato, "un testo vuoto non e' uno scarto")
        require(r.testo_corretto == testo, "il testo vuoto resta vuoto")
    require(c._client.chiamate == 0,
            f"nessuna chiamata per i vuoti, risultano {c._client.chiamate}")


def piu_segmenti_in_filiera() -> None:
    """Il batch riporta un risultato per ogni segmento, nell'ordine."""
    risposte = [
        _risposta([{"i": 0, "a": "matiala", "b": "maiala"}]),
        "risposta illeggibile",
        _risposta([{"i": 0, "a": "x", "b": "x"}]),
    ]
    c = _correttore(risposte)
    out = c.correggi([(0, "matiala vera"), (1, "una due"), (2, "x y")])

    require(len(out) == 3, f"3 risultati, risultano {len(out)}")
    require([r.idx for r in out] == [0, 1, 2], "l'ordine deve restare")
    require(not out[0].scartato and out[1].scartato and not out[2].scartato,
            "solo il secondo doveva essere scartato")
    require(out[0].testo_corretto == "maiala vera",
            f"risulta: {out[0].testo_corretto!r}")


def correggere_un_testo_già_corretto() -> None:
    """Correggere due volte non deve degradare il testo.

    Il batch e' pensato per essere ritentato — una notte interrotta, un
    modello migliore, un prima e un dopo da confrontare. Se correggere un
    testo gia' corretto lo peggiorasse, ritentare sarebbe un danno, e
    l'unico modo per saperlo e' poterlo misurare: qui la correzione non
    tocca niente, quindi il secondo giro deve essere identico al primo.
    """
    orig = "a stegnavano a matiala vera"
    correzioni = [{"i": 1, "a": "stegnavano", "b": "segnavano"},
                  {"i": 3, "a": "matiala", "b": "maiala"}]
    primo, _ = _applica(orig, correzioni)
    require(primo == "a segnavano a maiala vera", "il primo giro corregge")

    # Il secondo giro riceve il testo gia' corretto e non propone nulla.
    parole = primo.split()
    niente = [{"i": i, "a": p, "b": p} for i, p in enumerate(parole)]
    secondo, fissate = _applica(primo, niente)
    require(secondo == primo,
            f"il secondo giro deve essere un no-op: {secondo!r}")
    require(not any(f.cambiata for f in fissate),
            "nessuna parola deve risultare cambiata al secondo giro")


def _estrai_json_accetta_i_modi() -> None:
    """I modi in cui un modello avvolge il JSON non devono far perdere la risposta."""
    atteso = {"correzioni": [{"i": 0, "a": "a", "b": "a"}]}
    grezzi = [
        json.dumps(atteso),
        "```json\n" + json.dumps(atteso) + "\n```",
        "Ecco la risposta:\n```\n" + json.dumps(atteso) + "\n```\n",
        "Certamente! " + json.dumps(atteso) + " Spero sia utile.",
    ]
    for testo in grezzi:
        require(_estrai_json(testo) == atteso,
                f"non riconosciuto: {testo[:40]!r}")

    for cattivo in ("", "niente da correggere", "[1, 2, 3]", "{rotto"):
        require(_estrai_json(cattivo) is None,
                f"non doveva essere riconosciuto: {cattivo[:30]!r}")


def commento_in_coda_rinomato() -> None:
    """Un `#` arrivato come argomento va detto, non lasciato a argparse.

    Le righe di questo file e del README hanno un commento in coda, e
    vengono copiate. In bash il `#` viene ignorato; in zsh, nei comandi
    interattivi, no — arriva come argomento e argparse risponde
    «unrecognized arguments», che non dice nulla di utile. Il caso si
    ripresentera' ogni volta che qualcuno copia una riga, quindi il
    programma deve saperlo spiegare.

    Il comando che suggerisce non deve contenere il `#`: altrimenti il
    suggerimento sarebbe un modo per non uscire dall'errore.
    """
    import subprocess

    testo = ("Il '#' finale e' arrivato come argomento")
    r = subprocess.run(
        [sys.executable, str(HERE / ".." / "correct_text.py"),
         "--dry", "--limit", "5", "# cinque correzioni"],
        capture_output=True, text=True, cwd=str(ROOT), timeout=120)
    uscita = (r.stdout or "") + (r.stderr or "")
    require(r.returncode != 0, "un argomento in più deve far fallire")
    require(testo in uscita,
            f"deve spiegare la causa, dice: {uscita[-400:]!r}")
    require("zsh" in uscita and "interactive_comments" in uscita,
            f"deve dire come si risolve, dice: {uscita[-400:]!r}")

    # La riga suggerita deve essere eseguibile cosi' com'e'.
    suggerita = [l.strip() for l in uscita.splitlines()
                 if l.strip().startswith("correct_text.py")]
    require(suggerita, f"deve suggerire il comando, dice: {uscita[-400:]!r}")
    require("#" not in suggerita[0],
            f"il comando suggerito non deve contenere '#': {suggerita[0]!r}")

    # E un argomento davvero sbagliato continua a dare errore normale.
    r2 = subprocess.run(
        [sys.executable, str(HERE / ".." / "correct_text.py"), "--limiti", "5"],
        capture_output=True, text=True, cwd=str(ROOT), timeout=120)
    require(r2.returncode != 0, "un flag inesistente deve far fallire")
    require("limiti" in (r2.stdout or "") + (r2.stderr or ""),
            "l'errore normale deve nominare il flag sbagliato")


CHECKS = [
    ("il commento in coda viene spiegato, non ignorato",
     commento_in_coda_rinomato),
    ("il testo non cambia se il modello non cambia niente", testo_immutato),
    ("si correggono solo le parole giuste", correzione_solo_delle_parole_giuste),
    ("il numero di parole non puo' cambiare", numero_di_parole_invariabile),
    ("una risposta disallineata viene scartata",
     risposta_disallineata_scartata),
    ("la punteggiatura resta al suo posto", punteggiatura_conservata),
    ("WordFix e SegmentResult sono coerenti", coerenza_interna),
    ("il percorso completo produce il testo corretto", percorso_completo),
    ("una risposta illeggibile viene scartata", risposta_illeggibile_scartata),
    ("lo scarto per disallineamento passa dal percorso completo",
     risposta_disallineata_ai_flesse_scartata),
    ("una chiamata fallita si ritenta col backoff", tentativi_e_backoff),
    ("senza consenso esplicito non esce niente", consenso_esplicito),
    ("un testo vuoto non costa una chiamata", testo_vuoto_non_chiama_il_modello),
    ("il batch mantiene l'ordine dei segmenti", piu_segmenti_in_filiera),
    ("correggere due volte non degrada", correggere_un_testo_già_corretto),
    ("il JSON viene trovato sotto qualsiasi involucro",
     _estrai_json_accetta_i_modi),
    ("le varianti pubblicabili stanno accanto all'originale",
     varianti_affiancano_l_originale),
    ("senza correzioni non si scrive nessuna variante",
     senza_correzioni_niente_varianti),
    ("correggere i segmenti non tocca la fonte",
     correggere_segmenti_non_tocca_l_originale),
]


def main() -> int:
    passed = 0
    failed: list[str] = []
    for name, fn in CHECKS:
        try:
            if fn.__code__.co_argcount:
                with TemporaryDirectory() as tmp:
                    fn(tmp)
            else:
                fn()
            passed += 1
            print(f"  ok  {name}")
        except Failure as exc:
            failed.append(f"{name}: {exc}")
            print(f"  KO  {name} - {exc}")
        except Exception as exc:  # noqa: BLE001
            failed.append(f"{name}: {type(exc).__name__}: {exc}")
            print(f"  ERR {name} - {type(exc).__name__}: {exc}")

    print(f"\n{passed}/{len(CHECKS)} superati")
    if failed:
        print("Falliti:")
        for f in failed:
            print(f"  - {f}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
