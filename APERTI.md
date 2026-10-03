# Punti aperti

Stato al 3 ottobre 2026. Ogni punto dice **cosa manca**, **pro e
contro**, **perché** e **di chi è la decisione**. La responsabilità è
dichiarata perché la cosa peggiore di un elenco di cose aperte è non
sapere quale aspettare e quale fare.

Convenzione: **Io** = lavoro di codice che posso fare subito.
**Tu** = serve il registratore, una decisione tua, o un dispositivo che
non ho ancora.

I punti **2** (cache WAV), **3** (finestra notturna) e **4** (nomi dei
parlanti) sono **chiusi**, e c'è un punto nuovo sul **carico termico**.
Il punto **7** (saturazione del registratore) è **archiviato**: non è
risolto, è che non è risolvibile da qui. Sotto ciascuno è scritto cosa
è stato fatto e cosa resta.

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

**Cosa non fa.** Non rilegge l'audio e non rielabora: il merge delle
identità resta valido solo per le sessioni future. Rietichettare le
sessioni passate quando due voci vengono unite è un lavoro separato,
e non l'ho fatto.

---

### 5. Il denoise misura quantità, non qualità della punteggiatura

**Stato.** Le metriche del confronto (confidenza ASR, rapporto di
parlato, ritmo, segmenti ripetuti) sono tutte quantità. La
punteggiatura non è misurata.

**Pro.** Su una registrazione reale la variante ripulita produceva
punteggiatura migliore ("che è il modo realistico" contro "che è il
modo"), e le metriche non se ne accorgono: sceglievano l'originale.

**Contra.** Rilevare la punteggiatura è fragile — un errore di
riconoscimento su un punto è indistinguibile da un refuso reale, e una
metrica sbagliata in questo caso *sceglie il peggio*.

**Perché.** Se il corpus serve anche per analisi linguistica, la
punteggiatura è dato, non rumore.

**Io.** Posso misurarla, ma solo come **segnale debole** che non
sceglie da solo: vale come tie-breaker quando le altre metriche sono
pareggio. **Tu.** Se la punteggiatura conta più della velocità, questo
diventa il punto numero due.

---

### 6. Nessun segnale che dica "qui la trascrizione è rotta"

**Stato.** In un estratto reale, i primi 40 secondi erano audio non
intellegibile: il modello ci ha messo dentro testo plausibile e
sbagliato. Niente nel corpus dice "qui non c'era parlato".

**Pro.** Un flag `transcription_quality` per segmento (loop, prob
media bassa, parole per secondo implausibile) ti fa risparmiare il
lavoro di notte quando guardi i risultati, e ti avvisa prima di
costruirci sopra analisi.

**Contra.** Il rischio è fare di più la qualità media: un flag che
scatta troppo spesso viene ignorato, e a quel punto è rumore.

**Perché.** Meglio sapere che il 10% del corpus è illeggibile che
scoprirlo mesi dopo, dopo averci costruito sopra un'analisi.

**Io.** Il calcolo e la colonna. **Tu.** Quanto è accettabile che il
corpus contenga buchi.

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
che conosci già.

---

## Funzionalità — bloccate da cose che non ho

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

### 9. Il corpus non è mai stato pubblicato con dati veri

**Stato.** `publish_corpus.py` funziona ed è stato provato a secco, ma
mai su una coda vera di 18 file.

**Pro.** Verifica l'ultimo anello: privacy (che niente audio o nomi
veri escano), e che quello che finisce sulla repo sia interrogabile.

**Contra.** Nessuno, se non lo fai prima di avere mesi di materiale:
meglio scoprire un problema di privacy fra una settimana che fra un anno.

**Perché è urgente più di quanto sembri.** È l'unico punto in cui un
errore è irreversibile: dati pubblicati per errore non si richiamano.

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

---

## L'ordine in cui li farei

Fatti: **2** (cache WAV), **3** (finestra unica), **4** (nomi), e il
punto sul termico.

1. **1 — la prima notte vera.** Con 2–4 fatti è un test vero, e non è
   più un rischio teorico: è l'unica verifica che manca.
2. **6 — flag di qualità.** Prima di costruirci analisi sopra.
3. **9 — pubblicazione.** Prima che il corpus sia grosso, e prima che ci
   sia materiale che non vorresti vedere sulla repo.
4. **15 — misurare il termico vero.** Dopo una notte.
5. **8 — biometria.** Quando arriva l'hardware.

Il resto può aspettare che il sistema abbia girato qualche notte e
accumulato dati su cui decidere.