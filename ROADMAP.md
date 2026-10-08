# Roadmap — audio-to-text e corpus

Stato al 7 ottobre 2026. Questo documento dice **in che ordine** fare le
cose e **perché in quell'ordine**. I dettagli tecnici di ogni problema
già noto stanno in [`APERTI.md`](APERTI.md); qui si rimanda ai suoi
numeri (es. «APERTI 10»).

Le repo coinvolte:

- `pietrofabbri/audio-to-text` (pubblica) — la pipeline: import dal
  registratore, VAD, denoise, Whisper, diarizzazione, prosodia,
  correttore, corpus SQLite locale, giro notturno, pubblicazione.
- `pietrofabbri/corpus` (privata) — il materiale testuale pubblicato da
  `publish_corpus.py`. Non contiene mai audio, embedding vocali,
  database o log. Le voci compaiono come pseudonimi `GLOBAL_xxx`.
- Hardware: registratore TileRec, descritto nel documento di progetto
  `hardware-registrazione.md` (Project «Psico-fisio app» su claude.ai).

---

## Punto di partenza (misurato il 7 ottobre)

| Cosa | Valore |
|---|---|
| Sessioni nel corpus | 19, su 3 giorni (2, 4, 5 ottobre) |
| Parole trascritte | ~72.000 |
| Struttura corpus | una cartella per **file registrato** (`sessions/2026-10-05_09-39-09/`), 10–12 file ciascuna |
| Voci distinte nel database | 37, da 78 campioni |
| Coppie di voci in zona grigia | 49 (soglia 0,78) |
| Voci con un nome | nessuna nel corpus (solo pseudonimi) |
| Correzione con Gemini | **costruita** (`correct_text.py`), provata in asciutto sul 2 ottobre, **mai applicata**: nessun file `*.corrected.*` nel corpus |
| Colonna «Data» di `INDEX.md` | vuota («—») su tutte le righe |

Il registratore spezza le registrazioni in file da 60 minuti. Gli
intervalli misurati tra un file e il successivo:

| Giorno | Intervalli tra file consecutivi | Lettura |
|---|---|---|
| 2 ottobre (sera) | +90 s, +13 s, +4 s | registrazione continua |
| 4 ottobre | +252 s … +1.397 s | pause vere (4–23 min) |
| 5 ottobre | +44 s … +174 s | registrazione continua |

Quindi un «momento continuo» è una catena di file con buchi di pochi
secondi o minuti, ed è questo il criterio usato in Fase 2.

---

## L'ordine e il perché

```
Fase 0  Pulizia                     piccola, subito
Fase 1  Voci con un nome            prima di tutto il resto
Fase 2  Una cartella per giorno     dipende dagli ID stabili della Fase 1
Fase 3  Correzione con Gemini       dipende da nomi (glossario) e giorni (contesto)
Fase 4  Hardware, biometria, termico  in parallelo, quando c'è il materiale
```

- **Le voci vengono prima della struttura per giorno** perché
  consolidare le voci (unire i doppioni) cambia gli ID `GLOBAL_xxx`.
  Se si migra il corpus prima, lo si migra due volte.
- **La struttura per giorno viene prima di Gemini** perché il
  correttore lavora meglio con più contesto, e il giorno unificato è il
  contesto più lungo e coerente che esista.
- **I nomi vengono prima di Gemini** perché l'unico difetto noto del
  correttore è che riscrive i nomi propri (vedi Fase 3). Il rimedio è un
  glossario, e il glossario comincia dai nomi delle voci.

---

## Fase 0 — Pulizia

Taglia: piccola. Nessuna dipendenza.

1. **Colonna «Data» vuota in `INDEX.md`.** La data è già nel nome della
   sessione (`AAAA-MM-GG_hh-mm-ss`); `publish_corpus.py` non la
   estrae. Diventerà comunque superflua in Fase 2, ma intanto l'indice
   mente.
