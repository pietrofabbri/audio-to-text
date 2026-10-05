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

import core.text_correction as text_correction  # noqa: E402
from core.text_correction import (  # noqa: E402
    Correttore, SegmentResult, WordFix, _applica, _coda, _estrai_json,
    _modello_mancante, _piano, _retry_after, allinea_probabilita,
    correggi_segmenti, scrivi_varianti,
)
from core.text_correction import MODELLO, SOGLIA_PROB  # noqa: E402


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
        self.configs = []
        self.models = self

    def generate_content(self, model=None, contents=None, config=None):
        self.chiamate += 1
        self.configs.append(config or {})
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


class _Orologio:
    """Un `time` finto che registra le attese e non aspetta.

    Senza questo, due test su trentasei dormono davvero: quello del
    backoff e quello del modello sparito. Tra i due chiedevano 165
    secondi di attesa vera e ci restavano dentro — la suite della
    correzione durava quasi tre minuti, dei quali 164 secondi erano un
    test che guardava l'orologio di parete.

    Il sonno non verificava niente e costava tutto: un backoff che
    cominciasse a dormire trenta secondi invece di venti passerebbe in
    egual modo, e uno che non dormisse affatto anche. Sostituendolo con
    una lista si verifica anche la quantita' dell'attesa, che era la
    parte che il test dichiarava di coprire e non copriva.

    Si sostituisce `time` *nel modulo sotto test* e non `time.sleep`:
    quest'ultimo e' attributo del modulo `time` stesso, quindi
    rimpiazzarlo cambierebbe il sonno di tutto il processo, test
    vicini compresi, e un test che solleva un'eccezione prima di
    rimettere a posto lascerebbe il processo con l'attesa disattivata.
    Qui la sostituzione ha la durata di un `try`, e il modulo vero
    della produzione non viene toccato.
    """

    def __init__(self, attese: list[float]) -> None:
        self.attese = attese

    def sleep(self, secondi: float) -> None:
        self.attese.append(secondi)


def _orologio_falso(attese: list[float]):
    """Mette l'orologio finto e restituisce il modulo vero."""
    originale = text_correction.time
    text_correction.time = _Orologio(attese)
    return originale


