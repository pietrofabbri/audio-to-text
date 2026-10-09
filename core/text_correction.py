"""
Correzione di una trascrizione automatica con un LLM.

Il problema. Whisper sbaglia le parole in modo prevedibile: mette
«Savot» dove si diceva «Savot» in dialetto, «Botanic» per «Titanic»,
«matiala vera» per «maiala vera», e via. Sono errori che si riconoscono
dal contesto e che un modello di lingua corregge senza fatica, mentre
-nessun- modello acustico li corregge, perche' l'informazione che
manca non e' nel suono: e' che «maiala vera» e' una frase che esiste.

Quello che non si puo' fare e' riscrivere. Una riscrittura produce un
testo che sembra piu' buono e che non e' piu' quello che e' stato detto:
il modello, vedendo «stegnavano a telefono», puo' scrivere «segnavano
al telefono» — che e' probabilmente giusto — ma puo' anche scrivere
«segnavano i telefoni», che e' inventato, e i due testi sono
indistinguibili a chi li legge dopo. In un corpus che vuole misurare
la propria voce, un testo inventato e' peggio di un testo sbagliato:
lo sbagliato almeno si riconosce.

La regola che questo modulo segue, quindi: **niente parole nuove.** Il
modello puo' correggere, riscrivere, riorganizzare — ma il numero di
parole deve restare quello che era, e ogni correzione e' annotata. Se
il numero cambia, la risposta si scarta e si avvisa: un modello che
aggiunge o toglie parole non sta correggendo la trascrizione, sta
riscrivendo il testo, e in quel caso non lo si usa.

Perche' affiancato e non sostitutivo. Ogni parola conserva originale e
correzione, con un flag. L'analisi usa il testo corretto; tu puoi
sempre misurare quanto il modello sbaglia, e quanto sbagliava Whisper.
I due errori insieme sono la misura vera della qualita' della
trascrizione, e senza il confronto fra originale e correzione uno dei
due sparisce.

Due motori. Il predefinito e' **locale** (`motore="ollama"`): un modello
che gira sul Mac attraverso Ollama, gratuito, senza limiti di chiamate,
e il testo non esce dal computer. Scelta del 9 ottobre: Pietro ha
preferito qualcosa di gratuito e stabile a Gemini, che sul piano gratuito
puo' usare i testi per migliorare i prodotti di Google (decisione D2,
documento di progetto `roadmap.md`). Il motore `gemini` resta disponibile
per confronto, con `--motore gemini`, e solo li' il testo esce dal Mac.

`consentito` va impostato a True esplicitamente in entrambi i casi: e' il
punto in cui si decide di correggere, e con Gemini e' anche il consenso a
mandare il testo fuori.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

logger = logging.getLogger(__name__)

# La punteggiatura, da togliere per confrontare due parole e da
# riapplicare alla fine. E' tutto quello che separa «sera.» da «sera».
PUNTEGGIATURA = " \t\n.,;:!?()[]{}\"'«»…—-"

# Lo stesso insieme come insieme di caratteri: `_norm` li toglie uno a
# uno e deve poter chiedere «questo carattere c'e'?» per ognuno.
_PUNTEGGIATURA = frozenset(PUNTEGGIATURA)

# Il modello e' cambiato due volte, e per due motivi diversi.
#
# Prima `gemini-2.5-flash`: Google ha limitato l'accesso alla famiglia
# 2.5 riservandolo agli account che l'avevano gia' chiamato in passato,
# quindi per un account nuovo la richiesta si ferma con un errore che
# non dice nulla.
#
# Poi `gemini-3.8-flash`, che e' il piu' capace ma al momento risponde
# `503 high demand` a ogni tentativo. Il default deve essere il modello
# che funziona, non quello che sarebbe migliore: un batch notturno che
# scarta tutti i segmenti e' peggio di uno che corregge un po' meno.
MODELLO = "gemini-3.5-flash-lite"

# Il motore predefinito e il suo modello. `gemma3:12b` scrive bene
# l'italiano e sta in circa 8 GB di memoria (quantizzazione Q4); su un Mac
# con 8 GB in tutto si scende a `gemma3:4b` (circa 3 GB). Si cambia in
# `core/config.py` (`correzione_modello_locale`) o con `--model`.
MOTORE = "ollama"
MODELLO_LOCALE = "gemma3:12b"
OLLAMA_URL = "http://127.0.0.1:11434"

MODELLI_NOTI = {
    "gemini-3.5-flash-lite": "scelta predefinita: risponde regolarmente",
    "gemini-3.8-flash": "piu' capace, ma spesso in 503 per domanda alta",
}

# La probabilita' oltre la quale una parola non si tocca.
#
# 0.90 e' il valore che lascia passare il 46% delle parole, e su questi
# dati le parole che il correttore aveva corrette bene — «disastrati»
# da «drastisovati», «monopolio» da «monopolito», «stesso» da
# «steam»» — hanno probabilita' 0.41, 0.70 e 0.14: stanno tutte sotto.
# Sopra questa soglia il modello acustico ha detto che sa cosa sta
# sentendo, e un modello di lingua che sentisse diversamente ha torto
# per costruzione.
SOGLIA_PROB = 0.90

ISTRUZIONI = """\
Sei un correttore di trascrizioni automatiche di una conversazione \
parlata in italiano.

Il testo che ti do e' la trascrizione automatica di un registratore. \
Sbaglia le parole in modo prevedibile: fonemi scambiati, parole dialettali \
rese in italiano, nomi proprii storpiati.

