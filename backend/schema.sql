-- recommenderr.db
-- Owns: external world cache + computed signals + recommendation graph.
-- Cross-DB references to ytvideo (subscriptions, categories) are opaque IDs (no FK).

CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER PRIMARY KEY,
    applied_at REAL NOT NULL
);

-- ----- Fetched video/channel data -----

CREATE TABLE IF NOT EXISTS video_metadata (
    video_id TEXT PRIMARY KEY,
    genre TEXT,
    description TEXT,
    view_count INTEGER,
    like_count INTEGER,
    fetched_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS video_keywords (
    video_id TEXT NOT NULL,
    keyword TEXT NOT NULL,
    PRIMARY KEY (video_id, keyword)
);
CREATE INDEX IF NOT EXISTS idx_vk_keyword ON video_keywords(keyword);

CREATE TABLE IF NOT EXISTS channel_stats (
    channel_id TEXT PRIMARY KEY,
    channel_name TEXT,
    thumbnail TEXT,
    sub_count INTEGER,
    video_count INTEGER,
    last_upload_at REAL,
    avg_interval_days REAL,
    pattern TEXT,
    themes TEXT,
    recent_videos TEXT,
    fetched_at REAL
);

-- ----- Crawler -----

CREATE TABLE IF NOT EXISTS crawl_queue (
    video_id TEXT PRIMARY KEY,
    title TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    added_at REAL NOT NULL,
    crawled_at REAL,
    retry_count INTEGER NOT NULL DEFAULT 0,
    next_retry_at REAL
);
CREATE INDEX IF NOT EXISTS idx_cq_status ON crawl_queue(status);

-- ----- Recommendation graph + PPR -----

CREATE TABLE IF NOT EXISTS recommendation_edges (
    source_video_id TEXT NOT NULL,
    target_video_id TEXT NOT NULL,
    weight REAL NOT NULL DEFAULT 1.0,
    added_at REAL NOT NULL,
    PRIMARY KEY (source_video_id, target_video_id)
);
CREATE INDEX IF NOT EXISTS idx_re_target ON recommendation_edges(target_video_id);

CREATE TABLE IF NOT EXISTS ppr_scores (
    video_id TEXT PRIMARY KEY,
    score REAL NOT NULL,
    computed_at REAL NOT NULL,
    spam_mass REAL
);
CREATE INDEX IF NOT EXISTS idx_ppr_score ON ppr_scores(score DESC);

CREATE TABLE IF NOT EXISTS feed_recommendations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    video_id TEXT NOT NULL,
    title TEXT NOT NULL,
    thumbnail TEXT,
    duration INTEGER,
    author TEXT,
    author_id TEXT,
    source_video_id TEXT NOT NULL,
    source_video_title TEXT,
    added_at REAL NOT NULL,
    published_at REAL
);
CREATE INDEX IF NOT EXISTS idx_feed_added ON feed_recommendations(added_at DESC);
CREATE INDEX IF NOT EXISTS idx_feed_video ON feed_recommendations(video_id);

-- ----- Category recommendations (per-category PPR cache) -----
-- category_id is opaque; the canonical category tree lives in ytvideo.db.

CREATE TABLE IF NOT EXISTS category_recommendations (
    category_id INTEGER NOT NULL,
    video_id    TEXT    NOT NULL,
    score       REAL    NOT NULL,
    title       TEXT,
    author      TEXT,
    author_id   TEXT,
    thumbnail   TEXT,
    duration    INTEGER,
    computed_at REAL    NOT NULL,
    PRIMARY KEY (category_id, video_id)
);
CREATE INDEX IF NOT EXISTS idx_catrecs_cat_score
    ON category_recommendations(category_id, score DESC);

CREATE TABLE IF NOT EXISTS category_rec_jobs (
    category_id  INTEGER PRIMARY KEY,
    status       TEXT NOT NULL DEFAULT 'pending',
    last_run_at  REAL,
    next_run_at  REAL,
    last_error   TEXT
);

-- ----- Recommendation tuning (configured via admin UI) -----

CREATE TABLE IF NOT EXISTS weight_rules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    rule_type TEXT NOT NULL,
    match_value TEXT NOT NULL,
    multiplier REAL NOT NULL DEFAULT 2.0,
    created_at REAL NOT NULL,
    UNIQUE(rule_type, match_value)
);