def _ripristina_orologio(originale) -> None:
    text_correction.time = originale


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

    # Il modello viene interrogato a temperatura zero. Non e' un
    # dettaglio: la stessa parola veniva corretta in modo diverso a
    # ogni giro — «disastrati» e poi «distratti» — e una correzione che
    # cambia da una passata all'altra non e' una correzione, e' un tiro
    # a dadi. Su un corpus che si vuole interrogare, il risultato deve
    # essere riproducibile.
    config = c._client.configs[0]
    require(config.get("temperature") == 0,
            f"la temperatura deve essere 0, e' {config.get('temperature')!r}")
    require(config.get("response_mime_type") == "application/json",
            f"serve la risposta in JSON, e' {config!r}")


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

    Verifica anche quanto si aspetta fra un tentativo e l'altro, e non
    solo che si ritenta: un rate limit attende trenta secondi e un
    sovraccarico quindici, ed e' la differenza fra una notte che
    finisce e una notte che no.
    """
    attese: list[float] = []
    originale = _orologio_falso(attese)
    try:
        c = _correttore([RuntimeError("429"), RuntimeError("503"),
                         _risposta([{"i": 0, "a": "una", "b": "una"}])],
                        tentativi=3)
        r = c.correggi_segmento(0, "una due tre")
        require(not r.scartato, f"il terzo tentativo doveva riuscire: "
                                f"{r.motivo_scarto}")
        require(c._client.chiamate == 3, "devono essere state tre chiamate")
        require(attese == [30.0, 15.0],
                f"rate limit trenta secondi, sovraccarico quindici: {attese}")

        c = _correttore([RuntimeError("429"), RuntimeError("429")],
                        tentativi=2)
        r = c.correggi_segmento(0, "una due tre")
        require(r.scartato,
                "esauriti i tentativi il segmento resta non corretto")
        require(r.testo_corretto == "una due tre", "il testo non si perde")
        require("fallita" in r.motivo_scarto,
                f"il motivo deve dire che ha fallito, dice {r.motivo_scarto!r}")
    finally:
        _ripristina_orologio(originale)


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


def modello_non_disponibile() -> None:
    """Un modello che non esiste va detto per nome, non ripetuto tre volte.

    Il difetto che questo test copre e' gia' successo: il default era un
    modello che Google ha limitato agli account che lo avevano gia' usato
    in passato. Per un account nuovo la chiamata si ferma con un errore
    che non spiega niente, e il batch continuerebbe a consumare tentativi
    su una richiesta che non puo' riuscire — dichiarando ogni segmento
    «non corretto» senza che nessuno capisca perche'.
    """
    # L'errore si riconosce dalla forma del messaggio, non dal testo
    # esatto: Google cambia la formulazione da una versione all'altra.
    for exc, atteso in [
        (RuntimeError("404 NOT_FOUND: models/gemini-2.5-flash is not found"), True),
        (RuntimeError("404 model not supported"), True),
        (RuntimeError("PERMISSION_DENIED: the model is not available"), False),
        (RuntimeError("429 RESOURCE_EXHAUSTED: quota exceeded"), False),
        (RuntimeError("500 INTERNAL: model overloaded"), False),
        (RuntimeError("Connection reset by peer"), False),
    ]:
        require(_modello_mancante(exc) is atteso,
                f"{exc} -> {_modello_mancante(exc)}, atteso {atteso}")

    # E quando l'errore e' quello giusto, deve fermarsi subito e
    # nominare i modelli che funzionano.
    c = _correttore([RuntimeError("404 NOT_FOUND: models/xyz is not found")],
                    modello="xyz")
    try:
        c.correggi_segmento(0, "una due tre")
    except RuntimeError as exc:
        testo = str(exc)
        require("xyz" in testo, f"deve nominare il modello: {testo!r}")
        require("gemini-3.8-flash" in testo,
                f"deve suggerire un modello che funziona: {testo!r}")
        require(c._client.chiamate == 1,
                f"non deve riprovare, ha chiamato {c._client.chiamate} volte")
    else:
        raise Failure("un modello inesistente deve far fallire subito")

    # E non deve consigliare il modello che ha appena fallito: sarebbe
    # il modo piu' rapido per ritrovarsi lo stesso errore subito dopo.
    for fallito in ("gemini-2.5-flash", MODELLO):
        c = _correttore(
            [RuntimeError(f"404 NOT_FOUND: models/{fallito} is not found")],
            modello=fallito)
        try:
            c.correggi_segmento(0, "una due tre")
        except RuntimeError as exc:
            require(f"--model {fallito}" not in str(exc),
                    f"non deve riproporre {fallito}: {exc}")
            require("--model" in str(exc),
                    f"deve comunque proporre qualcosa: {exc}")
        else:
            raise Failure(f"{fallito}: doveva fallire")

    # Con un errore invece normale, il backoff resta. E resta anche
    # quando l'attesa non e' davvero un'attesa.
    attese: list[float] = []
    originale = _orologio_falso(attese)
    try:
        c = _correttore([RuntimeError("429 quota"),
                         RuntimeError("429 quota"),
                         _risposta([{"i": 0, "a": "una", "b": "una"}])],
                        tentativi=3)
        r = c.correggi_segmento(0, "una due tre")
        require(not r.scartato, "un rate limit si ritenta, non si ferma tutto")
        require(c._client.chiamate == 3, "deve aver riprovato")
        require(attese == [30.0, 30.0], f"due rate limit, due attese: {attese}")
    finally:
        _ripristina_orologio(originale)


def virgole_finali_nel_json() -> None:
    """Il JSON con le virgole finali è JSON: va letto, non scartato.

    Questo è successo davvero, con una risposta vera. Il modello
    scriveva il JSON correttissimo ma con una virgola prima di ogni
    `}`, `json.loads` lo rifiutava e il segmento finiva tra gli scarti:
    la correzione c'era, ed era stata pagata. Quattro chiamate perse
    per niente, e una voce che l'analisi non avrebbe mai visto.

    La trasformazione è sicura per costruzione: una virgola prima di
    `}` o `]` non è mai JSON valido, quindi toglierla non può cambiare
    il significato di un JSON che era valido.
    """
    grezzo = ('{\n  "correzioni": [\n'
              '    {"i": 0, "a": "Cominciatemi", "b": "Cominciatemi",},\n'
              '  ],\n}')
    atteso = {"correzioni": [{"i": 0, "a": "Cominciatemi",
                             "b": "Cominciatemi"}]}
    require(_estrai_json(grezzo) == atteso,
            f"non letto: {_estrai_json(grezzo)!r}")

    # Una virgola DENTRO una stringa non è una virgola finale: qui la
    # parola «così, no» deve arrivare intatta, perché è il testo che
    # si sta correggendo.
    dentro = '{"correzioni": [{"i": 1, "a": "cosi, no", "b": "x",}]}'
    got = _estrai_json(dentro)
    require(got == {"correzioni": [{"i": 1, "a": "cosi, no", "b": "x"}]}, got)

    # E il JSON senza virgole finali deve restare com'era.
    pulito = '{"correzioni": [{"i": 1, "a": "x", "b": "y"}]}'
    require(_estrai_json(pulito) == {"correzioni": [{"i": 1, "a": "x", "b": "y"}]},
            "il JSON valido non deve essere toccato")


def il_backoff_ascolta_il_server() -> None:
    """Un 503 o un 429 aspettano secondi, non mezzo secondo.

    Il batch seriale fa poche chiamate al secondo: non è il volume il
    problema, è che il server sta dicendo «non ora». Riprovare dopo
    mezzo secondo serve solo a farsi respingere di nuovo, e consuma
    quota per niente — la risposta è identica ma più tardi.

    Un permesso negato invece non si risolve aspettando: nessun piano.
    """
    for testo, atteso in [
        ("429 RESOURCE_EXHAUSTED quota exceeded", 30.0),
        ("RESOURCE_EXHAUSTED", 30.0),
        ("503 UNAVAILABLE: high demand", 15.0),
        ("overloaded", 15.0),
        ("400 INVALID_ARGUMENT: bad key", 0.0),
        ("PERMISSION_DENIED", 0.0),
    ]:
        got = _piano(Exception(testo))
        require(got == atteso, f"{testo} -> {got}, atteso {atteso}")

    # Se il server dice quanto aspettare, si ascolta lui.
    for testo, atteso in [
        ('{"status":"RESOURCE_EXHAUSTED","retryAfter": "42s"}', 42.0),
        ("Retry-After: 12", 12.0),
        ("retry-after=7", 7.0),
        ("nessun indicazione", None),
    ]:
        got = _retry_after(Exception(testo))
        require(got == atteso, f"{testo} -> {got}, atteso {atteso}")



# ----------------------------------------------------------------------
# La probabilita' per parola: il giudizio che non viene dal modello
# ----------------------------------------------------------------------

# Il difetto che questa sezione copre e' noto e ha un nome. Il
# correttore riscriveva i dialettalismi: su «Cominciatemi ragazzi,
# siamo drastisovati» propose «Camminate ... disastrati» e al secondo
# giro «Diamoci ... disastrati», e nessuna delle due parole nuove era
# piu' difendibile dell'originale. Nessun prompt lo ferma.
#
# Ma non tutte quelle parole sono uguali, e la differenza non e' nel
# testo: e' in quello che Whisper ne aveva capito. «disastrati» era
# stato udito con probabilita' 0.41, «Cominciatemi» con 0.98. Sopra una
# soglia il modello acustico ha gia' detto che sa cosa sta sentendo, e
# li' il giudizio che conta non e' quello del modello di lingua.
#
# Il pericolo di questa difesa e' il suo opposto, ed e' per questo che
# va provata: gettare via una parola che era giusta. Percio' il test
# verifica entrambe le direzioni, e che la parola bloccata resti nel
# file con la probabilita' accanto invece di sparire.

TESTO = "Cominciatemi ragazzi siamo drastisovati"
PROB = [0.98, 0.99, 0.97, 0.41]
PROPOSTE = [{"i": 0, "a": "Cominciatemi", "b": "Camminate"},
            {"i": 3, "a": "drastisovati", "b": "disastrati"}]


def allineamento_delle_timestamps() -> None:
    """Whisper spezza «C'e'» in due voci: l'allineamento se ne accorge."""
    parole = [{"word": " C", "prob": 0.5}, {"word": "'e'", "prob": 0.9},
              {"word": " la", "prob": 0.99}]
    p = allinea_probabilita("C'e la", parole)
    require(len(p) == 2, f"due parole, due probabilita': {p}")
    # La parola spezzata vale quanto il pezzo peggiore: se una meta'
    # e' stata indovinata, la parola intera e' stata indovinata.
    require(p[0] == 0.5, f"la parola spezzata prende la peggiore: {p}")
    require(p[1] == 0.99, f"la parola intera prende la sua: {p}")


