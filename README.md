# Orario scuola primaria — solver CP-SAT

Genera l'orario settimanale di una scuola primaria con
[OR-Tools CP-SAT](https://developers.google.com/optimization/cp/cp_solver).
I vincoli forti (copertura delle classi, monte ore dei corsi, materie da 2h in
slot consecutivi, ruoli speciali dei docenti…) sono imposti come vincoli HARD;
le preferenze MEDIUM/SOFT diventano termini penalizzati di una funzione
obiettivo pesata. L'output è un JSON con l'orario per classe e per docente e
l'elenco dei vincoli eventualmente violati.

Il modello è generico: la griglia oraria (4 mattine corte + 2 giorni lunghi con
mensa) è fissa nel codice, tutto il resto — docenti, classi, ore fisse degli
esperti, monte ore dei titolari, ore di potenziamento, pesi — vive in un file
di configurazione YAML.

## Struttura

```
scripts/
  data.py         griglia oraria (invariante) + loader del config YAML
  model.py        modello CP-SAT (variabili, vincoli, obiettivo)
  main.py         entry point: carica il config, risolve, valida, scrive il JSON
  output.py       serializzazione secondo docs/schema_output.json
  config.example.yaml   configurazione di esempio, dati anonimi
  requirements.txt      dipendenze con versioni fissate
  tests/          test di integrità dei dati (non lanciano il solver)
docs/
  requisiti_puliti.md   specifica dei requisiti
  schema_output.json    schema/esempio del JSON di output
  DECISIONS.md          deroghe di modellazione rispetto ai requisiti
```

## Setup

Serve Python ≥ 3.11.

```bash
python -m venv scripts/.venv
scripts/.venv/Scripts/python.exe -m pip install -r scripts/requirements.txt
# opzionale, per usare il progetto come pacchetto:
scripts/.venv/Scripts/python.exe -m pip install -e .
```

`main.py` si rifiuta di partire se non viene eseguito con l'interprete di
`scripts/.venv` (evita di girare in silenzio su un Python globale con versioni
diverse).

## Configurazione

```bash
cp scripts/config.example.yaml scripts/config.yaml
```

Poi apri `scripts/config.yaml` e inserisci i dati reali:

- `classes`, `teachers`: elenchi di etichette;
- `expert_fixed`: ore coperte da esperti esterni (slot bloccati);
- `expert_covers_interval`: `(classe, giorno)` il cui intervallo è già coperto
  dall'esperto;
- `courses`: monte ore dei titolari (una riga con due classi = co-docenza,
  conta 1h);
- `reinforcement`: docente, materia e ore di potenziamento per classe;
- `constraint_params`: materie da 2h, classi "tre giorni diversi", monte ore
  target, crediti di assistenza, cap giornaliero;
- `weights`: pesi dei termini MEDIUM/SOFT dell'obiettivo;
- `teacher_roles`: quale docente ha il ruolo `early_exit` (uscita a fine s3),
  `no_afternoon` (niente p1/p2 in un dato giorno), `teaching_only` (già a monte
  ore pieno di sola didattica).

`config.yaml` è in `.gitignore` e non va committato: i dati reali restano
locali. I test di integrità girano sul file di esempio:

```bash
scripts/.venv/Scripts/python.exe -m pytest scripts/tests -q
```

Se cambi i dati e le ore per classe non tornano a 24, un test fallisce prima
ancora di lanciare il solver.

## Esecuzione

```bash
scripts/.venv/Scripts/python.exe scripts/main.py --config scripts/config.yaml
```

Opzioni: `--time-limit <secondi>` (default 120), `--output <path>`,
`--verbose` (log di ricerca del solver).

Codici di uscita: `0` risolto e validato, `1` nessuna soluzione (infeasible),
`2` risolto ma la validazione indipendente ha trovato una violazione HARD.

## Interpretare `orario_output.json`

- `meta`: anno scolastico, stato del solver, valore obiettivo, griglia di
  giorni e slot.
- `by_class`: per ogni classe e giorno, gli slot didattici con materia,
  docente ed eventuali classi in co-docenza.
- `by_teacher`: per ogni docente, monte ore (totale / didattica / assistenza)
  e l'orario completo, incluse sorveglianze intervallo e turni mensa.
- `constraint_report`: `hard_violations`, `medium_violations`,
  `soft_violations`, ciascuna con `constraint` e `detail`.

## Se il solver riporta INFEASIBLE

Non esiste un orario che rispetti tutti i vincoli HARD insieme. `main.py`
esegue allora un'analisi dell'**unsat core** e stampa (e scrive in
`constraint_report.hard_violations`) i gruppi di vincoli HARD in conflitto,
con la loro descrizione (`H1`…`H12`). Per sbloccare: allenta o rinegozia uno
dei gruppi indicati — tipicamente rivedendo il monte ore dei titolari, le ore
fisse degli esperti o i ruoli speciali nel config — e rilancia.

## Decisioni di modellazione

Alcune regole che i requisiti originali indicano come HARD sono qui obiettivi
pesati, perché imporle come HARD rende il modello infeasible. Ogni deroga
(regola rilassata o aggiunta) è documentata, con il motivo tecnico e lo stato,
in [`docs/DECISIONS.md`](docs/DECISIONS.md).

## Licenza

MIT.
