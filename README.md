# audio-to-text

Pipeline locale per trascrizione, diarizzazione speaker e analisi prosodia di file audio/video lunghi (fino a 20+ ore), ottimizzata per Apple Silicon M1 Pro.

**Tutto gira in locale. Nessun dato inviato a servizi cloud. Gratuito.**

---

## Cosa fa

1. **VAD** — rileva i segmenti con voce (Silero VAD), scarta silenzi e rumori → dimezza il carico ASR
2. **Denoise** — pulisce il fruscio e sceglie da solo, su misure, fra originale e ripulita
3. **ASR** — trascrive con word-level timestamps (faster-whisper `large-v3-turbo`, CPU INT8)
4. **Diarizzazione** — assegna ogni parola a uno speaker (`SPEAKER_00`, `SPEAKER_01`, ...) e lo collega a un'identità vocale stabile fra file diversi
5. **Prosodia** — estrae F0, intensità, jitter, shimmer, velocità del parlato per ogni segmento
6. **Qualità** — dice quali segmenti il modello ha indovinato, ripetuto o attribuito ad audio che non conteneva parlato
7. **Corpus** — SQLite locale + repo privata, interrogabili per parola, per parlante, per giorno

**Tempi misurati su M1 Pro (16 GB), registrazioni vere:**
- Un file da 1h con 65% di parlato costa **~16 minuti**
- L'ASR gira a **3,9× realtime** sul parlato (a 4 thread, vedi sotto),
  la diarizzazione a **15,8×**
- Con 18 file da 1h al giorno: notte da 4h ne prende ~13, le passate
  diurne coprono il resto → **la coda si chiude**

I numeri non vengono da stime ma da misure sul tuo registratore, e il
modello che le usa è in [`core/cost.py`](core/cost.py).

**Attenzione al calore.** Di notte la pipeline gira con 4 thread, non 8,
e fa una pausa di 90 secondi fra un file e il successivo. Non è una
concessione: **4 thread sono risultati più veloci di 8** (3,86× contro
3,11× sul parlato reale), perché gli altri 4 core della M1 Pro sono
efficiency e insieme ai primi fanno contesa, non lavoro. Il tetto è in
[`core/config.py`](core/config.py) (`MAX_THREADS`) e un test impedisce di
alzarlo.