2. **Date impossibili in `APERTI.md`.** Diversi punti risultano «chiusi
   l'8 ottobre» o «il 10 ottobre», cioè nel futuro rispetto ai commit
   (l'ultimo è del 5 ottobre). Lo stesso errore è finito nel documento
   hardware («11 sessioni al 10/10»). Si ricostruiscono le date vere
   dai commit (`git log`) e si correggono.
3. **Modifiche non committate sul Mac.** Il commit `d00b6ab` dice che
   delle correzioni a `nightly.py` e `tests/test_nightly.py` erano nella
   copia di lavoro e non incluse. Va controllato con `git status` sul
   Mac: se ci sono ancora, vanno committate o scartate.

**Fatto quando:** l'indice mostra le date, `APERTI.md` non contiene date
successive all'ultimo commit, `git status` sul Mac è pulito.

**Stato: fatta il 7 ottobre.**

- Colonna Data: corretta in `publish_corpus.py` (commit `32440b9`, con
  test); l'indice del corpus è stato rigenerato e pubblicato (corpus
  `de5c074`). La data ora si legge da `session.json`, poi da
  `transcript.json`, poi dal nome della sessione.
- Date in `APERTI.md`: ricostruite da `git log` (4 e 5 ottobre) nello
  stesso commit; corretto anche il documento hardware del Project.
- Mac: `git status` pulito. Le modifiche a `nightly.py` citate nel
  messaggio di `d00b6ab` erano già nel commit stesso. La copia sul Mac
  era indietro di due commit ed è stata aggiornata; 11 suite verdi.

---

## Fase 1 — Dare un nome alle voci

Taglia: media. Dipende da: niente.

**Cosa esiste già.** `review_speakers.py` ha `list`, `name`, `merge`,
`split`, `threshold`, `consolidate`, `sync`. I nomi stanno nel database
locale delle voci e `sync` li propaga a tutto il materiale già scritto.
Il pezzo mancante non è lo strumento ma **il flusso**: oggi non c'è un
momento in cui il sistema ti chiede «chi è questa voce nuova?».

**Il problema da risolvere prima.** 37 voci da 78 campioni sono troppe
per 3 giorni di vita normale: molte sono la stessa persona spezzata in
due (sovrasegmentazione, APERTI 19). Dare un nome a 37 voci vuol dire
dare lo stesso nome più volte. Secondo APERTI 10, aggregando le
decisioni per coppia di voci invece che per singolo campione, le
indecisioni passano da 34 a 4.

Passi:

1. **Aggregazione per coppia nel report** (resto aperto di APERTI 10),
   poi decidere a mano le coppie rimaste con `merge`/`split`.
2. **Ascolto prima del nome.** Per ogni voce, 3–5 estratti audio brevi
   (pochi secondi, scelti fra i segmenti con più alta confidenza) più le
   frasi trascritte corrispondenti. Gli estratti restano **solo in
   locale**: sono audio e la regola del corpus li esclude. Comando
   proposto: `review_speakers.py ascolta GLOBAL_007`.
3. **Il rito dopo la notte.** Alla fine del giro notturno, un breve
   elenco: «stanotte sono comparse queste voci nuove, con almeno N
   secondi di parlato». Per ognuna: dai un nome, unisci a una voce nota,
   oppure lascia anonima (passanti, televisione, voci di sfondo). Le
   voci sotto la soglia di parlato non vengono proposte, per non
   chiedere un nome a ogni frase raccolta per strada.
4. **Nomi e corpus — decisione tua (D1).** Oggi il corpus pubblica solo
   pseudonimi; `push --with-names` pubblica i nomi. La repo è privata,
   ma contiene conversazioni di altre persone. Le due scelte sensate:
   - pseudonimi nel corpus e mappa nomi solo in locale (default attuale,
     più protettivo);
   - nomi nel corpus, accettando che chi accede alla repo sappia chi ha
     detto cosa.

