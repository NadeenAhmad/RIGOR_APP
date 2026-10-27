# ONCOREG — domain overview

ONCOREG is the clinical database of a hospital oncology day unit. It records
cancer patients, their tumour diagnoses, the chemotherapy regimens they receive
cycle by cycle, blood counts taken during treatment, the adverse events
(toxicities) caused by treatment, and tumour response assessments.

Main flow: pt_mstr (patient) -> tumor_dx (cancer diagnosis) -> tx_cyc
(chemotherapy cycle, uses a regimen from rgmn) -> lab_heme (blood counts) and
ae_tox (adverse events / toxicity). resp_asmt stores RECIST tumour response for
a diagnosis. onc_staff holds oncologists, nurses and pharmacists.

Conventions: grades follow the CTCAE severity scale (0 = none, 1 mild,
2 moderate, 3 severe, 4 life-threatening, 5 death). Dates are calendar dates.
Two links were never declared as foreign keys: tx_cyc.PRESCR_ID points to
onc_staff.STAFF_ID (prescribing oncologist) and ae_tox.LAB_REF points to
lab_heme.LAB_ID (the blood count used to grade the toxicity).