**Cosa non va ancora:** la lista dei punti aperti, con pro, contro e
responsabilità, è in [`APERTI.md`](APERTI.md). I punti 2, 3, 4, 5, 6 e
9 sono chiusi; restano la prima notte vera, la soglia speaker (manca la
seconda voce) e la biometria (manca l'hardware).

---

## Requisiti di sistema

- macOS con Apple Silicon (M1/M2/M3/M4)
- [Homebrew](https://brew.sh) con `ffmpeg` già installato
- venv `~/Desktop/Titoli Fabbri/whisperx_env` già presente

Verifica ffmpeg:
```bash
ffmpeg -version
```

---

## Setup iniziale

### 1. Installa le dipendenze mancanti

```bash
cd ~/Progetti/audio-to-text
./setup_env.sh
```

Installa: `mlx`, `mlx-whisper`, `praat-parselmouth`, `librosa`, `soundfile`.  
Non tocca: `torch`, `pyannote`, `faster-whisper`, `whisperx` (già presenti).

---

### 2. Token Hugging Face (necessario per la diarizzazione)

Il modello `pyannote/speaker-diarization-3.1` è open source (MIT) ma richiede
di accettare i termini d'uso su Hugging Face. **È completamente gratuito.**

#### Passo 1 — Crea un account HF (se non ce l'hai)
Vai su: https://huggingface.co/join

#### Passo 2 — Crea un token di accesso Read
Vai su: https://huggingface.co/settings/tokens  
→ clicca **New token**  
→ tipo: **Read**  
→ nome: `audio-to-text` (qualsiasi)  
→ clicca **Generate token**  
→ copia il token (inizia con `hf_...`)

#### Passo 3 — Accetta i termini dei modelli pyannote

Vai su queste due pagine e clicca **"Accept"** (richiede login HF):
- https://huggingface.co/pyannote/speaker-diarization-3.1
- https://huggingface.co/pyannote/segmentation-3.0

#### Passo 4 — Salva il token in locale

```bash
~/Desktop/Titoli\ Fabbri/whisperx_env/bin/hf auth login
```

Incolla il token quando richiesto. Viene salvato in `~/.huggingface/token`
e usato automaticamente dalla pipeline.

In alternativa, puoi usare una variabile d'ambiente:
```bash
export HF_TOKEN="hf_tuotoken"
```

---

### 3. Scarica il modello ASR (prima esecuzione)

Il modello `mlx-community/whisper-large-v3-turbo-q8` (~1.5 GB) viene scaricato
automaticamente alla prima esecuzione dalla cache Hugging Face.

Per scaricarlo manualmente prima:
```bash
~/Desktop/Titoli\ Fabbri/whisperx_env/bin/python -c "
import mlx_whisper
mlx_whisper.transcribe('', path_or_hf_repo='mlx-community/whisper-large-v3-turbo-q8')
"
```

---

## Uso

### Processare un file

```bash
~/Desktop/Titoli\ Fabbri/whisperx_env/bin/python run.py input/registrazione.mp3
```

Oppure attiva il venv prima:
```bash
source ~/Desktop/Titoli\ Fabbri/whisperx_env/bin/activate
python run.py input/registrazione.mp3
```

### Processare tutti i file in input/

```bash
python run.py --all
```

### Senza diarizzazione (non serve token HF)

```bash
python run.py input/file.mp3 --no-diarization
```

### Usare faster-whisper come fallback (solo CPU, più lento)

```bash
python run.py input/file.mp3 --backend faster --model large-v3-turbo
```

### Verificare lo stato dei job

```bash
python run.py --status
```

---

## Scheduling notturno (02:00 ogni notte)

### Installa il job launchd

```bash
python setup_launchd.py install
```

Ogni notte alle **02:00** la pipeline si avvia, scarica il registratore se
è collegato, trascrive la coda entro un budget di **4 ore** e pubblica il
corpus. Il terzo job (`it.pietrofabbri.audio-to-text-tile`) parte a ogni
inserimento del registratore: vedi «Inserisci il TileRec» più sotto. Viene
installato anche un job diurno: tre passate brevi (09:30, 15:30, 21:30,
40 minuti, 3 thread, priorità bassa) che fanno avanzare la coda senza
rubare la macchina.

Orario, finestra, thread, priorità e pausa stanno tutti in
[`core/config.py`](core/config.py): sono un numero solo, letto sia dal
job launchd sia da `nightly.py` lanciato a mano. Cambiarli lì cambia
entrambi — prima erano scritti in tre file e due non concordavano.

### Controlla lo stato

```bash
python setup_launchd.py status
```

### Test manuale (esegui subito)

```bash
python setup_launchd.py run-now
```

### Rimuovi il job

```bash
python setup_launchd.py uninstall
```

---

## Tempi reali misurati (le stime precedenti erano ottimistiche ~3x)

### Il numero di thread: perché 4 e non 8

Misurato sull'audio reale (10 chunk, 166 s di parlato), cambiando solo il
numero di thread:

| Thread | Tempo | Realtime sul parlato |
|---|---|---|
| 8 | 53,4 s | 3,11× |
| **4** | **43,0 s** | **3,86×** |
| 3 | 47,9 s | 3,47× |
| 2 | 66,9 s | 2,48× |

**Quattro thread sono più veloci di otto.** La M1 Pro ha 4 core
performance e 4 efficiency: usarli tutti non raddoppia il lavoro,
aggiunge contesa e tiene la CPU al pacchetto termico massimo, dopo il
quale scende la frequenza e va più piano di quanto andasse con la metà
dei core.

Questo cambia il modo di pensare il problema termico: non è «velocità
contro caldo», è che il vincolo era fasullo — la configurazione che
scaldava di più era anche la più lenta. Sotto i 4 thread il tempo
peggiora davvero, e con 2 si sente.

Resta una cosa che il tempo non dice: la pausa di 90 secondi fra un file
e il successivo. Quella costa davvero (~13 file a notte invece di ~15),
ed è dichiarata in `core/config.py` (`NIGHT_COOLDOWN_SEC`) con il flag
`--cooldown-sec 0` per disattivarla.

Il riscaldamento vero — watt, frequenza, ventola — è un'altra misura, e
c'è uno strumento per farla:

```bash
python thermal_probe.py                 # 60s di ascolto, uso CPU e livello termico
python thermal_probe.py --seconds 600   # 10 minuti
python thermal_probe.py --run           # baseline, poi la pipeline vera, e confronta
```

Su questa macchina `ioreg` non espone la temperatura e `powermetrics` chiede
permessi root: lo strumento funziona lo stesso e **dichiara cosa non ha
potuto misurare**, perché una temperatura non disponibile travestita da
zero farebbe pensare che la macchina sia fredda quando non lo è. La
lettura di watt e frequenza richiede `sudo powermetrics`.

### Il resto dei costi

Su questo Mac, misurati sul campione reale da 97,8 s (non stimati):

| Stadio | RTF misurato | 18 file da 1h |
|---|---|---|
| **ASR (faster-whisper large-v3-turbo, CPU INT8)** | **3,3x realtime** | **~5,5 h** |
| Diarizzazione (pyannote, MPS) | 14x realtime | ~77 min |
| VAD + ffmpeg | 33x realtime | ~33 min |
| Denoise (confronto su campione) | — | ~20 min |
| Prosodia + output | 49x realtime | ~22 min |
| **Totale** | | **~8 h** |

Nella finestra notturna di 4 ore entrano quindi **~13 file da un'ora**,
e gli altri restano sul device: la coda avanza dal più vecchio al più
nuovo, senza perdere nulla. Non è un limite aggirabile con l'attesa.

Il parallelismo non aiuta: `num_workers` di faster-whisper agisce solo
se si passano più segmenti in una singola chiamata, mentre la pipeline
chiama `transcribe()` un chunk alla volta — il guadagno misurato è 12%.

### La GPU non è la soluzione che sembra

Misurato sugli stessi 11,4 minuti di audio reale, modello in cache:

| Backend | RTF | 18 ore di audio |
|---|---|---|
| faster-whisper CPU INT8 | 3,15x | 5,7 ore |
| mlx-whisper GPU (`mlx-community/whisper-large-v3-turbo`) | 4,77x | 3,8 ore |

La GPU è **1,5x**, non 5-10x come suggerito da alcune stime. Con 18 ore
a notte si passerebbe da 3 a ~6 file per notte, al costo di un
sottoprocesso separato con IPC (mlx e PyTorch non convivono) e della
sua manutenzione. Il modello è gia in cache, quindi il confronto e
stato fatto davvero e non e' un calcolo teorico.

La conclusione netta: **18 ore di audio non si elaborano in una notte
su questa macchina, in nessuna configurazione misurata.** Il sistema e
costruito per che la coda avanzi di qualche file a notte, in ordine,
senza perdere nulla.

### Capacità misurata sulle registrazioni vere

Finestra notturna 02:00–06:00 (4h) più tre passate diurne brevi
(09:30, 15:30, 21:30, da 40 min con 3 thread e priorità bassa).

Il costo **non** è una costante per secondo di audio: caricare i modelli
e fare il campione per il confronto denoise si pagano una volta per
file, mentre l'ASR paga solo sul parlato. Per questo il modello di costo
sta in [`core/cost.py`](core/cost.py) e separa le due cose.

Su quattro registrazioni reali del registratore (209 minuti, 3 parlanti,
SNR 18–30 dB) il rapporto di parlato misurato è **65%**.

**Verifica su un campione indipendente, il 4 ottobre.** Sei file da un'ora
interi, presi da un registratore USB vero, con i tempi reali presi dal
manifest:

| Sessione | Parlato | Tempo reale | Previsto | Errore |
|---|---|---|---|---|
| `12-07-22` | 20% | 482 s | 586 s | +22% |
| `13-30-39` | 44% | 762 s | 809 s | +6% |
| `14-43-16` | 89% | 1.035 s | 1.227 s | +19% |
| `15-48-56` | 84% | 1.004 s | 1.180 s | +18% |
| `17-03-01` | 35% | 716 s | 727 s | +1% |
| `18-07-13` | 61% | 845 s | 958 s | +13% |
| **totale** | | **4.843 s** | **5.486 s** | **+13%** |

Il totale è la somma esatta, non quella delle righe già arrotondate:
4.843,1 s contro 5.485,8 s, che fa +13,3%.

Il modello sbaglia **sempre per eccesso**, dal +1% al +22%: è la
direzione giusta in cui sbagliare, perché la stima finisce per dire che
serve più tempo di quanto ne serva, e la notte non finisce mai a metà.
Non è però «entro l'1%», che era la taratura sulle quattro registrazioni
del 2 ottobre: su un secondo campione la deviazione tipica è del 13%.

Il tempo, fra l'altro, **non segue la durata ma il parlato**: fra il file
con 1.434 parole e quello con 6.492 la durata è la stessa e il tempo va
da 482 s a 1.035 s. Per questo il modello ha un termine per l'audio e uno
per il parlato, e per questo la coda impara dai file già fatti invece di
fidarsi di una tabella.

| Parlato | Costo per file da 1h | Notte (4h) | + diurno | Totale/giorno vs 18 |
|---|---|---|---|---|
| 95% | 20,3 min | 11 | 5 | 16 — scopre 2 |
| 80% | 18,1 min | 13 | 6 | 19 — in pari |
| **65% (misurato)** | **16,0 min** | **15** | **7** | **22 — in pari** |
| 50% | 13,8 min | 17 | 8 | 25 — in pari |
| 35% | 11,7 min | 20 | 10 | 30 — in pari |

**Con il 65% di parlato la coda si chiude.** Non è una stima ottimistica:
è il numero che esce dal modello tarato sulle tue registrazioni, e il
rapporto di parlato si rilegge da solo dalle sessioni già elaborate, quindi
si aggiusta da sé se le registrazioni cambiano.

Il margine si assottiglia sopra l'80% di parlato: se le registrazioni
diventassero quasi tutto parlato, servirebbe una macchina più veloce
oppure un modello ASR più leggero.

```bash
export A2T_ASR_MODEL=medium    # ~2x piu veloce, un po' meno accurato
```

`core/config.py` legge `A2T_ASR_MODEL` (e `A2T_ASR_THREADS` per i
thread): cambiare modello non richiede toccare il codice, e il checkpoint
riconosce cio che e gia fatto e non ricomincia.

### Cosa dicono le tue registrazioni

| | |
|---|---|
| Formato | MP3 32 kHz stereo, 128 kbps |
| Loudness | −12,1 LUFS (la registrazione è forte) |
| Picco vero | +3,2 dBFS: **il registratore satura** |
| Campioni a fondo scala | 0,19–0,30% |
| SNR stimato | 18–30 dB |
| Parlato | 65% |
| Parlanti per file | 2–3 |

Il **clipping** è l'unico dato che non si sistema dopo: quando il
campione è già stato saturato, l'informazione è persa e nessun filtro la
recupera. Si può mitigare in elaborazione (il denoise lo prova da sé e
sceglie), ma la soluzione vera è abbassare la sensibilità del
registratore, se ha un'impostazione del genere.

## Flusso col registratore (import → elaborazione → archiviazione)

## Chi parla: identità vocali fra file diversi

La diarizzazione etichetta `SPEAKER_00` dentro ogni file, ma quel numero
non significa niente fra un file e l'altro. Il confronto degli embedding
vocali assegna un ID globale stabile (`GLOBAL_001`, `GLOBAL_002`…), e il
giudizio finale — quando due voci sono la stessa persona — spetta a te:

```bash
python review_speakers.py                       # elenco + matrice
python review_speakers.py name GLOBAL_001 Pietro  # assegna (e riallinea)
python review_speakers.py merge GLOBAL_003 GLOBAL_004
python review_speakers.py split GLOBAL_005
python review_speakers.py sync                 # riallinea i nomi ovunque
python review_speakers.py sync --dry-run       # cosa cambierebbe
python review_speakers.py consolidate          # rifonde i cluster troppo brevi
python review_speakers.py consolidate --dry-run
python review_speakers.py voices               # coppie di voci da decidere, e dove l'hai sentita
python review_speakers.py voices --tutto       # anche tutte le coppie di campioni
python review_speakers.py nuove                # voci che non hai ancora guardato
python review_speakers.py ascolta GLOBAL_035 --play  # 4 estratti da sentire, con il testo
python review_speakers.py ignora GLOBAL_051    # vista, resta senza nome
```

### Dare un nome alle voci: il giro dopo una notte

Ogni notte, dopo la pubblicazione, il ciclo scrive
`output/voci_da_rivedere.md`: le voci senza nome, mai viste, con almeno un
minuto di parlato, ciascuna con la voce piu' somigliante e tre estratti
gia' tagliati in `data/ascolto/`. Il giro e' questo:

1. `python review_speakers.py nuove` — chi e' comparso.
2. `python review_speakers.py ascolta <voce> --play` — tre o quattro
   frasi di quella voce, da registrazioni diverse, con il testo accanto.
3. Poi una delle tre: `name <voce> <Nome>` se la riconosci, `merge
   <tenere> <unire>` se e' una voce che hai gia', `ignora <voce>` se non
   vuoi nominarla (passanti, televisione). In tutti e tre i casi sparisce
   dall'elenco.

Gli estratti si tagliano la notte stessa perche' l'audio originale resta
nell'archivio solo 7 giorni: un estratto tagliato resta, l'originale no.
Stanno in `data/`, fuori dal repo e fuori dal corpus, come il DB delle
voci: sono audio di persone.

**I nomi nel corpus.** Di default il corpus pubblica pseudonimi
(`GLOBAL_035`). I nomi reali sono ammessi (decisione D1 della
[ROADMAP](ROADMAP.md)): si pubblicano con `publish_corpus.py push
--with-names`, oppure ogni notte mettendo `corpus_with_names = True` in
`core/config.py`. Un nome pubblicato resta nella storia della repo.

**Le coppie da decidere.** `voices` mette in testa le coppie di **voci**
(non di campioni) che la soglia non chiude, con il coseno fra centroidi:
lo stesso numero che il sistema usa per assegnare le voci, quindi report
e sistema non si contraddicono. Accanto, quante volte le due voci hanno
parlato nella stessa registrazione: se succede, sono quasi certamente due
persone diverse.

### La stessa persona, vista da tutte le sessioni

`voices` raccoglie in una tabella quello che i singoli file non possono
mostrare: **dove hai incontrato ogni persona**.

```
GLOBAL_001    51.3 min  in 19-42  20-44  21-44  22-44
GLOBAL_004    33.0 min  in 19-42  20-44
GLOBAL_018    17.7 min  in 21-44  22-44
```

Le colonne sono le sessioni, non i file: la stessa identità che compare
in tre giornate diverse è **un interlocutore**, e senza questa vista il
conteggio delle persone che hai incontrato è sbagliato per costruzione.

Sotto, la parte che serve per decidere. La soglia 0,78 è un numero che
divide due cose che non hanno una divisione netta, e le coppie che gli
stanno addosso sono quelle che la macchina non può decidere:

```
Coppie entro 0,06 dalla soglia (6):
  0.771  GLOBAL_004[20-44] x GLOBAL_018[21-44]
  0.738  GLOBAL_006[19-42] x GLOBAL_013[20-44]
```

Sono le uniche che ti chiedono un giudizio. Ogni coppia sopra la soglia
è già stata unita, e ogni coppia sotto è già stata tenuta separata: qui
il numero non basta, e va detto tu. Sono anche le coppie da cui si impara
— se sono due persone diverse che si somigliano, o la stessa persona
che la diarizzazione ha spezzato, lo vedi in un colpo.

**Il nome vive in un posto solo.** La fonte è
`data/speakers_db.json` (locale, mai nel repo: sono dati biometrici).
Tutto il resto ne è una copia derivata — `corpus.db`, `session.json`,
`transcript.json` — e `name`, `merge` e `sync` la riallineano da soli.
Prima un rename valeva solo da quel momento in poi, e le sessioni vecchie
continuavano a dire `GLOBAL_001`: un rename che sembra non essere
successo.

Il **merge** fa di più: rietichetta anche le sessioni già scritte
(segmenti, statistiche, chiavi delle mappe) e le righe di `corpus.db`.
Senza, unire due voci lasciava due ID per la stessa persona nel corpus,
con statistiche che non si sommano — il problema che il merge doveva
risolvere restava aperto.

**I cluster troppo brevi non sono persone.** Su quattro ore di una stessa
conversazione la diarizzazione aveva prodotto 28 cluster locali e 21
identità globali: undici di quelle voci parlavano meno di novanta
secondi. Non era un errore — la pipeline finiva regolarmente — ma
ventuno voci su una conversazione a tavolo rendono il corpus
interrogabile solo in parte.

Ora i cluster che parlano meno di `merge_min_seconds` (90 s) si sciolgono
nel più simile prima che le identità globali vengano assegnate: **21
voci diventano 9**, e tutte e nove parlano almeno 160 secondi. L'ordine è
la parte che conta più della soglia: fuse dopo, non cambierebbe nulla,
perché il DB delle voci ha già dato un'identità a ogni frammento e non
torna mai indietro.

Le due soglie rispondono a domande diverse e non vanno confuse. Quella
fra file diversi resta a 0,78 e chiede «è la stessa persona?». Quella
di fusione è 0,45 e chiede «questa voce è troppo piccola per essere
qualcuno?», che è una domanda molto più facile — nessuno che parli
tredici secondi in una conversazione lunga è un interlocutore. Ogni
soglia fra 0,30 e 0,45 dà lo stesso risultato sui dati veri; 0,45 è la
più alta del pianoro, cioè la più conservatrice che ancora fa tutto il
lavoro utile.

La regola che tiene la cosa sicura è che **due voci grandi non si
fondono mai**, a nessuna somiglianza: se la fusione sbaglia, sbaglia
solo sui frammenti. Ogni sessione scrive `speaker_merge.json` con cosa è
stato fuso, perché fra sei mesi l'unica cosa che distingue «ha parlato
poco» da «la fusione ha sbagliato» è quella traccia.

Un dettaglio che non si vede ma conta: **`GLOBAL_0xx` non viene mai
riusato.** Un identificatore è il nome con cui una persona è citata in
ogni sessione, in `corpus.db` e sulla repo del corpus, quindi dopo un
merge gli ID salgono e restano dei buchi (GLOBAL_004, poi GLOBAL_018).
Un buco è innocuo; un numero riusato continuerebbe a citare una persona
che non c'è più, senza che nulla lo segnali. Per lo stesso motivo
`consolidate` conserva le identità che già ha: rieseguirlo non cambia
niente, e quello è il motivo per cui si può ritentare senza paura.

Su quattro registrazioni reali la separazione è netta: persone diverse
stanno a 0,13–0,29 di coseno, e l'unica coppia unita automaticamente
sta a 0,82. In mezzo, una fascia 0,53–0,67 dove la macchina non sa
decidere — è lì che serve un nome. La soglia di 0,78 sta sopra quella
fascia e sotto la coppia certa: si può cambiare con
`review_speakers.py threshold 0.70`, ma conviene aspettare più materiale
prima, perché i centroidi diventano più affidabili con le ore accumulate.

`voices` dice ora se quella soglia regge. Su 16 campioni e 109 coppie
**nessuna è sopra 0,78**, e sei sono entro 0,06 dalla linea: la soglia
non ha mai unito niente per errore, ma non ha nemmeno mai unito niente
per giusto. Finché non arrivano voci che si somiglino davvero, 0,78 resta
una scelta prudente più che una soglia tarata — e le sei coppie in zona
grigia sono la misura reale di quanto quella prudenza stia aspettando.

### Inserisci il TileRec, aspetta la notifica, staccalo

Dal 7 ottobre il registratore serve **solo per il tempo della copia**.
Una volta installati i job (`python setup_launchd.py install`, oppure
solo `install-tile`), il giro è questo:

1. **Inserisci il TileRec.** macOS lo monta come `/Volumes/Untitled` e
   launchd lancia da solo `sync_device.py scarica --auto`. Arriva una
   notifica: «TileRec collegato — copio N file, non staccarlo».
2. **Copia verificata in `input/coda/`.** Ogni file si copia calcolandone
   l'impronta mentre si legge, si scrive su disco, si rilegge la copia e
   le impronte si confrontano. Solo un file la cui copia coincide byte per
   byte viene cancellato dal registratore.
3. **Espulsione e notifica.** «TileRec copiato, puoi staccarlo» con il
   suono *Glass*. Se qualcosa non va il suono è *Basso* e il testo dice
   quanti file restano sul registratore (dettagli in `logs/scarico.log`).
4. **Trascrizione dalla coda.** Subito dopo parte una passata diurna
   (40 minuti, priorità bassa); quello che non entra lo prendono le
   passate delle 09:30, 15:30, 21:30 e la notte delle 02:00, che pubblica
   il corpus. Il registratore può già essere di nuovo al braccio.

Il perché: prima la trascrizione leggeva dal registratore e lo cancellava
solo a lavoro finito, quindi doveva restare collegato per ore. Il 5
ottobre è stato staccato a metà e la sessione `2026-10-05_09-39-09` ha
perso l'audio originale. Misurato con un registratore finto (immagine
disco exFAT `Untitled/RECORD`, il 7 ottobre): dall'inserimento
all'espulsione **4 secondi** per 3 file da 1 MB; per i file veri da
57,6 MB conta la velocità USB del TileRec, da misurare al primo uso.

Casi particolari, tutti coperti da test (`tests/test_scarico.py`):

| Caso | Cosa succede |
|---|---|
| Staccato durante la copia | Quello già copiato è intero in coda; il resto è sul registratore; notifica «reinseriscilo» |
| File troncato (batteria) | La parte leggibile va in coda e si trascrive; l'originale si cancella solo se all'inserimento dopo si ferma allo stesso byte con la stessa impronta |
| File già trascritto in passato | Non torna in coda; si libera solo lo spazio |
| Spazzatura (non audio) | Resta sul registratore, non entra in coda |
| Volume ancora in scrittura | Si aspetta che elenco e dimensioni restino fermi (2 s, massimo 30) |
| Una chiavetta qualsiasi | Il job parte ma esce in silenzio: non è il registratore |

**Al primo inserimento vero** macOS potrebbe chiedere il permesso di
accedere ai volumi rimovibili per `python3`: va concesso, una volta sola.
Se dopo mezzo minuto non arriva nessuna notifica, il motivo è in
`logs/scarico.log` o `logs/launchd_scarico.log`.

A mano, gli stessi passi:

```bash
python sync_device.py scarica --dry-run   # cosa copierebbe
python sync_device.py scarica             # copia, libera, espelli, avvisa
python sync_device.py scarica --no-delete # copia senza cancellare dal registratore
python sync_device.py pull --source input/coda   # trascrive la coda
```

### Il ciclo completo

Tutto il ciclo è in un comando:

```bash
python nightly.py --dry-run           # piano della notte: quanti file entrano
python nightly.py                     # ciclo completo: scarica, trascrive la coda, pubblica
```

Sotto, i singoli passi:

```bash
python sync_device.py detect          # che cosa è montato? non tocca nulla
python sync_device.py pull --dry-run  # cosa verrebbe fatto?
python sync_device.py pull            # importa, processa, archivia, cancella
python sync_device.py purge           # svuota l'archivio oltre 7 giorni
python publish_corpus.py push         # pubblica sulla repo privata
```

Il ciclo notturno si ferma **fra un file e l'altro** quando il budget
di tempo (`--max-seconds`, 4h di default) è esaurito: iniziare un file
che non finisce dentro la finestra costerebbe il suo tempo senza
produrre nulla. Quello che non entra resta in coda e riparte dalla
stessa condizione la notte dopo.

La stima del file successivo non è una tabella: dopo il primo file la
coda impara dal suo RTF reale. Per farlo la durata va letta **prima** che
il file venga cancellato — leggendola dopo, ffprobe non trova niente e il
budget conta un secondo di audio dove ne aveva 3.600, l'RTF imparato
diventa 482 e la coda si ferma dopo un file solo senza dire niente di
sbagliato nel log. Un test controlla che i secondi contati siano quelli
veri.

Il ciclo non finisce finché c'è un file da elaborare: tra l'uno e
l'altro c'è una **pausa di respiro** (`--cooldown-sec`, 90 s di notte,
30 s di giorno) e il ciclo è a `nice 10`. Il costo in tempo è dichiarato
e voluto: la coda avanza un po' meno, la macchina resta usabile il
giorno dopo.

I **WAV derivati** che la pipeline usa per il VAD stanno in
`data/wav_cache/` e vengono cancellati appena la sessione è finita —
ma **non** se il checkpoint è a metà, perché quello serve per riprendere
dal chunk interrotto. Erano ~4 GB al giorno e non finivano mai da soli.

Dalla coda la cancellazione resta l'ultimo atto (prima del 7 ottobre la
stessa regola valeva per il registratore, che quindi restava collegato
per ore):

