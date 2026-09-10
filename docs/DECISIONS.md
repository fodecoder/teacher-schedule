# Decisioni di modellazione — deroghe rispetto a `docs/requisiti_puliti.md`

Ogni voce documenta una deroga rispetto ai requisiti originali (§7 di
`requisiti_puliti.md`). Convenzione sugli stati:

- **confermato**: deroga approvata, in vigore nel modello;
- **aperto**: da rivedere.

Identificatori: `REL-n` = vincolo rilassato; `ADD-n` = vincolo aggiunto.

I ruoli citati (`early_exit`, `no_afternoon`, `teaching_only`, `reinforcement`)
sono assegnati a docenti concreti nel file di configurazione, sezione
`teacher_roles`. I numeri qui sotto si riferiscono al caso di
`config.example.yaml`.

---

## REL-1 — "monte ore target esatto per docente": HARD → obiettivo pesato

**Requisito originale** (§7, HARD): «Ogni docente: esattamente il monte ore
target totale (didattica + assistenza)».

**Modifica**: il vincolo di uguaglianza è sostituito da una penalità sullo
scostamento assoluto dal monte target (peso `monte_ore_target`), applicata a
tutti i docenti tranne quello con ruolo `teaching_only`, che resta HARD al
target di sola didattica (vincolo H12).

**Motivo tecnico**: imponendo il monte target come HARD per tutti i docenti il
modello è `INFEASIBLE` (verificato con il solver, prova completata; resta
`INFEASIBLE` anche rilassando il target di un singolo docente). Catena dei
vincoli in conflitto, sui dati di esempio:

- H4 + H10 fissano la didattica: Docente A 19h, Docente B 20h, Docente C 22h,
  Docente D 17h, Docente E 17h (12h disciplinari + 5h potenziamento).
- Assistenza totale richiesta per portare tutti a 22h:
  3 + 2 + 0 + 5 + 5 = 15h.
- H12 azzera l'assistenza del docente `teaching_only`: i 15h ricadono su 4
  docenti.
- H8: la mensa produce esattamente 4h di assistenza in tutto (2 giorni × 2
  turni × 1h).
- H11: max 1 intervallo per docente al giorno → ≤ 5 × 0,5h = 2,5h/settimana per
  docente.
- H6: il docente `early_exit` fa mensa solo nel suo unico pomeriggio → ≤ 1h di
  mensa.

Tetti reali di assistenza: docente `early_exit` 2,5h + 1h = 3,5h (contro 5h
richiesti, deficit 1,5h); docente `reinforcement` 2,5h + 2h = 4,5h (contro 5h,
deficit 0,5h). Il peso `monte_ore_target` resta il termine dominante
dell'obiettivo così che il solver minimizzi questi scostamenti prima di ogni
altra preferenza.

**Stato**: confermato.

---

## REL-2 — "il docente `early_exit` esce a fine s3 nei giorni pesati": HARD → obiettivo pesato

**Requisito originale** (§7, HARD): «Docente `early_exit`: nei giorni indicati e
nel giorno corto tra i due giorni con pomeriggio → niente s4 (esce alle
11:40)».

**Modifica**: il divieto di s4 resta HARD nel giorno corto tra i due giorni con
pomeriggio (vincolo H6). Nei giorni pesati (`weighted_no_s4_days` nel config —
mercoledì e venerdì nell'esempio) è una penalità (peso `uscita_anticipata`),
riportata nel report quando violata.

**Motivo tecnico**: imponendo il divieto di s4 anche nei giorni pesati il
modello è `INFEASIBLE`, unsat core `{H2, H3, H6}`. Quei giorni non hanno ore di
esperto, quindi a s4 ogni classe richiede un titolare (H2) e servono tanti
docenti distinti quante sono le classi (H3): a s4 è obbligato un abbinamento
perfetto classe–docente. Una classe (la 5ª nell'esempio) ha come titolari solo
due docenti, uno dei quali è quello `early_exit`. Vietandogli s4 restano
quattro titolari per cinque classi.

Il peso `uscita_anticipata` è tenuto sopra a qualunque singolo scarto di
mezz'ora del monte ore (REL-1) così che il solver non "compri" una violazione
di uscita anticipata per guadagnare ore di didattica.

**Stato**: confermato.

---

## ADD-1 — vincolo SOFT "max 2h/giorno della stessa materia per classe"

**Requisito originale**: assente. `requisiti_puliti.md` non pone alcun limite
giornaliero alle materie da 6h.

**Modifica**: aggiunta una penalità SOFT (peso
`max_2h_giorno_stessa_materia`) sull'eccedenza oltre il cap giornaliero
(`constraint_params.daily_subject_soft_cap`, 2h nell'esempio) della stessa
materia nella stessa classe.

**Motivo tecnico**: preferenza distributiva, per evitare che una materia
principale si concentri in un solo giorno. Non introduce infeasibility: il
modello resta `OPTIMAL` anche imponendo la regola come HARD.

**Stato**: confermato.
