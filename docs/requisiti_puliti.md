# Orario scolastico — requisiti risolti (input per lo script CP-SAT)

Specifica dei requisiti già disambiguata e con i controlli di coerenza
effettuati, così che lo script non debba reinterpretare un testo originale.
I dati numerici sono quelli del caso di esempio (`scripts/config.example.yaml`),
con docenti anonimi `Docente A`…`Docente E`. Per un caso reale si compila
`scripts/config.yaml` con la stessa struttura.

## 1. Griglia oraria

| Slot | Orario | Durata |
|---|---|---|
| s1 | 8:10–9:10 | 1h |
| s2 | 9:10–10:10 | 1h |
| intervallo | 10:10–10:40 | 0.5h |
| s3 | 10:40–11:40 | 1h |
| s4 | 11:40–12:40 | 1h |
| mensa | 12:40–13:45 | 1h* |
| p1 | 13:45–14:45 | 1h |
| p2 | 14:45–15:45 | 1h |

\* la finestra reale è di 65 minuti (12:40–13:45); ai fini del monte-ore viene
trattata come 1h. Discrepanza minore, non bloccante.

- **Lunedì, mercoledì, venerdì**: s1, s2, intervallo, s3, s4 (4 slot didattici, niente pomeriggio, niente mensa).
- **Martedì, giovedì**: s1, s2, intervallo, s3, s4, mensa, p1, p2 (6 slot didattici + mensa).

Ore settimanali per classe: didattica 4×3 + 6×2 = **24h**; + intervallo 5×0.5 = 2.5h;
+ mensa 2×1 = 2h → totale 28.5h. I vincoli derivano dalla griglia slot, non da
una riga riassuntiva.

Questa griglia è invariante ed è fissata nel codice (`scripts/data.py`); tutto
il resto è configurazione.

## 2. Classi e ore fisse per presenza di Esperti (HARD, non modificabili)

| Classe | Giorno | Slot | Materia | Note |
|---|---|---|---|---|
| 1ª | Lun | s3 | Religione | |
| 1ª | Lun | s4 | Religione | |
| 1ª | Gio | s1 | Inglese | **co-docenza con 2ª** (stessa aula/esperto, stesso slot) |
| 2ª | Gio | s1 | Inglese | co-docenza con 1ª |
| 2ª | Gio | s2 | Inglese | l'esperto copre anche l'intervallo successivo → **nessun titolare 2ª necessario per l'intervallo di quel giorno** |
| 2ª | Gio | s3 | Religione | **co-docenza con 3ª** |
| 2ª | Gio | s4 | Religione | co-docenza con 3ª |
| 3ª | Lun | s2 | Inglese | esperto copre anche l'intervallo successivo |
| 3ª | Mar | s3 | Inglese | |
| 3ª | Mar | s4 | Inglese | |
| 3ª | Gio | s3 | Religione | co-docenza con 2ª |
| 3ª | Gio | s4 | Religione | co-docenza con 2ª |
| 4ª | Lun | s4 | Inglese | |
| 4ª | Mar | s2 | Motoria | |
| 4ª | Gio | s3 | Inglese | |
| 4ª | Gio | s4 | Inglese | |
| 4ª | Gio | p1 | Religione | |
| 4ª | Gio | p2 | Religione | |
| 5ª | Lun | s1 | Religione | |
| 5ª | Lun | s2 | Religione | |
| 5ª | Lun | s3 | Inglese | |
| 5ª | Mar | s1 | Motoria | |
| 5ª | Gio | p1 | Inglese | |
| 5ª | Gio | p2 | Inglese | |

Queste ore sono coperte da esperti esterni: non vanno assegnate ai titolari e
non contano nel loro monte ore. Il modello deve però **bloccare** quegli slot
come "occupati" per la classe.

## 3. Ore titolari per classe (input diretto, HARD)

| Docente | Classe | Materia | Ore |
|---|---|---|---|
| Docente A | 1ª | Italiano | 6 |
| Docente A | 1ª | Storia | 1 |
| Docente A | 1ª | Geografia | 1 |
| Docente A | 1ª | Musica | 1 |
| Docente A | 1ª | Motoria | 1 |
| Docente A | 5ª | Italiano | 6 |
| Docente A | 5ª | Musica | 1 |
| Docente A | 5ª | Storia | 2 |
| Docente B | 2ª | Matematica | 6 |
| Docente B | 3ª | Matematica | 6 |
| Docente B | 3ª | Scienze | 1 |
| Docente B | 4ª | Matematica | 6 |
| Docente B | 4ª | Scienze | 1 |
| Docente C | 2ª | Italiano | 6 |
| Docente C | 3ª | Italiano | 6 |
| Docente C | 3ª | Storia | 2 |
| Docente C | 4ª | Italiano | 6 |
| Docente C | 4ª | Storia | 2 |
| Docente D | 1ª | Matematica | 6 |
| Docente D | 1ª | Scienze | 1 |
| Docente D | 1ª | Arte | 1 |
| Docente D | 5ª | Matematica | 6 |
| Docente D | 5ª | Scienze | 2 |
| Docente D | 5ª | Geografia | 1 |
| Docente E | 2ª | Scienze | 1 |
| Docente E | 2ª | Storia | 1 |
| Docente E | 2ª | Geografia | 1 |
| Docente E | 2ª | Musica | 1 |
| Docente E | 2ª | Arte | 1 |
| Docente E | 2ª/3ª | Motoria | 1 (co-docenza, **conta una sola volta**, stesso slot per entrambe le classi) |
| Docente E | 3ª | Geografia | 1 |
| Docente E | 3ª | Musica | 1 |
| Docente E | 3ª | Arte | 1 |
| Docente E | 4ª | Geografia | 1 |
| Docente E | 4ª | Musica | 1 |
| Docente E | 4ª | Arte | 1 |

