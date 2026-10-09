#!/usr/bin/env python3
"""
Correzione del testo delle sessioni con un LLM.

    python correct_text.py                    # cosa verrebbe fatto
    python correct_text.py --dry --limit 5     # cinque correzioni, niente scrittura
    python correct_text.py --consent           # scrive davvero
    python correct_text.py --consent --session 19-42-33
    python correct_text.py --session 19-42-33 --riscorri
    python correct_text.py --consent --dry --solo-proposte

`--solo-proposte` e' il modo di guardare una passata intera. Il
confronto originale/corretto serve per capire un segmento; su
quattrocentoventinove segmenti sono pagine, e la domanda che conta
dopo e' piu' piccola: quali parole il modello vuole cambiare, quante
volte, e con quanta sicurezza Whisper le aveva udite. Una riga per
parola, e in una pagina c'e' tutto. Le proposte respinte dal filtro
compaiono insieme alle altre, perche' e' guardando quelle che si decide
dove mettere la soglia.

Perche' `--consent` e non un interruttore silenzioso. Ogni segmento
mandato a un'API esterna porta fuori dal portatile il testo di una
conversazione personale. Il testo di questa pipeline e' gia' pensato
per non uscire mai — i nomi reali non arrivano al corpus pubblico, e
ci si e' messi cura. Mandare il testo a un servizio esterno e' una
decisione diversa, e non la prende uno script per abitudine: quindi
qui la porta e' chiusa per default e si apre solo quando lo chiedi
esplicitamente. Senza `--consent` il comando mostra cosa farebbe e si
ferma.

Perche' `--dry` esiste anche con `--consent`. Centoventisei segmenti per
sessione, quattro sessioni: sono oltre cinquecento correzioni, e il
giudizio su una correzione e' tuo, non mio. `--dry --limit 5` mostra
cinque segmenti originali e corretti uno sotto l'altro, e da li' si
decide se vale la pena spendere il resto. Costa cinque chiamate.

Il file che ne esce e' affiancato, non sostitutivo. Ogni parola
conserva originale, correzione e se e' cambiata. Questo permette di
misurare quanto il modello sbaglia e quanto sbagliava Whisper: due
errori insieme sono la misura vera della qualita' della trascrizione.
Senza il confronto uno dei due sparisce e non si sa quale.

Ogni segmento scarta e' dichiarato nel file con il motivo. Un segmento
non corretto non e' un errore: e' un segmento di cui non ci si fida, e
la differenza conta perche' il testo che resta e' il testo detto.

E non si riscrive tutto quello che il modello propone. Whisper sa con
quanta probabilita' ha udito ogni singola parola, e sopra una soglia
quel numero e' un giudizio piu' forte di quello di un modello di
lingua: quando il modello acustico era sicuro, la parola non si tocca.
Il filtro e' `--soglia-prob`, metti 0 per disattarlo.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from core.config import OUTPUT_DIR  # noqa: E402
from core.glossario import Glossario  # noqa: E402
from core.text_correction import (  # noqa: E402
    MODELLO, MODELLO_LOCALE, MOTORE, PUNTEGGIATURA, SOGLIA_PROB, Correttore, allinea_probabilita,
    scrivi_varianti,
)

# Quanti segmenti prima e dopo mandare come contesto. Due per parte sono
# circa una trentina di secondi di conversazione: abbastanza per capire
# di cosa si parla, poco abbastanza da non moltiplicare il costo.
CONTESTO = 2

logger = logging.getLogger("correct_text")

NOME_FILE = "text_correction.json"


def _firma(testo: str) -> str:
    """L'impronta di un testo, per capire se e' gia' stato corretto.

    Una notte interrotta e un tentativo rifatto non devono costare due
    volte, e non devono neppure correggere due volte lo stesso testo:
    la seconda passata su un testo gia' corretto non lo migliora, lo
    peggiora potenzialmente. L'impronta e' il testo intero perche' e'
    l'unica cosa che conta: se il segmento non e' cambiato, la risposta
    vecchia resta valida.
    """
    return hashlib.sha1(testo.encode("utf-8")).hexdigest()[:16]


def _leggi_sessione(d: Path) -> list[dict]:
    """I segmenti di una sessione, in ordine."""
    path = d / "segments.jsonl"
    if not path.exists():
        return []
    out = []
    for riga in path.read_text(encoding="utf-8").splitlines():
        if riga.strip():
            out.append(json.loads(riga))
    out.sort(key=lambda s: s.get("idx", 0))
    return out


def _leggi_probabilita(d: Path) -> dict[int, list[float | None]]:
    """La probabilita' di Whisper per ogni parola, segmento per segmento.

    Va letta dal checkpoint e non da `segments.jsonl`: e' li' che Whisper
    scrive la probabilita' di ogni parola, e `segments.jsonl` la omette
    di proposito per non portare dentro ogni riga un elenco di parole.
    Il checkpoint e' il posto dove il dato e' nato, ed e' l'unico che
    esiste anche quando la trascrizione e' gia' stata scritta.

    Se il checkpoint manca — una sessione processata da una versione
    vecchia, o i file di lavoro spostati — si torna con una tabella
    vuota e il filtro non fa niente, che e' il comportamento giusto:
    meglio correggere senza rete di protezione che fingersi che ci sia.
    """
    path = d / f"{d.name}.checkpoint.json"
    if not path.exists():
        return {}
    try:
        dati = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("%s: checkpoint illeggibile (%s), filtro disattivato",
                       path.name, exc)
        return {}

    out: dict[int, list[float | None]] = {}
    for chunk in dati.get("chunks") or []:
        testo = chunk.get("text") or ""
        if not testo.strip():
            continue
        out[int(chunk.get("idx", 0))] = allinea_probabilita(
            testo, chunk.get("words"))
    return out


def _carica_precedente(d: Path) -> dict[int, dict]:
    """Le correzioni gia' fatte in un giro precedente."""
    path = d / NOME_FILE
    if not path.exists():
        return {}
    try:
        dati = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        logger.warning("%s: illeggibile, lo rifaccio da capo", path.name)
        return {}
    return {int(r["idx"]): r for r in dati.get("segments", [])}


