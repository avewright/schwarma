-- Migration 001: Initial schema (baseline)
-- This captures the full schema that schema.sql had before versioned migrations.
-- For fresh installs it creates everything; for existing databases the
-- IF NOT EXISTS guards make it a no-op.

CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- ── Agents ──────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS agents (
    id              UUID PRIMARY KEY,
    name            TEXT NOT NULL,
    model_tier      TEXT NOT NULL DEFAULT 'STANDARD',
    capabilities    TEXT[] NOT NULL DEFAULT ARRAY['GENERAL'],
    metadata        JSONB NOT NULL DEFAULT '{}',
    watch_tags      TEXT[] NOT NULL DEFAULT '{}',
    is_suspended    BOOLEAN NOT NULL DEFAULT FALSE,
    total_solved    INTEGER NOT NULL DEFAULT 0,
    total_reviewed  INTEGER NOT NULL DEFAULT 0,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_agents_name ON agents (name);
CREATE INDEX IF NOT EXISTS idx_agents_model_tier ON agents (model_tier);

-- ── Problems ────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS problems (
    id                      UUID PRIMARY KEY,
    title                   TEXT NOT NULL,
    description             TEXT NOT NULL,
    author_id               UUID NOT NULL REFERENCES agents(id),
    status                  TEXT NOT NULL DEFAULT 'OPEN',
    tags                    TEXT[] NOT NULL DEFAULT ARRAY['GENERAL'],
    priority                INTEGER NOT NULL DEFAULT 0,
    bounty                  INTEGER NOT NULL DEFAULT 10,
    sensitivity             TEXT NOT NULL DEFAULT 'INTERNAL',
    min_solver_tier         TEXT,
    max_solvers             INTEGER NOT NULL DEFAULT 1,
    deadline                TIMESTAMPTZ,
    claimed_by              UUID[] NOT NULL DEFAULT '{}',
    solution_ids            UUID[] NOT NULL DEFAULT '{}',
    accepted_solution_id    UUID,
    context                 JSONB NOT NULL DEFAULT '{}',
    failure_report          JSONB,
    parent_id               UUID REFERENCES problems(id),
    sub_problem_ids         UUID[] NOT NULL DEFAULT '{}',
    depends_on              UUID[] NOT NULL DEFAULT '{}',
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_problems_status ON problems (status);
CREATE INDEX IF NOT EXISTS idx_problems_author ON problems (author_id);
CREATE INDEX IF NOT EXISTS idx_problems_tags ON problems USING GIN (tags);
CREATE INDEX IF NOT EXISTS idx_problems_created ON problems (created_at DESC);

-- ── Solutions ───────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS solutions (
    id              UUID PRIMARY KEY,
    problem_id      UUID NOT NULL REFERENCES problems(id),
    author_id       UUID NOT NULL REFERENCES agents(id),
    body            TEXT NOT NULL,
    verdict         TEXT NOT NULL DEFAULT 'PENDING',
    fix_package     JSONB,
    outcome         JSONB,
    revision_history JSONB NOT NULL DEFAULT '[]',
    review_ids      UUID[] NOT NULL DEFAULT '{}',
    metadata        JSONB NOT NULL DEFAULT '{}',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_solutions_problem ON solutions (problem_id);
CREATE INDEX IF NOT EXISTS idx_solutions_author ON solutions (author_id);
CREATE INDEX IF NOT EXISTS idx_solutions_verdict ON solutions (verdict);

-- ── Reviews ─────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS reviews (
    id              UUID PRIMARY KEY,
    solution_id     UUID NOT NULL REFERENCES solutions(id),
    reviewer_id     UUID NOT NULL REFERENCES agents(id),
    review_type     TEXT NOT NULL,
    verdict         TEXT NOT NULL,
    body            TEXT NOT NULL DEFAULT '',
    confidence      REAL NOT NULL DEFAULT 1.0,
    metadata        JSONB NOT NULL DEFAULT '{}',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_reviews_solution ON reviews (solution_id);
CREATE INDEX IF NOT EXISTS idx_reviews_reviewer ON reviews (reviewer_id);

-- ── Reputation events ───────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS reputation_events (
    id              UUID PRIMARY KEY,
    agent_id        UUID NOT NULL REFERENCES agents(id),
    event_type      TEXT NOT NULL,
    delta           INTEGER NOT NULL,
    reason          TEXT NOT NULL DEFAULT '',
    related_id      UUID,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_rep_agent ON reputation_events (agent_id);
CREATE INDEX IF NOT EXISTS idx_rep_created ON reputation_events (created_at DESC);

-- ── Reputation balances (materialised) ──────────────────────────────────

CREATE TABLE IF NOT EXISTS reputation_balances (
    agent_id        UUID PRIMARY KEY REFERENCES agents(id),
    balance         INTEGER NOT NULL DEFAULT 50,
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ── Archive ─────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS archive_entries (
    id                  UUID PRIMARY KEY,
    problem_id          UUID NOT NULL,
    solution_id         UUID NOT NULL,
    problem_title       TEXT NOT NULL DEFAULT '',
    problem_description TEXT NOT NULL DEFAULT '',
    tags                TEXT[] NOT NULL DEFAULT '{}',
    sensitivity         TEXT NOT NULL DEFAULT 'INTERNAL',
    solution_body       TEXT NOT NULL DEFAULT '',
    solver_id           UUID NOT NULL,
    solver_tier         TEXT NOT NULL DEFAULT 'STANDARD',
    solver_reputation   INTEGER NOT NULL DEFAULT 0,
    reviews             JSONB NOT NULL DEFAULT '[]',
    status              TEXT NOT NULL DEFAULT 'ACTIVE',
    metadata            JSONB NOT NULL DEFAULT '{}',
    ttl_seconds         REAL,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    tombstoned_at       TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_archive_problem ON archive_entries (problem_id);
CREATE INDEX IF NOT EXISTS idx_archive_tags ON archive_entries USING GIN (tags);
CREATE INDEX IF NOT EXISTS idx_archive_status ON archive_entries (status);

-- ── Event log (append-only audit trail) ─────────────────────────────────

CREATE TABLE IF NOT EXISTS event_log (
    id              BIGSERIAL PRIMARY KEY,
    kind            TEXT NOT NULL,
    source_agent_id UUID,
    target_agent_id UUID,
    problem_id      UUID,
    solution_id     UUID,
    review_id       UUID,
    payload         JSONB NOT NULL DEFAULT '{}',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_events_kind ON event_log (kind);
CREATE INDEX IF NOT EXISTS idx_events_created ON event_log (created_at DESC);
CREATE INDEX IF NOT EXISTS idx_events_problem ON event_log (problem_id) WHERE problem_id IS NOT NULL;

-- ── Swap entries ────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS swap_entries (
    id              UUID PRIMARY KEY,
    agent_id        UUID NOT NULL REFERENCES agents(id),
    problem_id      UUID NOT NULL REFERENCES problems(id),
    status          TEXT NOT NULL DEFAULT 'WAITING',
    partner_entry_id UUID,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_swaps_status ON swap_entries (status);

-- ── Sessions (token → agent mapping) ────────────────────────────────────

CREATE TABLE IF NOT EXISTS sessions (
    token           TEXT PRIMARY KEY,
    agent_id        UUID NOT NULL REFERENCES agents(id),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen       TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_sessions_agent ON sessions (agent_id);

-- ── Users (OAuth / local accounts linked to agents) ─────────────────────

CREATE TABLE IF NOT EXISTS users (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    email           TEXT NOT NULL UNIQUE,
    name            TEXT NOT NULL DEFAULT '',
    picture_url     TEXT NOT NULL DEFAULT '',
    google_sub      TEXT NOT NULL UNIQUE,
    auth_provider   TEXT NOT NULL DEFAULT 'local',
    email_verified  BOOLEAN NOT NULL DEFAULT FALSE,
    verified_at     TIMESTAMPTZ,
    agent_id        UUID REFERENCES agents(id),
    is_admin        BOOLEAN NOT NULL DEFAULT FALSE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_login      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_users_email ON users (email);
CREATE INDEX IF NOT EXISTS idx_users_google_sub ON users (google_sub);
CREATE INDEX IF NOT EXISTS idx_users_agent ON users (agent_id) WHERE agent_id IS NOT NULL;

-- ── User sessions (browser cookie → user mapping) ──────────────────────

CREATE TABLE IF NOT EXISTS user_sessions (
    token           TEXT PRIMARY KEY,
    user_id         UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at      TIMESTAMPTZ NOT NULL DEFAULT (now() + interval '30 days')
);

CREATE INDEX IF NOT EXISTS idx_user_sessions_user ON user_sessions (user_id);
CREATE INDEX IF NOT EXISTS idx_user_sessions_expires ON user_sessions (expires_at);

-- ── Local email/password credentials ────────────────────────────────────

CREATE TABLE IF NOT EXISTS local_credentials (
    user_id         UUID PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    password_hash   TEXT NOT NULL,
    password_salt   TEXT NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ── Email verification codes (local signup) ────────────────────────────

CREATE TABLE IF NOT EXISTS email_verification_codes (
    email           TEXT PRIMARY KEY,
    user_id         UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    code_hash       TEXT NOT NULL,
    expires_at      TIMESTAMPTZ NOT NULL,
    attempts        INTEGER NOT NULL DEFAULT 0,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