```
elaboro → verifico l'output → archivio in locale → tolgo dalla coda
```

Se un passaggio fallisce, **il file resta in coda**. Non esiste un
percorso in cui un file viene cancellato senza che la trascrizione esista
e sia stata verificata (`transcript.json` presente, con segmenti e almeno
poche parole). Ogni file toccato finisce in `logs/device_manifest.jsonl`
con hash ed esito.

### Il file che il registratore ha troncato

La prima notte vera è finita con un caso che nessun test precedente
poteva mostrare, perché serve un device vero: un file che il registratore
promette intero e non lo è.

`2026-10-04_10-49-40.MP3` dichiarava 57.600.000 byte — un'ora esatta di
audio — e sulla card ce n'erano 3.538.944. Il driver lo dice in una riga
di log che vale un ettaro:

```
EXFAT_BeginBlockmap: Read with requested offset >= file allocated size. Exiting.
```

Il registratore era rimasto senza corrente mentre scriveva: la voce in
directory è stata aggiornata, i cluster non sono mai stati allocati.

Due cose rendono facile sbagliare qui. La prima è che `ffprobe` **non se
ne accorge**: ricava la durata dal byte count e dal bitrate, e
57.600.000 / 16.000 dà esattamente 3600,0 s. Un file con tre minuti di
audio si presenta come un'ora, e ogni stima — budget della notte inclusa
— parte da lì. La seconda è che `Errno 22` non è un errore di rete né di
permessi: rilettare non serve, il filesystem ha già detto di no, quindi
un `dd` con blocchi da 1 MiB si ferma a 3 MiB e uno da 4 KiB arriva a
3,5 MiB. La differenza non è casuale: è dove cade il confine.

