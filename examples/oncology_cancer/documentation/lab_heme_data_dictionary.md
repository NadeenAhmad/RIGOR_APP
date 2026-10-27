# Table lab_heme (LabHeme) — haematology blood counts

One complete blood count (CBC) drawn during a chemotherapy cycle (tx_cyc).
These counts are the evidence for grading bone marrow depression (ae_tox.BM_DEP).

| Column | Meaning |
|---|---|
| LAB_ID | Blood count identifier. Primary key. |
| CYC_ID | Chemotherapy cycle the sample belongs to (tx_cyc.CYC_ID). |
| DRAW_DT | Date the blood sample was drawn. |
| WBC | White blood cell count, unit 10^9 cells/L. |
| ANC | Absolute neutrophil count, unit 10^9 cells/L. ANC < 0.5 is grade 4 neutropenia. |
| HGB | Haemoglobin concentration, unit g/dL. |
| PLT | Platelet count, unit 10^9 cells/L. |
| NADIR_FLG | True when this is the lowest count of the cycle (the nadir). |