def allineamento_senza_probabilita() -> None:
    """Nessuna probabilita' significa 'non lo so', non zero."""
    require(allinea_probabilita("a b", None) == [None, None],
            "senza parole non si inventa niente")
    require(allinea_probabilita("a b", [{"word": " a"}, {"word": " b"}])
            == [None, None],
            "una voce senza prob non vale zero: vale niente")


def allineamento_non_allineato() -> None:
    """Se i due elenchi non tornano, nessuna parola si appropria."""
    p = allinea_probabilita("qwerty zzzz",
                            [{"word": " a", "prob": 0.9},
                             {"word": " b", "prob": 0.9}])
    require(p == [None, None],
            f"una parola non allineata non prende la prob della vicina: {p}")


def parola_certa_non_si_tocca() -> None:
    """Una parola udita con certezza resta com'era."""
    c = _correttore([_risposta(PROPOSTE)])
    r = c.correggi_segmento(0, TESTO, PROB)
    require("Cominciatemi" in r.testo_corretto,
            f"parola certa riscritta: {r.testo_corretto}")
    require("disastrati" in r.testo_corretto,
            f"la correzione vera e' stata buttata: {r.testo_corretto}")
    require(r.n_cambiate == 1, f"una correzione sola: {r.n_cambiate}")
    require(r.n_bloccate == 1, f"una parola bloccata: {r.n_bloccate}")
    require(r.n_proposte == 2, f"due proposte: {r.n_proposte}")


def parola_incerta_si_corregge() -> None:
    """Sotto la soglia il modello di lingua ha ancora voce."""
    c = _correttore([_risposta([{"i": 0, "a": "Cominciatemi",
                                  "b": "Camminate"}])])
    r = c.correggi_segmento(0, "Cominciatemi ragazzi", [0.41, 0.99])
    require("Camminate" in r.testo_corretto,
            f"una parola incerta deve restare correggibile: "
            f"{r.testo_corretto}")
    require(r.n_bloccate == 0, "niente da bloccare")


def senza_probabilita_niente_filtro() -> None:
    """Se le probabilita' non ci sono, il correttore lavora come prima."""
    c = _correttore([_risposta(PROPOSTE)])
    r = c.correggi_segmento(0, TESTO)
    require("Camminate" in r.testo_corretto,
            "senza probabilita' non si blocca niente")
    require(r.n_bloccate == 0, "senza probabilita' non si blocca niente")