Per questo il salvataggio **riprova con blocchi più piccoli** invece di
arrendersi al primo errore: si dimezza, si riprova, si scende fino a
64 KiB e a quel punto si ferma. Su quel file vero la differenza fra la
prima versione e questa è stata di 393.216 byte — 24 secondi e mezzo di
registrazione, che non perdevano perché non ci fossero ma perché non si
chiedeva nel modo giusto.

Cosa fa adesso `pull`, in ordine:

1. legge finché il filesystem risponde e **si ferma al primo errore**,
   senza tentare di rileggere;
2. **salva il pezzo** in `logs/lavoro/` e controlla che dentro ci sia
   audio decodificabile — sotto 512 KiB non è una registrazione troncata,
   è un file rotto, e su quello conviene che una persona guardi;
3. passa il pezzo alla pipeline con il nome vero del device, così
   `session.json`, il manifest e il corpus dicono `2026-10-04_10-49-40.MP3`
   e non il nome della copia di lavoro;
4.Solo dopo che la trascrizione esiste ed è verificata, archivia il pezzo
   e cancella **sia** il pezzo di lavoro sia l'originale troncato.

Quel quarto punto è una scelta, non una conseguenza: l'originale non si
può più elaborare, perché manca proprio il pezzo che manca. TENERlo non
proteggerebbe nulla e occuperebbe la card davanti a ogni pull futuro. Ma
la cancellazione avviene **solo** se il salvataggio è riuscito e
l'output è stato verificato: se la pipeline fallisce, sul device
restano entrambi, e nel manifest l'esito è `kept` con il numero di byte
recuperati. Il campo `troncato` nel manifest dice quale dei due casi è.

`--dry-run` non salva niente: è un piano, e un piano che scrive dischi non
è un piano.

I 3 minuti e 41 secondi recuperati dal file di quella notte sono finiti
nella sessione `2026-10-04_10-49-40`, che in `session.json` porta
`"parziale": true` con i byte letti e quelli dichiarati: senza quel
marchio sembrerebbe una registrazione come le altre, e non lo è. Sono i
primi minuti, che sono quelli con il contesto di chi parla.

`detect` riconosce i formati di nome più comuni dei registratori
(`REC_20261003_220415.mp3`, `2026-10-03 22-04-15.m4a`, e così via) e
ricava l'ora di registrazione, che finisce in `session.json` come
`session_start_wall`: è il dato che rende poi possibile agganciare la
trascrizione ai dati biometrici. I nomi di cui non si riconosce la data
vengono importati ma senza orario, e il tool te lo dice.

L'archivio locale (`archive/`) tiene l'originale per 7 giorni, poi `purge`
lo cancella: abbastanza per rifare un ASR migliore, non abbastanza per
far crescere il disco per sempre. Con `A2T_KEEP_LOCAL=0` si disattiva.

### Riduzione dei fruscii: decide il software

Non c'è nessuno sveglio la notte ad ascoltare, quindi la scelta è
automatica e **misurata** (`core/config.py → DenoiseConfig`).

Il confronto usa le **stesse finestre temporali** per originale e
variante ripulita, altrimenti ogni differenza sarebbe inseparabile da
dove sono stati tagliati i chunk. Su entrambe si calcolano:

| Segnale | Cosa cattura |
|---|---|
| confidenza ASR media | quanto l'ASR è sicuro di quello che sente |
| rapporto di parlato (VAD) | quanto parlato è sopravvissuto alla pulizia |
| parole/secondo | allineamento rotto, dettatura assurda |
| quota di segmenti ripetuti | l'ASR che "si arrende" e ripete un nucleo |

Regola: si cambia variante solo se una delle due è sana e l'altra no, o
se il guadagno supera un margine. A parità si tiene l'originale, che è
il minormale. La scelta e i numeri che l'hanno prodotta finiscono in
`denoise_decision.json`: le soglie si possono ritoccare vedendo i dati
reali invece di indovinarli.

**La punteggiatura è un segnale debole, ed è come spareggio.** Su una
registrazione reale la variante ripulita produceva «che è il modo
realistico» contro «che è il modo», e tutte le metriche quantitative
erano in pari: la scelta finiva sull'originale. Ora i segni di
punteggiatura ogni 100 caratteri entrano **solo quando le altre metriche
sono in pareggio**. Se la confidenza dice chiaramente una delle due, la
punteggiatura non ha voto.

Tre scelte che rendono la misura onesta: sotto 200 caratteri non si
misura (su quattro lettere un punto cambia tutto, e un numero inventato
che decide è peggio di nessun numero); l'apostrofo italiano non conta
(«l'acqua» è elisione, non confine di frase); il margine è 0,6 segni
ogni 100 caratteri, il doppio di quello sulla confidenza, perché il dato
è più rumoroso e la direzione in cui sbaglia è tenere l'originale.

`afftdn` è già in ffmpeg, quindi nessuna dipendenza nuova. Se il fruscio
diventa cattivo (ventola, traffico) il passo successivo è DeepFilterNet,
non un modello dentro ffmpeg.

## Il corpus: dove finisce il materiale testuale

Due destinazioni, con due ruoli diversi.

**`publish_corpus.py`** pubblica su una repo GitHub **privata** il
materiale che un LLM deve poter leggere: transcript, segmenti, token,
frequenze, markdown di analisi, con `INDEX.md` come punto d'ingresso.

```bash
python publish_corpus.py init              # clona la repo privata in locale
python publish_corpus.py push              # pubblica le giornate nuove o cambiate
python publish_corpus.py push --dry-run    # cosa cambierebbe, senza scrivere
python publish_corpus.py push --with-names  # pubblica anche i nomi reali
python publish_corpus.py status            # cosa c'è e cosa manca
python publish_corpus.py reindex           # ricostruisce il database locale
```

`push` conta solo le giornate **diverse** da quelle già sulla repo: il
confronto è sul contenuto che andrebbe scritto, quindi il `--dry-run` e il
push vero dicono lo stesso numero, e un secondo push senza novità risponde
«Nessuna giornata nuova da pubblicare».

### Cosa c'è sulla repo: una cartella per giorno

Dal 7 ottobre la repo è organizzata per **giorno**: tutte le registrazioni
di un giorno stanno in un file per tipo, in fila, con l'**ora vera**
dell'orologio del registratore al posto del tempo dall'inizio del file
(`core/giorno.py`).

```
INDEX.md                        # una riga per giorno: registrazioni, orari, blocchi, parlato, parole, voci
.gitignore                      # scritto da publish_corpus.py: niente spazzatura del Finder
giorni/
└── 2026-10-05/
    ├── giorno.json             # manifesto: registrazioni, orari, buchi, blocchi continui,
    │                           # voci, decisioni di denoise, fusioni delle voci
    ├── transcript.txt  .srt    # il testo del giorno, con l'ora vera
    ├── segments.jsonl          # un segmento per riga: session + start/end nel file, clock_*/day_sec_*
    ├── tokens.jsonl            # una parola per riga, con day_segment_idx e speaker_global
    ├── prosody.csv             # un segmento per riga, con l'ora vera
    ├── wordfreq.csv            # frequenze del giorno, per voce
    ├── analysis_ready.md       # il giorno intero, pronto per un LLM
    └── *.corrected.*           # le stesse viste col testo corretto, dove esiste
voices/
└── voice_matrix.json           # somiglianza fra le voci, coppie di voci da decidere
```

Le regole, misurate sulle registrazioni vere:

- **Blocco continuo** = file consecutivi con un buco fino a 5 minuti. Il
  TileRec spezza ogni ora e perde da pochi secondi a ~3 minuti fra un file
  e l'altro; le pause vere del 4 ottobre andavano da 4 a 23 minuti. Nel
  testo ogni registrazione ha un'intestazione che dice se continua la
  precedente o arriva dopo una pausa, e di quanto.
- **Un blocco appartiene al giorno in cui comincia**: una conversazione
  che passa la mezzanotte resta intera nel giorno prima (nell'SRT l'ora
  continua oltre le 24).
- **I campi originali restano**: ogni segmento e ogni parola dicono da
  quale file vengono (`session`) e in che secondo di quel file
  (`start`/`end`), accanto all'ora vera.
- **La prosodia sta nel segmento**, non ripetuta in ogni parola: per il
  5 ottobre `tokens.jsonl` passa da 20 a 10 MB, la giornata intera pesa
  12 MB.

Non si pubblicano più, perché ridondanti: `transcript.json` (le stesse
cose di `segments.jsonl` + `tokens.jsonl`), `speaker_profiles.json` (una
fotografia del DB delle voci; la vista aggiornata è la matrice) e
`session.json` (nel manifesto). `denoise_decision.json` e
`speaker_merge.json` sono dentro `giorno.json`. In locale, in `output/`,
resta tutto com'era: l'elaborazione è per file, la giornata è una vista
costruita alla pubblicazione.

**La migrazione** dalla struttura per file (`sessions/`) avviene al primo
`push`: le sessioni che stanno solo sulla repo vengono prima riportate in
`output/`, poi `sessions/` sparisce in un unico commit.

`voices/voice_matrix.json` è **un file solo per tutto il corpus**, non uno
per sessione: riporta la somiglianza fra ogni coppia di voci e, in
`gray_zone`, le coppie entro 0,06 dalla soglia — quelle che la macchina non
riesce a decidere. Contiene solo pseudonimi, secondi e somiglianze:
**nessun embedding**, perché un embedding vocale è un'impronta biometrica.

Il report in forma di testo dice in testa quale operazione ha fatto: la
matrice confronta un campione con l'altro, mentre l'assegnazione delle
voci confronta l'embedding della sessione contro i centroidi salvati. Sono
due numeri diversi per la stessa domanda, e possono dare risposte diverse.

`reindex` non serve nel caso normale — la pipeline aggiorna il database
appena finisce una sessione. Serve dopo un rilascio che cambia come si
scrive l'output, dopo un restore, e per riparare un database indietro
senza rielaborare nulla. Allinea anche i nomi alla fonte e toglie dalla
tabella dei parlanti le voci che nessuna sessione cita più.

Sulla repo **non** finiscono mai: audio, embedding vocali, i database
locali, i checkpoint. Non è una scelta di comodità: testo, prosodia e
statistiche parlarie insieme ricostruiscono un profilo che nessun file
rivela da solo.

**I nomi reali dei parlanti** stanno nel DB delle voci e in `session.json`
in locale. Sulla repo, per default, compaiono solo gli pseudonimi: un
accesso alla repo non dà l'identità. `--with-names` li pubblica (nel testo
come «Nome (GLOBAL_001)», nel manifesto come mappa), oppure ogni notte con
`corpus_with_names = True` in `core/config.py`: pubblicare nomi veri non
si richiama, quindi il default resta spento.

