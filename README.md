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

Ogni notte alle **02:00** la pipeline si avvia, importa dal registratore,
elabora entro un budget di **4 ore** e pubblica il corpus. Viene
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
sta in [`core/cost.py`](core/cost.py) e separa le due cose. È tarato
sulle registrazioni vere e prevede il tempo entro l'1% della misura.

Su quattro registrazioni reali del registratore (209 minuti, 3 parlanti,
SNR 18–30 dB) il rapporto di parlato misurato è **65%**.

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
python review_speakers.py voices               # ogni voce, e dove l'hai sentita
python review_speakers.py voices --tutto       # anche tutte le coppie
```

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

Quando il registratore è collegato, tutto il ciclo è in un comando:

```bash
python nightly.py --dry-run           # piano della notte: quanti file entrano
python nightly.py                     # ciclo completo: importa, elabora, pubblica
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
produrre nulla. Quello che non entra resta sul device e riparte dalla
stessa condizione la notte dopo.

Il ciclo non finisce finché c'è un file da elaborare: tra l'uno e
l'altro c'è una **pausa di respiro** (`--cooldown-sec`, 90 s di notte,
30 s di giorno) e il ciclo è a `nice 10`. Il costo in tempo è dichiarato
e voluto: la coda avanza un po' meno, la macchina resta usabile il
giorno dopo.

I **WAV derivati** che la pipeline usa per il VAD stanno in
`data/wav_cache/` e vengono cancellati appena la sessione è finita —
ma **non** se il checkpoint è a metà, perché quello serve per riprendere
dal chunk interrotto. Erano ~4 GB al giorno e non finivano mai da soli.

I file **non** vengono copiati prima di essere elaborati: vengono letti
dove sono. Il registratore resta la fonte di verità finche il lavoro non
è finito, e la cancellazione è l'ultimo atto:

```
elboro → verifico l'output → archivio in locale → cancello dal device
```

Se una passaggio fallisce, **il file resta sul device**. Non esiste un
percorso in cui un file viene cancellato senza che la trascrizione esista
e sia stata verificata (`transcript.json` presente, con segmenti e almeno
poche parole). Ogni file toccato finisce in `logs/device_manifest.jsonl`
con hash ed esito.

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
python publish_corpus.py push              # pubblica le sessioni nuove
python publish_corpus.py push --with-names  # pubblica anche i nomi reali
python publish_corpus.py status            # cosa c'è e cosa manca
python publish_corpus.py reindex           # ricostruisce il database locale
```

`reindex` non serve nel caso normale — la pipeline aggiorna il database
appena finisce una sessione. Serve dopo un rilascio che cambia come si
scrive l'output, dopo un restore, e per riparare un database indietro
senza rielaborare nulla. Allinea anche i nomi alla fonte e toglie dalla
tabella dei parlanti le voci che nessuna sessione cita più.

Sulla repo **non** finiscono mai: audio, embedding vocali, i database
locali, i checkpoint. Non è una scelta di comodità: testo, prosodia e
statistiche parlarie insieme ricostruiscono un profilo che nessun file
rivela da solo.

**I nomi reali dei parlanti** stanno in `transcript.json` e
`session.json` in locale — è materiale che resta sulla tua macchina.
Sulla repo, per default, vengono sostituiti dagli pseudonimi: un accesso
alla repo non dà l'identità. `--with-names` li pubblica, ed è una
decisione che va presa **a ogni push**, non un'impostazione da
dimenticare: pubblicare nomi veri non si richiama.

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
├── tokens.jsonl         # una parola per riga con timestamp — KWIC, n-grammi
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
3. Comparirà una chiave che comincia per `AIza`. **Copiala subito**:
   Google la mostra una volta sola e non si può rivedere dopo.
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

> **Il modello conta.** Il default è `gemini-3.8-flash`. I modelli
> `2.5` sono ormai accessibili solo agli account che li avevano già
> usati in passato, quindi per un account nuovo la chiamata si ferma con
> un errore che non spiega nulla — è successo, ed è il motivo per cui
> l'errore adesso nomina i modelli che funzionano. Per una passata
> lunga: `--model gemini-3.5-flash-lite`.

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
python tests/run_all.py            # test veloci, ~10 secondi
python tests/run_all.py --full     # anche il ciclo completo, ~2 minuti
```

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
├── sync_device.py          # import dal registratore + cancellazione sicura
├── publish_corpus.py       # pubblicazione sulla repo privata del corpus
├── review_speakers.py      # chi è chi: nomi, merge, split, sync, matrice
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
│   ├── voice_matrix.py     # somiglianza fra voci di sessioni diverse
│   ├── text_correction.py  # correzione del testo con Gemini, affiancata
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
├── input/                  # metti qui i file audio/video
├── output/                 # risultati
├── archive/                # originali in attesa di purga (7 giorni)
├── data/                   # database voci e corpus (biometrico, gitignored)
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
