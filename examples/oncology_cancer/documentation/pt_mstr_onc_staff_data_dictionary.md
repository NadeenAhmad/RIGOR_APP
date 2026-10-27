# Tables pt_mstr (PtMstr) and onc_staff (OncStaff) — people

pt_mstr: patient master record, one row per cancer patient.

| Column | Meaning |
|---|---|
| PT_ID | Patient identifier. Primary key. |
| MRN | Hospital medical record number. |
| SEX_CD | Administrative sex: 1 male, 2 female, 9 unknown. |
| BIRTH_YR | Year of birth. |
| ECOG_PS | ECOG performance status 0-4 at first visit (0 fully active, 4 bedbound). |
| SMK_ST | Smoking status: N never, F former, C current. |
| DOD_DT | Date of death; empty while alive. |

onc_staff: clinical staff of the oncology unit.

| Column | Meaning |
|---|---|
| STAFF_ID | Staff identifier. Primary key. |
| STAFF_NM | Full name. |
| ROLE_CD | Role: ONC oncologist, HEM haematologist, RN nurse, PHARM pharmacist. |
| DEPT_CD | Department code. |
