# Analisi del corpus e pannello web — specifica

Stato al 9 ottobre 2026. Documento di progetto del Project «Psico-fisio
app», copiato anche nella repo `pietrofabbri/audio-to-text` in
`docs/analisi-corpus.md` (le due copie sono identiche).
Collegati: `ROADMAP.md` (ordine dei lavori della pipeline, decisioni
D1–D5), `hardware-registrazione.md` (TileRec, saturazione, posizione
d'uso; solo nel Project), `APERTI.md` nella repo (punti 36 e 37), README
della repo, sezione «Il pannello cifrato».

Questo documento dice **cosa** analizzare nel corpus, **come** (regole di
calcolo e di confronto), **come mostrarlo** (pannello web) e **dove
pubblicarlo**. È scritto per essere usato senza il contesto della
conversazione in cui è nato.

---

## 1. Scopo

Leggere in modo molto leggibile, man mano che si accumulano, i dati raccolti
dal diario audio di Pietro (registratore TileRec, parlato quotidiano con
consenso delle persone coinvolte) e dalle misure del braccialetto Amazfit
Helio Strap Pro, per capire come vive, parla, si relaziona e sta nel corpo, e
quali legami ci sono tra queste dimensioni.

Il soggetto delle analisi psicologiche è **solo Pietro** (`GLOBAL_001`, voce
presente in 18 sessioni su 19, nominata «Pietro» l'8 ottobre). Delle altre
persone si misurano solo dati di relazione (tempi, turni, temi condivisi),
mai indicatori del loro stato interiore: hanno acconsentito a essere
registrate, non a essere profilate.

I nomi delle persone **non si censurano**: nel corpus privato e nel
pannello cifrato compaiono come sono (decisione D1).

---

## 2. Fonti dei dati

| Fonte | Contenuto | Dove | Stato al 9/10 |
|---|---|---|---|
| Corpus testuale | Per giorno: `segments.jsonl`, `tokens.jsonl`, `prosody.csv`, `wordfreq.csv`, `transcript.*`, `giorno.json` (manifesto: file sorgenti, offset, blocchi continui, voci) | repo privata `pietrofabbri/corpus`, cartella `giorni/` | 4 giorni (2, 4, 5, 8 ottobre) |
| Metriche aggregate | Per giorno, `metriche/AAAA-MM-GG.json` (sezione 9) | repo privata del corpus | **calcolate dal 9/10** a ogni pubblicazione, dopo l'aggiornamento del codice sul Mac |
| Database locale | Voci (`GLOBAL_xxx`, nomi), campioni, audio | solo sul Mac | primi nomi assegnati l'8/10; revisione completa il 14/10 |
| Testo corretto | `*.corrected.*` prodotti da `correct_text.py` | corpus | **pronto, da accendere**: correzione con modello locale (Ollama, D2 del 9/10); serve installare Ollama e il modello sul Mac |
| Helio | Misure giornaliere e infragiornaliere (export Zepp). Atteso, da verificare sul primo export: battito, battito a riposo, HRV, stress, sonno (durata, fasi), passi/attività, eventuale indice di recupero | entrerà nel corpus | **non ancora collegato** |
| Voto serale | Valutazione soggettiva della giornata (1–10 + una parola) | da definire | **non esiste ancora**: meccanismo di raccolta da progettare |
| Variabili esterne | Diario di meditazione, attività fisica, alimentazione | altri progetti di Pietro | da collegare, facoltative |

Vincolo strutturale: **la registrazione è solo diurna e non costante** (non
tutte le ore, non tutti i giorni). L'Helio invece misura 24 ore su 24.
Vedi sezione 5.

---

## 3. Direttrici di analisi

Numerazione stabile (usata nella conversazione di progetto). Le direttrici
escluse restano elencate per non riproporle. La colonna «Nel pannello»
dice cosa è già calcolato al 9/10.

### Scelte

| # | Direttrice | Metriche principali | Modello linguistico? | Nel pannello |
|---|---|---|---|---|
| 1 | **La giornata nel tempo** | ore registrate, copertura per fascia oraria, % di parlato, blocchi continui, numero di voci | no | **sì** |
| 2 | **Relazioni** | minuti per persona; quota di parola di Pietro per conversazione; durata media dei turni; chi apre e chi chiude; sovrapposizioni e interruzioni | no | **sì**, tranne sovrapposizioni |
| 3 | **Contenuti e temi** | % di tempo per tema (sezione 7), separata in *esposizione* (tutto il parlato udito) e *parlato proprio*; parole caratteristiche del periodo rispetto allo storico; persone e luoghi nominati | sì (temi); no (parole caratteristiche) | no |
| 4 | **Come parla Pietro** | prosodia: velocità, altezza media e sua variabilità, energia, pause; marcatori linguistici: io/noi, domande vs affermazioni, negazioni, lessico emotivo, tempi verbali | no (dizionari locali) | **sì**, tranne lessico emotivo e tempi verbali |
| 5 | **Il corpo (Helio)** | battito, HRV, stress, sonno, passi; correlazioni con le altre direttrici (sezione 6) | no | no |
| 6 | **Qualità del dato** | copertura, % campioni a fondo scala (saturazione), SNR, confidenza ASR, % di testo corretto | no | **sì**, tranne saturazione e SNR |
| 9 | **Silenzio e solitudine** | minuti registrati senza parlato; minuti con sola voce di Pietro; presenza di musica o TV | no (classificatore di eventi sonori locale) | **sì**, tranne musica/TV |
| 10 | **Stile e lessico** | ricchezza lessicale (MATTR, finestra 50 parole), intercalari ogni 1000 parole, registro, dialetto e altre lingue; differenze per interlocutore | in parte | **in parte** (MATTR, intercalari) |
| 11 | **Risate e tono emotivo** | risate per ora registrata, per persona e per fascia oraria; tono emotivo per tratto di conversazione | no (risate dall'audio); sì (tono dal testo) | no |
| 12 | **Ascolto** | rapporto domande/affermazioni; riprese di parole dell'interlocutore; quota di parola | in parte | **in parte** (domande, quota) |
| 13 | **Impegni e decisioni** | elenco di promesse e decisioni («domani ti mando…», «allora facciamo così»), con data, ora e persona | sì | no |
| 14 | **Storie e ricordi** | episodi raccontati, storie ripetute e a chi, persone nominate ma assenti | sì | no |
| 15 | **Orari, routine e anomalie** | trasversale: profilo per ora del giorno di ogni indicatore («giornata tipo»), cosa fa scaturire ogni fascia oraria, scarti dalla giornata tipo segnalati come anomalie | no | **in parte** (profili orari del giorno) |
| 16 | **Variabili esterne** | meditazione, attività fisica, alimentazione, come assi aggiuntivi delle correlazioni | no | no |
| 17 | **Voto serale** | 1–10 + una parola; usato come metro per validare gli indicatori automatici | no | no |

### Escluse

- 7 — Luoghi e ambienti sonori (casa, auto, aula…): **no**.
- 8 — Ambiti di vita come etichetta del momento (scuola, coppia, band…): **no**.
  Nota: i temi della direttrice 3 classificano *di cosa si parla*, non *in
  che situazione ci si trova*; le due cose restano distinte.

### Dipendenze

- Direttrici 3 (temi), 11 (tono dal testo), 13, 14 richiedono un modello
  linguistico. Dal 9/10 il modello è **locale** (Ollama sul Mac, lo stesso
  del correttore): gratuito, e il testo non esce dal computer, quindi non
  serve un consenso ulteriore all'invio. Resta da misurare se un modello
  locale classifica abbastanza bene.
- Direttrice 2 e i tagli per persona diventano pienamente leggibili dopo
  l'assegnazione dei nomi alle voci (Fase 1, revisione completa fissata al
  14 ottobre).
- Le metriche lessicali (3, 4, 10) sono falsate dagli errori di trascrizione
  finché il testo non è corretto: il pannello lo segnala (direttrice 6) e,
  quando esiste `segments.corrected.jsonl`, le calcola sul testo corretto.
- Le metriche di energia e volume (4, 11) sono falsate dalla saturazione del
  TileRec (`hardware-registrazione.md`).

---

## 4. Tagli

### Temporali

Ogni direttrice si guarda su sei scale: **giorno, settimana, mese, stagione,
anno, totale**. Una scala si mostra solo quando ha abbastanza dati; altrimenti
il pannello scrive «dati insufficienti» invece di mostrare tendenze false.
Soglie iniziali (in `pannello/costruisci.py`, `SOGLIE`), da rivedere con l'uso:

| Scala | Minimo per mostrarla |
|---|---|
| Giorno | 1 giorno; entra nei confronti solo con copertura ≥ 3 h |
| Settimana | ≥ 4 giorni validi |
| Mese | ≥ 12 giorni validi |
| Stagione | ≥ 6 settimane con dati |
| Anno | ≥ 9 mesi con dati |
| Totale | sempre |

### Non temporali

- **Per persona**: tutto ciò che riguarda la relazione con una voce.
- **Per fascia oraria**: trasversale a tutto (direttrice 15).

---

## 5. Copertura: regole di calcolo

1. **Maschera di copertura.** Ogni minuto di ogni giorno è marcato
   *registrato* o *non registrato* (da `giorno.json`: inizio e fine di ogni
   file sorgente).
2. **Normalizzazione.** Le metriche audio si esprimono per ora registrata
   (parole/ora, intercalari/1000 parole, minuti per tema in %), mai come
   totali del giorno.
3. **Correlazioni solo sull'intersezione.** Le correlazioni audio↔Helio usano
   solo gli intervalli in cui ci sono entrambi.
4. **Giorni validi.** Un giorno entra nei confronti e nelle correlazioni
   giornaliere solo con copertura ≥ 3 h (`ORE_MINIME_VALIDO` in
   `core/metriche.py`). I giorni esclusi sono elencati nella pagina.
5. **Stesse ore a confronto.** I confronti tra giorni si fanno per fascia
   oraria, perché le ore registrate cambiano da un giorno all'altro e
   l'orario confonderebbe il confronto.
6. **Helio sempre completo.** Ha un pannello proprio sulle 24 ore,
   indipendente dalla copertura audio.
7. **Notte → giorno dopo.** Le relazioni che partono dal sonno sono le più
   affidabili (il sonno c'è sempre; serve solo qualche ora registrata il
   giorno dopo) e hanno priorità. Le ipotesi che richiedono registrazioni
   serali sono di seconda fila.

---

## 6. Correlazioni

Assi: **Corpo** (Helio), **Voce** (prosodia, risate), **Parole** (stile,
lessico, temi), **Relazione** (persone, turni, ascolto), **Tempo** (orario,
giorno della settimana, periodo scolastico), **Soggettivo** (voto serale),
**Esterni** (meditazione, attività, alimentazione).

Forza attesa: **forte** = meccanismo noto e ben documentato; **media**;
**esplorativa** = plausibile, tutta da scoprire nei dati di Pietro.

### Scala dei minuti (dentro un momento, solo dove c'è registrazione)

| Ipotesi | Attesa |
|---|---|
| Parlare ↔ battito più alto | forte |
| Risata ↔ picco di battito subito dopo | forte |
| Voce più acuta e più forte ↔ stress Helio nello stesso intervallo | media |
| Interruzioni e sovrapposizioni ↔ stress | media |
| Conversazione con una data persona ↔ stress durante e dopo | esplorativa |
| Silenzio da solo ↔ recupero dell'HRV nei 30 minuti successivi | esplorativa |

Artefatto da escludere: TileRec e Helio stanno sullo stesso braccio. Un
movimento brusco può produrre insieme un picco audio e un'alterazione del
battito. Gli intervalli con movimento intenso (da accelerometro Helio, se
disponibile) o con saturazione vanno esclusi o segnalati.

### Scala del giorno (sfasamenti di 0, 1, 2 giorni)

| Ipotesi | Attesa | Priorità |
|---|---|---|
| Sonno scarso ↔ voce più piatta, eloquio più lento, meno risate | forte | alta (notte→giorno) |
| Sonno scarso ↔ voto serale più basso | forte | alta |
| Sonno scarso ↔ più intercalari, lessico più povero | media | alta |
| Movimento del giorno ↔ voto serale e risate | media | alta |
| Meditazione ↔ meno stress, più pause, più ascolto | esplorativa | alta |
| Ascolto alto ↔ voto serale | esplorativa | media |
| Molte ore di socialità ↔ HRV serale più bassa, sonno più breve | esplorativa | media |
| Conversazioni dopo le 22 ↔ addormentamento più lento | media | bassa (servono registrazioni serali) |

### Scala settimana, mese, stagione

| Ipotesi | Attesa |
|---|---|
| Feriali/festivi ↔ tutto il resto (da trattare come fattore di disturbo da togliere) | forte |
| Periodi scolastici (inizio anno, scrutini, vacanze) ↔ stress, voce, temi | media |
| Luce e stagione ↔ voto serale, risate, sonno | media |
| Carico sociale della settimana ↔ recupero (HRV media, sonno) | esplorativa |
| Temi ricorrenti del periodo ↔ stress del periodo | esplorativa |

### Regole di metodo

1. **Ritmo circadiano.** Battito, voce e umore seguono l'orologio. Ogni
   confronto si fa a parità di fascia oraria; altrimenti molte correlazioni
   risultano forti solo perché entrambe le grandezze seguono l'ora.
2. **Quantità di dati.** A scala giornaliera ogni giorno è un punto. Un legame
   medio richiede circa 30–60 giorni validi; uno debole, mesi.
3. **Troppi confronti.** Le ipotesi delle tabelle sono dichiarate in anticipo
   e si *verificano*. Ogni altra correlazione trovata esplorando è mostrata
   come «da confermare» finché non si ripresenta nel periodo successivo.
4. **Correlazione non è causa.** Per i legami importanti si possono fare
   esperimenti personali (es. una settimana con meditazione serale e una
   senza).
5. **Validazione con il voto serale.** Un indicatore automatico che non si
   muove mai insieme al voto serale probabilmente misura altro; merita meno
   spazio nei pannelli.

---

## 7. Gerarchia dei temi (direttrice 3) — versione 1

Unità classificata: il tratto di conversazione su un tema (qualche minuto),
non la singola frase. Le percentuali si calcolano in minuti, due volte:
*esposizione* (tutto il parlato udito) e *parlato proprio* (solo Pietro).
Ogni tratto ha un tema principale (usato per le percentuali) e può averne di
secondari.

1. **Lavoro e scuola** — didattica e lezioni; colleghi e organizzazione;
   studenti (solo in forma aggregata); burocrazia e norme
2. **Studio e formazione** — psicologia e tirocinio; corsi; letture di studio
3. **Relazioni** — coppia; famiglia; amicizie; dinamiche e conflitti; persone
   nuove
4. **Vita pratica** — casa; cucina e cibo; spesa; spostamenti; appuntamenti e
   pianificazione; soldi e acquisti
5. **Corpo e benessere** — sonno; movimento; alimentazione; salute
6. **Pratiche interiori** — meditazione; yoga e tai chi; spiritualità e senso
7. **Progetti personali** — questa app e la tecnologia; il sito sulla
   meditazione; altri progetti
8. **Musica e arti** — band; suonare; ascoltare; ballo; teatro
9. **Svago e cultura** — film e serie; libri; viaggi; natura e piante; giochi
10. **Società e attualità** — politica; notizie; economia
11. **Chiacchiera di contesto** — saluti; meteo; convenevoli; scambi di
    servizio (bar, negozi)
12. **Non classificabile o non ancora identificato** — frammenti; TV o radio di
    sfondo; **argomenti reali non ancora presenti nella gerarchia**

Regole:

- **Emozioni e vissuti non sono un tema**: sono un asse trasversale (tono,
  direttrice 11), perché attraversano tutti i temi.
- **Evoluzione.** Ogni mese i tratti finiti in 12 come «non ancora
  identificati» vengono raggruppati per somiglianza e proposti a Pietro come
  nuove sottocategorie (o nuovi temi). La gerarchia ha un numero di versione;
  quando cambia, le percentuali storiche si ricalcolano con la nuova versione
  e il pannello indica quale versione usa.
- **Classificazione**: con il modello linguistico locale (sezione 3,
  «Dipendenze»).

---

## 8. Pannello web

### Struttura

Un selettore di scala (giorno → totale) in alto vale per tutte le pagine.
Ogni numero è confrontato con la mediana personale di Pietro sui giorni
validi, così «alto» e «basso» hanno un significato. Tema chiaro/scuro
automatico, con scelta manuale.

Cinque pannelli di sintesi (obiettivo):

1. **Il polso** — una schermata: copertura, sonno, voto serale, quanto ha
   parlato, con chi, i tre temi principali, risate, anomalie del periodo.
2. **Corpo e voce** — Helio, prosodia e risate, sovrapposti sull'asse delle ore.
3. **Persone** — con chi, quanto, distribuzione dello scambio, ascolto.
4. **Temi** — percentuali per tema, andamento, impegni e storie emersi.
5. **Tempo** — giornata tipo per ogni indicatore, routine, anomalie.

Più: una pagina **Correlazioni** (mappa di calore per scala; elenco delle più
forti con etichetta «confermata» / «da confermare»; grafico a punti con un
giorno per punto e grafico degli sfasamenti per le coppie scelte) e, sotto
ogni pannello, il dettaglio delle singole metriche.

Ogni pagina dichiara i limiti dei dati che mostra: copertura, giorni esclusi,
testo corretto o no, versione della gerarchia dei temi.

**Prima versione (9/10), già costruita** e provata sui 4 giorni veri:
- *Il polso*, con riquadri dichiarati «in attesa» per sonno, voto serale,
  temi e risate;
- *La giornata nel tempo*: striscia delle 24 ore e minuti per ora, divisi
  in silenzio, Pietro da solo, con altri;
- *Persone*: minuti per voce, conversazioni, chi apre e chi chiude, cambi
  di turno;
- *Come parli*: tono, pause, ricchezza lessicale, intercalari, io/noi,
  negazioni, profili orari;
- *Qualità del dato e limiti*;
- *Tutti i giorni*: una tabella.

### Pubblicazione — decisione D5: **GitHub Pages cifrato** (presa l'8/10)

Motivo: GitHub Pages pubblica siti privati solo con GitHub Enterprise Cloud;
con gli altri piani il sito è pubblico anche se la repo è privata. Il
pannello contiene dati su altre persone e sulla salute, quindi il contenuto
deve essere cifrato.

Repo pubblica di pubblicazione: nome neutro, che non dica cosa contiene:
**`pietrofabbri/taccuino`** (da creare a cura di Pietro). Titolo della
pagina: «Taccuino».

Architettura:

```
Mac (a ogni pubblicazione)        repo privata pietrofabbri/corpus
  core/metriche.py               ──push──▶  metriche/*.json
  + workflow pannello.yml                   .github/workflows/pannello.yml
                                               │
                                     GitHub Actions (al push di metriche/)
                                               │ 1. pannello/costruisci.py: una pagina
                                               │    con i dati INCORPORATI
                                               │ 2. StatiCrypt la cifra
                                               ▼
                                  repo pubblica pietrofabbri/taccuino
                                  ramo gh-pages (riscritto da zero) → GitHub Pages
```

Il codice del pannello sta nella repo `audio-to-text` (`core/metriche.py`,
`pannello/costruisci.py`, `pannello/modello.html`, `pannello/pannello.yml`);
il workflow lo scarica da lì a ogni build.

Regole di sicurezza (tutte implementate):

1. **Il calcolo resta sul Mac.** Actions disegna soltanto, a partire da
   metriche già aggregate. Le metriche non contengono testo delle
   conversazioni.
2. **Nessun file di dati in chiaro.** I dati sono incorporati nella pagina
   prima della cifratura; il workflow verifica che il sito contenga solo
   `index.html` cifrata, `robots.txt` e `.nojekyll`.
3. **Frase d'accesso robusta** (almeno 20 caratteri, meglio cinque o sei
   parole casuali), conservata solo come segreto di GitHub Actions nella
   repo privata; la imposta Pietro, non compare mai nel codice, nei log o
   nei documenti. «Ricordami per 30 giorni» disponibile sul dispositivo.
4. **Storia senza versioni vecchie.** Il ramo di pubblicazione viene
   riscritto da zero a ogni build (un solo commit).
5. **Niente di sensibile in chiaro** nei nomi dei file, nei titoli, negli
   indirizzi; `robots.txt` che chiede di non indicizzare.
6. **Accesso tra repo.** Actions nella repo privata scrive su quella pubblica
   con un token fine-grained con permesso *Contents: read and write* solo su
   quella repo.
7. **Testi citati al minimo.** Impegni (13) e storie (14), quando ci
   saranno, mostreranno riassunti brevi, non trascrizioni estese.
8. Se Pietro registra a scuola, le ore con studenti minorenni vanno escluse o
   anonimizzate del tutto prima di qualsiasi analisi.

Verifica fatta il 9/10 sui dati veri: con una frase sbagliata la pagina non
si apre; con quella giusta compare il pannello; nel file pubblicato non
compare nessun nome né numero in chiaro.

### Configurazione (passi di Pietro, una volta sola)

1. Creare su GitHub la repo **pubblica** e vuota `taccuino`.
2. Creare un token fine-grained con accesso solo a `taccuino`, permesso
   *Contents: Read and write*.
3. Nella repo privata `corpus`, Settings → Secrets and variables → Actions:
   segreto `PANNELLO_PASSWORD` (la frase), segreto `PANNELLO_TOKEN` (il
   token), variabile `PANNELLO_REPO` = `pietrofabbri/taccuino`.
4. Sul Mac, `git pull` in `audio-to-text`: la pubblicazione successiva
   porta nel corpus metriche e workflow.
5. Dopo il primo giro del workflow: in `taccuino`, Settings → Pages →
   *Deploy from a branch*, ramo `gh-pages`, cartella `/ (root)`.
   Indirizzo: `https://pietrofabbri.github.io/taccuino/`.

Finché i segreti mancano, il workflow chiude in verde con un avviso e non
pubblica niente.

---

## 9. Formato delle metriche (`metriche/AAAA-MM-GG.json`, versione 1)

Calcolato da `core/metriche.py` (`calcola_giorno`). Campi principali:

- `giorno`, `giorno_settimana` (1 = lunedì), `valido` (copertura ≥ 3 h);
- `limiti`: testo grezzo o corretto, e ciò che non è ancora misurato;
- `copertura`: ore registrate e di parlato, quota di parlato, intervalli
  registrati in secondi dalla mezzanotte, blocchi, prima e ultima ora;
- `silenzio`: minuti senza parlato, con sola voce di Pietro, con altri
  (un minuto conta come parlato da una voce se quella voce vi parla
  almeno 1 secondo);
- `voci`: per voce id, nome, minuti, parole, turni, durata media del turno;
- `relazioni`: persone, quota di parola di Pietro, conversazioni (separate
  da almeno 60 s senza parlato), quota aperte e chiuse da Pietro, cambi di
  turno per ora di parlato. L'elenco dettagliato delle conversazioni resta
  nel corpus e non entra nella pagina;
- `pietro`: minuti; prosodia (mediane di f0, variabilità di f0, intensità,
  velocità in sillabe/s, quota di pause, escludendo i segmenti
  inaffidabili); parole (MATTR, quota di domande, io e noi ogni 1000,
  io/(io+noi), negazioni e intercalari ogni 1000, intercalari più
  frequenti);
- `qualita`: quota di segmenti affidabili e incerti, probabilità media
  delle parole di Whisper, quota di parole con probabilità < 0,5, testo
  corretto sì/no;
- `ore`: per ogni ora del giorno minuti registrati, di silenzio, di Pietro
  da solo, con altri, di parlato; parole di Pietro per ora registrata,
  velocità, tono e quota di domande di Pietro.

Un cambio di significato di un campo alza `versione`.

---

## 10. Ordine dei lavori

1. ~~Formato delle metriche~~ — **fatto il 9/10** (sezione 9).
2. ~~Pipeline Actions + StatiCrypt + Pages~~ — **codice fatto il 9/10**;
   restano i passi di configurazione di Pietro (sezione 8).
3. ~~Il polso sui dati senza modello linguistico né Helio~~ — **prima
   versione fatta il 9/10**.
4. **Correzione del testo con modello locale**: installare Ollama e il
   modello sul Mac, accendere la correzione notturna (ROADMAP, Fase 3).
5. **Collegamento Helio**: primo export, mappatura dei campi, pannello 24 h,
   prime correlazioni notte → giorno.
6. **Voto serale**: scegliere il meccanismo di raccolta.
7. **Risate e musica/TV**: un classificatore di eventi sonori locale
   sull'audio, sul Mac.
8. **Direttrici con modello linguistico** (3, 11 tono, 13, 14), con il
   modello locale.
9. **Pagina Correlazioni**, quando ci sono almeno 30 giorni validi.

## 11. Domande aperte

- Meccanismo di raccolta del voto serale (direttrice 17).
- Qualità del modello locale per correzione e temi: da misurare.
- Campi effettivamente esportabili dall'Helio e frequenza di campionamento.
- Soglie di copertura e minimi per scala: valori iniziali, da rivedere.