def soglia_zero_disattiva_il_filtro() -> None:
    """`--soglia-prob 0` resta il comportamento di prima."""
    c = _correttore([_risposta(PROPOSTE)], soglia_prob=0.0)
    r = c.correggi_segmento(0, TESTO, PROB)
    require("Camminate" in r.testo_corretto,
            "con la soglia a zero il filtro e' spento")
    require(r.n_cambiate == 2, f"due correzioni: {r.n_cambiate}")


def parola_spezzata_protetta_solo_se_lo_e() -> None:
    """«C'e'» si corregge se una meta' e' incerta, si blocca se certe."""
    testo = "C'e la cosa"
    parole = [{"word": " C", "prob": 0.99}, {"word": "'e'", "prob": 0.2},
              {"word": " la", "prob": 0.99}, {"word": " cosa", "prob": 0.99}]
    correzione = [{"i": 0, "a": "C'e", "b": "Che"}]

    c = _correttore([_risposta(correzione)])
    r = c.correggi_segmento(0, testo, allinea_probabilita(testo, parole))
    require("Che" in r.testo_corretto,
            f"una meta' incerta: la parola si corregge ({r.testo_corretto})")

    parole[1]["prob"] = 0.95
    c = _correttore([_risposta(correzione)])
    r = c.correggi_segmento(0, testo, allinea_probabilita(testo, parole))
    require("C'e" in r.testo_corretto,
            f"tutto certo: la parola non si tocca ({r.testo_corretto})")


def parola_bloccata_restare_tracciata() -> None:
    """Il blocco si vede nel file, non e' una sparizione."""
    c = _correttore([_risposta(PROPOSTE)])
    d = c.correggi_segmento(0, TESTO, PROB).to_dict()
    bloccata = [w for w in d["words"] if w["blocked"]]
    require(len(bloccata) == 1, f"una parola bloccata: {len(bloccata)}")
    w = bloccata[0]
    require(w["prob"] == 0.98, f"la probabilita' resta: {w}")
    require(w["raw"] == "Cominciatemi", f"l'originale resta: {w}")
    require(w["fixed"] == w["raw"], f"e il testo e' quello: {w}")
    require(w["changed"] is False,
            "una parola bloccata non e' una parola cambiata")
    require(d["n_blocked"] == 1 and d["n_proposed"] == 2,
            f"il riepilogo conta le proposte e i blocchi: {d}")


def la_probabilita_e_registrata_anche_sulle_accettate() -> None:
    """Ogni parola cambiata deve portare la sua probabilita', non solo le bloccate.

    Il difetto: `prob` veniva impostata solo quando il filtro bloccava la
    parola. `--solo-proposte` stampava quindi `p=?` su **tutte** le
    proposte accettate, e quel `p=?` si leggeva come «probabilita'
    sconosciuta» mentre voleva dire «il filtro ha valutato e ha passato».

    Il costo non e' cosmetico: il numero c'era gia' ed e' quello che
    serve per tarare la soglia confrontando le proposte buone con quelle
    cattive. Senza, l'unica occasione di vederlo era dall'altra parte
    della soglia, e il punto 22 resta «si tarava a occhio» perche' non
    c'era niente da guardare.

    Qui si verifica che la parola accettata conservi la sua probabilita' e
    che non venga confusa con una bloccata: stessa probabilita'Recorded,
    esito opposto.
    """
    c = _correttore([_risposta(PROPOSTE)])
    d = c.correggi_segmento(0, TESTO, PROB).to_dict()

    accettate = [w for w in d["words"] if w["changed"]]
    require(len(accettate) == 1, f"una parola accettata: {len(accettate)}")
    w = accettate[0]
    require(w["prob"] is not None,
            f"una parola accettata deve avere la sua probabilita': {w}")
    require(w["prob"] == 0.41,
            f"la probabilita' e' quella giusta: {w}")
    require(w["blocked"] is False,
            f"accettata e non bloccata: {w}")

    # Il blocco non cambia: probabilita' alta, esito opposto. E i due
    # casi non si confondono.
    bloccate = [x for x in d["words"] if x["blocked"]]
    require(len(bloccate) == 1 and bloccate[0]["prob"] == 0.98,
            f"la bloccata conserva la sua: {bloccate}")
    require(d["n_blocked"] == 1 and d["n_proposed"] == 2,
            f"il riepilogo non cambia: {d}")


def il_batch_conosce_le_probabilita() -> None:
    """Anche via lista di segmenti il filtro resta attivo."""
    c = _correttore([_risposta(PROPOSTE), _risposta(PROPOSTE)])
    risultati = c.correggi([(0, TESTO, PROB), (1, TESTO, PROB)])
    require(len(risultati) == 2, "due risultati")
    for r in risultati:
        require(r.n_bloccate == 1, f"bloccata anche in batch: {r}")
    # E una coppia senza probabilita' continua a funzionare.
    c = _correttore([_risposta(PROPOSTE)])
    r = c.correggi([(0, TESTO)])[0]
    require(r.n_bloccate == 0, "senza probabilita' niente blocchi")



# ----------------------------------------------------------------------
# Il percorso completo: il comando come lo usa l'utente
# ----------------------------------------------------------------------

