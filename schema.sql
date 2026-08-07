-- GraphAtom — milestone 1. Huit tables, rien de volumineux en base.

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
    title          TEXT,                      -- le titre lisible du sujet, posé par le canal
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
    cycle         INT    NOT NULL DEFAULT 1,  -- passage courant, +1 par retry d'escalade
    escalations   INT    NOT NULL,            -- budget fini, décrémenté, jamais régénéré
    wall_deadline TIMESTAMPTZ NOT NULL,
    terminal_at   TIMESTAMPTZ,
    UNIQUE (subject_id, generation)
);

CREATE TABLE IF NOT EXISTS node_run (
    id               BIGSERIAL PRIMARY KEY,
    item_id          BIGINT NOT NULL REFERENCES work_item(id),
    node             TEXT   NOT NULL,
    cycle            INT    NOT NULL DEFAULT 1,  -- le passage dont la tentative fait partie
    attempt          INT    NOT NULL,         -- tentative dans ce passage, repart à 1
    status           TEXT   NOT NULL,         -- running | applied | superseded | stale | faulted
    fence            INT    NOT NULL,
    expected_version INT    NOT NULL,
    lease_expires_at TIMESTAMPTZ NOT NULL,
    outcome          TEXT,
    result           JSONB
    -- unicité d'une tentative : voir l'index du passage, tout en bas
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
    run_id      BIGINT,                        -- NULL : acte de parole du rail, sans run
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

-- Les battements : une ligne par producteur, tamponnée à chaque tour de sa
-- boucle. Le worker bat sous `rail`, le canal GitHub sous `github-sync` ;
-- le frontend et le canal lisent — personne ne décide d'un état avec.
-- Plusieurs workers tamponnent la même ligne : c'est « au moins un vivant »
-- qu'on mesure, pas qui est vivant.
CREATE TABLE IF NOT EXISTS heartbeat (
    who TEXT       NOT NULL,                  -- rail | github-sync : le batteur
    at  TIMESTAMPTZ NOT NULL DEFAULT now()
    -- unicité d'un batteur : voir l'index du passage, tout en bas
);

-- Le passage. `graphatom init-db` rejoue ce fichier à chaque déploiement, et
-- un CREATE TABLE ne voit rien d'une table déjà là : tout ce qui arrive après
-- coup s'écrit ici, idempotent, et vaut aussi bien sur une base neuve que sur
-- une base peuplée. Sans perte : les tentatives d'avant sont du passage 1.
ALTER TABLE subject   ADD COLUMN IF NOT EXISTS title TEXT;
ALTER TABLE work_item ADD COLUMN IF NOT EXISTS cycle INT NOT NULL DEFAULT 1;
ALTER TABLE node_run  ADD COLUMN IF NOT EXISTS cycle INT NOT NULL DEFAULT 1;
ALTER TABLE node_run  DROP CONSTRAINT IF EXISTS node_run_item_id_node_attempt_key;
CREATE UNIQUE INDEX IF NOT EXISTS node_run_attempt_key
    ON node_run (item_id, node, cycle, attempt);
-- Le battement prend l'identité de son batteur : la ligne unique `id = 1`
-- devient une ligne par producteur, et celle du worker devient `rail` — sans
-- perte, elle garde son horodatage. `id` part avec sa clé primaire ; l'unicité
-- passe sur `who`, et c'est elle que l'UPSERT du tampon vise.
ALTER TABLE heartbeat ADD COLUMN IF NOT EXISTS who TEXT;
UPDATE      heartbeat SET who = 'rail' WHERE who IS NULL;
ALTER TABLE heartbeat DROP COLUMN IF EXISTS id;
ALTER TABLE heartbeat ALTER COLUMN who SET NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS heartbeat_who_key ON heartbeat (who);