Il controllo finale non guarda solo le estensioni: cerca i nomi reali
**dentro il contenuto** di ogni file pubblicato, usando l'elenco delle
etichette che hai assegnato tu. È l'ultima rete — se un nome finisce in
un CSV o in un file lasciato da una versione precedente, il push si
ferma invece di pubblicare. La catena intera è provata in
[`tests/test_publish.py`](tests/test_publish.py) con una coda finta e un
clone git con remoto locale: nessun dato tuo esce dai test.

**`core/corpus_db.py`** tiene un SQLite **locale** che fa ciò che git
non sa fare: aggregare mesi di dati in una query. Lo schema è pensato
per le analisi che farai dopo, non per quelle di oggi.

```python
from core.corpus_db import CorpusDB
with CorpusDB() as db:
    db.query("SELECT word, SUM(freq) c FROM wordfreq GROUP BY word ORDER BY c DESC LIMIT 20")
    db.query("SELECT date, kind, summary FROM analyses ORDER BY date DESC")
```

Tabelle: `sessions`, `segments` (con le feature prosodiche come colonne
proprie, non dentro un JSON), `tokens` (forma originale **e** normalizzata:
in italiano la maiuscola dopo il punto è informazione), `wordfreq`,
`bigrams`, `corpora` (testi di riferimento), `analyses` (una riga per
data e tipo di analisi, con la sintesi in una riga: è il punto in cui
le analisi future diventano confrontabili nel tempo).

L'ingestione è idempotente: rielaborare una sessione sostituisce i dati
invece di duplicarli.

## Il pannello cifrato

Un pannello web legge le metriche del corpus: copertura della giornata,
con chi si è parlato, come parla Pietro, qualità del dato, su scale da
giorno a totale. Specifica completa in
[`docs/analisi-corpus.md`](docs/analisi-corpus.md); qui come funziona e
come si configura.

**Il percorso.** Il Mac, a ogni pubblicazione, calcola
`metriche/AAAA-MM-GG.json` (`core/metriche.py`: solo numeri aggregati,
nessun testo) e installa nel corpus `.github/workflows/pannello.yml`
(copia ufficiale: `pannello/pannello.yml`). Al push GitHub Actions, nella
repo privata del corpus, costruisce una pagina sola con i dati dentro
(`pannello/costruisci.py` + `pannello/modello.html`), la cifra con
StatiCrypt e la spinge sul ramo `gh-pages` di una repo **pubblica** dal
nome neutro, riscrivendo il ramo da zero ogni volta.

**Perché cifrata.** GitHub Pages pubblica siti privati solo con
Enterprise Cloud: con gli altri piani il sito è pubblico anche se la repo
è privata. La pagina pubblicata è quindi leggibile da chiunque abbia
l'indirizzo, ma è cifrata: senza la frase d'accesso non si vede niente.
La frase è l'unica protezione, quindi va lunga (almeno 20 caratteri;
meglio cinque o sei parole casuali).

**Configurazione, una volta sola:**

1. Crea su GitHub una repo **pubblica**, vuota, dal nome neutro (es.
   `taccuino`).
2. Crea un token *fine-grained* (Settings → Developer settings → Personal
   access tokens → Fine-grained tokens): accesso **solo** a quella repo,
   permesso *Contents: Read and write*.
3. Nella repo **privata** del corpus, Settings → Secrets and variables →
   Actions:
   - secret `PANNELLO_PASSWORD`: la frase d'accesso;
   - secret `PANNELLO_TOKEN`: il token del punto 2;
   - variabile (tab *Variables*) `PANNELLO_REPO`: `pietrofabbri/taccuino`.
4. Sul Mac, aggiorna il codice (`git pull` in `audio-to-text`): il
   prossimo push del corpus porta le metriche e il workflow.
5. Dopo il primo giro del workflow (tab Actions del corpus), nella repo
   pubblica: Settings → Pages → *Deploy from a branch*, ramo `gh-pages`,
   cartella `/ (root)`. L'indirizzo sarà
   `https://pietrofabbri.github.io/taccuino/`.

Finché i segreti mancano, il workflow chiude in verde con un avviso e non
pubblica niente.

**Vedere la pagina in chiaro sul Mac**, senza pubblicare:

```bash
python core/metriche.py corpus_repo/giorni /tmp/metriche
python pannello/costruisci.py /tmp/metriche /tmp/pannello.html
open /tmp/pannello.html
```

## Struttura output

Per ogni file `input/registrazione.mp3` viene creata la cartella `output/registrazione/`:

```
output/registrazione/
├── transcript.json      # struttura completa (testo + speaker + prosodia)
├── transcript.txt       # testo leggibile con etichette speaker
├── transcript.srt       # sottotitoli SRT
├── prosody.csv          # metadati prosodici in formato tabulare
├── session.json         # metadata sessione, durate, statistiche per speaker
├── segments.jsonl       # un segmento per riga (JSONL) — ingest LLM/analisi
├── tokens.jsonl         # una parola per riga: timestamp, asr_prob, prosodia — KWIC, n-grammi
├── wordfreq.csv         # frequenze parole per speaker
├── analysis_ready.md    # testo chunked pronto per un LLM
├── speaker_profiles.json# profilo aggregato delle voci globali (senza vettori)
├── speaker_merge.json  # cosa e' stato fuso fra i cluster troppo brevi
└── registrazione.checkpoint.json  # stato avanzamento (ripresa automatica)
```

### Identità vocali cross-file (speaker ID)

`pyannote` etichetta gli speaker con ID locali (`SPEAKER_00`) che valgono
solo per una sessione: la stessa persona può avere label diversi in due
file diversi. La pipeline risolve il problema con gli **embedding vocali**
che pyannote calcola già per il clustering (nessun costo aggiuntivo):
ogni voce viene confrontata per similarità coseno con un database
persistente e associata a un ID globale stabile (`GLOBAL_001`).

```
data/speakers_db.json   # database delle voci (dato biometrico, gitignored)
```

```python
from core.speaker_db import SpeakerDB
db = SpeakerDB()
db.set_name("GLOBAL_001", "Pietro")   # etichetta manuale
print(db.profiles())                  # ore parlate, sessioni, date
```

### Qualità della trascrizione: cosa non è detto

Un estratto reale di 40 secondi di audio non intellegibile era finito
nel corpus come frasi plausibili e sbagliate — non parole a caso, ma
italiano che si può leggere. Niente lo segnalava. Ora ogni segmento ha
un verdetto in `core/quality.py`:

| Segnale | Cosa cattura |
|---|---|
| `no_speech_prob` alto | il modello stesso dice «qui non parlavi» |
| parole al secondo fuori scala | allineamento rotto |
| loop di n-gramma | il caso tipico della registrazione vera |
| testo vuoto o troppo corto | segmenti che non dicono nulla |
| troppe parole a bassa probabilità | il modello che indovina, e lo sa |

I tre livelli sono `ok`, `low`, `unreliable`, e **un solo segnale dà
`low`**: una parola a probabilità 0,3 è ancora informazione, e un flag
che scatta spesso viene ignorato.

```python
from core.corpus_db import CorpusDB
with CorpusDB() as db:
    print(db.quality_report())    # quanto materiale è inaffidabile, per sessione
    for r in db.suspect_text(20): # i segmenti peggiori, da rivedere a mano
        print(r["stem"], r["start_sec"], r["quality_reasons"], r["text"][:60])
```