# Tutto quello sopra verifica il modulo. Ma il modulo non e' quello che
# gira: gira `correct_text.py`, e il comando ha una forma tutta sua — i
# giri precedenti da non perdere, il `--limit` che interrompe a meta',
# i file pubblicabili da riscrivere a ogni giro. Se uno di quegli
# incroci si rompe, gli unit test restano verdi e la notte produce un
# testo perso.
#
# Qui il comando viene davvero eseguito, in una cartella a caso, con un
# modello finto al posto di Gemini e l'orologio tarato: nessuna rete,
# nessuna chiave, e quello che viene scritto si guarda sul disco.


SESSIONE = "2026-10-04_21-00-00"


class _ClienteFinto:
    """Un Gemini finto che corregge la prima parola e basta."""

    def __init__(self) -> None:
        self.models = self
        self.viste: list[str] = []

    def generate_content(self, model=None, contents=None, config=None):
        testo = contents.split("Testo:\n", 1)[1]
        self.viste.append(testo)
        parole = testo.split()
        return _Risposta(json.dumps({"correzioni": [
            {"i": 0, "a": parole[0], "b": "CORRETTA" + parole[0]}]}))


def _sessione(tmp: Path | str, n: int = 3) -> Path:
    """Una sessione con tre segmenti, gia' trascritta."""
    from pipeline.assembler import _write_srt, _write_txt

    # Il `main` di questa suite passa la cartella temporanea come
    # stringa: e' `TemporaryDirectory` che la restituisce cosi', e
    # dividerla per un nome prima di averla cambiata in Path fallisce.
    d = Path(tmp) / SESSIONE
    d.mkdir(parents=True, exist_ok=True)
    segmenti = [
        {"idx": i, "start": float(i * 3), "end": float(i * 3 + 2),
         "speaker": "GLOBAL_001", "text": f"parola{i} del segmento {i}",
         "no_speech_prob": 0.01, "quality": "ok", "quality_reasons": []}
        for i in range(n)
    ]
    (d / "segments.jsonl").write_text(
        "\n".join(json.dumps(s, ensure_ascii=False) for s in segmenti) + "\n",
        encoding="utf-8")
    _write_txt(segmenti, d / "transcript.txt")
    _write_srt(segmenti, d / "transcript.srt")
    # Solo il primo segmento ha un checkpoint con parole: e' la situazione
    # reale, e serve a coprire il caso in cui la probabilita' manca per
    # gli altri senza che il comando si fermi.
    chunk = {"idx": 0, "start": 0.0, "end": 2.0,
             "text": segmenti[0]["text"], "language": "it",
             "no_speech_prob": 0.01, "duration_sec": 2.0,
             "words": [{"word": f" parola{i}", "start": 0.0, "end": 0.4,
                        "prob": 0.99} for i in range(3)] + [
                       {"word": " del", "start": 0.4, "end": 0.9, "prob": 0.99},
                       {"word": " segmento", "start": 0.9, "end": 1.4,
                        "prob": 0.98}]}
    (d / f"{SESSIONE}.checkpoint.json").write_text(
        json.dumps({"file": "a.wav", "stem": "a", "chunks": [chunk],
                    "stages": [], "speaker_global_map": {},
                    "diarization_segments": []}, ensure_ascii=False),
        encoding="utf-8")
    return d


def _esegui(tmp: Path | str, *argomenti: str) -> tuple[int, str, _ClienteFinto]:
    """Il comando, l'output del comando, e il modello finto che ha risposto."""
    import contextlib
    import io
    import logging
    import os

    import correct_text

    cliente = _ClienteFinto()
    originale_client = text_correction.Correttore._ottieni_client
    scrittura = io.StringIO()
    originale_argv = sys.argv
    chiave = os.environ.get("GOOGLE_API_KEY")
    attese: list[float] = []
    orologio = _orologio_falso(attese)
    # Il comando mette i log a INFO e li scrive su stderr: dentro una
    # suite sono solo rumore fra una riga di risultato e l'altra.
    logging.disable(logging.CRITICAL)
    text_correction.Correttore._ottieni_client = lambda self: cliente
    # Una chiave finta: `pronto()` chiede che ce ne sia una, e questa non
    # e' la chiave di nessuno.
    os.environ["GOOGLE_API_KEY"] = "chiave-finta-di-prova"
    sys.argv = ["correct_text.py", *argomenti, "--out-dir", str(tmp)]
    try:
        with contextlib.redirect_stdout(scrittura):
            try:
                codice = correct_text.main()
            except SystemExit as exc:
                codice = exc.code if exc.code is not None else 0
    finally:
        logging.disable(logging.NOTSET)
        text_correction.Correttore._ottieni_client = originale_client
        sys.argv = originale_argv
        _ripristina_orologio(orologio)
        if chiave is None:
            os.environ.pop("GOOGLE_API_KEY", None)
        else:
            os.environ["GOOGLE_API_KEY"] = chiave
    return codice, scrittura.getvalue(), cliente


def _corri(tmp: Path | str, *argomenti: str) -> tuple[int, _ClienteFinto]:
    """`correct_text.py` come lo lancia l'utente, in una cartella a caso."""
    codice, _, cliente = _esegui(tmp, *argomenti)
    return codice, cliente


