# Table ae_tox (AeTox) — adverse events / treatment toxicity

One row per adverse event observed during a chemotherapy cycle (tx_cyc).

| Column | Meaning |
|---|---|
| AE_ID | Adverse event identifier. Primary key. |
| CYC_ID | Chemotherapy cycle during which the event started (tx_cyc.CYC_ID). |
| ONSET_DT | Date the adverse event started. |
| BM_DEP | Bone marrow depression (myelosuppression) grade, CTCAE 0-5. |
| FN_FLG | Febrile neutropenia: fever with low neutrophil count. |
| NAUS_GR | Nausea grade, CTCAE 0-3. |
| PN_GR | Peripheral sensory neuropathy grade, CTCAE 0-4. |
| SAE_FLG | Serious adverse event (hospitalisation or life-threatening). |
| ATTR_CD | Attribution to treatment: 1 unrelated, 2 unlikely, 3 possible, 4 probable, 5 definite. |
| ACT_TKN | Action taken: NONE, DR (dose reduced), DLY (delayed), DC (discontinued). |
| LAB_REF | Blood count used to grade BM_DEP; refers to lab_heme.LAB_ID (not a declared FK). |