**Fatto quando:** le voci ricorrenti hanno un nome, le coppie in zona
grigia sono decise, e dopo una notte nuova il sistema chiede solo delle
voci davvero nuove.

**Stato al 7 ottobre: strumenti fatti, decisioni da prendere** (commit
`d918d9c` e successivo; dettaglio in APERTI 32 e nel README, sezione
«Dare un nome alle voci»).

| Passo | Stato |
|---|---|
| 1. Aggregazione per coppia | **Fatta.** `review_speakers.py voices`: 49 coppie di campioni in zona grigia → **10 coppie di voci** da decidere |
| 2. Ascolto prima del nome | **Fatto.** `review_speakers.py ascolta <voce> --play`; 24 estratti già tagliati per le 8 voci con più parlato |
| 3. Il rito dopo la notte | **Fatto.** `review_speakers.py nuove` e `ignora`; la notte scrive `output/voci_da_rivedere.md` e taglia gli estratti |
| 4. Nomi nel corpus (D1) | **Fatto.** `corpus_with_names` in `core/config.py`, default `False` |
| Decidere le 10 coppie | **Tuo.** Ascoltarle e usare `merge` dove sono la stessa persona |
| Dare i nomi | **Tuo.** Dopo le unioni: `nuove`, poi `name` o `ignora` |

Ordine consigliato per la parte tua: prima le 10 coppie (unire), poi i
nomi, così ogni persona si nomina una volta sola. `GLOBAL_001`, presente
in 18 sessioni su 19 con 240 minuti, è quasi certamente Pietro.

---

## Fuori dalle fasi — Import automatico dal TileRec

**Fatto il 7 ottobre** (APERTI 33, README «Inserisci il TileRec»). Il
registratore serve solo per il tempo della copia: all'inserimento un job
launchd copia le registrazioni in `input/coda/` con verifica
dell'impronta, le cancella dal registratore, lo espelle e notifica «puoi
staccarlo». La trascrizione avviene dopo, dalla coda (passata diurna
subito, poi passate delle 09:30/15:30/21:30 e la notte, che pubblica).

Da confermare al primo inserimento vero (8 ottobre): velocità USB del
TileRec, permesso di macOS sui volumi rimovibili, nome del volume.

**Fase 1, parte tua (coppie e nomi): rimandata al 14 ottobre**, su
richiesta di Pietro; c'è un promemoria programmato.

---

## Fase 2 — Una cartella per giorno, momenti continui unificati

Taglia: grande (tocca pubblicazione, indice, test, migrazione del
corpus). Dipende da: Fase 1 (ID delle voci stabili).

**Principio.** L'elaborazione **resta per file**: checkpoint, giro
notturno, limiti termici e recupero dei file troncati funzionano già
così e non vanno toccati. L'unificazione è una **vista**, costruita a
valle: nel database locale e nella pubblicazione.

**Struttura proposta del corpus:**

```
corpus/
  INDEX.md                 una riga per giorno
  giorni/
    2026-10-05/
      giorno.json          manifesto: file sorgenti, offset, blocchi, durate, voci
      transcript.txt       orari assoluti (hh:mm:ss), separatore tra blocchi
      transcript.srt
      segments.jsonl       ogni segmento con ora assoluta e file di origine
      tokens.jsonl
      prosody.csv
      wordfreq.csv         frequenze del giorno intero
      analysis_ready.md    il giorno intero, pronto per un LLM
      (*.corrected.*)      le varianti corrette, dopo la Fase 3
  voices/
    voice_matrix.json
```

**Regole da fissare (D3):**

- **Blocco continuo:** file consecutivi separati da meno di 5 minuti.
  I dati misurati sopra giustificano la soglia: il 2 e il 5 ottobre i
  buchi stanno sotto i 3 minuti, il 4 ottobre le pause vere superano i
  4. Dentro un blocco il testo scorre senza interruzioni; tra un blocco
  e l'altro c'è un separatore con l'ora e la durata della pausa.