def cli_senza_consente_non_esce_nulla(tmp: Path) -> None:
    """Senza `--consent` il comando spiega e basta."""
    d = _sessione(tmp)
    prima = sorted(p.name for p in d.iterdir())
    codice, cliente = _corri(tmp, "--dry")
    require(codice == 0, f"il comando deve usire pulito: {codice}")
    require(not cliente.viste, "nessuna chiamata al modello")
    require(sorted(p.name for p in d.iterdir()) == prima,
            "niente file scritti senza consenso")


def cli_con_consente_scrive_tutto(tmp: Path) -> None:
    """Con `--consent` il giro è completo: correzione e varianti."""
    d = _sessione(tmp)
    originale = (d / "transcript.txt").read_text(encoding="utf-8")
    codice, cliente = _corri(tmp, "--consent")
    require(codice == 0, f"il comando deve riuscire: {codice}")
    require(len(cliente.viste) == 3, f"tre segmenti, tre chiamate: "
                                    f"{len(cliente.viste)}")
    for nome in ("text_correction.json", "transcript.corrected.txt",
                 "transcript.corrected.srt", "segments.corrected.jsonl"):
        require((d / nome).exists(), f"manca {nome}: "
                                     f"{sorted(p.name for p in d.iterdir())}")
    # L'originale non si tocca mai: i due errori devono restare entrambi
    # visibili, e' il punto della correzione affiancata.
    require((d / "transcript.txt").read_text(encoding="utf-8") == originale,
            "transcript.txt non deve essere mai sovrascritto")
    # Il testo corretto contiene davvero le correzioni. Il segmento 0
    # no: e' quello con i timestamp di parola, e tutte le sue parole
    # sono state udite con probabilita' 0.98-0.99, quindi il filtro le
    # ha lasciate stare. Che il filtro arrivi fino al file scritto — e
    # non resti fermo al riepilogo — e' parte di quello che si verifica
    # qui.
    corretto = (d / "transcript.corrected.txt").read_text(encoding="utf-8")
    require("CORRETTAparola1" in corretto, f"correzione assente: {corretto}")
    require("CORRETTAparola2" in corretto, f"correzione assente: {corretto}")
    require("CORRETTAparola0" not in corretto,
            f"una parola che Whisper aveva capito non si riscrive: "
            f"{corretto}")
    require("parola0 del segmento 0" in corretto,
            f"il testo della parola bloccata resta: {corretto}")


def cli_i_giri_si_accumulano(tmp: Path) -> None:
    """Un giro interrotto non perde il lavoro del giro precedente."""
    d = _sessione(tmp)
    _corri(tmp, "--consent", "--limit", "1")
    _corri(tmp, "--consent", "--limit", "1")
    # Il terzo giro trova i due segmenti gia' corretti e rifinische il
    # terzo: non ripete le chiamate gia' fatte.
    _, cliente = _corri(tmp, "--consent")
    require(len(cliente.viste) == 1,
            f"l'ultimo giro deve correggere un segmento solo: "
            f"{len(cliente.viste)}")

    doc = json.loads((d / "text_correction.json").read_text(encoding="utf-8"))
    require(doc["summary"]["segments"] == 3,
            f"tutti i segmenti devono restare nel file: {doc['summary']}")
    idx = [s["idx"] for s in doc["segments"]]
    require(idx == sorted(idx), f"i segmenti devono restare in ordine: {idx}")

    # E il testo ricostruito contiene tutte le correzioni che il filtro
    # lascia passare: e' il punto di un giro che si ferma a meta'.
    # Il segmento 0 resta com'e' perche' il filtro lo protegge, e anche
    # questo deve valere a fine giro e non solo a meta'.
    corretto = (d / "transcript.corrected.txt").read_text(encoding="utf-8")
    for i in (1, 2):
        require(f"CORRETTAparola{i}" in corretto,
                f"la correzione del segmento {i} e' andata persa: {corretto}")
    require("parola0 del segmento 0" in corretto,
            f"la parola protetta dal filtro e' stata riscritta lo stesso: "
            f"{corretto}")


def cli_il_filtro_e_nel_riepilogo(tmp: Path) -> None:
    """Dal comando esce anche quante parole il filtro ha negate."""
    d = _sessione(tmp)
    _corri(tmp, "--consent")
    doc = json.loads((d / "text_correction.json").read_text(encoding="utf-8"))
    riepilogo = doc["summary"]
    require(riepilogo["words_proposed"] == 3,
            f"tre proposte: {riepilogo}")
    require(riepilogo["words_blocked"] == 1,
            f"la parola con probabilita' 0.99 non si tocca: {riepilogo}")
    require(riepilogo["words_changed"] == 2, f"le altre due passano: "
                                             f"{riepilogo}")
    require(riepilogo["prob_threshold"] == SOGLIA_PROB,
            f"la soglia va scritta: {riepilogo}")
    # Il blocco non e' una sparizione: la parola e' ancora li', con la
    # probabilita' che ha deciso.
    bloccate = [w for s in doc["segments"] for w in s["words"] if w["blocked"]]
    require(len(bloccate) == 1, f"una parola bloccata: {bloccate}")
    require(bloccate[0]["prob"] == 0.99, f"con la sua probabilita': {bloccate}")



