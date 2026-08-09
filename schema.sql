-- GraphAtom — tables durables du rail, rien de volumineux en base.

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
    candidate        INT,                     -- candidat du fan-out ; NULL hors fan-out
    status           TEXT   NOT NULL,         -- running | applied | superseded | stale | faulted
    fence            INT    NOT NULL,
    expected_version INT    NOT NULL,
    lease_expires_at TIMESTAMPTZ NOT NULL,
    finished_at      TIMESTAMPTZ,             -- fin du run : elle départage les candidats
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
    at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    worker_sha TEXT,
    worker_started_at TIMESTAMPTZ
    -- unicité d'un batteur : voir l'index du passage, tout en bas
);

-- Une table UNLOGGED est vidée par Postgres pendant une récupération après
-- crash, mais pas pendant une coupure réseau. Le worker y pose un jeton pour
-- distinguer une reprise de base d'une reconnexion au même serveur.
CREATE UNLOGGED TABLE IF NOT EXISTS database_incarnation (
    singleton BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (singleton),
    token     TEXT NOT NULL
);

-- Un relevé immuable des tarifs API publics. Plusieurs lignes par modèle
-- gardent l'histoire : un run épingle une ligne précise et son estimation ne
-- change donc pas lors de la relève quotidienne suivante.
CREATE TABLE IF NOT EXISTS model_price (
    id                        BIGSERIAL PRIMARY KEY,
    provider                  TEXT        NOT NULL,
    model                     TEXT        NOT NULL,
    source_url                TEXT        NOT NULL,
    fetched_at                TIMESTAMPTZ NOT NULL,
    input_per_million         NUMERIC     NOT NULL CHECK (input_per_million >= 0),
    cache_read_per_million    NUMERIC     NOT NULL CHECK (cache_read_per_million >= 0),
    cache_write_per_million   NUMERIC     NOT NULL CHECK (cache_write_per_million >= 0),
    output_per_million        NUMERIC     NOT NULL CHECK (output_per_million >= 0)
);

-- Le coût API équivalent d'un run, séparé du coût que son fournisseur a
-- rapporté. Les quatre classes sont disjointes : aucun token de cache ou de
-- raisonnement n'est compté deux fois.
CREATE TABLE IF NOT EXISTS run_cost (
    run_id                    BIGINT PRIMARY KEY REFERENCES node_run(id),
    price_id                  BIGINT      NOT NULL REFERENCES model_price(id),
    model_source              TEXT        NOT NULL,
    input_tokens              BIGINT      NOT NULL CHECK (input_tokens >= 0),
    cache_read_tokens         BIGINT      NOT NULL CHECK (cache_read_tokens >= 0),
    cache_write_tokens        BIGINT      NOT NULL CHECK (cache_write_tokens >= 0),
    output_tokens             BIGINT      NOT NULL CHECK (output_tokens >= 0),
    input_cost_usd            NUMERIC     NOT NULL CHECK (input_cost_usd >= 0),
    cache_read_cost_usd       NUMERIC     NOT NULL CHECK (cache_read_cost_usd >= 0),
    cache_write_cost_usd      NUMERIC     NOT NULL CHECK (cache_write_cost_usd >= 0),
    output_cost_usd           NUMERIC     NOT NULL CHECK (output_cost_usd >= 0),
    estimated_cost_usd        NUMERIC     NOT NULL CHECK (estimated_cost_usd >= 0),
    estimated_at              TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Le passage. `graphatom init-db` rejoue ce fichier à chaque déploiement, et
-- un CREATE TABLE ne voit rien d'une table déjà là : tout ce qui arrive après
-- coup s'écrit ici, idempotent, et vaut aussi bien sur une base neuve que sur
-- une base peuplée. Sans perte : les tentatives d'avant sont du passage 1.
ALTER TABLE subject   ADD COLUMN IF NOT EXISTS title TEXT;
ALTER TABLE work_item ADD COLUMN IF NOT EXISTS cycle INT NOT NULL DEFAULT 1;
ALTER TABLE node_run  ADD COLUMN IF NOT EXISTS cycle INT NOT NULL DEFAULT 1;
ALTER TABLE node_run  DROP CONSTRAINT IF EXISTS node_run_item_id_node_attempt_key;
-- l'unicité d'une tentative a suivi le fan-out : voir `node_run_candidate_key`,
-- plus bas. Le passage se rejoue en entier à chaque déploiement — recréer ici
-- l'index d'avant échouerait sur la première base qui porte des candidats.
-- Le battement prend l'identité de son batteur : la ligne unique `id = 1`
-- devient une ligne par producteur, et celle du worker devient `rail` — sans
-- perte, elle garde son horodatage. `id` part avec sa clé primaire ; l'unicité
-- passe sur `who`, et c'est elle que l'UPSERT du tampon vise.
ALTER TABLE heartbeat ADD COLUMN IF NOT EXISTS who TEXT;
ALTER TABLE heartbeat ADD COLUMN IF NOT EXISTS worker_sha TEXT;
ALTER TABLE heartbeat ADD COLUMN IF NOT EXISTS worker_started_at TIMESTAMPTZ;
UPDATE      heartbeat SET who = 'rail' WHERE who IS NULL;
ALTER TABLE heartbeat DROP COLUMN IF EXISTS id;
ALTER TABLE heartbeat ALTER COLUMN who SET NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS heartbeat_who_key ON heartbeat (who);
-- Le fan-out : une tentative n'est plus un run mais K candidats concurrents,
-- numérotés de 0 à K-1. Un nœud sans fan-out garde son run unique, et son
-- candidat reste NULL — d'où `NULLS NOT DISTINCT`, sans quoi l'unicité d'une
-- tentative ordinaire ne serait plus contrainte du tout. La date de fin,
-- elle, départage les candidats à égalité : le premier terminé tranche.
ALTER TABLE node_run  ADD COLUMN IF NOT EXISTS candidate INT;
ALTER TABLE node_run  ADD COLUMN IF NOT EXISTS finished_at TIMESTAMPTZ;
DROP INDEX IF EXISTS node_run_attempt_key;
CREATE UNIQUE INDEX IF NOT EXISTS node_run_candidate_key
    ON node_run (item_id, node, cycle, attempt, candidate) NULLS NOT DISTINCT;
CREATE INDEX IF NOT EXISTS model_price_latest
    ON model_price (provider, model, fetched_at DESC, id DESC);