- **Appartenenza al giorno:** un blocco appartiene al giorno in cui
  **comincia**, anche se attraversa la mezzanotte, così una
  conversazione serale non viene tagliata in due.
- **Confine tra file:** una frase spezzata dal cambio di file viene
  segnata (`"taglio_file": true`) e non ricucita a forza: l'audio tra i
  due file non esiste.
- **Ora assoluta:** ora di inizio dal nome del file
  (`session_start_wall`) più l'offset del segmento. La deriva
  dell'orologio del TileRec non è ancora misurata (Fase 4); finché non lo
  è, gli orari valgono al minuto, non al secondo.

**I pezzi orari (D4).** Proposta: non vengono più pubblicati come
cartelle separate. `giorno.json` conserva per ogni file sorgente l'ora
d'inizio, la durata, la decisione di denoise e l'esito delle unioni di
voci, così nessuna informazione va persa. `transcript.json` (≈800 KB a
sessione) è ridondante rispetto a `tokens.jsonl` e `segments.jsonl` e
può uscire dalla pubblicazione.

Passi:

1. Funzione di assemblaggio del giorno da N sessioni (pura, testata su
   un giorno sintetico con un buco e un taglio di mezzanotte).
2. `publish_corpus.py` pubblica `giorni/` invece di `sessions/`;
   aggiornare `ARTEFATTI_CORPUS`, l'indice e `tests/test_publish.py`.
3. Lo stesso nel database locale: interrogazioni per giorno e per
   blocco.
4. Migrazione: rigenerare i 3 giorni esistenti dalle 19 sessioni locali,
   verificare che il numero di parole e di segmenti torni identico,
   poi togliere `sessions/` dal corpus in un unico commit.

**Fatto quando:** il corpus ha una cartella per giorno, il conteggio di
parole e segmenti dopo la migrazione coincide con quello di prima, e una
notte nuova aggiorna la cartella del giorno invece di crearne una nuova.

