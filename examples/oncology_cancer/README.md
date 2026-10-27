# Oncology example: the case of `BM_DEP`

A fictional legacy oncology database, **ONCOREG**, built for the ISWC 2026 demo.
It is the medical counterpart of the Northwind example. Like most real legacy
databases, the schema knows the structure but not the meaning:

```sql
CREATE TABLE ae_tox (
    AE_ID   INTEGER PRIMARY KEY,
    CYC_ID  INTEGER NOT NULL REFERENCES tx_cyc (CYC_ID),
    BM_DEP  INTEGER,      -- ???
    ...
    LAB_REF INTEGER       -- points somewhere, never declared
);
```

`BM_DEP` is **bone marrow depression** (myelosuppression) graded on the CTCAE
0–5 scale. The only place that says so is a 2009 memo and the data dictionary.
RIGOR reads those, plus external vocabularies, and turns the schema into an
ontology where that meaning is explicit, linked, and traceable.

All data, documents, and ontologies here are invented for the demo.

## Files

| Upload zone in the app | File(s) |
|---|---|
| **Upload CSV or schema JSON** | `schema/oncology_schema.json` |
| Optional context → **Documents** | all 7 files in `documentation/` |
| Optional context → **External ontologies** | all 3 files in `external_ontologies/` |

Not uploaded to RIGOR; use them to show the "before" picture:

- `schema/oncology_schema.sql`: the same schema as plain DDL.
- `sample_data/ae_tox_sample.csv` and `sample_data/lab_heme_sample.csv`: a few
  rows, so the audience sees `BM_DEP` values like `0, 2, 3, 4, 5` with no
  explanation.

### Tables

| Table | What it is | Cryptic columns worth pointing at |
|---|---|---|
| `pt_mstr` | patient | `SEX_CD` (1/2/9), `ECOG_PS`, `SMK_ST` (N/F/C) |
| `onc_staff` | clinicians | `ROLE_CD` (ONC/HEM/RN/PHARM) |
| `tumor_dx` | cancer diagnosis | `TOPO_CD`, `MORPH_CD` (ICD-O-3), `T_CAT`/`N_CAT`/`M_CAT`, `STG_GRP`, `GRD` |
| `rgmn` | chemotherapy regimen | `CYC_LEN_D`, `INTENT_CD` |
| `tx_cyc` | chemotherapy cycle | `DOSE_PCT`, `DLY_FLG`, `PRESCR_ID` (undeclared link to `onc_staff`) |
| `lab_heme` | blood count | `ANC`, `PLT`, `HGB`, `WBC` (units in docs), `NADIR_FLG` |
| `ae_tox` | adverse event / toxicity | **`BM_DEP`**, `FN_FLG`, `NAUS_GR`, `PN_GR`, `ATTR_CD`, `ACT_TKN`, `LAB_REF` (undeclared link to `lab_heme`) |
| `resp_asmt` | tumour response | `RECIST_CD` (CR/PR/SD/PD), `SLD_MM` |

### External ontologies (demo stand-ins)

These are small fictional vocabularies under `http://example.org/ext/...`. They
are modelled on CTCAE, NCI Thesaurus, and RECIST, but they are not official
releases of any of them. They are kept small on purpose, because RIGOR retrieves
only the top 3 external hints per table.

- `demo_adverse_event_ontology.ttl` (`ae:`): `AdverseEvent` ⊒ `HematologicToxicity` ⊒
  `BoneMarrowDepression` ⊒ {`Neutropenia` ⊒ `FebrileNeutropenia`, `Thrombocytopenia`,
  `Anemia`}, plus `SeriousAdverseEvent`, `Nausea`, `PeripheralSensoryNeuropathy`,
  `severityGrade`, `occurredDuring`, `gradedFrom`, `actionTaken`, …
- `demo_oncology_ontology.ttl` (`onc:`): `CancerDiagnosis`, `ChemotherapyRegimen`,
  `ChemotherapyCycle`, RECIST response classes, `primarySiteCode`, `tnmStageGroup`, …
- `demo_clinical_ontology.ttl` (`clin:`): `Patient`, `Clinician` ⊒ `Oncologist`,
  `CompleteBloodCount`, `absoluteNeutrophilCount`, `plateletCount`, `prescribedBy`, …

Each document in `documentation/` is under 1,200 characters, so it is exactly
one retrieval chunk, and it repeats the table's cryptic column names so that
retrieval for that table finds it.

## Demo script

1. Show `schema/oncology_schema.sql` and `sample_data/ae_tox_sample.csv`: *"What is
   `BM_DEP`? What does 4 mean? What is `LAB_REF`?"* This is the "case file" slide.
2. In the app: paste the OpenRouter key and pick the model. Under *Optional
   context*, upload the 7 documents and 3 external ontologies. Upload
   `oncology_schema.json` and click **Summarize table** (8 tables).
3. Click **Run generation pipeline**. In the log, point at the
   `External hints for 'ae_tox'` lines: RIGOR retrieves the bone-marrow-depression
   vocabulary and the 2009 memo for that table.
4. In the Turtle preview or graph, look for these. LLM output varies from run to
   run, so this is a list of what to look for, not a guarantee:

| Case file | Before (database) | After (RIGOR ontology) |
|---|---|---|
| Meaning | `BM_DEP` | `rdfs:label` / `rdfs:comment` such as "bone marrow depression (myelosuppression) grade" |
| Values | `INTEGER`, 0–5 = ??? | comment explaining the CTCAE 0–5 grades |
| Standard term | ??? | alignment to `ae:BoneMarrowDepression` / `ae:severityGrade`; `AeTox` ⊑ `ae:AdverseEvent` |
| Hierarchy | flat tables | `HematologicToxicity` ⊒ `BoneMarrowDepression` ⊒ neutropenia / thrombocytopenia / anemia |
| Links | `LAB_REF`, `PRESCR_ID` are plain integers | possible object properties to `LabHeme` / `OncStaff`, recovered from the docs |
| Provenance | none | `prov:wasDerivedFrom` back to the source table and column |

5. Open the semantic review cards if OOPS! raised any, approve or reject one,
   and download the reviewed ontology.

## Before the demo

- **Run it once the night before, on the same model and laptop.** Save the outputs
  (final `.owl`, reviewed `.owl`, graph `.html`) in a `rigor_app_results_<model>/`
  folder, as for Northwind. If anything goes wrong live (Wi-Fi, OpenRouter
  credits), show the saved ontology with **Visualize** or **Repair an existing
  ontology**.
- That run also fills the OOPS! cache (`~/.cache/rigor_oops`). Re-checking the
  same saved ontology tomorrow is served from the cache, not the network.
- At the venue, use **Settings → Check OOPS! connection** before you start. If
  OOPS! is down, the run still completes: it falls back to cached results and
  local pitfall checks after one quick failure instead of hanging.