Il flag compare in `transcript.json`, in `segments.jsonl`, in
`prosody.csv` e in `analysis_ready.md` — in testa al documento e in
linea a ogni segmento, perché un LLM non chiede cosa non deve usare.

---

## Correggere le parole: un modello di lingua al posto dell'acustica

Whisper sbaglia le parole in modo prevedibile: fonemi scambiati, parole
dialettali rese in italiano, nomi proprii storpiati. In una conversazione
dell'2 ottobre: «Savot», «stegnavano a telefono», «matiala vera». Sono
errori che si riconoscono dal contesto, e un modello di lingua li
corregge senza fatica — nessun modello acustico li corregge, perché
l'informazione che manca non è nel suono: è che «maiala vera» è una
frase che esiste.

```bash
python correct_text.py                          # cosa verrebbe fatto
python correct_text.py --dry --limit 5          # cinque correzioni, niente scritto
python correct_text.py --consent                # scrive davvero
python correct_text.py --consent --session 19-42-33
```

> **Se sei in zsh e ricevi `unrecognized arguments`**, il `#` finale è
> arrivato come argomento: in zsh, nei comandi interattivi, `#` non è un
> commento se `INTERACTIVE_COMMENTS` non è impostato. Metti il commento
> nella riga precedente, oppure una volta sola in `~/.zshrc`:
>
> ```bash
> setopt interactive_comments
> ```
>
> Da lì in avanti le righe con il commento in coda si potranno copiare
> così come sono — com'è in `bash`.

**Serve `--consent` perché è una decisione, non un dettaglio.** Ogni
segmento mandate a un'API porta fuori dal portatile il testo di una
conversazione personale. Tutto il resto di questa pipeline è costruito
perché i dati non escano — i nomi reali non arrivano al corpus
pubblico. Mandare il testo a un servizio esterno è una scelta diversa, e
non la prende uno script per abitudine. Senza `--consent` il comando
mostra cosa farebbe e si ferma.

### Ottenere la chiave, passo passo

1. Apri **https://aistudio.google.com/app/apikey** e accedi con il tuo
   account Google. Se è la prima volta, accetta i termini: Google crea
   automaticamente un progetto Google Cloud di default.
2. Clicca **Create API key**. Comparirà una finestra che chiede in quale
   progetto metterla: lascia il default, se non ne hai altri.
3. Comparirà una chiave. **Copiala subito**: Google la mostra una volta
   sola e non si può rivedere dopo. Può cominciare per `AIza` (chiave
   standard) o per `AQ.` (authorization key, legata a un account di
   servizio: è quella che Google crea per impostazione predefinita dal
   maggio 2026). Va bene qualsiasi delle due.
4. Mettila nell'ambiente — su macOS con zsh:

   ```bash
   echo 'export GOOGLE_API_KEY="AIza..."' >> ~/.zshrc
   source ~/.zshrc
   ```

   Verifica che sia arrivata con `echo $GOOGLE_API_KEY | cut -c1-8`:
   deve stampare i primi caratteri della chiave. **Non stampare la
   chiave intera** in un terminale che può finire in una trascrizione.

Dipendenza Python, una volta sola:

```bash
pip install google-genai
```

Vale anche `GEMINI_API_KEY`; se sono entrambe presenti vince
`GOOGLE_API_KEY`.

> **Il modello conta.** Il default è `gemini-3.5-flash-lite`, non il più
> capace: `3.8-flash` al momento risponde `503 high demand` a ogni
> tentativo, e un batch notturno che scarta tutti i segmenti è peggio di
> uno che corregge un po' meno. I modelli `2.5` sono accessibili solo
> agli account che li avevano già usati in passato. Se un modello non è
> disponibile l'errore si ferma subito e nomina quelli che funzionano,
> invece di consumare i tentativi su una richiesta che non può riuscire.

### Quanto è affidabile — misurato, non dichiarato

Su segmenti veri, con chiave vera:

| | |
|---|---|
| **Correzioni giuste** | `drastisovati` → disastrati, `monopolito` → monopolio, `steam` → stesso |
| **Correzioni inventate** | `Cominciatemi` → «Camminate», poi → «Diamoci» |
| **Segmenti senza ritocco** | 3 su 6, il modello è cauto |

Il difetto è che riscrive le parole dialettali, e a occhio non si
distingue un errore di riconoscimento da una parola che suona stretta
solo perché è dialettale.

La risposta è la probabilità **per parola**, che Whisper calcola già e
che non era mai arrivata al correttore: ora la trovi in `tokens.jsonl`
(`asr_prob`) e nel checkpoint. Sopra una soglia il modello acustico ha
detto che sa cosa sta sentendo, e lì il giudizio che conta non è più
quello del modello di lingua. È il filtro `--soglia-prob`, descritto
sotto.

Per questo una passata automatica **con il filtro acceso** è ragionevole
sulle quattro sessioni vere. Gli originali restano sempre accanto ai
corretti, in `text_raw` e in `text_correction.json`, quindi ogni
decisione resta reversibile.

### Non si riscrive quello che Whisper aveva già capito

Il difetto più fastidioso del correttore non è che sbagli poche parole:
è che non distingue un errore di riconoscimento da una parola che
suona stretta solo perché è dialettale. Un prompt con una regola
esplicita — «una parola pronunciabile che sembra storta è quasi
certamente quello che è stato detto» — non lo ferma: è una limitazione
del modello, non dell'istruzione.

Ma non tutte quelle parole sono uguali, e la differenza non sta nel
testo: sta in quello che il modello acustico ne aveva capito. Su
`Cominciatemi ragazzi, siamo drastisovati`:

| parola | `asr_prob` | cosa significa |
|---|---|---|
| `drastisovati` | 0.41 | Whisper non sapeva cosa fosse: correggibile |
| `monopolito` | 0.70 | incerto: correggibile |
| `steam` | 0.14 | quasi inaudibile: correggibile |
| `Cominciatemi` | 0.63 | udito, solo che stretto: **protetto** |

Sopra `--soglia-prob` la parola non si tocca, per quanto il modello di
lingua insista. Il default è **0.90**. Quanto protegga davvero dipende
da dove si mette, e il numero si misura sulle parole vere:

| soglia | protette | correggibili |
|---|---|---|
| 0.70 | 12.268 (72,7%) | 4.597 (27,3%) |
| 0.75 | 11.617 (68,9%) | 5.248 (31,1%) |
| 0.80 | 10.865 (64,4%) | 6.000 (35,6%) |
| 0.85 | 10.049 (59,6%) | 6.816 (40,4%) |
| **0.90** | **9.063 (53,7%)** | **7.802 (46,3%)** |
| 0.95 | 7.519 (44,6%) | 9.346 (55,4%) |

Su 16.865 parole. La distribuzione è spiegata bene e vale la pena
guardarla: il 53,7% delle parole sta sopra 0.90 e il 35,5% sta sotto
0.80. Due mucchi con una coda lunga in mezzo — e le parole quasi
inaudibili sono il materiale su cui il correttore ha un compito, la
ragione per cui il filtro non può essere spinto a proteggere quasi
tutto.

```bash
python correct_text.py --consent --soglia-prob 0.90
python correct_text.py --consent --soglia-prob 0    # filtro spento
```

**Il 0.90 è una scelta, e non è quella giusta in assoluto.** Su cinque
parole etichettate a mano — tre correzioni giuste e due riscritture
sbagliate — la soglia non separa le due classi, perché sono intercalate:

| parola | `asr_prob` | che cos'è | con soglia 0.74 |
|---|---|---|---|
| `steam` | 0.14 | correzione giusta | passa |
| `drastisovati` | 0.41 | correzione giusta | passa |
| `Cominciatemi` | 0.63 | riscrittura da bloccare | **passa** |
| `monopolito` | 0.70 | correzione giusta | passa |
| `similiata` | 0.74 | invenzione da bloccare | bloccata |

Con 0.74 si blocca `similiata` e si lascia correggere `monopolito`; con
0.63 si fa il contrario — si salva `Cominciatemi` e si blocca una
correzione giusta. Non c'è un valore che vinca le due cose insieme, e il
motivo è che la probabilità di Whisper misura quanto era sicuro
l'orecchio, non quanto era giusta la correzione.

Cinque parole sono un campione troppo piccolo per decidere: servono
tutte le proposte del modello con la probabilità accanto, e si leggono
in una pagina con `--solo-proposte` sotto. Finché quel giro non c'è, il
default resta 0.90 e la scelta è dichiarata, non misurata.

**È una difesa, non una garanzia.** Nel caso qui sopra la soglia non
avrebbe salvato `Cominciatemi` (0.63): quel caso è stato risolto
togliendo le parole già certe, non aggiungendo un filtro. Quello che il
filtro fa è togliere di mezzo le parole su cui il modello di lingua si
butta a riscrivere il dialettalismo, e mettere accanto al testo il
numero che ha deciso — così il giudizio si può rifare con dati, non a
intuito.

Ogni parola bloccata resta nel file con la sua probabilità **e con la
proposta che è stata respinta**, perché è quella che serve per tarare la
soglia guardando i dati:

```json
{"i": 0, "raw": "Cominciatemi", "fixed": "Cominciatemi",
 "proposta": "Camminate", "changed": false, "prob": 0.98,
 "blocked": true}
```

`fixed` è il testo, e resta quello di prima. `proposta` è che cosa il
modello voleva scrivere lì: senza questo campo la parola bloccata era
identica a una parola che il modello non aveva mai toccato, e la soglia
si tarava guardando il nulla.

e il riepilogo della sessione conta le due cose per separate:

```json
{"words_proposed": 137, "words_changed": 61, "words_blocked": 76}
```

Se `words_blocked` è zero su una notte intera, o la soglia è troppo
alta o le probabilità non ci sono: le due cose si confondono, ed è
per questo il numero è nel riepilogo.