## 4. Verifica di coerenza — monte ore per classe (tutte tornano a 24h)

| Classe | Titolari | Esperti | Motoria condivisa | Potenziamento | Totale |
|---|---|---|---|---|---|
| 1ª | Docente A 10 + Docente D 8 = 18 | 3 | — | **3** | 24 |
| 2ª | Docente B 6 + Docente C 6 + Docente E 6 = 18 | 4 | (incl. sopra) | **2** | 24 |
| 3ª | Docente B 7 + Docente C 8 + Docente E 3 = 18 | 5 | +1 | 0 | 24 |
| 4ª | Docente B 7 + Docente C 8 + Docente E 3 = 18 | 6 | — | 0 | 24 |
| 5ª | Docente A 9 + Docente D 9 = 18 | 6 | — | 0 | 24 |

Il potenziamento del docente `reinforcement` nelle ore non assegnate di 1ª e 2ª
corrisponde esattamente a questi buchi: **3h in 1ª + 2h in 2ª = 5h**.

## 5. Monte ore docenti (22h: didattica + assistenza) — verifica di fattibilità

| Docente | Ore didattica (titolari + potenziamento) | Ore assistenza necessarie |
|---|---|---|
| Docente A | 19 | 3 |
| Docente B | 20 | 2 |
| Docente C | 22 | **0 — nessun margine, non può ricevere turni di mensa/intervallo** |
| Docente D | 17 | 5 |
| Docente E | 17 (12 disciplinari + 5 potenziamento) | 5 |
| **Totale richiesto** | | **15h** |

Offerta di ore di assistenza disponibili:
- Intervallo: fino a 5 giorni × 5 classi × 0.5h potenziali, ma con "max 1
  intervallo per docente al giorno" ogni docente arriva al più a 2.5h/settimana.
- Mensa: 2 giorni × 2 docenti × 1h = 4h in tutto.

Il monte ore esatto di 22h per **ogni** docente non è simultaneamente
soddisfacibile: vedi `docs/DECISIONS.md`, voce REL-1. Il vincolo "docente
`teaching_only`: 0h di assistenza" va imposto esplicitamente come HARD derivato.

## 6. Disambiguazioni applicate

1. **Credito ore intervallo**: la sorveglianza dell'intervallo (0.5h) da parte
   del docente che ha già s2 o s3 in quella classe **conta come 0.5h di
   assistenza** ai fini del monte 22h. Senza questa regola il problema sarebbe
   infeasible.
2. **Uscita del docente `early_exit`**: "esce prima" è interpretato come "niente
   s4" → esce alle **11:40** (fine s3). Si applica: nei giorni configurati
   (`weighted_no_s4_days`, mercoledì e venerdì nell'esempio) e nel giorno tra i
   due con pomeriggio in cui non lavora al pomeriggio.
3. **"Un solo pomeriggio a docente"**: regola SOFT generale per tutti i
   docenti; per il docente `early_exit` diventa HARD — non è una
   contraddizione, è un'eccezione più stringente per un solo docente.
4. **Co-docenza**: alcuni slot (motoria 2ª/3ª; Inglese 1ª/2ª; Religione 2ª/3ª)
   coinvolgono **due classi nello stesso slot con lo stesso
   insegnante/esperto**. Il modello CP-SAT rappresenta esplicitamente questi
   casi (stesso indice slot per entrambe le classi), non un semplice
   accoppiamento 1 classe – 1 docente per slot.

## 7. Elenco vincoli per priorità

**HARD**
- Ore fisse esperti (§2) non modificabili.
- Materie da esattamente 2h (storia, scienze, geografia) → slot consecutivi,
  non interrotti dall'intervallo.
- Docente `early_exit`: nei giorni configurati e in un giorno tra i due con
  pomeriggio → niente s4 (esce alle 11:40).
- Docente `early_exit`: un solo pomeriggio a settimana.
- Docente `no_afternoon`: mai p1 e/o p2 nel giorno indicato.
- Mensa: turno unico condiviso da 2 docenti, conta 1h per entrambi.
- Docente `teaching_only`: 0h di assistenza (già al monte ore di sola
  didattica).
- Docente `reinforcement`: potenziamento nelle ore libere di 1ª e 2ª (3h + 2h,
  vedi §4).
- Motoria 2ª/3ª nello stesso slot, conta 1h (non 2h) di didattica.
- Ogni docente: monte ore target totale (didattica + assistenza) — vedi
  `docs/DECISIONS.md` REL-1 per il trattamento come obiettivo pesato.

**MEDIUM**
- p1 e p2 stessa insegnante/stessa classe, dove possibile.
- Intervallo coperto da chi ha s2 o s3 in quella classe.
- Mensa assegnata preferibilmente a chi ha s4 o p1 quel giorno.

**SOFT**
- In 3ª-4ª-5ª: storia, scienze, geografia in tre giorni diversi.
- Un solo pomeriggio a docente (per tutti tranne il docente `early_exit`, che è
  HARD).
- Non troppi buchi/ore vuote tra una lezione e l'altra.
- Non più di 2h della stessa materia per classe in un giorno (vedi
  `docs/DECISIONS.md` ADD-1).

## 8. Nota sul modello — funzione obiettivo

CP-SAT: tutti i vincoli HARD come `Add(...)`; i MEDIUM/SOFT come termini
penalizzati in una funzione obiettivo pesata, minimizzata dopo aver garantito
la fattibilità HARD. Il JSON di output riporta anche quali vincoli soft/medium
sono stati violati, per trasparenza verso l'utente finale.
