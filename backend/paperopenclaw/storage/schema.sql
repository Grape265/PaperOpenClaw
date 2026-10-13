-- PaperOpenClaw relational schema
-- ---------------------------------------------------------------------------
-- The relational database is the *index* over two other truth sources: the
-- OKF bundle on disk (human-readable) and the vector store (semantic). Every
-- row that points at a file stores a POSIX-style path relative to the data
-- directory so the whole directory stays movable across operating systems.
--
-- Requirements realised here:
--   FR-2  paper master table with a unique hash index, authors with affiliations
--   FR-4  assets (figN / tabN) cropped into fig/ and tab/
--   FR-5  OKF concept index
--   FR-6  RAG pyramid units (A/B/C) plus inter-layer and cross-paper links
--   FR-7  fishbone knowledge graph nodes and edges
--   FR-9  installed skills
--   FR-10 agent sessions, isolated sub-agents and their messages
--   FR-13 referential integrity across all stores
-- ---------------------------------------------------------------------------

PRAGMA foreign_keys = ON;

-- ===========================================================================
-- 1. Papers (FR-2)
-- ===========================================================================

CREATE TABLE IF NOT EXISTS papers (
    hash            TEXT PRIMARY KEY,              -- SHA-256 of the PDF bytes (FR-2.2)
    title           TEXT NOT NULL DEFAULT '',
    title_original  TEXT NOT NULL DEFAULT '',
    journal         TEXT NOT NULL DEFAULT '',      -- 期刊 / 会议
    venue_type      TEXT NOT NULL DEFAULT '',      -- journal | conference | preprint | thesis | other
    published_at    TEXT NOT NULL DEFAULT '',      -- ISO-8601 date, 刊发时间
    year            INTEGER,
    doi             TEXT NOT NULL DEFAULT '',
    url             TEXT NOT NULL DEFAULT '',
    abstract        TEXT NOT NULL DEFAULT '',
    keywords        TEXT NOT NULL DEFAULT '[]',    -- JSON array
    language        TEXT NOT NULL DEFAULT 'en',
    page_count      INTEGER NOT NULL DEFAULT 0,
    file_size       INTEGER NOT NULL DEFAULT 0,
    file_path       TEXT NOT NULL DEFAULT '',      -- papers/<hash>/paper.pdf
    okf_path        TEXT NOT NULL DEFAULT '',      -- papers/<hash>/okf
    status          TEXT NOT NULL DEFAULT 'pending',  -- pending|parsing|indexing|ready|failed
    error           TEXT NOT NULL DEFAULT '',
    parse_provider  TEXT NOT NULL DEFAULT '',
    parse_model     TEXT NOT NULL DEFAULT '',
    created_at      TEXT NOT NULL DEFAULT '',
    updated_at      TEXT NOT NULL DEFAULT ''
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_papers_hash   ON papers(hash);
CREATE        INDEX IF NOT EXISTS idx_papers_title  ON papers(title);
CREATE        INDEX IF NOT EXISTS idx_papers_year   ON papers(year);
CREATE        INDEX IF NOT EXISTS idx_papers_status ON papers(status);
CREATE        INDEX IF NOT EXISTS idx_papers_journal ON papers(journal);

-- Authors, ordered, with corresponding-author flag (FR-2.3)
CREATE TABLE IF NOT EXISTS authors (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    paper_hash      TEXT NOT NULL,
    order_index     INTEGER NOT NULL DEFAULT 0,
    name            TEXT NOT NULL,
    name_en         TEXT NOT NULL DEFAULT '',
    is_corresponding INTEGER NOT NULL DEFAULT 0,
    email           TEXT NOT NULL DEFAULT '',
    orcid           TEXT NOT NULL DEFAULT '',
    FOREIGN KEY (paper_hash) REFERENCES papers(hash) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_authors_paper ON authors(paper_hash, order_index);
CREATE INDEX IF NOT EXISTS idx_authors_name  ON authors(name);

-- Affiliations / institutions (FR-2.3, 单位)
CREATE TABLE IF NOT EXISTS affiliations (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    paper_hash      TEXT NOT NULL,
    order_index     INTEGER NOT NULL DEFAULT 0,
    name            TEXT NOT NULL,
    department      TEXT NOT NULL DEFAULT '',
    country         TEXT NOT NULL DEFAULT '',
    raw             TEXT NOT NULL DEFAULT '',
    FOREIGN KEY (paper_hash) REFERENCES papers(hash) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_affiliations_paper ON affiliations(paper_hash, order_index);
CREATE INDEX IF NOT EXISTS idx_affiliations_name  ON affiliations(name);

-- Many-to-many: one author may carry several institutions
CREATE TABLE IF NOT EXISTS author_affiliations (
    author_id       INTEGER NOT NULL,
    affiliation_id  INTEGER NOT NULL,
    PRIMARY KEY (author_id, affiliation_id),
    FOREIGN KEY (author_id)      REFERENCES authors(id)      ON DELETE CASCADE,
    FOREIGN KEY (affiliation_id) REFERENCES affiliations(id) ON DELETE CASCADE
);

-- ===========================================================================
-- 2. Assets: figures and tables (FR-4)
-- ===========================================================================

CREATE TABLE IF NOT EXISTS assets (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    paper_hash      TEXT NOT NULL,
    kind            TEXT NOT NULL,                 -- figure | table
    label           TEXT NOT NULL,                 -- fig1, fig2, tab1, ...
    ordinal         INTEGER NOT NULL DEFAULT 0,    -- order of appearance
    page            INTEGER NOT NULL DEFAULT 0,
    bbox            TEXT NOT NULL DEFAULT '[]',    -- JSON [x0, y0, x1, y1] in PDF points
    caption         TEXT NOT NULL DEFAULT '',
    caption_source  TEXT NOT NULL DEFAULT 'local', -- local | model | merged | none
    content_text    TEXT NOT NULL DEFAULT '',      -- OCR/described content when available
    section         TEXT NOT NULL DEFAULT '',      -- fishbone slug this asset belongs to
    file_path       TEXT NOT NULL,                 -- papers/<hash>/fig/fig1.png
    width           INTEGER NOT NULL DEFAULT 0,
    height          INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL DEFAULT '',
    FOREIGN KEY (paper_hash) REFERENCES papers(hash) ON DELETE CASCADE
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_assets_hash_label ON assets(paper_hash, label);
CREATE        INDEX IF NOT EXISTS idx_assets_hash_kind  ON assets(paper_hash, kind, ordinal);

-- ===========================================================================
-- 3. OKF concept index (FR-5)
-- ===========================================================================

CREATE TABLE IF NOT EXISTS okf_concepts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    paper_hash      TEXT NOT NULL,
    concept_id      TEXT NOT NULL,                 -- bundle-relative path without .md
    type            TEXT NOT NULL,                 -- OKF `type` frontmatter field
    title           TEXT NOT NULL DEFAULT '',
    description     TEXT NOT NULL DEFAULT '',
    section         TEXT NOT NULL DEFAULT '',      -- fishbone slug when applicable
    file_path       TEXT NOT NULL,                 -- papers/<hash>/okf/<concept_id>.md
    ordinal         INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL DEFAULT '',
    FOREIGN KEY (paper_hash) REFERENCES papers(hash) ON DELETE CASCADE
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_okf_concepts_unique ON okf_concepts(paper_hash, concept_id);
CREATE        INDEX IF NOT EXISTS idx_okf_concepts_hash   ON okf_concepts(paper_hash, ordinal);

-- ===========================================================================
-- 4. RAG pyramid (FR-6)
-- ===========================================================================

-- One semantic unit. Layer A = detailed digest, B = per-dimension要点, C = abstract.
CREATE TABLE IF NOT EXISTS rag_units (
    id              TEXT PRIMARY KEY,              -- a:<hash>:<n> | b:<hash>:<section> | c:<hash>
    layer           TEXT NOT NULL,                 -- A | B | C
    paper_hash      TEXT NOT NULL,
    ordinal         INTEGER NOT NULL DEFAULT 0,
    title           TEXT NOT NULL DEFAULT '',
    text            TEXT NOT NULL,
    section         TEXT NOT NULL DEFAULT '',      -- fishbone slug (A and B)
    source_ref      TEXT NOT NULL DEFAULT '',      -- OKF concept id this unit came from
    -- Structured payload: layer-B points become knowledge-graph content nodes,
    -- and asset_refs records which figN/tabN a unit mentions (FR-7.4).
    meta            TEXT NOT NULL DEFAULT '{}',
    char_count      INTEGER NOT NULL DEFAULT 0,
    token_count     INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL DEFAULT '',
    FOREIGN KEY (paper_hash) REFERENCES papers(hash) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_rag_units_layer ON rag_units(layer, paper_hash, ordinal);
CREATE INDEX IF NOT EXISTS idx_rag_units_section ON rag_units(section);

-- Inter-layer and cross-paper links (FR-6.5, FR-6.6).
-- relation: a_to_b | b_to_c | c_to_c | a_to_c
CREATE TABLE IF NOT EXISTS rag_links (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    src_unit        TEXT NOT NULL,
    dst_unit        TEXT NOT NULL,
    relation        TEXT NOT NULL,
    weight          REAL NOT NULL DEFAULT 1.0,
    meta            TEXT NOT NULL DEFAULT '{}',
    created_at      TEXT NOT NULL DEFAULT ''
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_rag_links_unique ON rag_links(src_unit, dst_unit, relation);
CREATE        INDEX IF NOT EXISTS idx_rag_links_src    ON rag_links(src_unit, relation);
CREATE        INDEX IF NOT EXISTS idx_rag_links_dst    ON rag_links(dst_unit, relation);

-- ===========================================================================
-- 5a. Fishbone graph (七维鱼骨，来自 RAG B 层要点，FR-7.1/7.2/7.4)
--    与 5b 的 Knowledge Graph 是两个独立库，分别存储、互不混淆。
-- ===========================================================================

CREATE TABLE IF NOT EXISTS fishbone_nodes (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    node_key        TEXT NOT NULL,
    paper_hash      TEXT DEFAULT NULL,            -- NULL = 七维全局骨架节点
    kind            TEXT NOT NULL,                -- spine | content | asset | paper
    section         TEXT NOT NULL DEFAULT '',     -- fishbone slug
    label           TEXT NOT NULL,
    detail          TEXT NOT NULL DEFAULT '',
    ordinal         INTEGER NOT NULL DEFAULT 0,
    asset_id        INTEGER,
    unit_id         TEXT NOT NULL DEFAULT '',     -- originating RAG unit (layer B)
    meta            TEXT NOT NULL DEFAULT '{}',
    created_at      TEXT NOT NULL DEFAULT '',
    FOREIGN KEY (paper_hash) REFERENCES papers(hash) ON DELETE CASCADE
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_fishbone_nodes_key    ON fishbone_nodes(node_key);
CREATE        INDEX IF NOT EXISTS idx_fishbone_nodes_paper  ON fishbone_nodes(paper_hash, section, ordinal);
CREATE        INDEX IF NOT EXISTS idx_fishbone_nodes_section ON fishbone_nodes(section);

CREATE TABLE IF NOT EXISTS fishbone_edges (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    src_key         TEXT NOT NULL,
    dst_key         TEXT NOT NULL,
    relation        TEXT NOT NULL,                -- has_spine | spine_order | has_content | belongs_to | has_asset
    weight          REAL NOT NULL DEFAULT 1.0,
    meta            TEXT NOT NULL DEFAULT '{}',
    created_at      TEXT NOT NULL DEFAULT ''
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_fishbone_edges_unique ON fishbone_edges(src_key, dst_key, relation);
CREATE        INDEX IF NOT EXISTS idx_fishbone_edges_src    ON fishbone_edges(src_key);
CREATE        INDEX IF NOT EXISTS idx_fishbone_edges_dst    ON fishbone_edges(dst_key);

-- ===========================================================================
-- 5b. Knowledge graph (关键词图，来自 RAG A 层抽取的关键词，FR-7.1 重设计)
-- ===========================================================================

CREATE TABLE IF NOT EXISTS kg_nodes (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    node_key        TEXT NOT NULL,
    -- NULL means a global node shared by every paper (the seven spine nodes).
    -- A NULL foreign key is always satisfied, so spine nodes survive while
    -- paper-owned nodes still cascade on delete.
    paper_hash      TEXT DEFAULT NULL,
    kind            TEXT NOT NULL,                 -- spine | content | asset | paper
    section         TEXT NOT NULL DEFAULT '',      -- fishbone slug
    label           TEXT NOT NULL,
    detail          TEXT NOT NULL DEFAULT '',
    ordinal         INTEGER NOT NULL DEFAULT 0,
    asset_id        INTEGER,
    unit_id         TEXT NOT NULL DEFAULT '',      -- originating RAG unit (layer B)
    meta            TEXT NOT NULL DEFAULT '{}',
    created_at      TEXT NOT NULL DEFAULT '',
    FOREIGN KEY (paper_hash) REFERENCES papers(hash) ON DELETE CASCADE
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_kg_nodes_key    ON kg_nodes(node_key);
CREATE        INDEX IF NOT EXISTS idx_kg_nodes_paper  ON kg_nodes(paper_hash, section, ordinal);
CREATE        INDEX IF NOT EXISTS idx_kg_nodes_section ON kg_nodes(section);

CREATE TABLE IF NOT EXISTS kg_edges (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    src_key         TEXT NOT NULL,
    dst_key         TEXT NOT NULL,
    relation        TEXT NOT NULL,                 -- has_spine | spine_order | has_content | belongs_to | has_asset | mentions
    weight          REAL NOT NULL DEFAULT 1.0,
    meta            TEXT NOT NULL DEFAULT '{}',
    created_at      TEXT NOT NULL DEFAULT ''
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_kg_edges_unique ON kg_edges(src_key, dst_key, relation);
CREATE        INDEX IF NOT EXISTS idx_kg_edges_src    ON kg_edges(src_key);
CREATE        INDEX IF NOT EXISTS idx_kg_edges_dst    ON kg_edges(dst_key);

-- ===========================================================================
-- 6. Skills (FR-9)
-- ===========================================================================

CREATE TABLE IF NOT EXISTS skills (
    id              TEXT PRIMARY KEY,              -- directory name
    name            TEXT NOT NULL DEFAULT '',
    description     TEXT NOT NULL DEFAULT '',
    version         TEXT NOT NULL DEFAULT '',
    author          TEXT NOT NULL DEFAULT '',
    path            TEXT NOT NULL,                 -- skills/<id>
    enabled         INTEGER NOT NULL DEFAULT 1,
    tools           TEXT NOT NULL DEFAULT '[]',    -- JSON array of declared tool names
    instructions    TEXT NOT NULL DEFAULT '',      -- SKILL.md body
    frontmatter     TEXT NOT NULL DEFAULT '{}',
    installed_at    TEXT NOT NULL DEFAULT '',
    updated_at      TEXT NOT NULL DEFAULT '',
    source          TEXT NOT NULL DEFAULT ''       -- install source (path/zip)
);

-- ===========================================================================
-- 7. Sessions, agents and messages (FR-10, FR-11)
-- ===========================================================================

CREATE TABLE IF NOT EXISTS sessions (
    id              TEXT PRIMARY KEY,
    kind            TEXT NOT NULL,                 -- qa | brainstorm | agent
    title           TEXT NOT NULL DEFAULT '',
    topic           TEXT NOT NULL DEFAULT '',
    paper_hashes    TEXT NOT NULL DEFAULT '[]',    -- JSON array
    status          TEXT NOT NULL DEFAULT 'idle',  -- idle | running | done | failed
    rounds          INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL DEFAULT '',
    updated_at      TEXT NOT NULL DEFAULT '',
    meta            TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_sessions_kind ON sessions(kind, created_at);

-- One isolated sub-agent instance (FR-10.2)
CREATE TABLE IF NOT EXISTS agents (
    id              TEXT NOT NULL,                 -- profile id, e.g. methodologist
    session_id      TEXT NOT NULL,
    name            TEXT NOT NULL DEFAULT '',
    persona         TEXT NOT NULL DEFAULT '',
    profile_id      TEXT NOT NULL DEFAULT '',
    provider        TEXT NOT NULL DEFAULT '',
    model           TEXT NOT NULL DEFAULT '',
    tools           TEXT NOT NULL DEFAULT '[]',    -- tool allowlist (isolation)
    paper_hashes    TEXT NOT NULL DEFAULT '[]',    -- visible paper scope (isolation)
    workspace       TEXT NOT NULL DEFAULT '',      -- isolated scratch dir (isolation)
    status          TEXT NOT NULL DEFAULT 'idle',
    created_at      TEXT NOT NULL DEFAULT '',
    meta            TEXT NOT NULL DEFAULT '{}',
    PRIMARY KEY (session_id, id),
    FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS messages (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id      TEXT NOT NULL,
    agent_id        TEXT NOT NULL DEFAULT '',
    round           INTEGER NOT NULL DEFAULT 0,
    role            TEXT NOT NULL,                 -- system | user | assistant | tool | blackboard | synthesis
    content         TEXT NOT NULL DEFAULT '',
    tool_calls      TEXT NOT NULL DEFAULT '[]',
    tool_call_id    TEXT NOT NULL DEFAULT '',
    tool_name       TEXT NOT NULL DEFAULT '',
    citations       TEXT NOT NULL DEFAULT '[]',
    usage           TEXT NOT NULL DEFAULT '{}',
    created_at      TEXT NOT NULL DEFAULT '',
    FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id, id);
CREATE INDEX IF NOT EXISTS idx_messages_agent   ON messages(session_id, agent_id, id);

-- Durable log of an ingest job, also used as an audit trail
CREATE TABLE IF NOT EXISTS jobs (
    id              TEXT PRIMARY KEY,
    kind            TEXT NOT NULL,                 -- ingest | reparse | rebuild
    hash            TEXT NOT NULL DEFAULT '',
    filename        TEXT NOT NULL DEFAULT '',
    stage           TEXT NOT NULL DEFAULT '',
    stage_index     INTEGER NOT NULL DEFAULT 0,
    stage_total     INTEGER NOT NULL DEFAULT 7,
    status          TEXT NOT NULL DEFAULT 'pending', -- pending|running|done|failed|cancelled
    progress        REAL NOT NULL DEFAULT 0.0,
    message         TEXT NOT NULL DEFAULT '',
    error           TEXT NOT NULL DEFAULT '',
    stages          TEXT NOT NULL DEFAULT '[]',    -- JSON: per-stage timings
    usage           TEXT NOT NULL DEFAULT '{}',    -- JSON: model token usage
    started_at      TEXT NOT NULL DEFAULT '',
    finished_at     TEXT NOT NULL DEFAULT '',
    created_at      TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_jobs_status  ON jobs(status, created_at);
CREATE INDEX IF NOT EXISTS idx_jobs_hash    ON jobs(hash);

-- Cross-agent blackboard: the only sanctioned channel between isolated agents (FR-10.5)
CREATE TABLE IF NOT EXISTS blackboard (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id      TEXT NOT NULL,
    round           INTEGER NOT NULL DEFAULT 0,
    author_agent    TEXT NOT NULL DEFAULT '',
    entry_type      TEXT NOT NULL DEFAULT 'note',  -- note | summary | disagreement | evidence
    content         TEXT NOT NULL DEFAULT '',
    citations       TEXT NOT NULL DEFAULT '[]',
    created_at      TEXT NOT NULL DEFAULT '',
    FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_blackboard_session ON blackboard(session_id, round, id);

-- Embedding model provenance: which model produced the stored vectors
CREATE TABLE IF NOT EXISTS embedding_meta (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    collection      TEXT NOT NULL,
    provider        TEXT NOT NULL,
    model           TEXT NOT NULL,
    dimension       INTEGER NOT NULL DEFAULT 0,
    unit_count      INTEGER NOT NULL DEFAULT 0,
    updated_at      TEXT NOT NULL DEFAULT ''
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_embedding_meta_unique ON embedding_meta(collection, provider, model);