### Leggere una passata intera: `--solo-proposte`

Il confronto originale/corretto serve per capire **un** segmento. Su
429 segmenti sono pagine, e la domanda che conta dopo è più piccola:
quali parole il modello vuole cambiare, quante volte, e con quanta
sicurezza Whisper le aveva udite.

```bash
python correct_text.py --consent --dry --solo-proposte
```

Una riga per parola proposta, raggruppata per parola e ordinata per
frequenza, e alla fine una sintesi che mette tutte le sessioni
insieme:

```
  accettata  drastisovati. -> distratti.  x1  p=0.41  vocabolario: MAI UDITA
  accettata  frasci, -> frasi,  x12  p=0.32  vocabolario: 13x p=0.24
  respinta   Cominciatemi -> Camminate  x3  p=0.98  vocabolario: MAI UDITA
```

Quattro colonne, e ognuna dice una cosa diversa:

- **`x12`** — quante volte la proposta compare. Una parola proposta
  dodici volte è un fatto della trascrizione, una proposta unica è
  rumore: senza il conteggio hanno lo stesso peso sulla carta.
- **`p=0.32`** — la probabilità che Whisper aveva dato alla parola. È
  la stessa cifra che la soglia confronta, e serve a capire *dove* il
  filtro ha negato qualcosa (`respinta`).
- **`vocabolario`** — quante volte la parola **proposta** compare già
  nelle trascrizioni, e con quanta sicurezza Whisper l'aveva capita
  altrove. `frasi` c'è già, tredici volte: il modello non sta
  inventando, sta adattando.

**La colonna «vocabolario» è un dato, non un verdetto**, e la ragione
vale la pena dirla perché il primo ragionamento che viene in mente è
sbagliato: sulle quattro notti vere anche `distratti` — la correzione
che è *giusta*, `siamo drastisovati` → `siamo distratti` — non è mai
stata udita, perché in quelle ore nessuno ha detto «distratti». La
colonna dice che una parola non ha riscontro nel corpus; non dice che
è sbagliata.

Ed è per questo che a tarare la soglia non serve un altro numero
automatico: servono gli occhi. Sul campione vero di quattro segmenti
le proposte erano quattro, e due erano buone (`drastisovati →
distratti`, `frasci → frasi`), una dubbia (`Cominciatemi → Diamoci`)
e una peggio dell'originale (`similiata → sibilata`, che sostituisce
un nonsense con un altro nonsense). Con una riga per parola le quattro
entrano in una pagina e la decisione è tua.

Le proposte respinte dal filtro compaiono nella stessa tabella: è
guardando quelle che si sceglie dove mettere `--soglia-prob`.

### Il numero di parole non può cambiare

È la regola che tiene la cosa onesta. Il modello può correggere,
riscrivere, riorganizzare — ma non può aggiungere o togliere parole,
e ogni correzione è annotata. Se il numero cambia, la risposta si
scarta e si dice perché.

Il motivo non è la pedanteria. Una riscrittura produce un testo che
sembra *più buono* e che non è più quello che è stato detto: il modello,
vedendo «stegnavano a telefono», può scrivere «segnavano al telefono» —
probabilmente giusto — ma può anche scrivere «segnavano i telefoni»,
che è inventato, e i due testi sono indistinguibili a chi li legge
dopo. In un corpus che vuole misurare la propria voce, un testo
inventato è **peggio** di un testo sbagliato: lo sbagliato almeno si
riconosce.

Perciò il risultato è affiancato, non sostitutivo. Ogni parola
conserva originale, correzione e se è cambiata:

```json
{"i": 6, "raw": "matiala", "fixed": "maiala", "changed": true}
```

Senza il confronto fra le due forme uno dei due errori sparisce, e non
sai quale. Con entrambi hai la misura vera della qualità della
trascrizione: quanto sbaglia Whisper e quanto sbaglia il correttore.

### Quello che il correttore non fa

Non ricostruisce le frasi incomplete. Se un segmento sembra mozzato,
corregge le parole che ci sono e lascia il resto com'è. I segmenti che
non tornano — risposta illeggibile, parole non allineate, chiamata
fallita — restano **grezzi**, e il file dice quali e perché: un
segmento non corretto non è un errore, è un segmento di cui non ci si
fida.

Ogni giro registra un'impronta del testo, quindi un tentativo
interrotto a metà non si paga due volte e non corregge due volte lo
stesso testo.

### I nomi propri: il glossario

Il difetto più pericoloso del correttore è che riscrive i nomi che non
conosce: sulla prova del 2 ottobre «Zia Titti» è diventata «Gigi
D'Alessio», in entrambe le occorrenze. Due difese, entrambe nel codice e
non solo nel prompt:

- **il glossario**: i nomi dati alle voci (`review_speakers.py name`) più
  una lista a mano in `data/glossario.txt`, una voce per riga, anche di
  più parole (`Zia Titti`, `Teatro della Pace`); le righe con `#` sono
  commenti. Sta in `data/`, fuori dalla repo pubblica. Il glossario va
  anche nel prompt, così il modello può correggere una storpiatura *verso*
  un nome noto;
- **la maiuscola**: una parola maiuscola fuori da inizio frase non si
  tocca, e il modello non può introdurre un nome maiuscolo nuovo che non
  sia nel glossario.

Le parole bloccate restano nel file con `blocked_reason` (`glossario`,
`nome` o `prob`), e il riepilogo di sessione conta i nomi protetti.
Il caso «Zia Titti» è un test di regressione.

Il correttore manda anche due segmenti prima e due dopo come contesto
(`--contesto N`, 0 per nessuno); le correzioni restano solo sul
segmento centrale.

### Nel giro notturno: il motore locale (Ollama)