**Stato: fatta il 7 ottobre** (APERTI 34, README «Cosa c'è sulla repo»),
anticipata rispetto alla Fase 1 su richiesta di Pietro. La dipendenza
dalla Fase 1 non blocca: le giornate si ricostruiscono da `output/` a ogni
pubblicazione, quindi dopo un `merge` o un nome il push successivo
riscrive le giornate toccate. `core/giorno.py` compone la giornata;
`publish_corpus.py` pubblica `giorni/` e migra da solo `sessions/` al
primo push. Le giornate del corpus vero hanno gli stessi conteggi di
parole delle sessioni di partenza (16.740, 24.338, 30.665).

---

## Fase 3 — Controllo delle parole inverosimili con Gemini

Taglia: media. Dipende da: Fase 1 (glossario dei nomi), Fase 2
(contesto del giorno).

**Cosa esiste già.** `correct_text.py` + `core/text_correction.py`,
modello `gemini-3.5-flash-lite`. Funziona così:

- il modello propone correzioni parola per parola, senza riscrivere il
  testo;
- se il numero di parole cambia, la risposta si scarta;
- una parola che Whisper ha sentito con probabilità ≥ 0,90 non si tocca
  (`--soglia-prob`): sulla prova ha bloccato 63 proposte su 394 (16%);
- l'uscita è **affiancata**, non sostitutiva: ogni parola conserva
  l'originale, così si misura l'errore di entrambi i modelli;
- senza `--consent` non parte nessuna chiamata.

Sulla prova in asciutto del 2 ottobre: 331 correzioni accettate, una
quarantina controllate a mano, quasi tutte buone (`statole → scatole`,
`salate spensate → serate spensierate`).

**L'unico difetto noto: i nomi propri.** Su «Zia Titti» il modello ha
scritto «Gigi D'Alessio» in entrambe le occorrenze. Incontra un nome che
non conosce e lo sostituisce con uno che conosce. È l'errore più
pericoloso, perché il risultato sembra plausibile e non lo segnala
niente.

Passi:

1. **Glossario locale** dei nomi propri: i nomi delle voci (Fase 1) più
   una lista curata a mano (parenti, luoghi, soprannomi). Il glossario
   va nel prompt, e una regola nel codice vieta di cambiare una parola
   del glossario o una parola con la maiuscola fuori da inizio frase.
   Il caso «Zia Titti» diventa un test di regressione.
2. **Contesto più largo.** Mandare insieme al segmento quelli vicini
   dello stesso blocco continuo (Fase 2), sempre chiedendo correzioni
   solo sul segmento centrale.
3. **Decisione sulla privacy (D2).** Il consenso a essere registrati non
   copre automaticamente l'invio del testo a Google. Da decidere:
   - se le persone registrate vanno informate di questo passaggio;
   - quale piano API usare: secondo i termini di Google, sul piano
     gratuito i contenuti possono essere usati per migliorare i
     prodotti, su quello a pagamento no (**da verificare sui termini
     in vigore** prima di attivarlo);
   - in alternativa, un modello locale (es. tramite Ollama): niente
     esce dal Mac, ma la qualità sull'italiano parlato va misurata ed è
     probabilmente più bassa.
4. **Nella catena notturna.** Dopo la trascrizione e prima della
   pubblicazione, con il consenso dato una volta in configurazione e
   registrato nel log. Ordine di grandezza: un giorno come il 5 ottobre
   ha ~770 segmenti, quindi ~770 chiamate, da far stare nei limiti di
   frequenza del piano scelto.
5. **Misura.** Un campione di 100 parole cambiate, rivisto a mano,
   ogni tanto: percentuale di correzioni giuste, sbagliate e dubbie.
   È l'unico modo di sapere se il correttore migliora il testo o lo
   rende solo più scorrevole.

**Fatto quando:** il caso «Zia Titti» passa, ogni giorno nuovo esce
anche in versione corretta, e c'è una misura di qualità aggiornata.

---

## Fase 4 — In parallelo, quando c'è il materiale

Queste cose non bloccano le fasi precedenti e non ne sono bloccate.

- **Supporto del TileRec sulla fascia Helio** e **saturazione**
  (APERTI 7, documento hardware): prima capire da dove vengono i picchi
  (voce di Pietro, altre voci o urti), poi provare con e senza
  attenuatore sui microfoni.
- **Deriva dell'orologio** del TileRec: serve agli orari assoluti della
  Fase 2 e al sync biometrico.
- **Sync biometrico con Amazfit Helio** (APERTI 8): quando l'hardware è
  in uso.
- **Termico** (APERTI 3b): una notte misurata con `powermetrics`.

---

## Decisioni che spettano a te

| # | Domanda | Serve per | Stato |
|---|---|---|---|
| D1 | Nel corpus: pseudonimi (oggi) o nomi reali? | Fase 1 | **Decisa il 7/10:** i nomi reali sono ammessi nel corpus privato. **Attivati l'8/10** (`corpus_with_names = True` in `core/config.py`): nel corpus le voci con un nome compaiono come «Nome (GLOBAL_xxx)». Le note sulle voci restano solo in locale. |
| D2 | Gemini: si manda il testo a Google? Con quale piano, e informando chi è registrato? Oppure un modello locale? | Fase 3 | **In sospeso:** Pietro chiede prima l'ok alle persone registrate. |
| D3 | Blocco continuo = buchi sotto i 5 minuti; il blocco appartiene al giorno in cui comincia. Va bene? | Fase 2 | **Applicata il 7/10** con la Fase 2 (soglia in `core/giorno.py`, `SOGLIA_CONTINUITA_SEC`). |
| D4 | I pezzi orari spariscono dal corpus, restano solo nel manifesto del giorno. Va bene? | Fase 2 | **Decisa il 7/10:** Pietro preferisce un file unico per tipo per giorno; le sessioni restano descritte in `giorno.json` e in locale in `output/`. |