Correggi il testo nel modo piu' probabile, ma con quattro vincoli duri:

1. NON aggiungere e NON togliere parole. Il numero di parole deve \
restare identico. Se una frase ti sembra incompleta, correggi le \
parole che ci sono e lascia stare: non ricostruire il pensiero.
2. Non cambiare il registro ne il contenuto. Una conversazione fra \
amici resta una conversazione fra amici, con i suoi «boh» e i suoi \
«tipo».
3. ATTENZIONE, perche' e' l'errore piu' facile da commettere: il testo \
viene da una registrazione vera e chi parla e' una persona viva. Una \
parola che ti sembra storta ma che e' pronunciabile — un dialettalismo, \
un intercalare, una forma che si sentirebbe davvero a voce — e' quasi \
certamente quello che e' stato detto, e va lasciata. Non e' un errore \
della trascrizione: e' una persona che parla come parla. Cambiarla in \
italiano piu' corretto significa inventare.
4. Se una parola e' gia' plausibile ma non sei sicuro che sia quella \
giusta, lasciala. Un intervento a caso peggiora il testo.

Elenca SOLO le parole che cambi, una voce JSON per parola, con:
- "i": indice della parola nell'originale (0-based, contando le parole \
separate da spazi)
- "a": la parola originale, esattamente com'e'
- "b": la parola che hai scelto

