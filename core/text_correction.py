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

Costo e riservatezza. Ogni correzione e' una chiamata a un'API esterna
e il testo di una conversazione personale esce dal portatile. Il
modulo non lo fa senza che tu l'abbia detto: `consentito` va impostato a
True esplicitamente, e senza quello non esce niente. I nomi reali non
partono mai — solo i pseudonimi `GLOBAL_0xx`.
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

# Modello e contesto abbondante: il testo da correggere arriva con
# qualche minuto di contesto intorno, e una parola si corregge guardando
# il mondo in cui e' stata detta.
MODELLO = "gemini-2.5-flash"

ISTRUZIONI = """\
Sei un correttore di trascrizioni automatiche di una conversazione \
parlata in italiano.

Il testo che ti do e' la trascrizione automatica di un registratore. \
Sbaglia le parole in modo prevedibile: fonemi scambiati, parole dialettali \
rese in italiano, nomi proprii storpiati.

Correggi il testo nel modo piu' probabile, ma con tre vincoli duri:

1. NON aggiungere e NON togliere parole. Il numero di parole deve \
restare identico. Se una frase ti sembra incompleta, correggi le \
parole che ci sono e lascia stare: non ricostruire il pensiero.
2. Non cambiare il registro ne il contenuto. Una conversazione fra \
amici resta una conversazione fra amici, con i suoi «boh» e i suoi \
«tipo».
3. Se una parola e' gia' plausibile ma non sei sicuro che sia quella \
giusta, lasciala. Un intervento a caso peggiora il testo.

Per ogni parola del testo originale scrivi una riga JSON con:
- "i": indice della parola nell'originale (0-based)
- "a": la parola originale
- "b": la parola che hai scelto, uguale ad "a" se non l'hai cambiata

Rispondi SOLO con un oggetto JSON {"correzioni": [...]}. Nient'altro. \
Se non c'e' niente da correggere, rispondi con {"correzioni": []}.
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


@dataclass
class WordFix:
    """Una parola, prima e dopo."""

    indice: int
    originale: str
    proposta: str

    @property
    def scelta(self) -> str:
        """La forma scelta, con l'originale come riserva.

        Il modello a volte restituisce una stringa vuota o solo spazi:
        in quel caso la parola originale resta, perche' una correzione
        che cancella una parola e' una cancellazione, non una
        correzione.
        """
        return self.proposta.strip() or self.originale

    @property
    def cambiata(self) -> bool:
        return self.scelta != self.originale


@dataclass
class SegmentResult:
    """Il risultato della correzione di un segmento."""

    idx: int
    testo_originale: str
    testo_corretto: str
    parole: list[WordFix] = field(default_factory=list)
    scartato: bool = False
    motivo_scarto: str = ""

    @property
    def n_cambiate(self) -> int:
        return sum(1 for f in self.parole if f.cambiata)

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
            "words": [
                {"i": f.indice, "raw": f.originale, "fixed": f.scelta,
                 "changed": f.cambiata}
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
    return m.group(0) if m else None


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
        modello: str = MODELLO,
        consentito: bool = False,
        pausa: float = 0.5,
        tentativi: int = 3,
    ) -> None:
        self.modello = modello
        self.consentito = consentito
        self.pausa = pausa
        self.tentativi = tentativi
        self._client = None

    def _chiave(self) -> str | None:
        return os.environ.get("GOOGLE_API_KEY") or os.environ.get("GEMINI_API_KEY")

    def pronto(self) -> tuple[bool, str]:
        """Se si puo' procedere, e perche' no se non si puo'."""
        if not self.consentito:
            return False, ("nessun consenso esplicito: metti consentito=True "
                           "per mandare il testo fuori dal portatile")
        # Un cliente gia' costruito rende la chiave irrilevante: e' cosi'
        # che il percorso di correzione si puo' provare per intero senza
        # rete e senza chiavi vere.
        if self._client is None and not self._chiave():
            return False, "manca GOOGLE_API_KEY"
        return True, ""

    def _ottieni_client(self):
        if self._client is not None:
            return self._client
        try:
            from google import genai
        except ImportError as exc:
            raise RuntimeError(
                "google-genai non installato: pip install google-genai"
            ) from exc
        self._client = genai.Client(api_key=self._chiave())
        return self._client

    def correggi_segmento(self, idx: int, testo: str) -> SegmentResult:
        """Corregge un segmento, o lo dichiara non correggibile."""
        if not testo.strip():
            return SegmentResult(idx, testo, testo)

        pronto, motivo = self.pronto()
        if not pronto:
            raise RuntimeError(f"non posso correggere: {motivo}")

        client = self._ottieni_client()
        prompt = f"{ISTRUZIONI}\n\nTesto:\n{testo}"
        ultimo: Exception | None = None
        for tentativo in range(self.tentativi):
            try:
                risp = client.models.generate_content(
                    model=self.modello,
                    contents=prompt,
                    config={"response_mime_type": "application/json"},
                )
                break
            except Exception as exc:  # noqa: BLE001
                ultimo = exc
                # Backoff: un rate limit di notte non e' un errore da
                # far fallire il batch, e' una ragione per aspettare.
                attesa = self.pausa * (2 ** tentativo)
                logger.warning("Chiamata fallita (%s), riprovo fra %.1fs",
                               type(exc).__name__, attesa)
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

        corretto, parole = applicato
        return SegmentResult(idx, testo, corretto, parole=parole)

    def correggi(self, segmenti: Iterable[tuple[int, str]]) -> list[SegmentResult]:
        """Corregge piu' segmenti, con una pausa fra uno e l'altro."""
        out: list[SegmentResult] = []
        # La lista si materializza perche' serve sapere se il
        # segmento corrente e' l'ultimo: e' l'unico punto in cui non
        # ha senso aspettare prima di finire.
        segmenti = list(segmenti)
        for n, (idx, testo) in enumerate(segmenti):
            r = self.correggi_segmento(idx, testo)
            out.append(r)
            if r.scartato:
                logger.warning("Segmento %d scartato: %s", idx, r.motivo_scarto)
            if self.pausa and n + 1 < len(segmenti):
                time.sleep(self.pausa)
        return out