Dal 9 ottobre il motore predefinito è **locale**: un modello che gira sul
Mac attraverso [Ollama](https://ollama.com). È gratuito, non ha limiti di
chiamate e il testo non esce dal computer. Gemini resta disponibile per
confronto con `--motore gemini` (e allora vale tutto quello che è scritto
sopra sulla chiave), ma **non va usato sul piano gratuito**: lì i termini
di Google consentono di usare i testi per migliorare i prodotti, anche con
revisori umani.

Con `correzione_notturna = True` in `core/config.py` il giro notturno
lancia la correzione fra la trascrizione e la pubblicazione, così il
corpus esce già con le giornate corrette. È spenta di default. Per
accenderla:

1. installa Ollama e lascia l'app aperta (si avvia al login);
2. scarica il modello:

   ```bash
   ollama pull gemma3:12b     # circa 8 GB di memoria durante l'uso
   ```

   Su un Mac con 8 GB in tutto usa `gemma3:4b` (circa 3 GB) e scrivilo in
   `correzione_modello_locale` in `core/config.py`;
3. prova a mano su pochi segmenti, senza scrivere niente:

   ```bash
   python correct_text.py --consent --dry --limit 5
   ```

   La prima riga dice motore e modello; se Ollama non risponde o il
   modello non è scaricato, il comando si ferma subito e dice cosa fare;
4. metti `correzione_notturna = True`.

Le impostazioni, tutte in `core/config.py`: `correzione_motore`
(`"ollama"` o `"gemini"`), `correzione_modello_locale`, `ollama_url`,
`correzione_budget_sec` (due ore per notte: quello che non sta nel tempo
si riprende la notte dopo, dai segmenti non ancora corretti),
`correzione_giorni_esclusi`. Un errore (Ollama chiuso, modello mancante)
non ferma il giro: si pubblica il testo grezzo.

Il modello elenca solo le parole che cambia, non tutte: in locale è la
differenza fra secondi e minuti per segmento. Il tempo per segmento sul
Mac va misurato alla prima notte.

A mano, lo stesso passo è:

```bash
python correct_text.py --consent --sintetico --max-seconds 7200
```

### Dove finisce il testo corretto

In tre posti, tutti con l'originale accanto.

Nel **database**: `segments.text` diventa il testo da analizzare,
`segments.text_raw` conserva l'originale, e `n_words_changed` dice
quanto ha lavorato il correttore. I token, il conteggio delle parole e
i bigrami si ricostruiscono sul testo corretto — che è il punto: sul
testo grezzo il conteggio delle parole sbaglia.

```python
with CorpusDB() as db:
    print(db.correction_stats())
# {'segments': 429, 'corrected_segments': 0, 'corrected_share': 0.0, ...}
```

Quel numero serve a una cosa sola: accorgersi che un corpus «corretto
al 12%» è un corpus su cui il conteggio delle parole continua a
sbagliare per l'88% restante. Non dice se il correttore è bravo —
quello si giudica guardando le coppie originale/corretto.

Nei **file pubblicati**: `transcript.corrected.txt`,
`transcript.corrected.srt` e `segments.corrected.jsonl` finiscono
nella repo privata accanto agli originali. Senza, su GitHub si
continuerebbe a leggere il testo impreciso, che è il difetto che si
voleva chiudere. `transcript.txt` non viene mai sovrascritto: i due
errori devono restare entrambi visibili.

La **sorpresa utile**: i timestamp restano validi sul testo corretto.
L'i-esima parola del testo corretto è l'i-esima parola che l'ASR ha
collocato nel tempo, e funziona *proprio perché* il numero di parole
non può cambiare. È il regalo che fa la regola più stringente del
modulo: se i due elenchi avessero lunghezze diverse, i tempi
finirebbero addosso alle parole sbagliate — un errore invisibile,
perché il numero ci sarebbe e sembrerebbe giusto.

Per ricalcolare il database dopo una correzione:

```bash
python publish_corpus.py reindex
```

---

`speaker_local` (ID della sessione) e `speaker_names` (nome umano se
assegnato, **in locale**). La soglia di match è `match_threshold` in
`core/config.py` (default 0.78): più alta = più conservativo. Sopra la
soglia una voce è considerata nuova persona e nasce un ID nuovo.

`core/speaker_sync.py` allinea i nomi su tutto il materiale già scritto:
`review_speakers.py name`, `merge` e `sync` lo chiamano. Senza, un
rename vale solo per le sessioni successive e `corpus.db` continua a
restituire pseudonimi per voci che da settimane hanno un nome.

> `data/speakers_db.json` contiene embedding vocali, che sono
> identificatori biometrici: resta in locale e non va nel repo. Se lo
> perdi non si perde nulla — si ricostruisce riprocessando i file.### Test

```bash
python tests/test_speaker_db.py        # matching cross-file delle voci
python tests/test_quality.py           # flag di qualità della trascrizione
python tests/test_device_pipeline.py   # device, denoise, corpus, archivio, cache WAV, nomi
python tests/test_nightly.py           # piano notturno, finestra, carico termico
python tests/test_publish.py           # pubblicazione e controllo privacy
```

Girano tutti con embedding e file sintetici: nessun modello, nessun
audio, nessuna rete, pochi secondi. `test_publish.py` crea un clone git
vero con un **remoto locale** in una directory temporanea, quindi
`git add`, commit e push vengono eseguiti per davvero senza toccare
GitHub né `corpus_repo/`.

### Esempio transcript.txt

```
[00:00:04 → 00:00:12] SPEAKER_00
Buongiorno a tutti, oggi parleremo del progetto...

[00:00:12 → 00:00:28] SPEAKER_01
Grazie per l'introduzione. Come dicevo ieri...
```

### Metadati prosodici (prosody.csv)

| Colonna | Descrizione |
|---|---|
| `f0_mean_hz` | Pitch medio (Hz) — tono della voce |
| `f0_range_hz` | Range intonativo — espressività |
| `f0_std_hz` | Variabilità del pitch |
| `intensity_mean_db` | Intensità media (dB) |
| `voiced_fraction` | % audio vocalizzato |
| `jitter_local` | Variabilità ciclo-per-ciclo pitch (stress vocale) |
| `shimmer_local` | Variabilità ampiezza (affaticamento vocale) |
| `speech_rate_syl_per_sec` | Sillabe/secondo (velocità del parlato) |
| `pause_ratio` | % silenzio nel segmento |

---

## Configurazione avanzata

Tutti i parametri sono in `core/config.py`:

```python
# Cambia modello ASR
cfg.asr.model_id = "mlx-community/whisper-large-v3-mlx-q8"  # più accurato, 3GB

# Forza numero di speaker
cfg.diarization.num_speakers = 3

# Worker CPU per la prosodia. Il default è 2, e alzarlo insieme all'ASR
# è il modo più rapido per scaldare la macchina: sono due carichi paralleli.
cfg.prosody.num_workers = 3

# Timeout per singolo file
cfg.max_runtime_sec = 10800
```

I parametri **della pianificazione** stanno in cima allo stesso file e
non in una dataclass: orario, durata della finestra, thread, priorità e
pausa di respiro.

```python
NIGHT_WINDOW_SEC   = 4 * 3600    # 02:00 → 06:00
NIGHT_THREADS      = 4           # i core performance: 4 è più veloce di 8
NIGHT_NICE         = 10
NIGHT_COOLDOWN_SEC = 90          # 0 per non fermarsi mai
MAX_THREADS        = 4           # tetto misurato, verificato da un test
```

---

## Test: come provarlo senza il registratore

Un solo comando:

```bash
python tests/run_all.py            # test veloci, ~13 secondi
python tests/run_all.py --full     # anche il ciclo completo, ~2 minuti
```

**225 test su 11 suite**, e nessuno aspetta l'orologio di parete: i tempi
di attesa sono registrati e confrontati, non dormiti. Prima che fosse
così, due test aspettavano davvero l'attesa del backoff — 165 secondi,
per un totale di quasi tre minuti — senza verificare nulla che non
fosse già verificato.

Il percorso completo di `correct_text.py` è provato per intero, non
solo il modulo: sette test lo eseguono in una cartella a caso con un
modello finto al posto di Gemini, e controllano che senza `--consent`
non esca niente, che i giri interrotti non perdano il lavoro precedente,
che le parole che il filtro protegge restino tali **nel testo scritto
sul disco** e non solo nel riepilogo, e che il report `--solo-proposte`
cambi quello che si vede senza cambiare quello che si scrive.

`--full` è quello che conta quando qualcosa è cambiato: costruisce un
**registratore finto** e ci fa girare la catena vera. L'audio non è un
beep — è voce italiana vera, sintetizzata con `say` e poi sporcata di
rumore, perché un beep non attraversa VAD, diarizzazione e prosodia
come una voce e un test fatto di beep passerebbe senza provare niente.

Cosa viene provato, in sette passi: il device viene riconosciuto, i nomi
sporchi respinti (`untitled.mp3`, `00000001_000000.MP3`, `99999932`),
il piano a secco non tocca nulla, un file attraversa tutta la catena e
viene cancellato **solo dopo** la verifica, l'orario di registrazione
finisce in `session.json`, la spazzatura viene lasciata stare, due file
con lo stesso orario vengono entrambi trascritti, e rilanciare non
rifà il lavoro già fatto.

Tutto avviene in una directory temporanea: `A2T_ROOT_DIR` sposta output,
database e log, quindi i test non toccano la produzione.

Per guardare dentro senza eseguire:

```bash
python tests/make_fake_device.py --out /tmp/rec --count 5 --minutes 1 --edge
python sync_device.py detect --mounts /tmp
python sync_device.py pull --source /tmp/rec --dry-run
```

I file veri del registratore si provano con un estratto breve, così si
vede la qualità della trascrizione senza aspettare un'ora di elaborazione:

```bash
ffmpeg -i input/2026-10-02_22-44-20.MP3 -t 180 /tmp/rec/record/2026-10-02_19-42-33.MP3
python sync_device.py pull --source /tmp/rec
```

---

## Struttura del codice

```
audio-to-text/
├── run.py                  # entrypoint CLI della pipeline
├── nightly.py              # ciclo notturno: importa, elabora, pubblica
├── sync_device.py          # scarico dal registratore, trascrizione della coda, archivio
├── publish_corpus.py       # pubblicazione sulla repo privata del corpus
├── review_speakers.py      # chi è chi: nomi, merge, split, sync, matrice, ascolto
├── correct_text.py         # correzione delle parole con un LLM (serve --consent)
├── thermal_probe.py        # misura il riscaldamento durante una run
├── setup_env.sh            # installa dipendenze
├── setup_launchd.py        # scheduling notturno macOS
├── core/
│   ├── config.py           # tutti i parametri, compresa la pianificazione
│   ├── cost.py             # modello di costo della pipeline (tarato su misure)
│   ├── quality.py          # qualità della trascrizione, per segmento
│   ├── checkpoint.py       # persistenza stato per ripresa
│   ├── device.py           # rilevamento registratore e orario nei nomi file
│   ├── speaker_db.py       # identità vocali persistenti cross-file
│   ├── speaker_sync.py     # allineamento dei nomi al materiale già scritto
│   ├── speakers_merge.py   # fusione dei cluster troppo brevi
│   ├── voice_matrix.py     # somiglianza fra voci, per campione e per coppia di voci
│   ├── voice_review.py     # estratti da ascoltare e voci da rivedere
│   ├── scarico.py          # copia verificata dal registratore alla coda locale
│   ├── text_correction.py  # correzione del testo (Ollama locale o Gemini), affiancata
│   └── corpus_db.py        # indice SQLite locale per le analisi
├── pipeline/
│   ├── vad.py              # Voice Activity Detection
│   ├── transcriber.py      # ASR (mlx-whisper / faster-whisper)
│   ├── diarizer.py         # diarizzazione speaker (pyannote)
│   ├── denoise.py          # pulizia frusci + scelta automatica variante
│   ├── prosody.py          # analisi prosodia (Parselmouth + librosa)
│   └── assembler.py        # assemblaggio output (JSON/TXT/SRT/CSV/JSONL/MD)
├── tests/                  # test + generatore di registratore finto
│   ├── run_all.py          # un comando per eseguire tutto
│   ├── make_fake_device.py # crea un registratore finto, con voce vera
│   ├── test_e2e.py         # ciclo completo su device finto
│   ├── test_quality.py     # flag di qualità della trascrizione
│   ├── test_publish.py     # pubblicazione e controllo privacy
│   └── test_nightly.py     # piano, budget, coda, finestra, carico termico
├── input/                  # file audio/video; input/coda/ = copie dal registratore
├── output/                 # risultati
├── archive/                # originali in attesa di purga (7 giorni)
├── data/                   # database voci, corpus, estratti audio (gitignored)
└── logs/                   # log di esecuzione e manifest del device
```

---

## Risoluzione problemi

**"mlx_whisper non trovato"**  
→ Esegui `./setup_env.sh`

**"Token Hugging Face non trovato"**  
→ Vedi sezione "Token Hugging Face" sopra  
→ Oppure usa `--no-diarization` per saltare la diarizzazione

**"ffmpeg non trovato"**  
→ `brew install ffmpeg`

**Il job notturno non si avvia**  
→ `python setup_launchd.py status` per vedere i log  
→ Verifica che il Mac non sia in modalità "Non disturbare" con blocco accesso disco

**Vuoi riprendere da dove si era fermato**  
→ La pipeline riprende automaticamente: riesegui lo stesso comando

---

## Licenze

- [mlx-whisper](https://github.com/ml-explore/mlx-examples) — MIT
- [faster-whisper](https://github.com/SYSTRAN/faster-whisper) — MIT
- [pyannote.audio](https://github.com/pyannote/pyannote-audio) — MIT
- [praat-parselmouth](https://github.com/YannickJadoul/Parselmouth) — GPL-3.0
- [librosa](https://librosa.org) — ISC
- [whisper](https://github.com/openai/whisper) — MIT (modello OpenAI)
