# Tables tx_cyc (TxCyc) and rgmn (Rgmn) — chemotherapy

tx_cyc: one administered chemotherapy cycle for a cancer diagnosis.

| Column | Meaning |
|---|---|
| CYC_ID | Treatment cycle identifier. Primary key. |
| DX_ID | Cancer diagnosis being treated (tumor_dx.DX_ID). |
| RGMN_CD | Chemotherapy regimen given (rgmn.RGMN_CD). |
| CYC_NO | Cycle number within the regimen (1, 2, 3 ...). |
| ADMIN_DT | Date the cycle was administered. |
| DOSE_PCT | Percent of the planned dose actually given (100 = full dose). |
| DLY_FLG | True when the cycle was delayed, e.g. because of toxicity. |
| PRESCR_ID | Prescribing oncologist; refers to onc_staff.STAFF_ID (not a declared FK). |

rgmn: catalogue of chemotherapy regimens (protocols).

| Column | Meaning |
|---|---|
| RGMN_CD | Regimen code, e.g. AC-T, FOLFOX, CARBO-PAC. Primary key. |
| RGMN_NM | Full regimen name and drugs. |
| CYC_LEN_D | Cycle length in days (e.g. 14 or 21). |
| INTENT_CD | Treatment intent: C curative, P palliative, A adjuvant, N neoadjuvant. |
