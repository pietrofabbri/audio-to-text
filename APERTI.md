# Punti aperti

Stato al 3 ottobre 2026. Ogni punto dice **cosa manca**, **pro e
contro**, **perché** e **di chi è la decisione**. La responsabilità è
dichiarata perché la cosa peggiore di un elenco di cose aperte è non
sapere quale aspettare e quale fare.

Convenzione: **Io** = lavoro di codice che posso fare subito.
**Tu** = serve il registratore, una decisione tua, o un dispositivo che
non ho ancora.

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

### 2. La cache WAV non viene mai cancellata

**Stato.** I WAV derivati finiscono in `data/wav_cache/` e ci restano
per sempre. Misurato: **132 MB per quattro estratti da 8 minuti**. Un
file da un'ora sono 115 MB, più 115 MB della variante ripulita.
Con 18 file al giorno sono **~4 GB al giorno**, ~120 GB al mese.

**Pro di fare subito.** Cresce da solo, senza che nessuno se ne accorga
finché il disco non è pieno — e quando è pieno la notte si ferma a
metà, che è il modo peggiore.

**Contra.** Nessuno. È un difetto, non una scelta.

**Perché proprio.** Il checkpoint ha bisogno del WAV per riprendere un
file interrotto, ma solo per i file in corso. Finita la sessione non
serve più.

**Io.** Pulizia a fine sessione, con un test che verifica che il
checkpoint continui a funzionare dopo.

---

### 3. La finestra notturna è scritta in tre posti, e due non concordano

**Stato.** `setup_launchd.py` dice 4 ore (02:00–06:00),
`nightly.DEFAULT_WINDOW_SEC` dice 3 ore, il README dice 4. Lanciando
`nightly.py` a mano senza argomenti si ottiene una finestra più corta di
quella che il job launchd usa.

**Pro di sistemarlo.** Il piano che leggi a mano e quello che gira di
notte devono essere lo stesso numero, altrimenti "la coda non si
chiude" significa due cose diverse a seconda di chi lo chiede.

**Contra.** Cinque minuti di lavoro.

**Perché.** È la classe di difetto più insidiosa: non rompe niente,
finché un giorno non rompe.

**Io.** Una sola costante, letta da entrambi.

---

## Qualità — il corpus si degrada piano, e non se ne accorge

### 4. Rinominare una voce non aggiorna nulla di esistente

**Stato.** Ci sono **tre** posti dove vive il nome di un parlante e
nessuno parla con gli altri:

| Dove | Contenuto dopo un rename |
|---|---|
| `data/speakers_db.json` | `Pietro` ✅ |
| `corpus.db`, tabella `speakers.name` | `GLOBAL_001` ❌ |
| `session.json` delle sessioni già scritte | `GLOBAL_001` ❌ |

Verificato: dopo `review_speakers.py name GLOBAL_001 Pietro`, le sessioni
precedenti continuano a dire `GLOBAL_001`.

**Pro.** Una fonte sola. Ogni query sul corpus restituisce il nome senza
codice di join, e le sessioni vecchie si aggiornano con un comando.

**Contra.** Riusare `speaker_names` dentro `transcript.json` significa
scrivere un nome vero in un file che oggi pubblica solo pseudonimi: va
deciso se il nome resta locale o entra nel corpus pubblicato.

**Perché conta.** Hai detto tu che vuoi trovare gli stessi personaggi
fra file diversi e poi analizzarli per persona. Oggi puoi farlo solo per
le sessioni elaborate *dopo* il rename.

**Io.** Il refactor. **Tu.** La decisione su dove il nome può finire.

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

### 7. Il registratore satura, e questo non lo posso sistemare io

**Stato.** Picco vero **+3,2 dBFS**, **0,19–0,30%** dei campioni a
fondo scala. Loudness −12,1 LUFS: registra forte.

**Pro di intervenire.** Nessun vantaggio. Il dato è che l'informazione
**è già stata persa** nel file: nessun filtro la ricrea. Si può solo
limitare il danno a valle, e il denoise già lo fa da sé scegliendo la
variante migliore.

**Contra (del non fare nulla).** Le registrazioni successive avranno
sempre più voce distorta, e la qualità ASR peggiora lentamente.

**Perché è tuo.** Se il registratore ha un'impostazione di
sensibilità o di AGC, va regolata lì. È l'unico punto dell'elenco in
cui la soluzione non è nel software.

**Tu.** Verificare se il dispositivo ha la regolazione.

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

---

## L'ordine in cui li farei

1. **2 — cache WAV.** Cresce da sola e rompe la notte. Difetto puro.
2. **3 — finestra notturna.** Cinque minuti, elimina una classe di bug.
3. **4 — una fonte sola per i nomi.** Il corpus è inutilizzabile per
   persona senza questo.
4. **1 — la prima notte vera.** Con 1–3 fatti, è un test vero.
5. **6 — flag di qualità.** Prima di costruirci analisi sopra.
6. **7 — sensibilità del registratore.** Tuo, e vale per tutto il resto.
7. **9 — pubblicazione.** Prima che il corpus sia grosso.
8. **8 — biometria.** Quando arriva l'hardware.

I punti 1–3 sono di un'ora di lavoro e chiudono i rischi che si
presentano da soli. Il resto può aspettare che il sistema abbia girato
qualche notte e accumulato dati su cui decidere.