-- ONCOREG: legacy oncology clinical database (fictional demo schema).
-- This is the "before" picture: the DDL as it exists in production.
-- No column comments, two links (tx_cyc.PRESCR_ID, ae_tox.LAB_REF) were never
-- declared as foreign keys, and the meaning of the codes lives only in old
-- documents. oncology_schema.json is the same schema in RIGOR's input format.

CREATE TABLE pt_mstr (
    PT_ID      INTEGER PRIMARY KEY,
    MRN        TEXT NOT NULL,
    SEX_CD     INTEGER,
    BIRTH_YR   INTEGER,
    ECOG_PS    INTEGER,
    SMK_ST     TEXT,
    DOD_DT     DATE
);

CREATE TABLE onc_staff (
    STAFF_ID   INTEGER PRIMARY KEY,
    STAFF_NM   TEXT NOT NULL,
    ROLE_CD    TEXT,
    DEPT_CD    TEXT
);

CREATE TABLE tumor_dx (
    DX_ID      INTEGER PRIMARY KEY,
    PT_ID      INTEGER NOT NULL REFERENCES pt_mstr (PT_ID),
    DX_DT      DATE,
    TOPO_CD    TEXT,
    MORPH_CD   TEXT,
    T_CAT      TEXT,
    N_CAT      TEXT,
    M_CAT      TEXT,
    STG_GRP    TEXT,
    GRD        INTEGER
);

CREATE TABLE rgmn (
    RGMN_CD    TEXT PRIMARY KEY,
    RGMN_NM    TEXT,
    CYC_LEN_D  INTEGER,
    INTENT_CD  TEXT
);

CREATE TABLE tx_cyc (
    CYC_ID     INTEGER PRIMARY KEY,
    DX_ID      INTEGER NOT NULL REFERENCES tumor_dx (DX_ID),
    RGMN_CD    TEXT NOT NULL REFERENCES rgmn (RGMN_CD),
    CYC_NO     INTEGER,
    ADMIN_DT   DATE,
    DOSE_PCT   DECIMAL,
    DLY_FLG    BOOLEAN,
    PRESCR_ID  INTEGER
);

CREATE TABLE lab_heme (
    LAB_ID     INTEGER PRIMARY KEY,
    CYC_ID     INTEGER NOT NULL REFERENCES tx_cyc (CYC_ID),
    DRAW_DT    DATE,
    WBC        DECIMAL,
    ANC        DECIMAL,
    HGB        DECIMAL,
    PLT        INTEGER,
    NADIR_FLG  BOOLEAN
);

CREATE TABLE ae_tox (
    AE_ID      INTEGER PRIMARY KEY,
    CYC_ID     INTEGER NOT NULL REFERENCES tx_cyc (CYC_ID),
    ONSET_DT   DATE,
    BM_DEP     INTEGER,
    FN_FLG     BOOLEAN,
    NAUS_GR    INTEGER,
    PN_GR      INTEGER,
    SAE_FLG    BOOLEAN,
    ATTR_CD    INTEGER,
    ACT_TKN    TEXT,
    LAB_REF    INTEGER
);

CREATE TABLE resp_asmt (
    RESP_ID    INTEGER PRIMARY KEY,
    DX_ID      INTEGER NOT NULL REFERENCES tumor_dx (DX_ID),
    ASMT_DT    DATE,
    RECIST_CD  TEXT,
    SLD_MM     DECIMAL
);