CREATE TABLE IF NOT EXISTS attributes (
    name TEXT PRIMARY KEY,
    description TEXT DEFAULT '',
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS video_attribute_scores (
    video_id TEXT NOT NULL,
    attribute TEXT NOT NULL,
    score REAL NOT NULL,
    scored_at REAL NOT NULL,
    PRIMARY KEY (video_id, attribute)
);
CREATE INDEX IF NOT EXISTS idx_vas_attr ON video_attribute_scores(attribute);

CREATE TABLE IF NOT EXISTS channel_attribute_scores (
    channel_id TEXT NOT NULL,
    channel_name TEXT,
    attribute TEXT NOT NULL,
    score REAL NOT NULL,
    scored_at REAL NOT NULL,
    PRIMARY KEY (channel_id, attribute)
);
CREATE INDEX IF NOT EXISTS idx_cas_attr ON channel_attribute_scores(attribute);

-- ----- Music metadata cache -----

CREATE TABLE IF NOT EXISTS music_library (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    video_id TEXT NOT NULL UNIQUE,
    title TEXT,
    thumbnail TEXT,
    duration INTEGER,
    author TEXT,
    author_id TEXT,
    track TEXT,
    artist TEXT,
    album TEXT,
    genre TEXT,
    source_job_id INTEGER,
    source_video_id TEXT,
    added_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ml_genre ON music_library(genre);
CREATE INDEX IF NOT EXISTS idx_ml_added ON music_library(added_at DESC);

CREATE TABLE IF NOT EXISTS music_jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    playlist_id INTEGER,
    playlist_title TEXT,
    status TEXT DEFAULT 'pending',
    total INTEGER DEFAULT 0,
    processed INTEGER DEFAULT 0,
    errors INTEGER DEFAULT 0,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

-- New: recognition cache so ytvideo/ytmusic can ask "is this music?" cheaply.
CREATE TABLE IF NOT EXISTS recognition_cache (
    video_id TEXT PRIMARY KEY,
    is_music INTEGER NOT NULL,
    confidence REAL NOT NULL,
    track TEXT,
    artist TEXT,
    album TEXT,
    isrc TEXT,
    sources TEXT,
    recognized_at REAL NOT NULL
);

-- ----- Artist release radar (computed/fetched; user-follow choice is in ytmusic) -----

CREATE TABLE IF NOT EXISTS artist_release_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    artist_key TEXT NOT NULL,
    artist_name TEXT NOT NULL,
    release_key TEXT NOT NULL,
    title TEXT NOT NULL,
    release_date TEXT,
    cover_art TEXT,
    source TEXT,
    created_at REAL NOT NULL,
    UNIQUE(artist_key, release_key)
);
CREATE INDEX IF NOT EXISTS idx_artist_release_events_created
    ON artist_release_events(created_at DESC);

-- ----- Content classification override (consulted by both frontends) -----

CREATE TABLE IF NOT EXISTS video_media_overrides (
    video_id TEXT PRIMARY KEY,
    media_override TEXT NOT NULL,
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_video_media_overrides_updated
    ON video_media_overrides(updated_at DESC);

-- ----- Stub tables: owned by ytvideo / ytmusic (will be replaced by REST calls in Phase 5+) -----
-- These keep the workers alive in standalone mode without errors.

CREATE TABLE IF NOT EXISTS watch_history (
    video_id TEXT PRIMARY KEY,
    title TEXT,
    author_id TEXT,
    watched_at REAL
);

CREATE TABLE IF NOT EXISTS playlist_videos (
    playlist_id INTEGER NOT NULL,
    video_id TEXT NOT NULL,
    title TEXT,
    author_id TEXT,
    PRIMARY KEY (playlist_id, video_id)
);

CREATE TABLE IF NOT EXISTS categories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    parent_id INTEGER,
    description TEXT,
    created_at REAL
);

CREATE TABLE IF NOT EXISTS video_category_assignments (
    video_id TEXT NOT NULL,
    category_id INTEGER NOT NULL,
    PRIMARY KEY (video_id, category_id)
);

CREATE TABLE IF NOT EXISTS channel_category_assignments (
    channel_id TEXT NOT NULL,
    category_id INTEGER NOT NULL,
    PRIMARY KEY (channel_id, category_id)
);

CREATE TABLE IF NOT EXISTS category_tags (
    category_id INTEGER NOT NULL,
    tag_id INTEGER NOT NULL,
    PRIMARY KEY (category_id, tag_id)
);

CREATE TABLE IF NOT EXISTS video_tags (
    video_id TEXT NOT NULL,
    tag_id INTEGER NOT NULL,
    PRIMARY KEY (video_id, tag_id)
);


-- ----- Stub tables: ratings + filters owned by ytvideo/ytmusic (synced via REST in Phase 5+) -----

CREATE TABLE IF NOT EXISTS video_ratings (
    video_id TEXT PRIMARY KEY,
    rating TEXT NOT NULL,
    rated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS channel_ratings (
    channel_id TEXT PRIMARY KEY,
    channel_name TEXT,
    rating TEXT NOT NULL,
    rated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS feed_filters (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    filter_type TEXT NOT NULL,
    match_value TEXT NOT NULL,
    created_at REAL NOT NULL,
    UNIQUE(filter_type, match_value)
);

CREATE TABLE IF NOT EXISTS album_ratings (
    album_key TEXT PRIMARY KEY,
    album_title TEXT NOT NULL,
    album_artist TEXT,
    cover_art TEXT,
    source TEXT,
    playlist_id TEXT,
    playlist_title TEXT,
    rating INTEGER NOT NULL,
    rated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS album_tracks (
    video_id TEXT PRIMARY KEY,
    album_key TEXT NOT NULL,
    album_title TEXT NOT NULL,
    album_artist TEXT,
    playlist_id TEXT,
    playlist_title TEXT,
    track_index INTEGER,
    added_at REAL NOT NULL
);


CREATE TABLE IF NOT EXISTS feed_feedback (
    video_id TEXT NOT NULL,
    category TEXT NOT NULL DEFAULT '',
    feedback INTEGER NOT NULL,
    author_id TEXT,
    created_at REAL NOT NULL,
    dislike_reason TEXT,
    PRIMARY KEY (video_id, category)
);
CREATE INDEX IF NOT EXISTS idx_ff_category ON feed_feedback(category);
CREATE INDEX IF NOT EXISTS idx_ff_author ON feed_feedback(author_id);

-- video_categories: flat per-video category (owned by ytvideo, mirrored here)
CREATE TABLE IF NOT EXISTS video_categories (
    video_id TEXT PRIMARY KEY,
    category TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'auto',
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_vc_category ON video_categories(category);

-- tags + channel_tags: tag name lookup + channel tag assignments
CREATE TABLE IF NOT EXISTS tags (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE COLLATE NOCASE,
    description TEXT DEFAULT '',
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS channel_tags (
    channel_id TEXT NOT NULL,
    tag_id INTEGER NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
    PRIMARY KEY (channel_id, tag_id)
);
CREATE INDEX IF NOT EXISTS idx_ctag_tag ON channel_tags(tag_id);

-- ----- Stub table: artist_follows (owned by ytmusic in Phase 4+) -----
CREATE TABLE IF NOT EXISTS artist_follows (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    artist_key TEXT NOT NULL UNIQUE,
    artist_name TEXT NOT NULL,
    image TEXT,
    source TEXT,
    spotify_artist_id TEXT,
    deezer_artist_id TEXT,
    itunes_artist_id TEXT,
    last_release_key TEXT,
    last_release_title TEXT,
    last_release_date TEXT,
    last_release_cover_art TEXT,
    last_release_source TEXT,
    last_checked_at REAL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

-- ----- Radio service: graph cache + Bandcamp album lookup -----
CREATE TABLE IF NOT EXISTS radio_graph_cache (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    seed_key TEXT NOT NULL,
    track TEXT NOT NULL DEFAULT '',
    artist TEXT NOT NULL DEFAULT '',
    video_id TEXT NOT NULL,
    title TEXT,
    thumbnail TEXT,
    duration INTEGER,
    author TEXT,
    author_id TEXT,
    sources TEXT,
    score REAL NOT NULL DEFAULT 0.5,
    is_music_confirmed INTEGER NOT NULL DEFAULT 0,
    fetched_at REAL NOT NULL,
    UNIQUE(seed_key, video_id)
);
CREATE INDEX IF NOT EXISTS idx_radio_graph_seed ON radio_graph_cache(seed_key);
CREATE INDEX IF NOT EXISTS idx_radio_graph_fetched ON radio_graph_cache(fetched_at DESC);

CREATE TABLE IF NOT EXISTS radio_bandcamp_lookup (
    seed_key TEXT PRIMARY KEY,
    bandcamp_url TEXT,
    fetched_at REAL NOT NULL
);

-- Music tag tables (also owned by recommenderr for video-info enrichment)
CREATE TABLE IF NOT EXISTS music_tag_groups (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    system_key TEXT UNIQUE,
    position INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_music_tag_groups_name
    ON music_tag_groups(lower(name));

CREATE TABLE IF NOT EXISTS music_tags (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    parent_id INTEGER REFERENCES music_tags(id) ON DELETE SET NULL,
    position INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    kind TEXT NOT NULL DEFAULT 'existing',
    group_id INTEGER REFERENCES music_tag_groups(id) ON DELETE SET NULL
);
CREATE INDEX IF NOT EXISTS idx_music_tags_parent
    ON music_tags(parent_id, position);
CREATE INDEX IF NOT EXISTS idx_music_tags_kind_parent
    ON music_tags(kind, parent_id, position);
CREATE INDEX IF NOT EXISTS idx_music_tags_group_parent
    ON music_tags(group_id, parent_id, position);

CREATE TABLE IF NOT EXISTS music_tag_assignments (
    tag_id INTEGER NOT NULL REFERENCES music_tags(id) ON DELETE CASCADE,
    video_id TEXT NOT NULL,
    created_at REAL NOT NULL,
    PRIMARY KEY (tag_id, video_id)
);
CREATE INDEX IF NOT EXISTS idx_music_tag_assignments_video
    ON music_tag_assignments(video_id);

CREATE TABLE IF NOT EXISTS custom_categories (
    name TEXT PRIMARY KEY,
    keywords TEXT NOT NULL DEFAULT "",
    created_at REAL
);

-- ----- PPR config (runtime-editable algorithm knobs) -----
-- NOTE: this table is read by routers/ppr.py but was previously missing from
-- schema.sql (it was created by hand in prod). Declaring it here ensures fresh
-- DBs are fully initialised.
CREATE TABLE IF NOT EXISTS ppr_config (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at REAL NOT NULL DEFAULT 0
);

-- ----- Item abstraction (schemes + generic item store) -----

-- schemes: declares a content type and its field structure.
-- Users can add new schemes via the admin UI; built-in ones are seeded on startup.
CREATE TABLE IF NOT EXISTS schemes (
    name         TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    description  TEXT,
    fields_json  TEXT NOT NULL DEFAULT '[]',  -- [{name, type, label, required}]
    created_at   REAL NOT NULL
);

-- items: one row per discrete piece of content, across all schemes.
-- external_id is the scheme-internal natural key (video_id, artist_key, album_key…).
-- metadata_json stores all scheme-declared field values.
CREATE TABLE IF NOT EXISTS items (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    scheme      TEXT NOT NULL REFERENCES schemes(name),
    external_id TEXT NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    added_at    REAL NOT NULL,
    UNIQUE(scheme, external_id)
);
CREATE INDEX IF NOT EXISTS idx_items_scheme     ON items(scheme);
CREATE INDEX IF NOT EXISTS idx_items_added      ON items(added_at DESC);

-- item_aliases: links two items that represent the same physical thing.
-- E.g. a music_track and the yt_video that embeds it share an alias.
-- Lets PPR attribute graph edges to either representation.
CREATE TABLE IF NOT EXISTS item_aliases (
    item_id          INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    alias_scheme     TEXT    NOT NULL,
    alias_external_id TEXT   NOT NULL,
    PRIMARY KEY (alias_scheme, alias_external_id)
);
CREATE INDEX IF NOT EXISTS idx_item_aliases_item ON item_aliases(item_id);

-- ----- Source registry -----
-- Declarative source state: code declares available sources, DB persists runtime state.
-- Seeded on startup from source_registry.SOURCES_DECL via INSERT OR IGNORE.
CREATE TABLE IF NOT EXISTS sources (
    name                TEXT PRIMARY KEY,
    display_name        TEXT NOT NULL,
    kind                TEXT NOT NULL DEFAULT 'api',   -- api | scraper | extractor | feed
    enabled             INTEGER NOT NULL DEFAULT 1,
    weight              REAL NOT NULL DEFAULT 1.0,
    credentials_json    TEXT,                          -- {env_var: override_value} — write-only over HTTP
    rate_limit_per_min  INTEGER,
    last_success_at     REAL,
    last_error_at       REAL,
    last_error          TEXT,
    failure_streak      INTEGER NOT NULL DEFAULT 0,
    circuit_open_until  REAL,
    metadata_json       TEXT                           -- env_var declarations, etc.
);

-- ----- Personas (synthetic seed bundles for topic-sensitive PPR) -----

CREATE TABLE IF NOT EXISTS personas (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL UNIQUE,
    description TEXT,
    scheme      TEXT NOT NULL DEFAULT 'yt_video',
    alpha       REAL NOT NULL DEFAULT 0.15,
    min_seed_rating INTEGER NOT NULL DEFAULT 0,
    created_at  REAL NOT NULL,
    updated_at  REAL NOT NULL,
    version     INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS persona_seeds (
    persona_id  INTEGER NOT NULL REFERENCES personas(id) ON DELETE CASCADE,
    item_id     INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    weight      REAL NOT NULL DEFAULT 1.0,
    PRIMARY KEY (persona_id, item_id)
);

CREATE TABLE IF NOT EXISTS persona_scores (
    persona_id  INTEGER NOT NULL REFERENCES personas(id) ON DELETE CASCADE,
    video_id    TEXT NOT NULL,
    score       REAL NOT NULL,
    spam_mass   REAL,
    computed_at REAL NOT NULL,
    PRIMARY KEY (persona_id, video_id)
);
CREATE INDEX IF NOT EXISTS idx_ps_persona_score ON persona_scores(persona_id, score DESC);

CREATE TABLE IF NOT EXISTS persona_jobs (
    persona_id      INTEGER PRIMARY KEY REFERENCES personas(id) ON DELETE CASCADE,
    status          TEXT NOT NULL DEFAULT 'pending',
    last_run_at     REAL,
    next_run_at     REAL,
    last_error      TEXT,
    claimed_version INTEGER
);
