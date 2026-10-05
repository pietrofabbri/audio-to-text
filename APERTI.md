# Punti aperti

Stato al 3 ottobre 2026. Ogni punto dice **cosa manca**, **pro e
contro**, **perché** e **di chi è la decisione**. La responsabilità è
dichiarata perché la cosa peggiore di un elenco di cose aperte è non
sapere quale aspettare e quale fare.

Convenzione: **Io** = lavoro di codice che posso fare subito.
**Tu** = serve il registratore, una decisione tua, o un dispositivo che
non ho ancora.

**Chiusi:** 2 (cache WAV), 3 (finestra notturna), 4 (nomi dei
parlanti), 5 (punteggiatura come segnale debole), 6 (flag di qualità),
9 (pubblicazione, provata con una coda finta).
**Archiviato:** 7 (saturazione) — non risolvibile da qui.
**Chiuso anche:** il carico termico, che è stato il punto 3b ed è
diventato una scoperta (4 thread sono più veloci di 8).
**Aperti:** 1 (la prima notte vera), 8 (biometria), 10–14 (da valutare).
**Chiusi stanotte:** 15 (la prosodia in parallelo, che non terminava
mai), 16 (rifare la trascrizione perdeva gli interlocutori), 17 (la
cache dei WAV), 18 (il database locale vuoto), 19 (la sovrasegmentazione
delle voci: 21 identita' globali su quattro ore di una conversazione
sono diventate 9). Tutti e cinque scoperti elaborando i file veri: vedi
la sezione in fondo.

---

## Bloccanti — da fare prima della prima notte vera

### 1. ~~Nessuna notte reale è mai girata end-to-end~~ — chiuso il 4 ottobre

**Stato.** Chiuso, e meglio di quanto si sperasse: ha prodotto un difetto
che nessun test precedente poteva vedere.

**Cosa è successo.** `pull` su un registratore USB vero (un
`HS USB FlashDisk`, exFAT, 62 GiB), sette file da un'ora del 4 ottobre.
Sei sono passati per la catena intera: **6 completati, 0 falliti**,
ognuno verificato, archiviato e cancellato dal device. Il settimo ha
fatto trovare il difetto.

**Il difetto.** `2026-10-04_10-49-40.MP3` dichiarava 57.600.000 byte — un'ora
esatta — e sulla card ce n'erano 3.538.944. Il driver lo dice in una riga
di log che vale un ettaro:

```
EXFAT_BeginBlockmap: Read with requested offset >= file allocated size. Exiting.
```

Il registratore era rimasto senza corrente mentre scriveva. Il file non
era illeggibile in blocco: era troncato, e `Errno 22` è la risposta a un
offset che non esiste, non un errore di rete o di permessi.

**Perché i test non lo avevano visto.** Servivano due cose che un test
finto non ha: un filesystem che dica di no, e un `ffprobe` che menta. E
`ffprobe` menteva: ricavava 3600,0 s dividendo il byte count per il
bitrate, quindi un file con tre minuti si presentava come un'ora, e ogni
stima partiva da lì senza che nessuno potesse accorgersene.

**Cosa è cambiato nel codice.** Il file troncato non viene più saltato:
si salva il pezzo leggibile, lo si elabora con il nome vero del device,
e solo dopo che la trascrizione esiste ed è verificata si cancellano
sia il pezzo di lavoro sia l'originale. Dieci test nuovi. E due difetti
miei che sono usciti solo quando la cosa ha girato sul vero:

- la copia di lavoro aveva un suffisso di hash nel nome, e la pipeline
  scrive `meta.stem` in `transcript.json` a partire dal nome del file che
  riceve: la sessione finiva nel corpus come
  `2026-10-04_10-49-40-1508d6ee` mentre la cartella era
  `2026-10-04_10-49-40`, e i due nomi non tornavano più insieme;
- il pezzo di lavoro sparisce con l'archivio, e veniva comunque
  «cancellato» subito dopo: un `avviso di impossibile cancellare`
  stampato accanto a un cancellamento riuscito, che è il modo più rapido
  per non far notare il prossimo avviso che conta.

**Cosa è rimasto aperto.** La coda ha impiegato 1 h 31 min per 6 file da
un'ora, e il tempo dipende dalla quantità di parlato più che dalla
durata: 482 s per un file con 1.434 parole e 1.035 s per uno con 6.492.

**Il difetto che quella notte non poteva mostrare.** La coda era stata
lanciata **senza budget**, e con `--max-seconds` il comportamento è un
altro. Nel codice la durata del file veniva letta con ffprobe **dopo**
la cancellazione: il file non c'era più, ffprobe restituiva `None`, e
`finished_file(None or 0.0)` finiva con `max(0.0, 1.0)` — il budget
contava **un secondo** di audio dove ne aveva 3.600.

Da lì in poi l'RTF che aveva imparato era **482** invece di 0,13, la stima
sul file successivo dava **1.735.200 secondi** — venti giorni — e con la
finestra notturna da 4 ore la coda si fermava dopo il primo file. Gli
altri sette restavano sul registratore, ogni notte, e nessun errore: il
log diceva solo «Budget esaurito», che è un comportamento legittimo.

Non è un difetto della notte del 4 ottobre, che è passata senza budget e
come tale non lo mostrava. È un difetto che la notte con budget avrebbe
mostrato alla prima esecuzione, e che nessuna prova fino ad adesso
guardava. La durata ora si legge prima che il file sparisca, e un test
verifica che il budget conti i secondi di audio veri.

---

### 2. ~~La cache WAV non viene mai cancellata~~ — chiuso il 3 ottobre

**Stato.** Chiuso. `pipeline/vad.py` espone `purge_wav_cache()`, chiamata
da `run.py` appena la sessione è completa.

**La regola, che è la parte difficile.** Tre casi, non uno:

| Caso | Cosa si fa | Perché |
|---|---|---|
| sessione finita | cancella subito | il WAV non serve più |
| nessun checkpoint lo cita, età > 6 h | cancella | è un orfano: nessuno lo riapre |
| nessuno lo cita, appena scritto | **tira avanti** | può essere una run in corso |
| checkpoint incompleto | **mai** | serve per riprendere dal chunk interrotto |

L'ultima riga è quella che viene facilmente sbagliata: cancellare il WAV
di una sessione a metà significa che la notte dopo ricomincia dal primo
chunk, e il lavoro di notte è buttato. Lo copre
`test_purge_keeps_wav_of_unfinished_session`.

**Conto.** ~4 GB/giorno smettono di accumularsi, e il controllo spazio
disco di `sync_device.py check` smette di minacciare.

---

### 3. ~~La finestra notturna è scritta in tre posti~~ — chiuso il 3 ottobre

**Stato.** Chiuso. Orario, durata, thread, priorità e pausa stanno tutti
in `core/config.py`; `nightly.py` e `setup_launchd.py` li leggono da lì.

**Perché una costante e non tre copie.** Il difetto non era il numero
sbagliato: era che esistevano tre numeri. Il piano che leggi a mano e
quello che gira di notte erano due piani diversi per la stessa notte, e
"la coda non si chiude" significava due cose a seconda di chi chiedeva.

Il test `t_window_lives_in_one_place` verifica la catena intera, non
solo la costante: prende il `--max-seconds` che il plist passa a
`nightly.py` e lo confronta con il default che `nightly.py` usa quando
lo lanci a mano. Sono due cose diverse che possono divergere.

---

## Termico — la macchina scalda troppo di notte

### 3b. La pipeline teneva la CPU al massimo per quattro ore

