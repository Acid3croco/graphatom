-- GraphAtom — milestone 1. Sept tables, rien de volumineux en base.

CREATE TABLE IF NOT EXISTS graph_revision (
    id           TEXT PRIMARY KEY,            -- sha256 du bundle canonique
    name         TEXT NOT NULL,
    bundle       JSONB NOT NULL,
    published_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS subject (
    id             BIGSERIAL PRIMARY KEY,
    graph          TEXT NOT NULL,
    subject_key    TEXT NOT NULL,
    lineage_budget INT  NOT NULL DEFAULT 3,   -- ré-admissions restantes, jamais régénéré
    UNIQUE (graph, subject_key)
);

CREATE TABLE IF NOT EXISTS work_item (
    id            BIGSERIAL PRIMARY KEY,
    subject_id    BIGINT NOT NULL REFERENCES subject(id),
    generation    INT    NOT NULL,
    revision      TEXT   NOT NULL REFERENCES graph_revision(id),
    state         TEXT   NOT NULL,
    version       INT    NOT NULL DEFAULT 0,
    fence         INT    NOT NULL DEFAULT 0,
    escalations   INT    NOT NULL,            -- budget fini, décrémenté, jamais régénéré
    wall_deadline TIMESTAMPTZ NOT NULL,
    terminal_at   TIMESTAMPTZ,
    UNIQUE (subject_id, generation)
);

CREATE TABLE IF NOT EXISTS node_run (
    id               BIGSERIAL PRIMARY KEY,
    item_id          BIGINT NOT NULL REFERENCES work_item(id),
    node             TEXT   NOT NULL,
    attempt          INT    NOT NULL,
    status           TEXT   NOT NULL,         -- running | applied | superseded | stale | faulted
    fence            INT    NOT NULL,
    expected_version INT    NOT NULL,
    lease_expires_at TIMESTAMPTZ NOT NULL,
    outcome          TEXT,
    result           JSONB,
    UNIQUE (item_id, node, attempt)
);

CREATE TABLE IF NOT EXISTS event (
    item_id      BIGINT NOT NULL REFERENCES work_item(id),
    item_version INT    NOT NULL,
    at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    kind         TEXT   NOT NULL,             -- admitted | result | answer | deadline | reaped | wall
    from_state   TEXT,
    to_state     TEXT   NOT NULL,
    outcome      TEXT,
    run_id       BIGINT,
    PRIMARY KEY (item_id, item_version)
);

CREATE TABLE IF NOT EXISTS effect (
    op_id       BIGSERIAL PRIMARY KEY,
    item_id     BIGINT NOT NULL REFERENCES work_item(id),
    run_id      BIGINT NOT NULL,
    logical_key TEXT   NOT NULL,              -- dérivée de l'action logique, pas de la tentative
    target_uri  TEXT   NOT NULL,
    intent      JSONB  NOT NULL,
    observation TEXT   NOT NULL DEFAULT 'not_attempted',
                                              -- not_attempted | applied | rejected | uncertain
    UNIQUE (target_uri, logical_key)
);

CREATE TABLE IF NOT EXISTS question (
    id          BIGSERIAL PRIMARY KEY,
    item_id     BIGINT NOT NULL REFERENCES work_item(id),
    node        TEXT   NOT NULL,
    text        TEXT   NOT NULL,
    options     JSONB  NOT NULL,              -- question fermée : jamais de texte libre
    owner       TEXT   NOT NULL,
    deadline    TIMESTAMPTZ NOT NULL,
    state       TEXT   NOT NULL DEFAULT 'open',   -- open | answered | expired
    answer      TEXT,
    answered_by TEXT,
    answered_at TIMESTAMPTZ
);