def _scrivi(d: Path, risultati: list[dict], modello: str,
            soglia: float = SOGLIA_PROB) -> None:
    """Il file di correzione della sessione, con il riepilogo.

    Il riepilogo conta le parole e quante sono cambiate, perche' e' la
    domanda che ci si pone subito: «quanto ha lavorato il modello?» Ma
    la risposta interessante e' l'altra, e sta anche nel file: quante
    parole ha cambiato piu' di una volta, e quante sono state scartate.
    """
    parole = sum(r["n_words"] for r in risultati)
    cambiate = sum(r["n_changed"] for r in risultati)
    scartati = sum(1 for r in risultati if r.get("discarded"))
    proposte = sum(r.get("n_proposed", 0) for r in risultati)
    bloccate = sum(r.get("n_blocked", 0) for r in risultati)
    riepilogo = {
        "segments": len(risultati),
        "words": parole,
        "words_proposed": proposte,
        "words_changed": cambiate,
        "words_blocked": bloccate,
        "changed_share": round(cambiate / parole, 3) if parole else 0.0,
        "discarded": scartati,
        "model": modello,
        "prob_threshold": soglia,
    }
    out = {"summary": riepilogo, "segments": risultati}
    (d / NOME_FILE).write_text(
        json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")


def _mostra_confronto(righe: list[tuple[dict, dict]]) -> None:
    """Originale e correzione uno sotto l'altro, per segmenti.

    Leggere le due colonne in fila e' difficile: servono due passaggi
    con l'occhio e si perde il nesso. Uno sopra l'altro si legge come
    una correzione fatta a mano, che e' esattamente quello che si vuole
    giudicare.
    """
    for seg, res in righe:
        print(f"\n  [{seg.get('start', 0):7.1f}s] {seg.get('speaker', '?')}")
        print(f"    prima : {res['original_text']}")
        if res.get("discarded"):
            print(f"    scartato: {res.get('discard_reason', '')}")
            continue
        print(f"    dopo  : {res['corrected_text']}")
        if res["n_changed"]:
            parole = [f"{w['raw']}->{w['fixed']}" for w in res["words"]
                      if w["changed"]]
            print(f"    {res['n_changed']} parole: {', '.join(parole)}")
        else:
            print("    nessuna correzione")
        # Le proposte respinte si vedono anche loro: e' la traccia di
        # quello che il filtro ha salvato, senza la quale «nessuna
        # correzione» non distingue «il modello non ha proposto nulla»
        # da «il modello voleva cambiare sei parole e gliele ho negate».
        bloccate = [w for w in res["words"] if w.get("blocked")]
        if bloccate:
            dettaglio = ", ".join(
                f"{w['raw']} (p={w['prob']:.2f})" for w in bloccate)
            print(f"    {len(bloccate)} bloccate perche' certe: {dettaglio}")


def _parola_pulita(parola: str) -> str:
    """La parola come si cerca nel vocabolario: senza punteggiatura."""
    return parola.strip(PUNTEGGIATURA).lower()


def _vocabolario(sessioni: list[Path]) -> dict[str, tuple[int, float, int]]:
    """Ogni parola delle trascrizioni: quante volte, e con quanta sicurezza.

    Serve a una cosa sola: dire se la parola che il modello propone
    esiste gia' nelle trascrizioni o e' la prima volta che compare.
    «frasi» che il correttore propone per «frasci» e' una parola che
    Whisper aveva gia' capita tredici volte: il modello non sta
    inventando, sta adattando. Una parola che non compare mai e' un
    caso da guardare con piu' attenzione.

    Il conteggio e' su tutte le occorrenze, la media solo su quelle che
    hanno la probabilita': un segmento senza checkpoint — o una parola
    che Whisper ha spezzato in due — non abbassa la media con uno zero
    che non e' mai stato misurato. Il terzo numero del valore, quante
    occorrenze hanno contribuito alla media, serve a non mostrare una
    media come se fosse stata misurata su tutte. Media e conteggio non
    coincidono, e con questa tabella e' meglio che si veda.

    Ma non e' un verdetto, ed e' meglio dirlo qui perche' il primo
    dato che viene in testa e' sbagliato: sulle quattro notti vere anche
    «distratti» — la correzione che e' giusta, «siamo drastisovati» in
    «siamo distratti» — non e' mai stata udita, perche' in quelle ore
    nessuno ha detto «distratti». La colonna dice che una parola non
    ha riscontro nel corpus: non dice che e' sbagliata. A decidere
    resta la lista, quindi il giudizio.
    """
    conta: dict[str, list[float]] = {}
    for d in sessioni:
        if not (d / "segments.jsonl").exists():
            continue
        probabilita = _leggi_probabilita(d)
        for s in _leggi_sessione(d):
            idx = int(s.get("idx", 0))
            for posizione, parola in enumerate(s.get("text", "").split()):
                voce = conta.setdefault(_parola_pulita(parola), [0.0, 0.0, 0.0])
                voce[0] += 1
                probs = probabilita.get(idx) or []
                if posizione < len(probs) and probs[posizione] is not None:
                    voce[1] += float(probs[posizione])
                    voce[2] += 1.0
    return {k: (int(v[0]), v[1] / v[2] if v[2] else 0.0, int(v[2]))
            for k, v in conta.items() if k}


def _raccogli_proposte(risultati: list[dict], sessione: str) -> dict:
    """Le parole che il modello ha proposto di cambiare, raggruppate.

    Una parola proposta venti volte e' un fatto della trascrizione: si
    sente male e la si scrive ogni volta in un modo diverso. Una parola
    proposta una volta sola e' rumore. Senza il conteggio le due cose
    hanno lo stesso peso sulla carta, e chi legge la lista finisce per
    decidere sul caso singolo mentre il caso ripetuto passa inosservato.

    Le proposte respinte dal filtro contano insieme alle altre, e con la
    stessa importanza: e' proprio sapere che cosa il modello voleva
    scrivere dove Whisper era sicuro che permette di tarare la soglia
    sui dati invece che a caso.
    """
    out: dict[tuple[str, str, bool], dict] = {}
    for r in risultati:
        for w in r.get("words") or []:
            if not (w.get("changed") or w.get("blocked")):
                continue
            raw, proposta = w.get("raw") or "", w.get("proposta") or ""
            # Una proposta uguale all'originale non e' una proposta, ne'
            # solo di punteggiatura ne' altro: qui non c'e' niente da
            # giudicare, e una riga in piu' rende la tabella meno
            # leggibile. Il confronto lo fa il testo affiancato.
            if _parola_pulita(raw) == _parola_pulita(proposta):
                continue
            chiave = (raw, proposta, bool(w.get("blocked")))
            voce = out.setdefault(
                chiave, {"n": 0, "probs": [], "sessioni": set()})
            voce["n"] += 1
            p = w.get("prob")
            if p is not None:
                voce["probs"].append(float(p))
            voce["sessioni"].add(sessione)
    return out


def _unisci(uno: dict, due: dict) -> dict:
    """Somma due raccolte, tenendo conto anche delle sessioni."""
    for chiave, voce in due.items():
        dest = uno.setdefault(chiave, {"n": 0, "probs": [], "sessioni": set()})
        dest["n"] += voce["n"]
        dest["probs"] += voce["probs"]
        dest["sessioni"] |= voce["sessioni"]
    return uno


def _nota_vocabolario(proposta: str, vocabolario: dict,
                      sessioni: set[str] = frozenset()) -> str:
    """La parola proposta, e quanto Whisper l'ha capita in altre occasioni.

    Le sessioni sono un parametro e non una parte del vocabolario: sono
    le sessioni da cui e' venuta *questa* proposta, e possono essere due
    mentre la parola proposta si trova anche in altre tre.
    """
    voce = vocabolario.get(_parola_pulita(proposta))
    if voce is None:
        nota = "vocabolario: MAI UDITA"
    else:
        n, media, quante = voce
        p = f"p={media:.2f}" if quante else "p=? (mai misurata)"
        su = f" su {quante}" if quante < n else ""
        nota = f"vocabolario: {n}x {p}{su}"
    dove = ""
    if sessioni:
        sedute = sorted(sessioni)
        dove = "  " + (", ".join(sedute) if len(sedute) <= 2
                       else f"{len(sedute)} sessioni")
    return f"{nota}{dove}"


def _ordina(proposte: dict) -> list[tuple]:
    """Le piu' frequenti prima, e fra parita' in ordine alfabetico.

    L'ordinamento mette davanti le parole che il modello propone piu'
    spesso perche' sono quelle che decidono: «frasci -> frasi» dodici
    volte insegna piu' di dodici correzioni uniche e fragili. Sul resto
    l'ordine e' quello del dizionario, cosi' che due passate sulla stessa
    lista si possano confrontare riga per riga.
    """
    return sorted(proposte.items(),
                  key=lambda kv: (-kv[1]["n"], _parola_pulita(kv[0][1])))


def _riga_proposta(chiave: tuple[str, str, bool], voce: dict,
                   vocabolario: dict,
                   con_sessioni: bool = False) -> str:
    raw, proposta, bloccata = chiave
    ps = voce["probs"]
    p = f"p={min(ps):.2f}" if ps else "p=?"
    sessioni = voce["sessioni"] if con_sessioni else frozenset()
    return (f"  {'respinta' if bloccata else 'accettata'}  "
            f"{raw} -> {proposta}  x{voce['n']}  {p}  "
            f"{_nota_vocabolario(proposta, vocabolario, sessioni)}")


def _stampa_proposte(sessione: str, proposte: dict, vocabolario: dict) -> None:
    if not proposte:
        print(f"\n=== {sessione}: nessuna parola proposta ===")
        return
    accettate = sum(v["n"] for k, v in proposte.items() if not k[2])
    respinte = sum(v["n"] for k, v in proposte.items() if k[2])
    distinte_acc = sum(1 for k in proposte if not k[2])
    distinte_res = sum(1 for k in proposte if k[2])
    mai = sum(1 for k in proposte
              if _parola_pulita(k[1]) not in vocabolario)
    print(f"\n=== {sessione}: {len(proposte)} parole diverse proposte "
          f"({distinte_acc} accettate, {distinte_res} respinte), "
          f"in {accettate + respinte} occorrenze "
          f"({accettate} da correggere, {respinte} bloccate), "
          f"{mai} mai udite ===")
    for chiave, voce in _ordina(proposte):
        print(_riga_proposta(chiave, voce, vocabolario))


def _stampa_sintesi(proposte: dict, vocabolario: dict,
                    limite: int = 25) -> None:
    """Tutte le sessioni insieme, e solo le parole piu' frequenti.

    Il limite serve perche' l'elenco completo di quattro notti puo'
    essere lungo, e le parole che compaiono una volta sola sono rumore
    di Whisper piu' che un difetto del correttore: non fanno decidere
    nulla da sole.
    """
    if not proposte:
        return
    accettate = sum(v["n"] for k, v in proposte.items() if not k[2])
    respinte = sum(v["n"] for k, v in proposte.items() if k[2])
    mai = sum(1 for k in proposte if _parola_pulita(k[1]) not in vocabolario)
    ordinate = _ordina(proposte)
    print(f"\n=== tutte le sessioni: {len(proposte)} parole diverse, "
          f"{accettate} occorrenze da correggere, "
          f"{respinte} respinte dal filtro, "
          f"{mai} mai udite in nessuna sessione ===")
    for chiave, voce in ordinate[:limite]:
        print(_riga_proposta(chiave, voce, vocabolario, con_sessioni=True))
    if len(ordinate) > limite:
        print(f"  ... e altre {len(ordinate) - limite} parole, "
              f"una volta ciascuna o poche")


def _contesti(tutti: list[dict], n: int) -> dict[int, tuple[list[str], list[str]]]:
    """Per ogni segmento, il testo dei vicini nella stessa sessione.

    I vicini sono presi dalla sessione intera e non solo dai segmenti da
    correggere in questa passata: un giro ripreso a meta' deve vedere lo
    stesso contesto di un giro fatto tutto in una volta, altrimenti la
    stessa frase verrebbe corretta in modo diverso a seconda di quando
    la notte si e' interrotta.
    """
    if n <= 0:
        return {}
    testi = [s.get("text", "") for s in tutti]
    out = {}
    for k, s in enumerate(tutti):
        out[int(s.get("idx", 0))] = (testi[max(0, k - n):k], testi[k + 1:k + 1 + n])
    return out


def _giorno(d: Path) -> str:
    """La data della sessione, `AAAA-MM-GG`, dal nome della cartella."""
    return d.name[:10]


def _sessioni_da_elaborare(args, out_dir: Path) -> list[Path]:
    """Le cartelle da guardare, con `--session` che accetta l'ora sola.

    Le cartelle si chiamano `2026-10-02_19-42-33`, ma l'ora e' l'unica
    parte che distingue una sessione dall'altra e l'unica che si ricorda.
    Accettare anche il nome intero evita che `--session 19-42-33` finisca
    col messaggio «sessione non trovata» su una sessione che c'e'.
    """
    if args.session:
        scelto = args.session.strip()
        trovate = [p for p in sorted(out_dir.glob("*")) if p.is_dir()
                   and (p == scelto or p.name.endswith(f"_{scelto}")
                        or p.name == scelto)]
        if not trovate:
            raise SystemExit(f"sessione non trovata: {scelto} in {out_dir}")
        return [p for p in trovate if (p / "segments.jsonl").exists()] or trovate
    return sorted(p for p in out_dir.glob("*") if p.is_dir()
                  and (p / "segments.jsonl").exists())


def _config_motore() -> str:
    from core.config import config
    return getattr(config, "correzione_motore", MOTORE)


def _config_modello_locale() -> str:
    from core.config import config
    return getattr(config, "correzione_modello_locale", MODELLO_LOCALE)


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Correggi con un LLM il testo delle trascrizioni")
    ap.add_argument("--session", help="una sessione sola (es. 19-42-33)")
    ap.add_argument("--consent", action="store_true",
                    help="autorizza la correzione (con --motore gemini anche "
                         "l'invio del testo a Google)")
    ap.add_argument("--motore", choices=("ollama", "gemini"),
                    default=_config_motore(),
                    help="ollama: modello locale sul Mac, gratuito, il testo "
                         "non esce (default); gemini: API di Google")
    ap.add_argument("--dry", action="store_true",
                    help="stampa le correzioni senza scrivere nulla")
    ap.add_argument("--riscorri", action="store_true",
                    help="ricalcola anche i segmenti gia' corretti")
    ap.add_argument("--limit", type=int, default=0,
                    help="al massimo N segmenti per sessione")
    ap.add_argument("--model", default=None,
                    help=f"modello; default {MODELLO_LOCALE} con ollama "
                         f"(core/config.py: correzione_modello_locale), "
                         f"{MODELLO} con gemini")
    ap.add_argument("--pausa", type=float, default=0.5,
                    help="secondi fra una chiamata e l'altra")
    ap.add_argument("--soglia-prob", type=float, default=SOGLIA_PROB,
                    help="non correggere le parole che Whisper aveva "
                         f"gia' udite con almeno questa probabilita' "
                         f"(default {SOGLIA_PROB}; 0 per disattivare)")
    ap.add_argument("--solo-proposte", action="store_true",
                    help="mostra solo le parole che il modello propone di "
                         "cambiare, non i testi: una riga per parola, con "
                         "la probabilita' che Whisper le aveva dato e "
                         "quante volte la proposta compare")
    ap.add_argument("--out-dir", default=str(OUTPUT_DIR),
                    help="cartella delle sessioni")
    ap.add_argument("--contesto", type=int, default=CONTESTO,
                    help=f"segmenti vicini mandati come contesto, per parte "
                         f"(default {CONTESTO}; 0 per nessuno)")
    ap.add_argument("--no-glossario", action="store_true",
                    help="non usare data/glossario.txt ne' i nomi delle voci "
                         "(la regola della maiuscola resta)")
    ap.add_argument("--escludi-giorno", action="append", default=[],
                    metavar="AAAA-MM-GG",
                    help="non correggere le sessioni di questo giorno "
                         "(ripetibile): per i giorni in attesa di consenso")
    ap.add_argument("--max-seconds", type=float, default=0,
                    help="fermati dopo questo tempo (0 = nessun limite); "
                         "le sessioni restanti si riprendono al giro dopo")
    ap.add_argument("--sintetico", action="store_true",
                    help="niente confronto riga per riga: una riga per "
                         "sessione (per il giro notturno)")
    args, avanzi = ap.parse_known_args()

    # Le righe con un commento in coda si copiano dal README, e in zsh
    # il `#` non e' un commento interattivo: il finale arriva qui come
    # argomento e argparse risponde «unrecognized arguments», che non
    # dice nulla di utile. Meglio nominarlo.
    if avanzi:
        if avanzi[0].startswith("#"):
            # Il comando suggerito ricostruito **senza** il commento: se
            # contenesse ancora il `#`, chi lo copia per provare
            # riceverebbe lo stesso errore, e il suggerimento sarebbe
            # un modo per non uscirne.
            pulito = [a for a in sys.argv[1:] if not a.startswith("#")]
            raise SystemExit(
                f"\n  Il '#' finale e' arrivato come argomento.\n"
                f"  In zsh, nei comandi interattivi, '#' non e' un commento\n"
                f"  se INTERACTIVE_COMMENTS non e' impostato.\n\n"
                f"  Questo funziona:\n"
                f"    {Path(sys.argv[0]).name} {' '.join(pulito)}\n\n"
                f"  Oppure, una volta sola in ~/.zshrc:\n"
                f"    setopt interactive_comments\n"
                f"  e da li' in avanti i commenti in coda si potranno copiare\n"
                f"  dalle righe del README.\n"
            )
        ap.error("argomenti sconosciuti: " + " ".join(avanzi))

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    out_dir = Path(args.out_dir)
    sessioni = _sessioni_da_elaborare(args, out_dir)
    if not sessioni:
        raise SystemExit(f"nessuna sessione in {out_dir}")
    esclusi = {g.strip() for g in args.escludi_giorno if g.strip()}
    if esclusi:
        saltate = [d.name for d in sessioni if _giorno(d) in esclusi]
        sessioni = [d for d in sessioni if _giorno(d) not in esclusi]
        if saltate:
            print(f"Escluse (giorni senza consenso): {', '.join(saltate)}")
        if not sessioni:
            print("Nessuna sessione da correggere dopo le esclusioni.")
            return 0

    # Unocchiata a cosa ci sarebbe da correggere, prima di qualsiasi
    # decisione sulla chiave API.
    print(f"{len(sessioni)} sessioni in {out_dir}")
    totale = 0
    for d in sessioni:
        n = len(_leggi_sessione(d))
        fatte = len(_carica_precedente(d))
        da_fare = 0 if args.riscorri else n - fatte
        totale += max(0, da_fare)
        print(f"  {d.name}: {n} segmenti, {fatte} gia' corretti, "
              f"{max(0, da_fare)} da correggere")
    print(f"\n{totale} segmenti da correggere in tutto")

    if not args.consent:
        print("\nNessuna chiamata: manca --consent.")
        return 0

    if args.dry:
        print(f"\n[dry-run] fino a {args.limit or 'tutti'} segmenti per "
              f"sessione, niente scritto\n")

    proposte_totali: dict = {}
    # Il vocabolario si costruisce solo se serve, e una volta sola: sono
    # quattro checkpoint da rileggere, e serve a una colonna sola.
    vocabolario = _vocabolario(sessioni) if args.solo_proposte else {}
    glossario = None if args.no_glossario else Glossario.carica()
    if glossario is not None:
        print(f"Glossario: {len(glossario)} nomi protetti"
              + ("" if glossario else
                 " (aggiungili in data/glossario.txt, uno per riga)"))
    modello = args.model or (_config_modello_locale() if args.motore == "ollama"
                             else MODELLO)
    print(f"Motore: {args.motore}, modello {modello}"
          + (" (locale: il testo non esce dal Mac)" if args.motore == "ollama"
             else " (il testo va a Google)"))
    correttore = Correttore(modello=modello, motore=args.motore, consentito=True,
                            pausa=args.pausa, soglia_prob=args.soglia_prob,
                            glossario=glossario)
    pronto, motivo = correttore.pronto()
    if not pronto:
        raise SystemExit(f"non posso procedere: {motivo}")

    inizio = time.monotonic()
    interrotto = False
    for d in sessioni:
        if args.max_seconds and time.monotonic() - inizio > args.max_seconds:
            # Si ferma fra una sessione e l'altra, mai a meta': il file di
            # correzione si scrive alla fine della sessione, e il lavoro
            # gia' fatto resta. La sessione successiva riprende da qui.
            print(f"\nTempo esaurito ({args.max_seconds:.0f}s): le sessioni "
                  f"restanti si correggono al prossimo giro.")
            interrotto = True
            break
        segmenti = _leggi_sessione(d)
        if not segmenti:
            continue
        contesti = _contesti(segmenti, args.contesto)
        if not args.riscorri:
            gia = _carica_precedente(d)
            segmenti = [s for s in segmenti
                        if int(s.get("idx", 0)) not in gia]
        if args.limit:
            segmenti = segmenti[:args.limit]
        if not segmenti:
            print(f"{d.name}: niente da fare")
            continue

        print(f"\n=== {d.name}: {len(segmenti)} segmenti ===")
        probabilita = _leggi_probabilita(d) if args.soglia_prob > 0 else {}
        if args.soglia_prob > 0 and not probabilita:
            logger.warning("%s: nessuna probabilita' per parola, il filtro "
                           "--soglia-prob non puo' agire", d.name)
        da_inviare = [(int(s.get("idx", 0)), s.get("text", ""),
                       probabilita.get(int(s.get("idx", 0))),
                       contesti.get(int(s.get("idx", 0))))
                      for s in segmenti]
        risultati = correttore.correggi(da_inviare)
        esiti = [res.to_dict() for res in risultati]

        righe = []
        out_segmenti = []
        for seg, r in zip(segmenti, esiti):
            r["start"] = seg.get("start")
            r["end"] = seg.get("end")
            r["speaker"] = seg.get("speaker")
            r["firma"] = _firma(seg.get("text", ""))
            out_segmenti.append(r)
            righe.append((seg, r))
            if not args.dry and not args.solo_proposte and not args.sintetico:
                print(f"  [{seg.get('idx'):>4}] {r['n_changed']:>3} correzioni"
                      + ("  SCARTATO" if r["discarded"] else ""))

        if args.solo_proposte:
            raccolte = _raccogli_proposte(esiti, d.name)
            _unisci(proposte_totali, raccolte)
            _stampa_proposte(d.name, raccolte, vocabolario)
        elif args.sintetico:
            cambiate = sum(r["n_changed"] for r in esiti)
            nomi = sum(r.get("n_blocked_names", 0) for r in esiti)
            scartati = sum(1 for r in esiti if r["discarded"])
            print(f"  {d.name}: {len(esiti)} segmenti, {cambiate} parole "
                  f"corrette, {nomi} nomi protetti, {scartati} scartati")
        else:
            _mostra_confronto(righe)

        if args.dry:
            continue

        # I segmenti scartati restano nel file: sapere che quel testo e'
        # rimasto grezzo e' informazione, non rumore. Saltarli perderebbe
        # la traccia di dove il modello ha fallito.
        #
        # E i giri precedenti vanno tenuti: se il file si riscrive solo
        # coi segmenti di adesso, un girointerrotto a meta' perderebbe
        # tutto il lavoro di ieri. Si unisce per indice, e un segmento
        # rifatto in questa passata sostituisce il suo vecchio.
        uniti = {r["idx"]: r for r in _carica_precedente(d).values()}
        uniti.update({r["idx"]: r for r in out_segmenti})
        finali = sorted(uniti.values(), key=lambda r: r["idx"])
        _scrivi(d, finali, args.model, args.soglia_prob)

        # Le varianti pubblicabili, cosi' che su GitHub si legga il
        # testo corretto e non quello grezzo. `transcript.txt` resta
        # intatto: i due errori devono restare entrambi visibili.
        scritti = scrivi_varianti(d, _leggi_sessione(d), {
            r["idx"]: r for r in finali})
        print(f"  scritto {d / NOME_FILE}")
        for p in scritti:
            print(f"  scritto {p.name}")

    if args.solo_proposte and len(sessioni) > 1:
        _stampa_sintesi(proposte_totali, vocabolario)

    riepilogo = getattr(correttore._client, "riepilogo", None)
    if callable(riepilogo) and riepilogo():
        print("\n" + riepilogo())
    if args.dry:
        print("\n[dry-run] niente scritto. Con --consent si scrive.")
    if interrotto:
        print("Correzione parziale: riprende al prossimo giro.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