def proposta_respinta_sopravvive_al_blocco() -> None:
    """La parola che il filtro ha negato conserva quello che il modello
    voleva scrivere.

    Prima non era cosi': il filtro azzerava la proposta, e nel file
    restava solo la parola originale con un `blocked`. Il commento nel
    codice diceva il contrario — «non si butta via la proposta» — e la
    ragione per cui il filtro esiste e' proprio guardare che cosa il
    modello voleva scrivere dove Whisper era sicuro. Senza quel dato la
    soglia si tarerebbe guardando il nulla.
    """
    c = _correttore([_risposta(PROPOSTE)])
    d = c.correggi_segmento(0, TESTO, PROB).to_dict()
    bloccata = [w for w in d["words"] if w["blocked"]]
    require(len(bloccata) == 1, f"una parola bloccata: {bloccata}")
    w = bloccata[0]
    require(w["raw"] == "Cominciatemi", f"l'originale resta: {w}")
    require(w["proposta"] == "Camminate",
            f"la proposta respinta deve restare nel file: {w}")
    require(w["fixed"] == w["raw"],
            f"ma il testo non la prende: {w}")
    require(w["changed"] is False, "una parola bloccata non e' cambiata")


def il_vocabolario_conta_le_parole(tmp: Path) -> None:
    """Il vocabolario somma le parole di tutte le sessioni con la loro
    probabilita' media."""
    import correct_text

    d = _sessione(tmp)
    vocabolario = correct_text._vocabolario([d])
    # Ogni segmento e' «parolaN del segmento N»: `parola1` compare una
    # volta sola, `del` e `segmento` tre volte ciascuno. E il conteggio
    # conta tutte le occorrenze, mentre la media copre solo quelle con la
    # probabilita' — e nel checkpoint di questa sessione c'e' il solo
    # segmento 0, dove `del` e' stato udito con 0.99.
    n, _, quante = vocabolario["del"]
    require(n == 3, f"`del` compare in tutti e tre i segmenti: {n}")
    require(quante == 0,
            f"ma solo il segmento 0 ha un checkpoint, quindi nessuna "
            f"probabilita' misurata: {quante}")
    # `parola0` e' l'unica parola del segmento con checkpoint che
    # l'allineamento riconosce, ed e' l'unica con una probabilita' media.
    n, media, quante = vocabolario["parola0"]
    require((n, quante) == (1, 1), f"una volta, una misurata: {vocabolario}")
    require(abs(media - 0.99) < 1e-6, f"la media e' quella: {media}")
    require(vocabolario["parola1"][0] == 1,
            f"una parola che compare una volta: {vocabolario['parola1']}")
    require("corretta" not in vocabolario,
            "il vocabolario si legge sulle trascrizioni, non sulle risposte")
    # E una media che non c'e' si dichiara, invece di diventare uno zero
    # che si legge come «Whisper non era sicuro».
    require("mai misurata" in correct_text._nota_vocabolario("del", vocabolario),
            f"la nota deve dire che non e' misurata: "
            f"{correct_text._nota_vocabolario('del', vocabolario)}")


def il_report_distingue_le_parole_dalle_occorrenze(tmp: Path) -> None:
    """L'intestazione non deve mescolare due grandezze diverse.

    Il caso reale: l'intestazione scriveva «147 parole diverse proposte,
    126 accettate, 22 respinte», ma 147 veniva da `len(proposte)` — le
    parole *diverse* — mentre 126 e 22 venivano dalla somma di `v["n"]`,
    cioe' le *occorrenze* di quelle parole nel testo. Letto come si legge,
    sembra che 126 delle 147 parole siano state accettate: non e' vero,
    sono 126 occorrenze distribuite su un numero minore di parole.

    Il numero e' giusto, la parola che lo introduce no: e' quello che
    faceva leggere il rapporto come un sottoinsieme.
    """
    _sessione(tmp)
    codice, out, _ = _esegui(tmp, "--consent", "--solo-proposte")
    require(codice == 0, f"il comando deve uscire pulito: {codice}")
    riga = [l for l in out.splitlines() if "parole diverse proposte" in l]
    require(riga, f"l'intestazione della sessione deve esserci: {out}")
    testo = riga[0]
    require("diverse proposte (" in testo,
            f"le parole diverse e le occorrenze vanno dichiarate come due "
            f"grandezze diverse:\n{testo}")
    require("occorrenze" in testo,
            f"le occorrenze devono avere la loro etichetta:\n{testo}")
    require("da correggere" in testo,
            f"le occorrenze da correggere vanno dette come tali:\n{testo}")


def il_report_elenca_le_proposte(tmp: Path) -> None:
    """`--solo-proposte` mostra una riga per parola e non i testi."""
    _sessione(tmp)
    codice, out, _ = _esegui(tmp, "--consent", "--solo-proposte")
    require(codice == 0, f"il comando deve uscire pulito: {codice}")
    require("parola0 -> CORRETTAparola0" in out,
            f"la parola bloccata deve comparire come respinta: {out}")
    require("parola1 -> CORRETTAparola1" in out,
            f"una riga per parola proposta: {out}")
    require("x1" in out, f"il conteggio delle occorrenze: {out}")
    require("prima :" not in out,
            f"col report compatto i testi non si stampano: {out}")
    require("MAI UDITA" in out or "vocabolario:" in out,
            f"la colonna del vocabolario: {out}")