Rispondi SOLO con un oggetto JSON {"correzioni": [...]}. Nient'altro. \
Se non c'e' niente da correggere, rispondi con {"correzioni": []}.
"""

# I nomi propri, aggiunti alle istruzioni. Sulla prova del 2 ottobre il
# modello ha riscritto «Zia Titti» in «Gigi D'Alessio»: incontra un nome
# che non conosce e lo sostituisce con uno famoso. La regola nel prompt
# non basta da sola (il codice blocca comunque, vedi `_filtra`), ma
# riduce le proposte da respingere e soprattutto permette il contrario:
# correggere una storpiatura *verso* un nome che il glossario conosce.
ISTRUZIONI_NOMI = """\
5. NOMI PROPRI: non sostituire MAI un nome di persona, di luogo o un \
soprannome con un altro nome, neppure se quello che leggi ti sembra \
strano: e' quasi sempre una persona che non conosci. Non trasformare \
mai una parola in un nome famoso.
"""

ISTRUZIONI_GLOSSARIO = """\
Questi sono nomi propri che compaiono davvero nelle conversazioni. \
Scritti cosi' sono giusti: non cambiarli. Se nel testo trovi una \
storpiatura evidente di uno di questi, puoi correggerla verso il nome \
dell'elenco.
Nomi: {nomi}
"""

ISTRUZIONI_CONTESTO = """\
Ti do anche qualche frase prima e dopo, SOLO come contesto per capire \
di cosa si parla. Correggi esclusivamente il "Testo da correggere": gli \
indici "i" si riferiscono alle sue parole, contate da 0.
"""


def correggi_segmenti(segmenti: Iterable[dict], correzioni: dict) -> list[dict]:
    """I segmenti con il testo da analizzare, senza toccare gli originali.

    Restituisce copie: i segmenti in ingresso rappresentano quello che
    e' stato detto e vanno conservati, e una correzione non deve
    scrivere sopra la fonte da cui e' venuta.
    """
    out = []
    for s in segmenti:
        corr = (correzioni or {}).get(s.get("idx"))
        if not corr or corr.get("discarded") or not corr.get("corrected_text"):
            out.append(dict(s))
            continue
        nuovo = dict(s)
        nuovo["text_raw"] = s.get("text")
        nuovo["text"] = corr["corrected_text"]
        nuovo["n_words_changed"] = corr.get("n_changed") or 0
        out.append(nuovo)
    return out


def scrivi_varianti(
    session_dir: Path,
    segmenti: list[dict],
    correzioni: dict,
) -> list[Path]:
    """Le varianti corrette della sessione, con l'originale accanto.

    Il corpus pubblicato copia i **file** di sessione, non il database:
    se il testo corretto restasse solo in `corpus.db`, su GitHub si
    continuerebbe a leggere il testo impreciso — che e' esattamente il
    difetto che si voleva chiudere. Percio' qui si scrivono file
    paralleli, e non si sovrascrive niente: `transcript.txt` resta
    quello che Whisper ha capito.

    I formattatori sono quelli dell'assembler, non altri: il formato
    deve essere identico per costruzione, altrimenti un file diverrebbe
    piu' avanti di un altro e nessuno se ne accorgerebbe guardando i
    due affiancati.

    Se nessuna correzione e' stata applicata non si scrive niente: un
    `transcript.corrected.txt` identico all'originale suggerirebbe un
    passaggio di correzione che non e' avvenuto.
    """
    from pipeline.assembler import _write_srt, _write_txt

    usabili = {
        i: r for i, r in (correzioni or {}).items()
        if not r.get("discarded") and r.get("corrected_text")
    }
    if not usabili:
        return []

    corretti = correggi_segmenti(segmenti, usabili)
    if not any(c.get("text") != s.get("text")
               for c, s in zip(corretti, segmenti)):
        return []

    session_dir = Path(session_dir)
    scritti = []
    for nome, scrivi in (
        ("transcript.corrected.txt", lambda p: _write_txt(corretti, p)),
        ("transcript.corrected.srt", lambda p: _write_srt(corretti, p)),
    ):
        p = session_dir / nome
        scrivi(p)
        scritti.append(p)

    p_jsonl = session_dir / "segments.corrected.jsonl"
    with p_jsonl.open("w", encoding="utf-8") as f:
        for s in corretti:
            f.write(json.dumps(s, ensure_ascii=False) + "\n")
    scritti.append(p_jsonl)
    return scritti


def _norm(parola: str) -> str:
    """Una parola ridotta alla sua parte parlata.

    Serve a confrontare la parola del testo con quella dei timestamp di
    parola: sono due rappresentazioni della stessa cosa e differiscono
    solo per la punteggiatura e per lo spazio che Whisper mette davanti.

    La punteggiatura si toglie ovunque, non solo ai bordi, ed e' il punto
    in cui sta il trucco. Whisper spezza «C'e'» in «C» e «'e'», quindi
    l'apostrofo sta in *mezzo* a una delle due meta' e non come bordo:
    togliendolo solo alle estremi il confronto non tornerebbe mai e
    l'allineamento si fermerebbe al 63% delle parole.
    """
    return "".join(c for c in parola if c not in _PUNTEGGIATURA).casefold()


def _come_prob(valore: Any) -> float | None:
    """Una probabilita' usabile, o None.

    Un checkpoint puo' essere stato scritto da una versione che non
    salvava il campo, e un valore non numerico farebbe fallire il
    confronto con la soglia piu' avanti, con un errore che non dice
    nulla di dove sia il problema.
    """
    if isinstance(valore, bool) or not isinstance(valore, (int, float)):
        return None
    return float(valore)


def allinea_probabilita(
    testo: str,
    parole: list[dict] | None,
) -> list[float | None]:
    """La probabilita' di Whisper per ogni parola del testo, o None.

    Whisper calcola gia' la probabilita' di ogni parola — sta nei suoi
    timestamp di parola, col nome `prob` — ma senza questa funzione il
    correttore non puo' distinguere una parola che il modello acustico
    aveva capito da una che aveva tirato a indovinare, e finisce per
    fidarsi dell'unico giudizio che non lo sa: quello del modello di
    lingua.

    I due elenchi non hanno la stessa lunghezza e non sono allineati: il
    testo ha «C'e'» come una parola, i timestamp ce l'hanno come due,
    «C» e «'e'». Percio' il confronto e' una camminata monotonica che
    consuma, per ogni parola del testo, tutte le voci dei timestamp
    finche' la loro somma non e' proprio quella parola. Non e' un
    confronto per indice: su questi dati allineerebbe il 54% delle
    parole e da quel punto in poi assegnerebbe a ogni parola la
    probabilita' della vicina.

    Una lista, non un dizionario: la posizione e' tutto quello che
    serve, e restituisce un valore per ogni parola cosi' il chiamante
    non deve gestire i buchi. `None` vuol dire «non lo so» e non va
    confuso con 0.0, che significa «Whisper era sicuro che fosse
    sbagliata».
    """
    tokens = _tokenizza(testo)
    out: list[float | None] = [None] * len(tokens)
    if not parole:
        return out

    i = 0
    for posizione, token in enumerate(tokens):
        atteso = _norm(token)
        if not atteso:
            continue
        # Caso normale: una sola voce corrisponde alla parola.
        if i < len(parole) and _norm(str(parole[i].get("word", ""))) == atteso:
            out[posizione] = _come_prob(parole[i].get("prob"))
            i += 1
            continue
        # Caso divisibile: piu' voci che messe insieme fanno la parola.
        accumulato = ""
        j = i
        while j < len(parole) and len(accumulato) < len(atteso):
            accumulato += _norm(str(parole[j].get("word", "")))
            j += 1
            if accumulato == atteso:
                break
        if accumulato == atteso:
            # Se una parola e' stata spezzata fra piu' timestamp, la
            # sua probabilita' e' la peggiore dei pezzi: e' la meno
            # affidabile, ed e' quella che deve decidere se il modello
            # di lingua ha diritto di toccarla.
            pezzi = (_come_prob(parole[k].get("prob")) for k in range(i, j))
            out[posizione] = min((p for p in pezzi if p is not None),
                                 default=None)
            i = j
        # Altrimenti la parola non si allinea e resta None: non protegge
        # niente. Meglio che attribuirle la probabilita' di una vicina,
        # che la esporrebbe a un filtro costruito su un numero che parla
        # d'un'altra parola.

    return out


@dataclass
class WordFix:
    """Una parola, prima e dopo."""

    indice: int
    originale: str
    proposta: str
    prob: float | None = None
    bloccata: bool = False
    # Perche' e' stata bloccata: "prob" (Whisper era sicuro), "glossario"
    # (nome protetto), "nome" (maiuscola fuori da inizio frase, o un nome
    # nuovo che il modello voleva introdurre). Serve a leggere il file
    # dopo: le tre difese hanno errori diversi e si tarano separatamente.
    motivo_blocco: str = ""

    @property
    def scelta(self) -> str:
        """La forma che finisce nel testo, con l'originale come riserva.

        Il modello a volte restituisce una stringa vuota o solo spazi:
        in quel caso la parola originale resta, perche' una correzione
        che cancella una parola e' una cancellazione, non una
        correzione.

        E una parola bloccata torna com'era, anche se il modello aveva
        proposto qualcosa: il filtro ha negato la proposta e il testo non
        deve accorgersene. La proposta respinta resta pero' in
        `proposta`, ed e' l'unico posto dove si vede che cosa il modello
        voleva scrivere li': senza questo la parola bloccata e'
        indistinguibile da una parola che il modello non ha mai toccata,
        e la soglia si tarerebbe a caso.
        """
        if self.bloccata:
            return self.originale
        return self.proposta.strip() or self.originale

    @property
    def cambiata(self) -> bool:
        # Una parola bloccata non e' cambiata: il filtro l'ha rimessa
        # com'era, e dirne cambiata qualcosa di riportato al suo stato
        # iniziale farebbe dire al riepilogo che il modello ha lavorato
        # dove non ha lavorato.
        return self.scelta != self.originale and not self.bloccata


@dataclass
class SegmentResult:
    """Il risultato della correzione di un segmento."""

    idx: int
    testo_originale: str
    testo_corretto: str
    parole: list[WordFix] = field(default_factory=list)
    scartato: bool = False
    motivo_scarto: str = ""
    n_proposte: int = 0

    @property
    def n_cambiate(self) -> int:
        return sum(1 for f in self.parole if f.cambiata)

    @property
    def n_bloccate(self) -> int:
        """Quante proposte sono state respinte perche' certe.

        Il numero conta anche perche' e' l'unico modo di capire se il
        filtro sta lavorando o e' solo acceso: se su una notte intera
        blocca zero parole, o la soglia e' troppo alta o le probabilita'
        non ci sono, e le due cose si confondono.
        """
        return sum(1 for f in self.parole if f.bloccata)

    @property
    def n_parole(self) -> int:
        return len(self.parole)

    @property
    def quota_cambiate(self) -> float:
        return self.n_cambiate / self.n_parole if self.n_parole else 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "idx": self.idx,
            "original_text": self.testo_originale,
            "corrected_text": self.testo_corretto,
            "n_words": self.n_parole,
            "n_changed": self.n_cambiate,
            "changed_share": round(self.quota_cambiate, 3),
            "discarded": self.scartato,
            "discard_reason": self.motivo_scarto,
            "n_proposed": self.n_proposte,
            "n_blocked": self.n_bloccate,
            "n_blocked_names": sum(1 for f in self.parole if f.bloccata
                                   and f.motivo_blocco in ("glossario", "nome")),
            "words": [
                {"i": f.indice, "raw": f.originale, "fixed": f.scelta,
                 "proposta": f.proposta, "changed": f.cambiata,
                 "prob": f.prob, "blocked": f.bloccata,
                 "blocked_reason": f.motivo_blocco}
                for f in self.parole
            ],
        }


def _tokenizza(testo: str) -> list[str]:
    """Le parole del testo, con la punteggiatura attaccata.

    Serve che le parole del testo e quelle dell'elenco del modello
    coincidano una a una, altrimenti il confronto non vuol dire niente.
    Percio' la tokenizzazione e' la piu' semplice possibile: spazi.
    """
    return testo.split()


def _estrai_json(testo: str) -> dict[str, Any] | None:
    """Il JSON dentro una risposta che potrebbe avere del testo attorno.

    I modelli racchiudono spesso il JSON in un blocco di codice o in una
    frase. Tentare `json.loads` sul testo intero fallisce piu' spesso di
    quanto sembri, e qui fallire significa buttare via il lavoro di una
    chiamata.
    """
    testo = (testo or "").strip()
    if testo.startswith("```"):
        testo = re.sub(r"^```[a-zA-Z]*\n?", "", testo)
        testo = re.sub(r"\n?```$", "", testo).strip()
    # Le virgole finali sono il caso piu' comune di risposta che sembra
    # JSON e non lo e'. I modelli le scrivono per abitudine di elenco
    # Markdown, e `json.loads` le rifiuta: succedeva davvero, e il
    # segmento veniva scartato con la correzione gia' fatta e pagata
    # dentro. Una virgola prima di `}` o `]` non e' mai JSON valido,
    # quindi toglierla non può cambiare il significato di un JSON che
    # era valido — la trasformazione e' sicura per costruzione.
    testo = re.sub(r",(\s*[}\]])", r"\1", testo)
    for candidato in (testo, _solo_oggetto(testo)):
        if candidato is None:
            continue
        try:
            doc = json.loads(candidato)
        except json.JSONDecodeError:
            continue
        # Solo un oggetto. Una lista e' un JSON valido ma non e' una
        # risposta: accettarla farebbe fallire il chiamante su un
        # `.get` che su una lista non esiste.
        if isinstance(doc, dict):
            return doc
    return None


def _solo_oggetto(testo: str) -> str | None:
    """Il primo {...} di un testo che potrebbe avere altro attorno."""
    m = re.search(r"\{.*\}", testo, re.S)
    return re.sub(r",(\s*[}\]])", r"\1", m.group(0)) if m else None


def _applica(originale: str, correzioni: list[dict]) -> tuple[str, list[WordFix]] | None:
    """Ricostruisce il testo corregato, parola per parola.

    Ritorna None se qualcosa non torna: un indice fuori range o una
    parola che non corrisponde a quella che il modello dice di aver
    corretto. In entrambi i casi il riallineamento e' perso e correggere
    significherebbe spostare le parole di un segmento su quelle del
    successivo — che e' peggio che non correggere.

    Il numero di parole non e' verificato qui perche' non puo' cambiare:
    la lista di uscita ha la lunghezza di quella di ingresso per
    costruzione, e una correzione che punta fuori dal testo viene
    respinta prima. Il numero di parole e' quindi invariabile per
    costruzione, non per controllo.
    """
    parole = _tokenizza(originale)
    fissate: dict[int, str] = {}
    for c in correzioni:
        try:
            i = int(c.get("i", -1))
        except (TypeError, ValueError):
            return None
        if not 0 <= i < len(parole):
            return None
        # La parola che il modello dice di aver corretto deve essere
        # quella che c'e'. Se non e', i due elenchi non sono allineati e
        # applicare comunque sposterebbe tutto.
        detta = (c.get("a") or "").strip(PUNTEGGIATURA)
        reale = parole[i].strip(PUNTEGGIATURA)
        if detta and reale and detta.casefold() != reale.casefold():
            return None
        fissate[i] = c.get("b") or ""

    out = list(parole)
    for i, nuovo in fissate.items():
        testo = (nuovo or "").strip(PUNTEGGIATURA)
        if not testo:
            # Una correzione vuota lascia la parola com'era: cancellare
            # una parola non e' correggere, e' togliere informazione.
            continue
        # La punteggiatura e' dell'originale, non del modello. Se il
        # modello la ripete e poi ci aggiungo la coda, «sera.» diventerebbe
        # «sera..». Si butta via quella che ha messo lui e si rimette
        # quella che c'era: cosi' la parola finale e' identica a quella
        # che finisce davvero nel testo, e il flag «cambiata» dice la
        # verita' anche a parola singola.
        out[i] = testo + _coda(parole[i])

    return " ".join(out), [
        WordFix(indice=i, originale=parole[i], proposta=out[i])
        for i in range(len(parole))
    ]


def _motivo(exc: Exception) -> str:
    """Il pezzo di errore che dice perche' si e' fallito, in breve."""
    testo = str(exc)
    for segno in ("503", "UNAVAILABLE", "429", "RESOURCE_EXHAUSTED",
                  "overloaded", "high demand", "quota"):
        if segno.lower() in testo.lower():
            return segno
    return "errore"


def _piano(exc: Exception) -> float:
    """Secondi minimi da aspettare, secondo il tipo di errore.

    Una richiesta che il server dichiara «temporaneamente non
    disponibile» o «troppe richieste» ha bisogno di secondi, non di
    frazioni: il batch seriale di notte fa poche chiamate al secondo,
    quindi non e' il volume il problema, e riprovare subito non porta
    da nessuna parte.
    """
    testo = str(exc).lower()
    if "429" in testo or "resource_exhausted" in testo or "quota" in testo:
        return 30.0
    if ("503" in testo or "unavailable" in testo
            or "overloaded" in testo or "high demand" in testo):
        return 15.0
    return 0.0


def _retry_after(exc: Exception) -> float | None:
    """Quanto ha detto il server di aspettare, se lo ha detto.

    Il nome del campo cambia (`Retry-After`, `retryAfter`, `retry-after`)
    e può arrivare come intestazione o dentro il corpo dell'errore:
    il punto interrogativo copre il separatore che ci mette in mezzo.
    """
    m = re.search(r"retry.?after[\"':=\s]+(\d+)", str(exc), re.I)
    return float(m.group(1)) if m else None


def _modello_mancante(exc: Exception) -> bool:
    """L'errore dice «modello sconosciuto», non una punta esclamativa.

    Google risponde con un 404 e un testo che varia; qui si cerca la
    parola che conta per non dipendere dalla formulazione esatta. Il
    caso da distinguere e' solo uno: il modello esiste ma non e'
    accessibile a questo account — per esempio 2.5, che ora e' riservato
    a chi lo aveva gia' usato.
    """
    testo = str(exc).lower()
    return ("not found" in testo or "404" in testo) and (
        "model" in testo or "not supported" in testo)


class _RispostaOllama:
    def __init__(self, testo: str) -> None:
        self.text = testo


class ClientOllama:
    """Un cliente per Ollama con la stessa forma di quello di Gemini.

    `client.models.generate_content(model=..., contents=..., config=...)`
    e una risposta con `.text`: cosi' il resto del correttore (tentativi,
    backoff, filtri) non sa e non deve sapere quale motore sta usando.

    Si parla con il server locale di Ollama sull'API `/api/generate`, con
    `format: json` (la risposta e' JSON valido per costruzione),
    temperatura 0 (riproducibile) e un contesto di 8192 token: il prompt
    con glossario e contesto, piu' una riga di uscita per parola, supera i
    4096 del default.
    """

    def __init__(self, url: str = OLLAMA_URL, timeout: float = 300.0) -> None:
        self.url = url.rstrip("/")
        self.timeout = timeout
        self.models = self

    def _chiama(self, percorso: str, dati: dict | None = None) -> dict:
        import urllib.error
        import urllib.request
        corpo = json.dumps(dati).encode("utf-8") if dati is not None else None
        req = urllib.request.Request(
            self.url + percorso, data=corpo,
            headers={"Content-Type": "application/json"},
            method="POST" if corpo is not None else "GET")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return json.loads(r.read().decode("utf-8") or "{}")
        except urllib.error.HTTPError as exc:
            # Il corpo dell'errore dice il perche' («model 'x' not found»):
            # va nel messaggio, perche' `_modello_mancante` lo riconosca.
            dettaglio = exc.read().decode("utf-8", "replace")[:300]
            raise RuntimeError(f"ollama {exc.code}: {dettaglio}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(
                f"ollama non raggiungibile su {self.url}: {exc.reason}. "
                "L'app Ollama e' aperta?") from exc

    def modelli(self) -> list[str]:
        """I modelli scaricati sul Mac."""
        return [m.get("name", "") for m in self._chiama("/api/tags").get("models", [])]

    def verifica(self, modello: str) -> tuple[bool, str]:
        """Se il server risponde e il modello e' scaricato."""
        try:
            presenti = self.modelli()
        except RuntimeError as exc:
            return False, str(exc)
        nomi = set(presenti) | {n.split(":")[0] for n in presenti
                                if n.endswith(":latest")}
        if modello not in nomi:
            return False, (f"il modello '{modello}' non e' scaricato: "
                           f"ollama pull {modello}")
        return True, ""

    def generate_content(self, model=None, contents=None, config=None):
        config = config or {}
        dati = {
            "model": model,
            "prompt": contents,
            "stream": False,
            "options": {"temperature": config.get("temperature", 0),
                        "num_ctx": 8192},
        }
        if config.get("response_mime_type") == "application/json":
            dati["format"] = "json"
        return _RispostaOllama(self._chiama("/api/generate", dati).get("response", ""))


def _coda(parola: str) -> str:
    """La punteggiatura finale di una parola, staccata dal testo.

    «vera.» -> «vera» + «.». Il modello corregge la parola e non la
    punteggiatura, quindi se si prende anche quella si perde un
    segnale del tutto e si introduce rumore.
    """
    m = re.search(r"[.,;:!?)\]}]*$", parola)
    return m.group(0) if m else ""


class Correttore:
    """Chiamate a Gemini per correggere i segmenti.

    Il cliente si costruisce in modo pigro: senza chiave API il modulo
    si puo' importare e provare senza toccare la rete, che e' la
    condizione perché i test restino offline.
    """

    def __init__(
        self,
        modello: str | None = None,
        consentito: bool = False,
        pausa: float = 0.5,
        tentativi: int = 3,
        soglia_prob: float = SOGLIA_PROB,
        glossario=None,
        proteggi_nomi: bool = True,
        motore: str = MOTORE,
    ) -> None:
        if motore not in ("ollama", "gemini"):
            raise ValueError(f"motore sconosciuto: {motore}")
        self.motore = motore
        self.modello = modello or (MODELLO_LOCALE if motore == "ollama" else MODELLO)
        # `Glossario` (core/glossario.py) o None. Anche senza glossario la
        # regola della maiuscola resta attiva: e' `proteggi_nomi`, e non si
        # spegne se non per misurare quanto blocca.
        self.glossario = glossario
        self.proteggi_nomi = proteggi_nomi
        self.consentito = consentito
        self.pausa = pausa
        self.tentativi = tentativi
        self.soglia_prob = soglia_prob
        self._client = None

    def _chiave(self) -> str | None:
        """La chiave API: dall'ambiente, o dal file in `data/`.

        Il file serve al giro notturno: launchd non legge `~/.zshrc`, e
        una chiave esportata li' esiste nel Terminale ma non alle 2 di
        notte. `data/` e' fuori dalla repo (vedi `.gitignore`).
        """
        chiave = os.environ.get("GOOGLE_API_KEY") or os.environ.get("GEMINI_API_KEY")
        if chiave:
            return chiave
        from core.config import ROOT_DIR
        f = ROOT_DIR / "data" / "gemini_api_key.txt"
        try:
            testo = f.read_text(encoding="utf-8").strip()
        except OSError:
            return None
        return testo or None

    def pronto(self) -> tuple[bool, str]:
        """Se si puo' procedere, e perche' no se non si puo'."""
        if not self.consentito:
            return False, ("nessun consenso esplicito: metti consentito=True "
                           "(con --motore gemini il testo esce dal portatile)")
        if self.motore == "ollama":
            # Il server e il modello si controllano prima di cominciare:
            # meglio una riga chiara qui che settecento segmenti scartati.
            client = self._client or self._ottieni_client()
            verifica = getattr(client, "verifica", None)
            if verifica is not None:
                return verifica(self.modello)
            return True, ""
        # Un cliente gia' costruito rende la chiave irrilevante: e' cosi'
        # che il percorso di correzione si puo' provare per intero senza
        # rete e senza chiavi vere.
        if self._client is None and not self._chiave():
            return False, ("manca GOOGLE_API_KEY (nell'ambiente o in "
                           "data/gemini_api_key.txt)")
        return True, ""

    def _ottieni_client(self):
        if self._client is not None:
            return self._client
        if self.motore == "ollama":
            from core.config import config
            self._client = ClientOllama(getattr(config, "ollama_url", OLLAMA_URL))
            return self._client
        try:
            from google import genai
        except ImportError as exc:
            raise RuntimeError(
                "google-genai non installato: pip install google-genai"
            ) from exc
        self._client = genai.Client(api_key=self._chiave())
        return self._client

    def prompt(self, testo: str,
               contesto: tuple[list[str], list[str]] | None = None) -> str:
        """Le istruzioni complete per un segmento.

        `contesto` e' (frasi prima, frasi dopo) dello stesso blocco: il
        correttore capisce meglio una parola storpiata se sa di cosa si
        stava parlando, ma le correzioni restano limitate al segmento
        centrale, perche' gli indici delle parole sono i suoi.
        """
        parti = [ISTRUZIONI, ISTRUZIONI_NOMI]
        if self.glossario:
            parti.append(ISTRUZIONI_GLOSSARIO.format(
                nomi=self.glossario.per_prompt()))
        prima, dopo = contesto or ([], [])
        prima = [t for t in prima if t and t.strip()]
        dopo = [t for t in dopo if t and t.strip()]
        if prima or dopo:
            parti.append(ISTRUZIONI_CONTESTO)
            if prima:
                parti.append("Contesto prima (non correggere):\n"
                             + "\n".join(prima) + "\n")
            parti.append(f"Testo da correggere:\n{testo}\n")
            if dopo:
                parti.append("Contesto dopo (non correggere):\n"
                             + "\n".join(dopo) + "\n")
            return "\n".join(parti)
        parti.append(f"Testo:\n{testo}")
        return "\n".join(parti)

    def correggi_segmento(
        self,
        idx: int,
        testo: str,
        prob: list[float | None] | None = None,
        contesto: tuple[list[str], list[str]] | None = None,
    ) -> SegmentResult:
        """Corregge un segmento, o lo dichiara non correggibile.

        `prob` e' la probabilita' di Whisper parola per parola, nella
        stessa divisione di `testo.split()`. Se arriva, il filtro e'
        attivo; se manca, il segmento viene corretto come prima e senza
        rete di protezione, perche' in quel caso non c'e' niente su cui
        basare un giudizio.
        """
        if not testo.strip():
            return SegmentResult(idx, testo, testo)

        pronto, motivo = self.pronto()
        if not pronto:
            raise RuntimeError(f"non posso correggere: {motivo}")

        client = self._ottieni_client()
        prompt = self.prompt(testo, contesto)
        ultimo: Exception | None = None
        for tentativo in range(self.tentativi):
            try:
                risp = client.models.generate_content(
                    model=self.modello,
                    contents=prompt,
                    # Temperatura 0: due passate sullo stesso testo
                    # devono dare la stessa risposta. Senza, la stessa
                    # parola veniva corretta in modo diverso a ogni
                    # giro — «disastrati» e poi «distratti» — e una
                    # correzione che cambia da una passata all'altra non
                    # e' una correzione, e' un tiro a dadi. Su un
                    # corpus che si vuole interrogare, il risultato
                    # deve essere riproducibile.
                    config={"response_mime_type": "application/json",
                            "temperature": 0},
                )
                break
            except Exception as exc:  # noqa: BLE001
                ultimo = exc
                # Un modello che non esiste piu' non si risolve
                # aspettando: neppure al terzo tentativo risponde come
                # al primo. Fermarsi subito e dirlo vale piu' di tre
                # chiamate perse e di un segmento dichiarato «non
                # corretto» senza che nessuno capisca perche'.
                if _modello_mancante(exc) and self.motore == "ollama":
                    raise RuntimeError(
                        f"il modello '{self.modello}' non e' scaricato sul "
                        f"Mac: ollama pull {self.modello}") from exc
                if _modello_mancante(exc):
                    # Non si suggerisce il modello che ha appena
                    # fallito: sarebbe il modo piu' rapido per
                    # ritrovarsi lo stesso errore subito dopo.
                    altri = {k: v for k, v in MODELLI_NOTI.items()
                             if k != self.modello}
                    elenco = "\n  ".join(f"{k}: {v}" for k, v in altri.items())
                    primo = next(iter(altri), None) if altri else None
                    scelta = (
                        f"\n\n  Riprova con: --model {primo}\n"
                        f"  (oppure cambia il default MODELLO in "
                        f"core/text_correction.py)"
                        if primo else
                        "\n\n  Non ci sono altri modelli noti: cambia "
                        "MODELLO in core/text_correction.py"
                    )
                    raise RuntimeError(
                        f"il modello '{self.modello}' non e' piu' "
                        f"disponibile per questo account.\n  "
                        f"{elenco}{scelta}"
                    ) from exc
                # Backoff. Un rate limit o un 503 non si risolvono
                # riproendo subito: il server sta dicendo che ora non
                # puo', e rimandare di mezzo secondo serve solo a
                # farsi respingere di nuovo, consumando quota. Si
                # aspetta un minimo di qualche secondo, e se il
                # server ha indicato quanto, si ascolta lui.
                attesa = max(self.pausa * (2 ** tentativo), _piano(exc))
                indicato = _retry_after(exc)
                if indicato:
                    attesa = max(attesa, indicato)
                logger.warning("Chiamata fallita (%s: %s), riprovo fra %.1fs",
                               type(exc).__name__, _motivo(exc), attesa)
                time.sleep(attesa)
        else:
            return SegmentResult(
                idx, testo, testo, scartato=True,
                motivo_scarto=f"chiamata fallita: {type(ultimo).__name__}",
            )

        try:
            doc = _estrai_json(risp.text)
        except Exception as exc:  # noqa: BLE001
            doc = None
        if not doc or not isinstance(doc.get("correzioni"), list):
            return SegmentResult(
                idx, testo, testo, scartato=True,
                motivo_scarto="risposta non in formato JSON",
            )

        applicato = _applica(testo, doc["correzioni"])
        if applicato is None:
            return SegmentResult(
                idx, testo, testo, scartato=True,
                motivo_scarto=("le parole del modello non corrispondono a "
                               "quelle del testo: scartata"),
            )

        _, parole = applicato
        proposte = sum(1 for f in parole if f.scelta != f.originale)
        parole = self._proteggi_nomi(parole)
        parole = self._filtra(parole, prob)
        corretto = " ".join(f.scelta for f in parole)
        return SegmentResult(idx, testo, corretto, parole=parole,
                             n_proposte=proposte)

    def _proteggi_nomi(self, parole: list[WordFix]) -> list[WordFix]:
        """I nomi propri non si cambiano, e non se ne inventano di nuovi.

        Tre casi bloccati (vedi `core/glossario.py` per il perche'):

        - la parola originale e' nel glossario;
        - la parola originale ha la maiuscola fuori da inizio frase:
          Whisper l'ha riconosciuta come nome;
        - la proposta introduce una parola maiuscola fuori da inizio
          frase che prima non c'era e che non e' nel glossario: il
          modello sta inventando un nome. E' il caso «Titti» →
          «D'Alessio».

        Una correzione *verso* un nome del glossario resta permessa: e'
        l'unico modo di sistemare un nome che Whisper ha storpiato.
        """
        if not self.proteggi_nomi:
            return parole
        from core.glossario import e_nome_proprio

        originali = [f.originale for f in parole]
        proposte = [f.scelta for f in parole]
        for f in parole:
            if not f.cambiata:
                continue
            i = f.indice
            g = self.glossario
            if g and g.contiene(f.originale):
                f.bloccata, f.motivo_blocco = True, "glossario"
            elif e_nome_proprio(originali, i):
                f.bloccata, f.motivo_blocco = True, "nome"
            elif (e_nome_proprio(proposte, i)
                  and not (g and g.contiene(f.proposta))):
                f.bloccata, f.motivo_blocco = True, "nome"
        return parole

    def _filtra(
        self,
        parole: list[WordFix],
        prob: list[float | None] | None,
    ) -> list[WordFix]:
        """Le parole che Whisper aveva gia' capito non si toccano.

        Il difetto che questo chiude e' noto e si vede in un esempio:
        su «Cominciatemi ragazzi, siamo drastisovati» il correttore ha
        proposto «Camminate ragazzi, siamo disastrati» e, al secondo
        giro, «Diamoci ragazzi, siamo disastrati». La seconda parola e'
        un buon correzione; la prima e' un dialettalismo riscritto in
        italiano, e nessun prompt lo ferma.

        Ma le due non sono uguali per Whisper: «disastrati» era stato
        udito con probabilita' 0.41, «stea-» con 0.14, mentre
        «Cominciatemi» era stato udito con 0.63. Sopra una soglia il
        modello acustico ha gia' detto che sa cosa sta sentendo, e li'
        il giudizio che conta non e' piu' quello del modello di lingua.
        E' una difesa, non una garanzia: distingue gli errori acustici
        dai dialettalismi nella maggior parte dei casi, non in tutti.
        """
        if not prob or self.soglia_prob <= 0:
            return parole

        for f in parole:
            if f.indice >= len(prob):
                continue
            p = prob[f.indice]
            if p is None or not (f.cambiata or f.bloccata):
                continue
            # La probabilita' si registra su **ogni** parola cambiata, non
            # solo su quelle bloccate. Prima stava solo sulle bloccate, e
            # `--solo-proposte` stampava `p=?` su tutte le accettate: il
            # che si leggeva come «probabilita' sconosciuta» mentre voleva
            # dire «il filtro ha valutato e ha passato». Il numero c'era
            # gia', ed e' quello che permette di tarare la soglia guardando
            # la distribuzione delle proposte buone e cattive invece che a
            # occhio — che e' esattamente quello che resta aperto al punto
            # 22 di APERTI.md.
            f.prob = p
            if p < self.soglia_prob:
                continue
            # Non si butta via la proposta: resta nel file accanto alla
            # probabilita', perche' «il modello voleva cambiarla e Whisper
            # era sicuro» e' informazione, ed e' l'unica che permette di
            # tarare la soglia guardando i dati invece di indovinarla.
            # `proposta` non si tocca: `scelta` fa risalire il testo
            # all'originale da sola, e azzerare qui la proposta
            # farebbe sparire l'informazione proprio nel posto in cui
            # la si va a leggere.
            f.bloccata = True
            f.motivo_blocco = f.motivo_blocco or "prob"
        return parole

    def correggi(self, segmenti: Iterable[Any]) -> list[SegmentResult]:
        """Corregge piu' segmenti, con una pausa fra uno e l'altro.

        Ogni elemento e' una coppia (idx, testo) o una terna
        (idx, testo, probabilita'), per poter passare il filtro anche
        quando chi chiama ce l'ha.
        """
        out: list[SegmentResult] = []
        # La lista si materializza perche' serve sapere se il
        # segmento corrente e' l'ultimo: e' l'unico punto in cui non
        # ha senso aspettare prima di finire.
        segmenti = list(segmenti)
        for n,voce in enumerate(segmenti):
            idx, testo = voce[0], voce[1]
            prob = voce[2] if len(voce) > 2 else None
            contesto = voce[3] if len(voce) > 3 else None
            r = self.correggi_segmento(idx, testo, prob, contesto)
            out.append(r)
            if r.scartato:
                logger.warning("Segmento %d scartato: %s", idx, r.motivo_scarto)
            elif r.n_bloccate:
                logger.info("Segmento %d: %d correzioni proposte, %d "
                            "bloccate perche' certe", idx, r.n_proposte,
                            r.n_bloccate)
            if self.pausa and n + 1 < len(segmenti):
                time.sleep(self.pausa)
        return out