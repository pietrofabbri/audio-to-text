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

### 1. Nessuna notte reale è mai girata end-to-end

**Stato.** Tutto è stato provato con device finto e con estratti reali,
ma mai con un `pull` notturno vero su 18 file da un'ora.

**Pro di farlo subito.** È l'unico test che manca, e vale più di tutti
gli altri messi insieme: i bug trovati finora (stem incoerente,
`cpu_threads=None`, cache sul registratore) erano tutti di integrazione,
e sono emersi solo quando i pezzi sono stati collegati.

**Contra.** Costa una notte di registrazione, o un pomeriggio se si
simula con i tuoi file.

**Perché ora.** Il rischio non è che fallisca: è che fallisca *di
notte*, senza nessuno che guardi i log.

**Io.** Prepara il comando e una procedura di verifica in tre righe.
**Tu.** Lo lanci e mi dici l'esito.

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

**Due query che lo rendono usabile** (`CorpusDB.quality_report()` e
`suspect_text()`). Senza, il flag sarebbe una colonna che nessuno
guarda: la domanda utile non è "quanti segmenti sono brutti" ma "posso
analizzare questa sessione", e il riassunto è pesato sulle **parole**,
non sui segmenti.

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

**10 — soglia 0,78.** C'è **una sola voce** in `speakers_db.json`. Con
una voce la soglia non è valutabile: nessuna coppia da confrontare. Il
giudizio su una soglia si fa sulle coppie certe e su quelle incerte, e
quelle arriveranno dalla seconda persona che parla nel registratore.
`review_speakers.py list` stampa la matrice quando è il momento.

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

**Verificato sui dati veri.** 28 cluster locali in 16, 21 voci globali
in 9. Le quattro sessioni hanno 4/5/6/3 voci dove prima erano
5/8/11/3. Il corpus pubblicato e' stato ricalcolato e ripubblicato.

---

## L'ordine in cui li farei

Fatti: **2** (cache WAV), **3** (finestra unica), **4** (nomi),
**5** (punteggiatura), **6** (flag di qualità), **9** (pubblicazione),
e il carico termico.

1. **1 — la prima notte vera.** Con tutto il resto fatto è l'unica
   verifica che manca, e non è più un rischio teorico.
2. **6, in verifica** — guardare i primi flag di qualità prodotti da
   una notte vera. Se `unreliable` è sotto il 5% il flag è tarato
   bene; se è sopra il 30%, le soglie vanno alzate prima che il flag
   diventi rumore, che è il suo unico modo di morire.
3. **10 — la soglia**, quando ci sarà la seconda voce.
4. **15 — il termico**, con `powermetrics` e una notte di misura.
5. **8 — biometria.** Quando arriva l'hardware.

Il resto può aspettare che il sistema abbia girato qualche notte e
accumulato dati su cui decidere.