def il_report_raggruppa_e_sintetizza(tmp: Path) -> None:
    """La stessa parola proposta in tre segmenti e' una riga sola, e il
    riepilogo finale mette le sessioni insieme."""
    _sessione(tmp)
    codice, out, _ = _esegui(tmp, "--consent", "--solo-proposte")
    require(codice == 0, f"il comando deve uscire pulito: {codice}")
    # Tre segmenti, tre proposte diverse (`parola0/1/2`), nessuna
    # ripetuta: il raggruppamento non deve inventare aggregazioni.
    require(out.count("-> CORRETTA") == 3, f"tre proposte: {out}")
    # Con una sola sessione non c'e' una sintesi da fare: sarebbe la
    # stessa tabella due volte.
    require("tutte le sessioni" not in out,
            f"una sola sessione non ha bisogno di sintesi: {out}")


def il_report_senza_consente_non_stampa_nulla(tmp: Path) -> None:
    """Senza `--consent` resta la spiegazione, e nessuna parola."""
    _sessione(tmp)
    codice, out, _ = _esegui(tmp, "--solo-proposte")
    require(codice == 0, f"il comando deve uscire pulito: {codice}")
    require("Nessuna chiamata" in out, f"la spiegazione c'e': {out}")
    require("->" not in out, f"nessuna proposta senza consenso: {out}")


def il_report_non_impedisce_la_scrittura(tmp: Path) -> None:
    """Il report cambia quello che si vede, non quello che si scrive.

    Il rischio di un interruttore che cambia il percorso di scrittura e'
    esatto: il giro produce il file e le varianti come sempre, e le
    proposte respinte arrivano in `text_correction.json` come in un giro
    senza report.
    """
    d = _sessione(tmp)
    _esegui(tmp, "--consent", "--solo-proposte")
    require((d / "text_correction.json").exists(), "il file deve esserci")
    require((d / "transcript.corrected.txt").exists(),
            "e anche le varianti pubblicabili")
    doc = json.loads((d / "text_correction.json").read_text(encoding="utf-8"))
    bloccate = [w for s in doc["segments"] for w in s["words"] if w["blocked"]]
    require(len(bloccate) == 1, f"una parola bloccata: {bloccate}")
    require(bloccate[0]["proposta"] == "CORRETTAparola0",
            f"e la sua proposta respinta e' nel file: {bloccate}")


CHECKS = [
    ("l'intestazione distingue parole diverse da occorrenze",
     il_report_distingue_le_parole_dalle_occorrenze),
    ("il JSON con virgole finali viene letto, non scartato",
     virgole_finali_nel_json),
    ("il backoff ascolta il server e aspetta secondi",
     il_backoff_ascolta_il_server),
    ("il commento in coda viene spiegato, non ignorato",
     commento_in_coda_rinomato),
    ("un modello non più disponibile si ferma subito",
     modello_non_disponibile),
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
    ("i timestamp spezzati si allineano alle parole",
     allineamento_delle_timestamps),
    ("senza probabilita' non si inventa un numero",
     allineamento_senza_probabilita),
    ("una parola non allineata non prende la prob della vicina",
     allineamento_non_allineato),
    ("una parola che Whisper aveva capito non si tocca",
     parola_certa_non_si_tocca),
    ("una parola incerta resta correggibile", parola_incerta_si_corregge),
    ("senza probabilita' il correttore lavora come prima",
     senza_probabilita_niente_filtro),
    ("la soglia a zero spegne il filtro", soglia_zero_disattiva_il_filtro),
    ("una parola spezzata e' protetta solo se lo sono tutte le parti",
     parola_spezzata_protetta_solo_se_lo_e),
    ("la parola bloccata resta tracciata nel file",
     parola_bloccata_restare_tracciata),
    ("la probabilita' resta anche sulle parole accettate",
     la_probabilita_e_registrata_anche_sulle_accettate),
    ("il batch passa le probabilita' al filtro",
     il_batch_conosce_le_probabilita),
    ("senza consenso il comando non scrive niente",
     cli_senza_consente_non_esce_nulla),
    ("con consenso il giro e' completo",
     cli_con_consente_scrive_tutto),
    ("un giro interrotto non perde il lavoro precedente",
     cli_i_giri_si_accumulano),
    ("il filtro compare nel riepilogo del comando",
     cli_il_filtro_e_nel_riepilogo),
    ("la proposta respinta resta nel file dopo il blocco",
     proposta_respinta_sopravvive_al_blocco),
    ("il vocabolario conta le parole delle sessioni",
     il_vocabolario_conta_le_parole),
    ("il report compatto elenca le proposte una riga per parola",
     il_report_elenca_le_proposte),
    ("il report non ripete la stessa tabella due volte",
     il_report_raggruppa_e_sintetizza),
    ("senza consenso il report non stampa proposte",
     il_report_senza_consente_non_stampa_nulla),
    ("il report non cambia quello che si scrive",
     il_report_non_impedisce_la_scrittura),
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