**Stato.** Chiuso, e il risultato ha superato le aspettative.

**Cosa è successo misurando, non ragionando.** Ho misurato l'ASR su
10 chunk e 166 s di parlato della registrazione vera, cambiando solo il
numero di thread:

| Thread | Tempo | Realtime sul parlato |
|---|---|---|
| 8 | 53,4 s | 3,11× |
| **4** | **43,0 s** | **3,86×** |
| 3 | 47,9 s | 3,47× |
| 2 | 66,9 s | 2,48× |

Quattro thread sono **più veloci** di otto. La M1 Pro ha 4 core
performance e 4 efficiency: usarli tutti insieme non raddoppia il
lavoro, aggiunge contesa e tiene la CPU al pacchetto termico massimo,
dopo il quale scende la frequenza e va più piano di quanto andasse con
la metà dei core.

**Perché conta più del tempo.** Il risparmio termico non è il prezzo di
un rallentamento: **fa parte** del miglioramento. Il file da 16 minuti
resta da 16 minuti — anzi, un filo più corto — e la macchina non si
scalda. Quello che resta da calmare è il resto.

**Cosa è stato fatto.**

- `NIGHT_THREADS = 4`, tetto dichiarato in `MAX_THREADS` e verificato da
  un test: nessuno può alzarlo "perché tanto la macchina è libera",
  che è esattamente il cambiamento che peggiorerebbe tempo e calore
  insieme;
