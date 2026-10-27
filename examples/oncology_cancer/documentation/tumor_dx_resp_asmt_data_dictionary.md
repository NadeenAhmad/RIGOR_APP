# Tables tumor_dx (TumorDx) and resp_asmt (RespAsmt) — cancer diagnosis

tumor_dx: one cancer (malignant neoplasm) diagnosis of a patient.

| Column | Meaning |
|---|---|
| DX_ID | Diagnosis identifier. Primary key. |
| PT_ID | Diagnosed patient (pt_mstr.PT_ID). |
| DX_DT | Date of diagnosis. |
| TOPO_CD | ICD-O-3 topography code: primary site, e.g. C50.9 breast. |
| MORPH_CD | ICD-O-3 morphology code: histology, e.g. 8500/3 ductal carcinoma. |
| T_CAT / N_CAT / M_CAT | TNM categories: primary tumour, regional nodes, distant metastasis. |
| STG_GRP | AJCC stage group, I to IV. |
| GRD | Histologic grade 1-4 (well to undifferentiated). |

resp_asmt: tumour response assessment for a diagnosis.

| Column | Meaning |
|---|---|
| RESP_ID | Response assessment identifier. Primary key. |
| DX_ID | Assessed diagnosis (tumor_dx.DX_ID). |
| ASMT_DT | Date of the imaging assessment. |
| RECIST_CD | RECIST 1.1 response: CR complete, PR partial, SD stable, PD progressive, NE not evaluable. |
| SLD_MM | Sum of longest diameters of target lesions, in millimetres. |