- prosodia da 4 a 2 worker (è un secondo carico parallelo, e insieme
  all'ASR conta più di quanto dichiarato);
- `nice 10` di notte, `nice 15` di giorno — anche lanciando a mano, non
  solo da launchd: il termico non deve dipendere da chi ha digitato il
  comando;
- pausa di respiro di 90 s fra un file e il successivo (30 s di giorno).
  Il costo è dichiarato nel docstring e nel `--help`: la coda avanza un
  po' meno, la macchina resta usabile il giorno dopo.

**Il conto, per onestà.** Con 18 file da un'ora al giorno: 4 thread
(16 min/file) + 90 s di respiro ≈ 17,5 min per file, ~13 file a notte
contro ~15 prima. Le passate diurne coprono il resto. La coda resta in
pari o quasi, ma il margine è più stretto di prima e va guardato nelle
prime notte vere.

**Cosa resta aperto.** Il comportamento termico vero — ventola,
frequenza, temperatura — non l'ho misurato: per farlo servono
`powermetrics` e qualche ora di registrazione (punto 15). Quello che ho
misurato è il tempo, che è il termine che conta per la coda.

---

## Qualità — il corpus si degrada piano, e non se ne accorge

### 4. ~~Rinominare una voce non aggiorna nulla di esistente~~ — chiuso il 3 ottobre

**Stato.** Chiuso. La fonte è una sola, `data/speakers_db.json`. Tutto il
resto è derivato e si riallinea con un comando.

**La decisione presa sul nome.** Il nome reale può stare in
`transcript.json` e in `session.json` anche in locale: è materiale che
resta sulla tua macchina. Resta però una decisione separata e più grave,
che è **cosa finisce sulla repo del corpus**. Il default di
`publish_corpus.py` continua a sostituire i nomi con gli pseudonimi, e
esiste `push --with-names` per pubblicarli volutamente. La ragione della
separazione è che cambiare la privacy del materiale locale è reversibile,
mentre pubblicare nomi veri non lo è.

**Cosa è stato fatto.**

- `core/speaker_sync.py`: allinea `corpus.db`
  (`CorpusDB.sync_speaker_names`) e ogni `session.json` /
  `transcript.json` già scritto;
- `review_speakers.py sync`, e `name` e `merge` lo chiamano da soli — un
  rename non finisce più a metà, con il nome nuovo in un file e
  invecchiato in tutti gli altri;
- le voci senza nome non vengono riempite con il pseudonimo: nella
  colonna `name`, `GLOBAL_004` sembrerebbe un nome e non lo è.

**Una sottigliezza che è costata un test.** La mappa dei nomi di una
sessione si ricostruisce dagli ID che *quel file* contiene, non dal DB
delle voci: copiare tutto in ogni sessione significa che il
`session.json` di martedì contiene informazioni su persone che non hanno
parlato martedì. Ma gli ID già elencati nella mappa contano come
riferimenti, altrimenti togliere un nome non lo toglierebbe mai da lì.

**Cosa non fa.** Non rilegge l'audio e non rielabora. Ma il merge delle
identità, quello sì lo fa: `review_speakers.py merge` **rietichetta le
sessioni già scritte** (segmenti, statistiche, mappe locali, anche le
chiavi dei dizionari in `session.json`) e le righe di `corpus.db`.
Senza questo, unire due voci lasciava due ID per la stessa persona nel
corpus, con statistiche che non si sommano — cioè il problema che il
merge doveva risolvere restava aperto.

Ci sono voluti due bug per farlo bene, e sono il tipo di bug che si
incontrano solo provando:

- cancellando la voce assorbita dalla tabella voci, se nel corpus c'era
  **solo** quella, la tabella restava vuota e le query per parlante non
  trovavano più nessuno. Ora l'ID di destinazione viene creato se
  manca;
- in `session.json` gli speaker sono un **dizionario indicato per ID**:
  sostituire solo i valori lasciava la voce vecchia come chiave. E
  rinominare le chiavi durante l'iterazione del dizionario solleva un
  `RuntimeError` che avrebbe fatto fallire il merge a metà.

---

### 5. ~~Il denoise misura quantità, non qualità della punteggiatura~~ — chiuso il 3 ottobre

**Stato.** Chiuso, come **segnale debole**, cioè esattamente come era
stato proposto.

`pipeline/denoise.py` misura ora i segni di punteggiatura ogni 100
caratteri e li usa **solo come spareggio**: entrano in gioco quando
confidenza, ritmo, parlato e segmenti degeneri sono in pareggio. Se la
confidenza dice chiaramente una delle due varianti, la punteggiatura non
ha voto — ci sono due test che lo verificano, perché è il modo più
naturale di fare in modo che uno "segnale debole" finisca per essere
un criterio.

**Tre scelte che rendono la misura onesta.**

- **Sotto 200 caratteri non si misura.** Su quattro lettere un punto
  cambia tutto, e un numero inventato che può decidere una scelta è
  peggio di nessun numero.
- **L'apostrofo non conta.** "l'acqua" è elisione, non confine di
  frase: contarlo inflazionerebbe ogni frase con due parole elise.
- **Il margine è più alto del doppio** (0,6 segni/100 caratteri) di
  quello sulla confidenza, perché il dato è più rumoroso. La direzione
  in cui sbaglia è tenere l'originale.

**Il costo dichiarato.** Su una differenza vera di una virgola (0,82
contro 1,23 segni/100 caratteri) la decisione resta "originale". Il
dato serve, non decide: serve nei casi in cui la differenza è grande,
e li si vedranno solo dopo qualche settimana di `denoise_decision.json`.

---

### 6. ~~Nessun segnale che dica "qui la trascrizione è rotta"~~ — chiuso il 3 ottobre

**Stato.** Chiuso. `core/quality.py` valuta ogni segmento e gli assegna
`ok` / `low` / `unreliable`, con i **motivi** che ci vanno dietro. Il
verdetto finisce in `transcript.json`, in `segments.jsonl`, in
`prosody.csv`, in `analysis_ready.md` (in testa al documento e in linea
a ogni segmento), e nelle colonne `quality` / `quality_reasons` di
`corpus.db`.

**Cinque segnali, ciascuno per un modo diverso in cui Whisper sbaglia:**

| Segnale | Cosa cattura |
|---|---|
| `no_speech_prob` alto | il modello stesso dice "qui non parlavi" |
| parole al secondo fuori scala | allineamento rotto |
| loop di n-gramma | il caso tipico della registrazione vera |
| testo vuoto o troppo corto | segmenti che non dicono nulla |
| quota di parole a bassa probabilità | il modello che indovina, e lo sa |

**Un bug vero trovato scrivendo i test.** Il loop più comune in
assoluto — una sola parola ripetuta — non veniva intercettato: le
occorrenze di un n-gramma sono sovrapposte, e la guardia che contava
le parole minime come se fossero disgiunte chiedeva 12 parole dove ne
bastavano 7. Ora il caso reale ("ma tu non vado a fare il bambino" ×22)
viene visto, e c'è un test dedicato.

**Il pericolo dichiarato, e come è stato tenuto sotto controllo.** Un
flag che scatta spesso viene ignorato. Per questo:

- **un solo segnale dà `low`, non `unreliable`** — una parola a
  probabilità 0,3 è ancora informazione;
- **senza timestamp di parola non si giudica la confidenza** — il
  transcriber ricade apposta su quei chunk, e penalizzarli sarebbe
  rumore inventato;
- **`low_word_share` oltre alla media**: con quattro parole a 0,9 e
  una a 0,2 la media è 0,74 e sembra ottima. Il modello aveva indovinato
  una parola su cinque e la media lo copriva.

**Due query che lo rendono usibile** (`CorpusDB.quality_report()` e
`suspect_text()`). Senza, il flag sarebbe una colonna che nessuno
guarda: la domanda utile non è "quanti segmenti sono brutti" ma "posso
analizzare questa sessione", e il riassunto è pesato sulle **parole**,
non sui segmenti.

**Taratura, misurata l'8 ottobre su undici sessioni (1.060 segmenti).**
Era la verifica che mancava, e l'aveva rimandata esplicitamente: «se
`unreliable` è sotto il 5% il flag è tarato bene; se è sopra il 30%, le
soglie vanno alzate».

| | segmenti | ok | low | unreliable |
|---|---|---|---|---|
| tutte le sessioni | 1.060 | 999 | 9 | **52 (4,9%)** |

**4,9%: le soglie restano.** Non vanno alzate, ed è una conclusione
nascosta dentro una domanda che sembrava dovesse dare una risposta
sbagliata.

Il numero aggregato dice poco, perché la varianza fra sessioni è enorme
(da 0,0% a 28,6%). Il dato che decide è **perché** scattano: 45 dei 45
segmenti segnalati per `ritmo_fuori_scala`, e per 35 di questi il testo
sono cinque parole o meno dentro una finestra di 25 secondi. La
tentazione era concludere che il flag fosse un falso positivo — finestre
lunghe con testo breve, come «Grazie a tutti.» che compare 11 volte.

**Era sbagliato, e i timestamp per parola lo smentiscono.** Dentro
quelle finestre i conteggi tornano esatti («3/3», «6/6»): dentro ci sono
solo le parole del segmento, nessuna in più, quindi non è padding. E la
probabilità che Whisper dà a quelle parole **crolla**:

| | parole | probabilità mediana | sotto 0,90 | sotto 0,60 |
|---|---|---|---|---|
| finestre sospette | 160 | **0,636** | 73,1% | **45,0%** |
| segmenti normali | 36.781 | 0,960 | 38,7% | 15,9% |

E i casi peggiori sono parlanti: `per(0.04)`, `i(0.05)`, `dove(0.14)`,
`Vivo!(0.51)`. Non è che il modello abbia sentito bene e sia stato
etichettato a torto: è che lì il modello **non sentiva niente** e ha
scritto lo stesso. Il flag sta trovando il difetto vero.

Un dato che è uscito e che nessuno aveva chiesto: **l'RTF segue il
parlato, non la durata**. Fra il file con 1.434 parole e quello con 6.492
la durata è la stessa (un'ora) e il tempo va da 482 s a 1.035 s. Una
stima che dia lo stesso costo a due file da un'ora sbaglia sempre, e
sbaglia di più proprio sul file peggiore.

---

### 7. ~~Il registratore satura~~ — archiviato il 3 ottobre

**Stato.** Archiviato, non risolto. Hai detto che del registratore non
puoi fare nulla, e la misura dice che hai ragione a non tentare: picco
+3,2 dBFS, 0,19–0,30% dei campioni a fondo scala. L'informazione è già
persa nel file, e nessun filtro la ricrea. Si può solo limitare il
danno a valle, e il denoise lo fa già scegliendo da solo la variante
migliore.

Non lo chiudo perché sia risolto: lo chiudo perché è l'unico punto
dell'elenco in cui la soluzione non è nel software, e tenerlo in aperto
accanto a cose che posso fare io serviva solo a ricordarti un vincolo
che conosci già. Resta scritto qui perché il dato — +3,2 dBFS, 0,19–0,30%
a fondo scala — è la misura con cui giudicare se le registrazioni
future peggiorano.

---

---

## Funzionalità — una parte chiusa, una bloccata

### 9. ~~Il corpus non è mai stato pubblicato con dati veri~~ — chiuso il 3 ottobre

**Stato.** Chiuso come **prova**, non come pubblicazione vera: la catena
è stata provata per intero con una coda finta e un clone git con
remoto locale. Nessun dato tuo è uscito, e GitHub non è stato toccato.

**Cosa è stato provato** (`tests/test_publish.py`): `git add`, commit e
`push` veri; nessun file vietato nel remoto; i nomi veri assenti dal
**contenuto** di ogni file pubblicato; gli pseudonimi e i segmenti
presenti.

**Il buco che il test ha trovato.** Il controllo finale guardava solo
estensioni e nomi di file noti. Non guardava il contenuto: se un nome
fosse finito in un CSV, in una riga di `segments.jsonl` o in un file
lasciato da una versione precedente, sarebbe passato. Ora
`_guard_repo()` cerca i nomi reali dentro ogni file pubblicato, usando
l'elenco esatto delle etichette che hai assegnato tu — l'unica
informazione disponibile, che non produce falsi positivi. È l'ultima
rete, e c'è un test che le mette sotto le mani un nome di sfuggimento.

**Cosa resta.** La pubblicazione vera, quando ci sarà materiale vero da
pubblicare. Il rischio residuo è solo il contenuto: i formati sono gli
stessi che sono già stati provati, ma nessuno ha mai visto `INDEX.md`
con dentro le tue date.

---

### 8. Sync biometrico: mai costruito

**Stato.** Non esiste. È stato il motivo per cui `session_start_wall`
esiste, e quel pezzo **ora funziona** (prima scriveva sotto una chiave
inesistente e crashava).

**Pro.** È ciò che trasforma il corpus da testo in serie di misure nel
tempo, ed è l'unica cosa che non si può ricostruire dopo: le misure
biometriche vanno scaricate mentre esistono nel cloud.

**Contra.** Non ho il dispositivo né il token Zepp. E richiede di sapere
come Amazon/Zepp espongono i dati — è reverse engineering di un'API non
ufficiale, quindi fragile.

**Perché è in fondo all'elenco.** Il prerequisito (`session_start_wall`)
è a posto; il resto dipende da hardware che non ho.

**Io.** Lo scheletro e l'allineamento temporale, quando i dati
esistono. **Tu.** Il dispositivo e l'accesso all'app.

---

## Da valutare, nessuna urgenza

| # | Punto | Pro | Contra | Di chi |
|---|---|---|---|---|
| 10 | Soglia speaker a 0,78 | Più voci distinte subito | Merge sbagliati non distinguibili dopo | Io, dopo più materiale |
| 11 | Denoise su campione di 180 s | Costo notturno costante | Su un'ora la qualità può variare dentro il file | Io |
| 12 | `A2T_ASR_MODEL=medium` | ~2× più veloce | Qualità ASR peggiore | **Tu** |
| 13 | Archivio locale a 7 giorni | Rete di sicurezza | 7 GB occupati | Io |
| 14 | Segmenti da ~18 s per l'analisi | Prosodia misurabile per frase | Turni di parlato meno leggibili | Io |
| 15 | Misurare il termico vero (ventola, °C) | Il dato vero, non dedotto dal tempo | Serve `powermetrics` e qualche ora | Io, dopo una notte |

### Sul perché questi sei sono ancora aperti

Non sono rimasti indietro: sono in attesa di dati che non esistono
ancora, e ognuno ha il motivo per cui aspettare.

**10 — soglia 0,78.** ~~Non valutabile, c'era una sola voce.~~
Rivalutata l'8 ottobre, con quattordici voci e 621 coppie. **La soglia
non è spostabile**, e i numeri dicono perché.

Il caso che chiude la questione è `GLOBAL_004 × GLOBAL_018`: quattordici
confronti **fra le stesse due voci**, in sessioni diverse, che valgono da
**0,661 a 0,784**. La soglia 0,78 li divide 1 sopra e 13 sotto. Per
metterli tutti dalla stessa parte servirebbe una soglia fra 0,777 e
0,784: una finestra di **0,007**, più stretta della differenza che c'è fra
due registrazioni diverse della stessa coppia di voci. Lo stesso vale
per `GLOBAL_001 × GLOBAL_028` (0,688–0,848, 2 sopra e 8 sotto) e per
`GLOBAL_006 × GLOBAL_026` (0,614–0,786, 1 sopra e 4 sotto).

In altre parole: **la soglia non dà nemmeno una risposta coerente alla
stessa domanda.** Chiedi se due voci sono la stessa persona e la risposta
cambia a seconda della sessione che scegli di confrontare. Spostare il
numero non sistema niente, perché il problema non è il numero: è che un
coseno fra due embedding singoli non è una grandezza sufficiente.

La zona grigia è esplosa da 6 a **31 coppie**, quasi tutte concentrate su
poche coppie di voci (`GLOBAL_004 × GLOBAL_018` da sola ne ha 9).

**Una cosa che ho verificato e che non reggeva:** pensavo che le voci con
poco audio fossero quelle più incerte, e che bastasse registrare di piu'.
Misurato, non e' vero: l'escursione delle somiglianze vale **0,646** per
le voci sotto i 10 minuti e **0,629** per quelle sopra, rapporto 1,0x.
Quello che conta non e' la quantita' di audio ma il numero di confronti
fatti, che e' una cosa di campionamento. Registrare di piu' aiuta poco.

**Il ritrovamento che conta di piu'.** La matrice che guardi e il
confronto che il sistema fa **non sono la stessa operazione**. La matrice
confronta campione con campione; `SpeakerDB._best_match()` confronta
l'embedding della sessione **contro i centroidi** memorizzati. Per
`GLOBAL_028` la matrice dice 0,848 contro `GLOBAL_001` — ben sopra la
linea — mentre il sistema ne ha visto 0,734 e ha giustamente aperto una
voce nuova.

Il sistema e' coerente: non e' un bug. Ma il numero che vedi nel report
**non e' quello che il sistema ha usato**, e senza dirlo uno legge 0,848,
conclude che il sistema ha sbagliato, e magari corregge a mano un merge
che era giusto. Il report dovrebbe mostrare entrambi i numeri, o dire
qual è dei due quello che decide. Non l'ho cambiato: quale delle due
grandezze vuoi usare come riferimento e' una decisione tua.

**11 — campione denoise di 180 s.** Costa 45 s per file, ed è il prezzo
di non fare una seconda passata ASR su un'ora sola. Su un file la
qualità dell'audio non cambia di minuto in minuto: si sceglie una volta
sui primi tre minuti e si applica a tutto il resto. Se un giorno
dovesse risultare sbagliato — qualità buona all'inizio, fruscio
dall'ora 40 — il numero da cambiare è `DenoiseConfig.sample_sec`, e il
costo sale di conseguenza.

**12 — modello `medium`.** È una tua decisione e la risposta è già
nel conto: si risparmia circa la metà del tempo notturno al prezzo di
una qualità di riconoscimento peggiore. Su un corpus che serve per
analisi linguistica, quella qualità **è** il dato. Non lo cambiavo.

**13 — archivio a 7 giorni.** Ora pesa 35 MB con quattro file. Diventa
un problema con settimane di materiale, non prima. `A2T_KEEP_LOCAL=0`
lo disattiva.

**14 — segmenti da ~18 s.** Il chunker produce segmenti da 29 s
massimo per il clock Whisper, con chunk che coprono il tempo trascorso
e non la somma del parlato: un chunk da 29 secondi di clock con 18 di
parlato era il bug che produceva prosodia calata su due minuti e mezzo
di silenzio. Ridurli a 18 significherebbe più segmenti, meno efficienza e
turni di parlato meno leggibili. Il limite di 29 s è giusto: è il
vincolo del modello.

**15 — il termico vero.** `thermal_probe.py` esiste e funziona, ma su
questa macchina `ioreg` non espone la temperatura e `powermetrics` chiede
permessi root che non sono stati concessi. Quello che lo strumento fa
senza permessi è uso CPU e livello termico di macOS, e già segnala la
frequenza se dovesse leggerla. Manca il pezzo vero — watt e
frequenza — che si misura con `sudo powermetrics`. La stima su quattro
thread è pienamente supportata dai dati; il comportamento termico
proprio no, e non va dato per misurato.

---

## Scoperti elaborando i file veri, il 3 ottobre

### 15. ~~La prosodia in parallelo non terminava mai~~ — chiuso

**Stato.** Chiuso. Con il percorso parallelo l'intero WAV finiva
dentro ogni compito della coda di worker. Con lo start method "spawn"
ogni compito viene serializzato e spedito attraverso una pipe: un file
da un'ora voleva dire circa 29 GB di trasferimento per una sola
sessione. La prosodia non falliva e non scriveva errori, semplicemente
non tornava.

**Perché nessuno se ne accorgeva in tempo.** La soglia per andare in
parallelo è di 20 segmenti, e il ciclo end-to-end gira su file da
mezzo minuto con sei segmenti: il difetto toccava solo i file veri da
un'ora, quindi di notte, con la macchina già calda. Corretto, lo stesso
stadio passa da oltre dieci minuti bloccato a dieci secondi.

### 16. ~~Rifare la trascrizione faceva perdere gli interlocutori~~ — chiuso

**Stato.** Chiuso. Rifare il testo svuota i chunk ASR e li ricrea, ma i
turni di diarizzazione vivono nello stadio accanto, che risultava già
fatto e veniva saltato. I chunk nuovi nascevano quindi senza voce.

**Il sintomo è il peggiore possibile.** Una sessione intera etichettata
`UNKNOWN`, senza che nulla fallisse e con tutti i file scritti: la
sessione risultava perfettamente riuscita e inutilizzabile. Trovato
guardando i quattro file veri, uno dei quali aveva 126 segmenti e nessun
interlocutore.

**Il resto è a posto, per una volta.** Nomi coerenti dentro la stessa
trascrizione (Gianlu sei volte, Schumacher cinque), 6.130 parole nel
primo file, flag di qualità distribuiti su ok/low/unreliable.

---

### 17. ~~La cache dei WAV cresceva di due gigabyte a notte~~ — chiuso

**Stato.** Chiuso. Due difetti insieme. Il VAD riconverteva in un file
identico tutto cio' che era gia' 16 kHz mono, quindi 115 MB e qualche
decina di secondi di CPU per un'ora di audio; e la copia che ne nasceva
non era citata da nessun checkpoint, finiva nel ramo degli orfani e
aspettava sei ore, che a quel punto non erano sei ore ma spazzatura
stabile. Verificato sui quattro file veri: 401,9 MB rimasti in cache.

**Perche' nessuno se ne accorgeva.** La cache sta fuori dalla vista di
chi lavora, e il disco si riempie piano. Un disco pieno, di notte, fa
fermare la coda a meta' senza che nessuno capisca perche'.

### 18. ~~Il database locale era vuoto~~ — chiuso

**Stato.** Chiuso. A riempire il database era solo `sync_device pull`,
cioe' il percorso del registratore. Ogni sessione elaborata a mano finiva
nell'output e da li' nel nulla: quattro ore di registrazione e
sediciottomila parole, zero righe nel database.

**La parte peggiore e' un'altra.** La parte interrogativa del progetto,
quella che dovrebbe servire a chiedere qualcosa al proprio corpus, non
poteva essere provata perche' dentro non c'era niente. Con `reindex` si
ripara senza rielaborare nulla.

### 19. ~~La sovrasegmentazione delle voci~~ — chiuso

**Stato.** Chiuso. Quattro ore di una stessa conversazione erano finite
in ventuno voci globali, undici delle quali parlavano meno di novanta
secondi. Non era un errore di programma: la pipeline era completa, i
file scritti, nessuna eccezione. Ventuno voci pero' non erano ventuno
persone, e un corpus con ventuno voci su una conversazione a tavolo non
e' interrogabile per interlocutore.

**La chiave e' stata capire dove si perde, e l'ordine conta piu' della
soglia.** Il cluster debole si scioglie nel piu' simile prima che le
identita' globali vengano assegnate: **21 voci diventano 9**, e tutte e
nove parlano almeno 160 secondi. La stessa fusione applicata *dopo* non
cambia niente, perche' il DB delle voci ha gia' dato un'identita' a ogni
frammento e non torna mai indietro a riconoscere che aveva contato due
volte la stessa voce.

**Le due soglie rispondono a domande diverse.** `match_threshold` (0,78)
chiede «e' la stessa persona fra sessioni diverse?» e resta dove e'.
`merge_threshold` (0,45) chiede «questa voce e' troppo piccola per essere
qualcuno?», che e' facile: nessuno che parli tredici secondi in una
conversazione lunga e' un interlocutore. La soglia 0,45 non e' un
numero scelto a caso: ogni soglia fra 0,30 e 0,45 dà lo stesso risultato
sui quattro file veri, e 0,45 e' la piu' alta del pianoro, cioe' la piu'
conservatrice che ancora cattura tutta la fusione utile.

**La regola che tiene la cosa sicura.** Due voci grandi non si fondono
mai, a nessuna somiglianza. Se la fusione sbaglia, sbaglia solo sui
frammenti, che sono rumore comunque. Unire due persone vere sarebbe
stato l'errore che costa di piu', perche' il DB delle voci non torna
indietro.

**Tre difetti che sono usciti insieme.** Rietichettare le sessioni vecchie
richiedeva che il DB delle voci potesse *dimenticare* una sessione
(`forget_session`), altrimenti il frammento ritrovava subito l'identita'
che si era creato. La tabella dei parlanti in `corpus.db` non potava mai
le voci assorbite, e `sync_speaker_names` non sapeva cancellare un nome
che la fonte non ha piu': ci si era un «Pietro» che nessuno sapeva piu'
da dove venisse.

**Il seguito, trovato riguardando il lavoro di ieri.** Il comando che
rifonde le sessioni vecchie non era idempotente: rieseguirlo assegnava
numeri nuovi alle stesse persone e rietichettava l'intero corpus. Non
era un errore di qualita' ma di stabilita', ed e' la proprieta' che
qualunque comando di riparazione deve avere — se il giorno dopo lo
riesegui per un motivo qualsiasi, tutte le sessioni passate cambiano
interlocutore e nessuno sa perche'. Due cause, entrambe vere:

  - `review_speakers.py consolidate` dimenticava e ricalcolava ogni
    identita' da capo. Ora se un cluster conserva la sua etichetta e il
    suo embedding somiglia abbastanza al centroide gia' salvato, lo
    riappiglia alla stessa voce e non tocca niente. Solo quando la
    fusione ha cambiato una voce cosi' tanto che non e' piu' la stessa
    cerca davvero un'identita' nuova.
  - `_next_id` contava le voci con `len()`. Dopo una fusione il
    conteggio calava e la voce successiva prendeva un numero gia'
    stato di qualcun altro. Ora conta il massimo mai usato: gli ID non
    si riusano e i buchi sono il prezzo. Un buco e' innocuo; un ID
    riusato e' silenziosamente falso.

Una terza cosa e' venuta fuori strada e merita un nome a se': riappare
anche in `forget_session`, che lasciava nel DB le voci gia' vuote
perche' il suo `continue` le saltava. Il sintomo sarebbe stato
`review_speakers.py list` che mostra per sempre una persona con zero
minuti, senza che nessuna sessione la generi piu'.

**Verificato sui dati veri.** 28 cluster locali in 16, 21 voci globali
in 9; `consolidate` rieseguito due volte d'a lo stesso identico
risultato. Le quattro sessioni hanno 4/5/6/3 voci dove prima erano
5/8/11/3. Il corpus pubblicato e' stato ricalcolato e ripubblicato.
23 test sulla fusione, 130 in tutto su 9 suite.

---

### 20. ~~Le parole sbagliate~~ — chiuso

**Stato.** Chiuso. Su quattro ore di conversazione Whisper sbagliava
parole in modo sistematico: «Savot», «stegnavano a telefono», «matiala
vera», «Botanic». Non a caso — sono fonemi scambiati e parole dialettali
rese in italiano, errori che si riconoscono dal contesto e che un
modello di lingua corregge, mentre nessun modello acustico li corregge:
l'informazione che manca non e' nel suono, e' che «maiala vera» e' una
frase che esiste.

**La regola che tiene la cosa onesta e' una sola: il numero di parole non
puo' cambiare.** Il modello puo' correggere, riscrivere, riorganizzare,
ma non aggiungere o togliere parole. Non e' pedanteria: una riscrittura
produce un testo che sembra *piu' buono* e che non e' piu' quello detto,
e i due sono indistinguibili a chi legge dopo. In un corpus che vuole
misurare la propria voce un testo inventato e' peggio di un testo
sbagliato, perche' lo sbagliato almeno si riconosce. Percio' l'elenco
delle correzioni non puo' eccedere la lista delle parole, e una risposta
che punta fuori dal testo viene scartata invece che applicata — anche
perche' un modello che ha diviso diversamente il testo produce due
elenchi uguali ma allineati in modo diverso, e applicare comunque
sposterebbe ogni parola di un segmento su quella del successivo.

**Perche' affiancato e non sostitutivo.** Ogni parola conserva originale,
correzione e se e' cambiata. Senza il confronto uno dei due errori
sparisce e non si sa quale; con entrambi si ha la misura vera della
qualita' della trascrizione — quanto sbaglia Whisper e quanto sbaglia il
correttore.

**Perche' `--consent`.** Ogni segmento mandate a un'API porta fuori dal
portatile il testo di una conversazione personale. Tutto il resto della
pipeline e' costruito perché i dati non escano; mandare il testo a un
servizio esterno e' una scelta diversa e non la prende uno script per
abitudine. Senza `--consent` il comando mostra cosa farebbe e si ferma.

**Tre difetti incontrati strada facendo, tutti veri.** Il flag per-parola
diceva «cambiata» anche quando il testo non era cambiato, perche' la
punteggiatura veniva gestita due volte: lo strumento di misura mentiva
proprio nel caso che non accaderebbe mai, il meno interessante. E se il
modello ripeteva la punteggiatura, «sera.» diventava «sera..». Poi una
risposta JSON valida ma fatta di lista faceva fallire il chiamante su
un `.get` che su una lista non esiste — crash, non scarto.

**Il seguito, che mancava ed era il punto vero.** Il correttore scriveva
il file e **nessuno lo leggeva**: il corpus continuava a mangiare il
testo grezzo, e su GitHub si sarebbe continuato a leggere proprio il
testo impreciso che si voleva correggere. Correzione senza consumatore
e' un file che nessuno apre.

Ora il testo corretto finisce in tre posti, tutti con l'originale
accanto:

  - **`segments.text`** riceve il testo da analizzare, `segments.text_raw`
    tiene l'originale, `n_words_changed` dice quanto ha lavorato il
    correttore. Token, conteggio delle parole e bigrami si ricostruiscono
    sul testo corretto.
  - **`transcript.corrected.txt` / `.srt` / `segments.corrected.jsonl`**
    finiscono nella repo privata. `transcript.txt` non e' mai
    sovrascritto.
  - **`correction_stats()`** dice quanto materiale e' davvero corretto,
    perche' un corpus al 12% e' un corpus su cui il conteggio delle
    parole continua a sbagliare.

**Il regalo che fa la regola piu' stringente.** Se il numero di parole
non puo' cambiare, l'i-esima parola del testo corretto e' l'i-esima
parola che l'ASR ha collocato nel tempo: i timestamp restano giusti
anche sul testo corretto. Il costo dell'invarianza — non poter
riscrivere — si paga qui come un vantaggio che, altrimenti, non si
avrebbe.

**Due difetti di cui uno e' sparso.** Il filtro sugli scarti stava solo
nel lettore del file, non in `ingest_session`: chi passava il dizionario
a mano applicava una risposta che il correttore aveva dichiarato
inaffidabile. La garanzia deve valere per chiunque passi le
correzioni, quindi e' stata spostata dentro. E la migrazione del
database aggiungeva `text_raw` ma non lo riempiva: le righe gia'
scritte avrebbero avuto l'originale a NULL, e `suspect_text` — che serve
a rivedere a mano i segmenti sospetti — avrebbe restituito una colonna
vuota proprio li'.

**Verificato.** 18 test sulla correzione, 9 sulla matrice delle voci, 11
nuovi sull'ingestione, 172 in tutto su 11 suite. Le varianti
pubblicabili provate su una sessione vera in copia: `transcript.txt`
intatto, `transcript.corrected.txt` corretto. La migrazione provata sul
database reale in copia: 429 righe, nessuna senza originale, riaprendo
non cambia niente.

---

### 21. ~~Le voci viste una sessione alla volta~~ — chiuso

**Stato.** Chiuso. Ogni file dice quante voci ha, ma nessuno dice **dove
hai incontrato ogni persona**: l'informazione era sparsa in quattro
sessioni e nessuno la metteva insieme. Il raggruppamento trasversale
esiste (`review_speakers.py voices`) e mostra ogni voce con le sessioni in
cui compare e i secondi totali.

**Perche' conta piu' di quanto sembri.** Una persona incontrata in tre
giornate diverse e' un interlocutore. Senza questa vista il numero delle
persone che hai incontrato e' sbagliato per costruzione, ed e' lo stesso
difetto della sovrasegmentazione, una scala piu' piccola.

**Il dato che chiude il punto 10.** Su 16 campioni e 109 coppie nessuna e'
sopra 0,78, e sei sono entro 0,06 dalla linea. La soglia non ha mai unito
niente per errore — ma nemmeno mai unito niente per giusto. Finche' non
arrivano voci che si somigliano davvero, 0,78 resta una scelta prudente
piu' che una soglia tarata, e le sei coppie in zona grigia sono la misura
reale di quanto quella prudenza stia aspettando.

**Una coppia che non si confronta con se stessa.** Il confronto ha senso
solo fra due osservazioni diverse: se lo stesso campione entrasse due
volte avrebbe somiglianza 1.0 e finirebbe dritto sopra la soglia, e la
matrice direbbe «questa persona e' sicuramente qualcun altro» guardandola
allo specchio.

**Verificato.** 9 test sulla matrice. `voices` sui dati veri: 9 voci,
16 campioni, 109 coppie, 6 in zona grigia.

### 22. ~~Il correttore: cosa fa davvero~~ — chiuso, con una riserva

**Stato.** Chiuso sul lato meccanico, con una riserta aperta su quello
che non si puo' delegare a un modello. Provato con una chiave vera su
segmenti veri, non più con una chiave finta: e li' sono usciti tre
difetti che i test offline non potevano vedere.

**Le virgole finali facevano perdere il lavoro gia' pagato.** Il modello
rispondeva JSON correttissimo ma con una virgola prima di ogni `}`, e
`json.loads` lo rifiuta: il segmento finiva tra gli scarti **con la
correzione dentro**. Quattro chiamate buttate e una voce che l'analisi
non avrebbe mai visto. La correzione e' sicura per costruzione — una
virgola prima di `}` o `]` non e' mai JSON valido, quindi toglierla non
puo' cambiare il significato di un JSON che era valido.

**Il backoff era troppo corto per gli errori che importano.** Mezzo
secondo, poi uno: è la scala giusta per un errore di sintassi, non per
un `503 UNAVAILABLE` o un `429`. Riprovare cosi' serve solo a farsi
respingere di nuovo consumando quota. Ora un 503 aspetta almeno
quindici secondi, un 429 trenta, e se il server ha scritto quanto
aspettare si ascolta lui. Il batch seriale non è il problema: fa poche
chiamate al secondo.

**Il modello predefinito non rispondeva.** `3.8-flash` ha dato 503 a
ogni tentativo; `3.5-flash-lite` ha risposto regolarmente. Il default
deve essere il modello che funziona, non quello che sarebbe migliore.

**La riserva, che è la cosa che conta.** Il correttore riscrive le
parole dialettali. Su `Cominciatemi ragazzi, siamo drastisovati` ha
proposto «Camminate» e poi, a una seconda esecuzione, «Diamoci» —
nessuna delle due è piu' difendibile dell'originale. Una regola esplicita
nel prompt («una parola pronunciabile che sembra storta e' quasi
certamente quello che è stato detto») non l'ha fermato: è una
limitazione del modello, non un difetto di istruzione.

Il danno è limitato e per costruzione: il numero di parole non cambia,
l'originale resta in `text_raw` e ogni parola affiancata nel file.

**La riserva è stata chiusa, e il difetto era diverso da come l'avevo
descritto.** Non è che la probabilità per parola non si salvasse:
Whisper la calcola, sta nei timestamp di parola col nome `prob`, e
finiva nel checkpoint. Il difetto era che da lì non usciva più — chi
correggere il testo leggeva `segments.jsonl`, che i timestamp li omette
di proposito, e la probabilità si perdeva sul pavimento.

Ora la probabilità arriva dove serve:

- `tokens.jsonl` ha `asr_prob` su ogni parola (16.865 righe sulle
  quattro sessioni, rigenerate senza riascoltare l'audio);
- `allinea_probabilita` la riporta alla divisione di `text.split()`,
  che non coincide: Whisper spezza «C'è» in «C» e «'è». Un confronto
  per indice allineerebbe il 54% delle parole, una camminata che
  consuma le voci finché non fanno la parola ne allinea il 100%;
- `--soglia-prob` (default 0.90) impedisce di correggere le parole che
  Whisper aveva udito con sicurezza, e ogni blocco resta nel file con
  la probabilità accanto.

Su 16.865 parole reali la soglia 0.90 protegge 9.063, il 53.7%, e le
tre parole che il correttore aveva corrette bene — `drastisovati`
(0.41), `monopolito` (0.70), `steam` (0.14) — stanno tutte sotto.

**Quello che il filtro non risolve**, e va detto con chiarezza:
`Cominciatemi` ha probabilità 0.63 e resta sotto la soglia, quindi quel
caso non è stato il filtro a salvarlo ma la decisione di togliere
del tutto le parole già certe. Il filtro non distingue un dialettalismo
da un errore acustico: riduce il numero di occasioni in cui il modello
di lingua riscrive il dialettalismo, non le elimina. Le quattro
sessioni si possono girare, ma il giudizio finale resta tuo.

Quel che funziona, e va detto: `drastisovati` → «disastrati»,
`monopolito` → «monopolio», `steam` → «stesso». Sono correzioni che
nessun modello acustico avrebbe fatto. Solo che non sono distinguibili,
a occhio, dalle invenzioni.

**Verificato.** Temperatura 0, perché due passate sullo stesso testo
davano risultati diversi e una correzione che cambia da una passata
all'altra non è una correzione: ora è riproducibile.

La suite della correzione **dormiva davvero**: due test aspettavano
l'attesa del backoff invece di verificarla, e ci restavano dentro
165 secondi — la suite durava quasi tre minuti, dei quali 164 erano un
test che guardava l'orologio di parete. Il sonno non verificava niente
e costava tutto: un backoff che aspettasse trenta secondi invece di
venti sarebbe passato in egual modo. Adesso l'attesa è registrata e
confrontata — `[30, 15]` per un rate limit e un sovraccarico — e la
suite dura 0,2 secondi.

Aggiunto anche quello che mancava del tutto: **il percorso completo
del comando**. Fino ad ora era provato solo il modulo, ma gira
`correct_text.py`, che ha incroci suoi — i giri precedenti da non
perdere, il `--limit` che interrompe a metà, i file pubblicabili da
riscrivere. Sette test lo eseguono davvero in una cartella a caso,
con un modello finto e l'orologio tarato, e verificano anche che il
filtro arrivi fino al testo scritto su disco e non si fermi al
riepilogo, e che il report cambi quello che si vede senza cambiare
quello che si scrive.

**42 test** sulla correzione (4 sul percorso completo, 2 sul
backoff, 6 sul report `--solo-proposte`), 2 sulla scrittura di
`asr_prob`, **212 in tutto su 11 suite**.

La prima passata asciutta con chiave vera ha mostrato una cosa che i
test non potevano: su quattro segmenti il modello ha proposto quattro
parole e due erano buone, una dubbia e una peggio dell'originale
(`similiata` → `sibilata`, un nonsense sostituito con un altro
nonsense). Nessun numero automatico lo distingue da
`drastisovati` → `distratti`, che e' la correzione giusta: la somiglianza
fra le due parole dice 0.48 e 0.82, cioe' il contrario, e la parola
giusta non compare mai nel vocabolario delle quattro notti. Quindi la
soglia si continua a tarare a occhio, e con `--solo-proposte` si fa
sulle righe, non sulle pagine.

---

### 23. ~~Le parole contate due volte~~ — chiuso il 10 ottobre

**Stato.** Chiuso. Trovato guardando i numeri del corpus vero, non da un
test: il database dichiarava **12 sessioni per 11 cartelle**.

**Il difetto.** `ingest_session` cancella le righe del proprio stem e
riescrive: e' idempotente finche' lo stem non *cambia*. Lo stem viene da
`transcript.json`, e quando il fix del file troncato (punto 1) ha tolto
l'hash dal nome della copia di lavoro, quello e' passato da
`2026-10-04_10-49-40-1508d6ee` a `2026-10-04_10-49-40`. Le righe del nome
vecchio non sono state cancellate da nessuna parte: non per una
dimenticanza dell'ingest, ma perche' nessuno le cerca piu'. La
sessione vecchia e' sparita dalla tabella `sessions` — non perche' qualcosa
l'avesse rimossa, ma perche' nella tabella finisce solo cio' che si
ingesta, e cio' col nome nuovo. Restavano **160 token, 116 wordfreq e
149 bigrams** che duplicavano parola per parola un testo gia' presente:
erano un sottoinsieme esatto dei 170 token della sessione buona.

**Perche' nessuno se ne accorgesse.** La riga `sessions` col nome vecchio
era ancora li', quindi le foreign key erano soddisfatte e
`pragma integrity_check` rispondeva `ok`. Il vincolo c'era — `tokens`,
`wordfreq` e `bigrams` lo dichiarano tutti — ma un vincolo fra due tabelle
che sono *entrambe* sbagliate non ha niente da dire. Il danno non e' un
errore di scrittura: e' che `a` pesava 5 volte su 531 e `e` 2 su 1.364, e
una frequenza sbagliata in un corpus che vuoi interrogare e' peggio di un
database rotto, perche' risponde.

**Il rimedio.** `prune_missing_sessions(output_dir)`: confronta gli stem
nella tabella con le cartelle che hanno un `transcript.json` e cancella
le tabelle figlie di quelli che non trovano niente. Agganciato a
`publish_corpus.py reindex`, accanto alla potatura delle voci che gia'
esisteva.

**La regola che tiene la cosa sicura.** Una cartella senza
`transcript.json` non conta come assente: e' una sessione non finita, e
quella la si riprende, non la si dichiara inesistente. E una directory
`output/` vuota o inesistente non fa potare niente: un percorso sbagliato
non deve azzerare l'indice — e' un errore che si vede subito ma che si fa
male prima di accorgersene. Senza sessioni da cui misurare, la potatura
non guarda niente.

Cancellate esplicitamente tutte e quattro le tabelle figlie invece di
fidarsi di `ON DELETE CASCADE`: funziona, ma sui database creati prima che
il vincolo ci fosse no, e il risultato di una potatura non deve dipendere
dallo schema che si trova sul disco.

**Verificato sui dati veri.** 12 → 11 sessioni, `-160` token, `-116`
wordfreq, `-149` bigrams, `-0` segmenti (i segmenti del nome vecchio non
c'erano gia': erano stati sostituiti da quelli del nome nuovo). Le 11
sessioni valide sono risultate **identiche parola per parola** a prima del
purge, confronto fatto riga per riga sui 41.782 token e sul wordfreq
completo. 2 test nuovi, **212 in tutto su 11 suite**.

Il primo dei due test e' stato verificato rotto: con la chiamata alla
potatura disattivata fallisce con «la sessione col nome vecchio
2026-10-02_21-44-16 e' ancora in sessions». Il secondo copre il caso
opposto — una `output/` vuota non deve cancellare niente — perche' una
potatura che non distingue «non c'e' niente» da «non so dove guardare» e'
peggio della duplicazione che corregge.

---

### 24. ~~`tokens.jsonl` non era mai stato pubblicato~~ — chiuso il 10 ottobre

**Stato.** Chiuso. Le trascrizioni del 4 ottobre erano trascritte,
indicizzate e pubblicate: quello che mancava era un file per sessione.

**Il buco.** `tokens.jsonl` — una parola per riga con timestamp
proprio — era dichiarato nell'`INDEX.md` fra i formati pubblicati e non
era nella lista `PUBLISHABLE` di chi va copiato. Non mancava per un
errore di copia: non era mai stato pubblicato, e nessuno se ne accorse
perche' l'indice diceva che quel file ci fosse, il push usciva 0 e la
notte passava. Il file che rende il corpus interrogabile parola per parola
— KWIC, n-grammi, collocazione, sincronizzazione con dati biometrici al
secondo — era il documento di riferimento e insieme l'unico assente.
Aggiunto anche `speaker_merge.json`, che mappa i cluster locali sulle
voci globali: senza, dalla repo non si capisce come due frammenti della
stessa persona sono diventati una voce sola.

**Perche' la pubblicazione non era sistematica.** Lo era, in un punto:
`nightly.py` chiama `publish_corpus.py push` a fine ciclo. Ma il
codice di uscita del push dice solo che il comando e' finito, non che
tutto sia arrivato — un file assente dall'elenco esce 0 come uno
presente. Ora, dopo il push, `_sessioni_non_pubblicate()` confronta
`output/` con `corpus_repo/sessions/` e **dice per nome** quello che manca,
sessione per sessione. Non fallisce la notte: un file in meno non e' un
motivo per buttare quattro ore di elaborazione, ma non passa neppure in
silenzio.

Il confronto si ferma ai file che vanno pubblicati: un file che non e'
nell'elenco non e' un buco, altrimenti il controllo urlerebbe sempre e
diventerebbe rumore che nessuno legge. E una sessione senza
`transcript.json` non si controlla, perche' e' non finita.

**Verificato.** Il test e' stato verificato rotto con il controllo
disattivato: fallisce con «il file mancante deve essere detto per nome,
risulta {}». Sul disco, dopo il push, tutte e 11 le sessioni hanno i 12
file, `tokens.jsonl` compreso, e il controllo notturno non segnala
nulla. **213 test su 11 suite** (poi 215 con la matrice delle voci, punto 25).

**Una cosa trovata di sfuggita.** Nella repo c'e' `2026-10-02_17-02-36`
che non esiste ne' in `output/` ne' nel database: una delle prime due
sessioni di prova del 2 ottobre, con dieci file e nessuna fonte. Non
l'ho toccata perche' rimuovere da una repo pubblicata non si fa senza
decidere: e' un pezzo di storia, e la domanda («la sessione che non ha
piu' una fonte la si tiene o la si lascia nel corpus pubblico?») non ha
una risposta che si possa scegliere al posto dell'utente.

---

### 25. ~~La matrice delle voci non era mai stata pubblicata~~ — chiuso il 10 ottobre

**Stato.** Chiuso. Maniottava anche questo, ed è il pezzo che dice *chi* ha
parlato.

**Il buco.** La matrice si generava solo con `review_speakers.py voices
--json`, e quel `--json` non compariva in nessuna parte della corsa
notturna. Il corpus pubblicato aveva i minuti per voce — `session.json` li
ha — ma non il numero che mette due voci vicine: quanto somigliano, e
soprattutto quali coppie la soglia non riesce a decidere. **34 coppie in
zona grigia**, il buco aperto da chiudere con una decisione, invisibile
perche' nessun file della repo le conteneva.

**La forma giusta.** Va pubblicata perche' è l'unica cosa che distingue
«ha parlato qualcuno» da «chi era». `VoiceReport.to_dict()` mette fuori
pseudonimo, sessione, secondi e somiglianza: **nessun embedding**. Un
embedding vocale è un'impronta biometrica e questa repo non ne tiene, e la
scelta va tenuta verificata, non dichiarata: il test legge il JSON
pubblicato e controlla che non ci sia né la chiave `embedding` né ID locali
di pyannote, oltre a verificare che ogni lista contenga solo minuti e
somiglianze.

Ora si genera a ogni `push` e finisce in `voices/voice_matrix.json`, con la
riga nell'INDEX che la dichiara come formato.

**Un secondo strato di controllo, perche' il primo non la vedeva.** Il
confronto file per file del punto 24 cammina dentro `sessions/<nome>/`, e
la matrice non sta in nessuna cartella di sessione: è un file solo per tutto
il corpus. Per costruzione quel controllo non può accorgersene. Per questo
`ARTEFATTI_CORPUS` elenca i file che stanno a livello di corpus e hanno
bisogno di un controllo loro — oggi solo la matrice, domani quello che
verrà. Il test lo dimostra: tolta la matrice, `_sessioni_non_pubblicate()`
non segnala niente e `_artefatti_mancanti()` dice
`['voices/voice_matrix.json']`.

**Verificato.** Sul disco: 14 voci, 38 campioni, 621 coppie, 34 in zona
grigia; nessun embedding, nessun ID locale, nessun nome reale nel JSON
pubblicato. Il file è pushato (`9b08ef3`). I due test nuovi sono stati
verificati rotti: con la generazione disattivata «la matrice delle voci non
e' stata pubblicata in .../voices/voice_matrix.json», con il controllo
artefatti disattivato «la matrice mancante deve essere detta per nome».
**215 test su 11 suite.**

---

## L'ordine in cui li farei

Fatti: **1** (prima notte vera), **2** (cache WAV), **3** (finestra
unica), **4** (nomi), **5** (punteggiatura), **6** (flag di qualità,
tarati), **9** (pubblicazione), e il carico termico.

1. ~~**1 — la prima notte vera.**~~ Fatta il 4 ottobre su un
   registratore USB vero: 6 file su 7 passati per la catena intera, e il
   settimo ha fatto trovare il difetto del file troncato.
2. ~~**6 — taratura dei flag.**~~ Fatta l'8 ottobre su 1.060 segmenti:
   `unreliable` al 4,9%, sotto il 5%, e i timestamp per parola hanno
   mostrato che i segmenti segnalati sono davvero peggiori
   (probabilità mediana 0,636 contro 0,960). Le soglie **non** si alzano.
3. **10 — la soglia**, rivalutata l'8 ottobre con 14 voci e 621 coppie.
   Il numero non è spostabile: le stesse due voci si somigliano da 0,661
   a 0,784 a seconda della sessione. La domanda aperta non è più il
   numero ma **quale grandezza vuoi come riferimento**: la matrice
   confronta campione con campione, il sistema confronta embedding contro
   centroidi, e il report mostra solo il primo dei due.
4. **15 — il termico**, con `powermetrics` e una notte di misura.
5. **8 — biometria.** Quando arriva l'hardware.

Aggiunto dopo: la stima di costo della coda non può prevedere la
densità di parlato, e non è una cosa che si può correggere prima di
trascrivere. Va almeno detto dove la coda va a finire quando i file non
sono tutti uguali.

Da fare prima dell'analisi sul testo: girare `correct_text.py` sulle
quattro sessioni. Tutto quello che segue — conteggio delle parole,
sentiment, sintesi — va fatto sul testo corretto, perche' sul testo
grezzo il conteggio delle parole sbaglia.

Il resto può aspettare che il sistema abbia girato qualche notte e
accumulato dati su cui decidere.