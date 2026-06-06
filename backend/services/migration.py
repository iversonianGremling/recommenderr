"""One-shot idempotent migrations for recommenderr.db schema evolution."""
from __future__ import annotations

import json
import logging
import time

logger = logging.getLogger("migration")

# ---------------------------------------------------------------------------
# Built-in scheme declarations
# ---------------------------------------------------------------------------

_SCHEMES: list[dict] = [
    {
        "name": "yt_video",
        "display_name": "YouTube Video",
        "description": "Video fetched via Invidious or yt-dlp",
        "fields": [
            {"name": "title",        "type": "text",      "label": "Title",    "required": True},
            {"name": "author",       "type": "text",      "label": "Channel"},
            {"name": "author_id",    "type": "text",      "label": "Channel ID"},
            {"name": "thumbnail",    "type": "url-image", "label": "Thumbnail"},
            {"name": "duration",     "type": "duration",  "label": "Duration"},
            {"name": "published_at", "type": "date",      "label": "Published"},
            {"name": "genre",        "type": "text",      "label": "Genre"},
        ],
    },
    {
        "name": "music_track",
        "display_name": "Music Track",
        "description": "Track from music_library, recognised via multi-source lookup",
        "fields": [
            {"name": "title",    "type": "text",      "label": "Title",   "required": True},
            {"name": "artist",   "type": "text",      "label": "Artist"},
            {"name": "album",    "type": "text",      "label": "Album"},
            {"name": "genre",    "type": "text",      "label": "Genre"},
            {"name": "isrc",     "type": "text",      "label": "ISRC"},
            {"name": "duration", "type": "duration",  "label": "Duration"},
            {"name": "cover_art","type": "url-image", "label": "Cover"},
        ],
    },
    {
        "name": "music_album",
        "display_name": "Music Album",
        "description": "Album from album_ratings",
        "fields": [
            {"name": "title",        "type": "text",      "label": "Title",  "required": True},
            {"name": "artist",       "type": "text",      "label": "Artist"},
            {"name": "cover_art",    "type": "url-image", "label": "Cover"},
            {"name": "source",       "type": "text",      "label": "Source"},
            {"name": "rating",       "type": "number",    "label": "Rating"},
            {"name": "release_date", "type": "date",      "label": "Released"},
        ],
    },
    {
        "name": "music_artist",
        "display_name": "Music Artist",
        "description": "Artist from artist_follows",
        "fields": [
            {"name": "name",               "type": "text",      "label": "Name",    "required": True},
            {"name": "image",              "type": "url-image", "label": "Image"},
            {"name": "source",             "type": "text",      "label": "Source"},
            {"name": "spotify_artist_id",  "type": "text",      "label": "Spotify ID"},
            {"name": "deezer_artist_id",   "type": "text",      "label": "Deezer ID"},
            {"name": "itunes_artist_id",   "type": "text",      "label": "iTunes ID"},
        ],
    },
]


# ---------------------------------------------------------------------------
# Migration
# ---------------------------------------------------------------------------

def migrate_to_items_v1(conn) -> None:
    """
    Idempotent backfill: seed schemes + populate items from existing domain tables.
    conn must already have foreign_keys=ON and be committed after.
    Only runs if schema_version < 2.
    """
    row = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()
    current = row[0] if row and row[0] else 0
    if current >= 2:
        logger.debug("migrate_to_items_v1: already at version %d, skipping", current)
        return

    logger.info("migrate_to_items_v1: starting (current version=%d)", current)
    t0 = time.monotonic()

    _seed_schemes(conn)
    n_yt      = _backfill_yt_video(conn)
    n_music   = _backfill_music_track(conn)
    n_albums  = _backfill_music_album(conn)
    n_artists = _backfill_music_artist(conn)

    conn.execute(
        "INSERT OR IGNORE INTO schema_version(version, applied_at) VALUES (2, ?)",
        (time.time(),),
    )
    conn.commit()

    elapsed = time.monotonic() - t0
    logger.info(
        "migrate_to_items_v1: done in %.1fs — yt_video=%d music_track=%d music_album=%d music_artist=%d",
        elapsed, n_yt, n_music, n_albums, n_artists,
    )


def _seed_schemes(conn) -> None:
    for s in _SCHEMES:
        conn.execute(
            """
            INSERT INTO schemes (name, display_name, description, fields_json, created_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(name) DO UPDATE SET
                display_name = excluded.display_name,
                description  = excluded.description,
                fields_json  = excluded.fields_json
            """,
            (s["name"], s["display_name"], s.get("description", ""),
             json.dumps(s["fields"]), time.time()),
        )


def _backfill_yt_video(conn) -> int:
    """Populate items from video_metadata, enriching title/author from crawled rows."""
    rows = conn.execute(
        """
        SELECT
            vm.video_id,
            vm.genre,
            COALESCE(fr.title, wh.title, pv.title, vm.video_id) AS title,
            fr.author                                             AS author,
            COALESCE(fr.author_id, wh.author_id, pv.author_id)  AS author_id,
            fr.thumbnail                                          AS thumbnail,
            fr.duration                                           AS duration,
            fr.published_at                                       AS published_at,
            vm.fetched_at
        FROM video_metadata vm
        LEFT JOIN (
            SELECT video_id, title, author, author_id, thumbnail, duration, published_at
            FROM feed_recommendations
            GROUP BY video_id
        ) fr ON fr.video_id = vm.video_id
        LEFT JOIN watch_history wh ON wh.video_id = vm.video_id
        LEFT JOIN (
            SELECT video_id, title, author_id
            FROM playlist_videos
            GROUP BY video_id
        ) pv ON pv.video_id = vm.video_id
        """,
    ).fetchall()

    now = time.time()
    count = 0
    for r in rows:
        meta = {
            "title":        r["title"],
            "author":       r["author"],
            "author_id":    r["author_id"],
            "thumbnail":    r["thumbnail"],
            "duration":     r["duration"],
            "published_at": r["published_at"],
            "genre":        r["genre"],
        }
        conn.execute(
            """
            INSERT INTO items (scheme, external_id, metadata_json, added_at)
            VALUES ('yt_video', ?, ?, ?)
            ON CONFLICT(scheme, external_id) DO NOTHING
            """,
            (r["video_id"], json.dumps(meta), r["fetched_at"] or now),
        )
        count += 1

    return count


def _backfill_music_track(conn) -> int:
    """Populate items from music_library + add yt_video alias for each."""
    rows = conn.execute(
        """
        SELECT id, video_id, track, artist, album, genre, thumbnail, duration, added_at
        FROM music_library
        """
    ).fetchall()

    now = time.time()
    count = 0
    for r in rows:
        # Use "ml:<id>" as the external_id so it's stable even if video_id is reused
        ext_id = f"ml:{r['id']}"
        meta = {
            "title":    r["track"] or r["video_id"],
            "artist":   r["artist"],
            "album":    r["album"],
            "genre":    r["genre"],
            "cover_art": r["thumbnail"],
            "duration": r["duration"],
        }
        conn.execute(
            """
            INSERT INTO items (scheme, external_id, metadata_json, added_at)
            VALUES ('music_track', ?, ?, ?)
            ON CONFLICT(scheme, external_id) DO NOTHING
            """,
            (ext_id, json.dumps(meta), r["added_at"] or now),
        )
        # Link the music_track to its yt_video item via an alias on the yt_video side
        item_row = conn.execute(
            "SELECT id FROM items WHERE scheme='music_track' AND external_id=?",
            (ext_id,),
        ).fetchone()
        if item_row and r["video_id"]:
            conn.execute(
                """
                INSERT OR IGNORE INTO item_aliases (item_id, alias_scheme, alias_external_id)
                VALUES (?, 'yt_video', ?)
                """,
                (item_row["id"], r["video_id"]),
            )
        count += 1

    return count


def _backfill_music_album(conn) -> int:
    rows = conn.execute(
        "SELECT album_key, album_title, album_artist, cover_art, source, rating, rated_at FROM album_ratings"
    ).fetchall()

    now = time.time()
    count = 0
    for r in rows:
        meta = {
            "title":   r["album_title"],
            "artist":  r["album_artist"],
            "cover_art": r["cover_art"],
            "source":  r["source"],
            "rating":  r["rating"],
        }
        conn.execute(
            """
            INSERT INTO items (scheme, external_id, metadata_json, added_at)
            VALUES ('music_album', ?, ?, ?)
            ON CONFLICT(scheme, external_id) DO NOTHING
            """,
            (r["album_key"], json.dumps(meta), r["rated_at"] or now),
        )
        count += 1

    return count


def _backfill_music_artist(conn) -> int:
    rows = conn.execute(
        """
        SELECT artist_key, artist_name, image, source,
               spotify_artist_id, deezer_artist_id, itunes_artist_id, created_at
        FROM artist_follows
        """
    ).fetchall()

    now = time.time()
    count = 0
    for r in rows:
        meta = {
            "name":              r["artist_name"],
            "image":             r["image"],
            "source":            r["source"],
            "spotify_artist_id": r["spotify_artist_id"],
            "deezer_artist_id":  r["deezer_artist_id"],
            "itunes_artist_id":  r["itunes_artist_id"],
        }
        conn.execute(
            """
            INSERT INTO items (scheme, external_id, metadata_json, added_at)
            VALUES ('music_artist', ?, ?, ?)
            ON CONFLICT(scheme, external_id) DO NOTHING
            """,
            (r["artist_key"], json.dumps(meta), r["created_at"] or now),
        )
        count += 1

    return count


# ---------------------------------------------------------------------------
# v3: named graphs — graphs table + composite PK on ppr_scores/cosine_scores
# ---------------------------------------------------------------------------

_DEFAULT_GRAPHS_V3 = [
    (1, "default", "mixed"),
    (2, "music",   "music"),
    (3, "video",   "video"),
]

_DEFAULT_GRAPHS_V4 = [
    (1, "Mixed",   "mixed"),
    (2, "Songs",   "music"),
    (3, "Videos",  "video"),
    (4, "Albums",  "album"),
    (5, "Artists", "artist"),
]


def migrate_to_graphs_v3(conn) -> None:  # noqa: keep for callers
    """
    Idempotent: creates the graphs table, seeds default graphs, and migrates
    ppr_scores / cosine_scores to composite PKs (video_id, graph_id).
    Only runs if schema_version < 3.
    """
    row = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()
    current = row[0] if row and row[0] else 0
    if current >= 3:
        logger.debug("migrate_to_graphs_v3: already at version %d, skipping", current)
        return

    logger.info("migrate_to_graphs_v3: starting (current version=%d)", current)
    t0 = time.monotonic()
    now = time.time()

    conn.execute("""
        CREATE TABLE IF NOT EXISTS graphs (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            name         TEXT NOT NULL UNIQUE,
            content_type TEXT NOT NULL DEFAULT 'mixed'
                         CHECK(content_type IN ('mixed','music','video')),
            config_json  TEXT,
            created_at   REAL NOT NULL
        )
    """)
    for gid, name, ct in _DEFAULT_GRAPHS_V3:
        conn.execute(
            "INSERT OR IGNORE INTO graphs (id, name, content_type, created_at) VALUES (?,?,?,?)",
            (gid, name, ct, now),
        )

    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()}

    # Migrate ppr_scores → composite PK
    if "ppr_scores" in tables and "ppr_scores_old" not in tables:
        conn.execute("ALTER TABLE ppr_scores RENAME TO ppr_scores_old")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS ppr_scores (
            video_id    TEXT NOT NULL,
            graph_id    INTEGER NOT NULL DEFAULT 1 REFERENCES graphs(id) ON DELETE CASCADE,
            score       REAL NOT NULL,
            computed_at REAL NOT NULL,
            spam_mass   REAL,
            PRIMARY KEY (video_id, graph_id)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_ppr_score ON ppr_scores(graph_id, score DESC)")
    if "ppr_scores_old" in tables:
        conn.execute("""
            INSERT OR IGNORE INTO ppr_scores (video_id, graph_id, score, computed_at, spam_mass)
            SELECT video_id, 1, score, computed_at, spam_mass FROM ppr_scores_old
        """)
        conn.execute("DROP TABLE ppr_scores_old")

    # Migrate cosine_scores → composite PK
    if "cosine_scores" in tables and "cosine_scores_old" not in tables:
        conn.execute("ALTER TABLE cosine_scores RENAME TO cosine_scores_old")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS cosine_scores (
            video_id    TEXT NOT NULL,
            graph_id    INTEGER NOT NULL DEFAULT 1 REFERENCES graphs(id) ON DELETE CASCADE,
            score       REAL NOT NULL,
            computed_at REAL NOT NULL,
            PRIMARY KEY (video_id, graph_id)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_cosine_score ON cosine_scores(graph_id, score DESC)")
    if "cosine_scores_old" in tables:
        conn.execute("""
            INSERT OR IGNORE INTO cosine_scores (video_id, graph_id, score, computed_at)
            SELECT video_id, 1, score, computed_at FROM cosine_scores_old
        """)
        conn.execute("DROP TABLE cosine_scores_old")

    conn.execute(
        "INSERT OR IGNORE INTO schema_version(version, applied_at) VALUES (3, ?)",
        (time.time(),),
    )
    conn.commit()

    elapsed = time.monotonic() - t0
    logger.info("migrate_to_graphs_v3: done in %.1fs", elapsed)


# ---------------------------------------------------------------------------
# v4: album/artist graph types + rename built-in graphs
# ---------------------------------------------------------------------------

def migrate_to_graphs_v4(conn) -> None:
    """
    Idempotent: extends graphs.content_type CHECK to include 'album' and 'artist',
    renames built-in graphs to clearer names, and inserts Albums/Artists graphs.
    Only runs if schema_version < 4.
    """
    row = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()
    current = row[0] if row and row[0] else 0
    if current >= 4:
        logger.debug("migrate_to_graphs_v4: already at version %d, skipping", current)
        return

    logger.info("migrate_to_graphs_v4: starting (current version=%d)", current)
    t0 = time.monotonic()
    now = time.time()

    # SQLite 3.26+ updates FK references in child tables when a parent table is
    # renamed. Renaming graphs→graphs_v3 then dropping graphs_v3 leaves
    # ppr_scores/cosine_scores with a dangling REFERENCES graphs_v3(id).
    # Detect whether the graphs table already has the v4 schema (album/artist in
    # CHECK) — if so, skip the rename dance and just update the data.
    graphs_sql_row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='graphs'"
    ).fetchone()
    needs_recreate = graphs_sql_row is None or "'album'" not in (graphs_sql_row[0] or "")

    if needs_recreate:
        # Old schema — recreate the table to extend the CHECK constraint.
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.execute("ALTER TABLE graphs RENAME TO graphs_v3")
        conn.execute("""
            CREATE TABLE graphs (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                name         TEXT NOT NULL UNIQUE,
                content_type TEXT NOT NULL DEFAULT 'mixed'
                             CHECK(content_type IN ('mixed','music','video','album','artist')),
                config_json  TEXT,
                created_at   REAL NOT NULL
            )
        """)
        conn.execute("INSERT INTO graphs SELECT * FROM graphs_v3")
        conn.execute("DROP TABLE graphs_v3")
        conn.execute("PRAGMA foreign_keys=ON")
        # Repair FK references broken by the rename — recreate ppr_scores and
        # cosine_scores so their graph_id column points to the new graphs table.
        for tbl, extra_cols in [
            ("ppr_scores",    "spam_mass   REAL,"),
            ("cosine_scores", ""),
        ]:
            bak = f"{tbl}_v4bak"
            conn.execute(f"ALTER TABLE {tbl} RENAME TO {bak}")
            conn.execute(f"""
                CREATE TABLE {tbl} (
                    video_id    TEXT NOT NULL,
                    graph_id    INTEGER NOT NULL DEFAULT 1 REFERENCES graphs(id) ON DELETE CASCADE,
                    score       REAL NOT NULL,
                    computed_at REAL NOT NULL,
                    {extra_cols}
                    PRIMARY KEY (video_id, graph_id)
                )
            """)
            conn.execute(f"INSERT OR IGNORE INTO {tbl} SELECT * FROM {bak}")
            conn.execute(f"DROP TABLE {bak}")

    # Rename built-ins (only if they still have old names)
    conn.execute("UPDATE graphs SET name='Mixed'   WHERE id=1 AND name='default'")
    conn.execute("UPDATE graphs SET name='Songs'   WHERE id=2 AND name='music'")
    conn.execute("UPDATE graphs SET name='Videos'  WHERE id=3 AND name='video'")

    # Insert Albums and Artists built-in graphs
    for gid, name, ct in _DEFAULT_GRAPHS_V4[3:]:
        conn.execute(
            "INSERT OR IGNORE INTO graphs (id, name, content_type, created_at) VALUES (?,?,?,?)",
            (gid, name, ct, now),
        )

    conn.execute(
        "INSERT OR IGNORE INTO schema_version(version, applied_at) VALUES (4, ?)",
        (time.time(),),
    )
    conn.commit()

    elapsed = time.monotonic() - t0
    logger.info("migrate_to_graphs_v4: done in %.1fs", elapsed)


# ---------------------------------------------------------------------------
# v5: signal_sources table + fix Albums/Artists content_type
# ---------------------------------------------------------------------------

def migrate_to_v5(conn) -> None:
    """
    Idempotent: creates signal_sources table, seeds default ytvideo signal
    sources, and updates Albums/Artists graphs to use content_type='music'.
    Only runs if schema_version < 5.
    """
    import os as _os

    row = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()
    current = row[0] if row and row[0] else 0
    if current >= 5:
        logger.debug("migrate_to_v5: already at version %d, skipping", current)
        return

    logger.info("migrate_to_v5: starting (current version=%d)", current)
    t0 = time.monotonic()
    now = time.time()

    conn.execute("""
        CREATE TABLE IF NOT EXISTS signal_sources (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            name            TEXT NOT NULL UNIQUE,
            kind            TEXT NOT NULL CHECK(kind IN ('watch_history','likes','playlists','custom')),
            endpoint_url    TEXT NOT NULL,
            converter       TEXT NOT NULL DEFAULT 'ytfront_v1'
                            CHECK(converter IN ('ytfront_v1','ytfront_likes_v1','native')),
            auth_header     TEXT,
            enabled         INTEGER NOT NULL DEFAULT 1,
            is_system       INTEGER NOT NULL DEFAULT 0,
            created_at      REAL NOT NULL,
            last_synced_at  REAL,
            last_count      INTEGER,
            last_error      TEXT
        )
    """)

    ytvideo_url = _os.environ.get("YTVIDEO_URL", "http://127.0.0.1:9002")
    token = _os.environ.get("RECOMMENDERR_TOKEN", "")
    auth_header = f"Bearer {token}" if token else None

    conn.execute("""
        INSERT OR IGNORE INTO signal_sources
            (name, kind, endpoint_url, converter, auth_header, is_system, created_at)
        VALUES (?, ?, ?, ?, ?, 1, ?)
    """, ("ytvideo watch history", "watch_history", ytvideo_url, "ytfront_v1", auth_header, now))

    conn.execute("""
        INSERT OR IGNORE INTO signal_sources
            (name, kind, endpoint_url, converter, auth_header, is_system, created_at)
        VALUES (?, ?, ?, ?, ?, 1, ?)
    """, ("ytvideo playlists", "playlists", ytvideo_url, "ytfront_likes_v1", auth_header, now))

    # Fix Albums (id=4) and Artists (id=5) content_type
    conn.execute(
        "UPDATE graphs SET content_type='music' WHERE id IN (4, 5) AND content_type IN ('album', 'artist')"
    )

    conn.execute(
        "INSERT OR IGNORE INTO schema_version(version, applied_at) VALUES (5, ?)",
        (now,),
    )
    conn.commit()

    elapsed = time.monotonic() - t0
    logger.info("migrate_to_v5: done in %.1fs", elapsed)


def migrate_to_v6(conn) -> None:
    """
    Per-graph independence: adds graph_sources, graph_feed_items tables; adds graph_id
    to pipeline_config, feed_filters, weight_rules, serendipity_scores; seeds
    graph_sources with content-type affinity defaults.
    Only runs if schema_version < 6.
    """
    row = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()
    current = row[0] if row and row[0] else 0
    if current >= 6:
        logger.debug("migrate_to_v6: already at version %d, skipping", current)
        return

    logger.info("migrate_to_v6: starting (current version=%d)", current)
    t0 = time.monotonic()
    now = time.time()

    # ── graph_sources ─────────────────────────────────────────────────────────
    conn.execute("""
        CREATE TABLE IF NOT EXISTS graph_sources (
            graph_id        INTEGER NOT NULL REFERENCES graphs(id) ON DELETE CASCADE,
            source_name     TEXT NOT NULL,
            weight_override REAL,
            PRIMARY KEY (graph_id, source_name)
        )
    """)

    # ── graph_feed_items ──────────────────────────────────────────────────────
    conn.execute("""
        CREATE TABLE IF NOT EXISTS graph_feed_items (
            graph_id        INTEGER NOT NULL REFERENCES graphs(id) ON DELETE CASCADE,
            video_id        TEXT NOT NULL,
            source_video_id TEXT NOT NULL,
            added_at        REAL NOT NULL,
            PRIMARY KEY (graph_id, video_id, source_video_id)
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_gfi_graph ON graph_feed_items(graph_id, added_at DESC)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_gfi_video ON graph_feed_items(graph_id, video_id)"
    )

    # ── pipeline_config: add graph_id column (rename→recreate→copy→drop) ─────
    cols = {r[1] for r in conn.execute("PRAGMA table_info(pipeline_config)").fetchall()}
    if "graph_id" not in cols:
        conn.execute("ALTER TABLE pipeline_config RENAME TO pipeline_config_old")
        conn.execute("""
            CREATE TABLE pipeline_config (
                graph_id   INTEGER NOT NULL DEFAULT 1,
                key        TEXT NOT NULL,
                value      TEXT NOT NULL,
                updated_at REAL NOT NULL,
                PRIMARY KEY (graph_id, key)
            )
        """)
        conn.execute("""
            INSERT INTO pipeline_config (graph_id, key, value, updated_at)
            SELECT 1, key, value, updated_at FROM pipeline_config_old
        """)
        conn.execute("DROP TABLE pipeline_config_old")

    # ── feed_filters: add graph_id column ────────────────────────────────────
    cols = {r[1] for r in conn.execute("PRAGMA table_info(feed_filters)").fetchall()}
    if "graph_id" not in cols:
        conn.execute("ALTER TABLE feed_filters RENAME TO feed_filters_old")
        conn.execute("""
            CREATE TABLE feed_filters (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                graph_id    INTEGER NOT NULL DEFAULT 1,
                filter_type TEXT NOT NULL,
                match_value TEXT NOT NULL,
                created_at  REAL NOT NULL,
                UNIQUE(graph_id, filter_type, match_value)
            )
        """)
        conn.execute("""
            INSERT INTO feed_filters (graph_id, filter_type, match_value, created_at)
            SELECT 1, filter_type, match_value, created_at FROM feed_filters_old
        """)
        conn.execute("DROP TABLE feed_filters_old")

    # ── weight_rules: add graph_id column ────────────────────────────────────
    cols = {r[1] for r in conn.execute("PRAGMA table_info(weight_rules)").fetchall()}
    if "graph_id" not in cols:
        conn.execute("ALTER TABLE weight_rules RENAME TO weight_rules_old")
        conn.execute("""
            CREATE TABLE weight_rules (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                graph_id    INTEGER NOT NULL DEFAULT 1,
                rule_type   TEXT NOT NULL,
                match_value TEXT NOT NULL,
                multiplier  REAL NOT NULL DEFAULT 2.0,
                created_at  REAL NOT NULL,
                UNIQUE(graph_id, rule_type, match_value)
            )
        """)
        conn.execute("""
            INSERT INTO weight_rules (graph_id, rule_type, match_value, multiplier, created_at)
            SELECT 1, rule_type, match_value, multiplier, created_at FROM weight_rules_old
        """)
        conn.execute("DROP TABLE weight_rules_old")

    # ── serendipity_scores: add graph_id (PK change → recreate) ──────────────
    cols = {r[1] for r in conn.execute("PRAGMA table_info(serendipity_scores)").fetchall()}
    if "graph_id" not in cols:
        conn.execute("ALTER TABLE serendipity_scores RENAME TO serendipity_scores_old")
        conn.execute("""
            CREATE TABLE serendipity_scores (
                video_id    TEXT NOT NULL,
                graph_id    INTEGER NOT NULL DEFAULT 1,
                score       REAL NOT NULL,
                computed_at REAL NOT NULL,
                PRIMARY KEY (video_id, graph_id)
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_serendipity_score ON serendipity_scores(graph_id, score DESC)"
        )
        conn.execute("""
            INSERT INTO serendipity_scores (video_id, graph_id, score, computed_at)
            SELECT video_id, 1, score, computed_at FROM serendipity_scores_old
        """)
        conn.execute("DROP TABLE serendipity_scores_old")

    # ── Seed graph_sources with content-type affinity ─────────────────────────
    # video sources → Videos graph (id=3)
    # music sources → Songs (2), Albums (4), Artists (5)
    # user_signals  → all four active graphs (2, 3, 4, 5)
    _MUSIC_SOURCES = {"lastfm", "spotify", "deezer", "itunes", "musicbrainz", "bandcamp", "discogs"}
    _VIDEO_SOURCES = {"invidious", "ytdlp", "youtube_rss"}
    _ALL_GRAPHS = [2, 3, 4, 5]
    _MUSIC_GRAPHS = [2, 4, 5]
    _VIDEO_GRAPHS = [3]

    existing_sources = {
        r[0] for r in conn.execute("SELECT name FROM sources").fetchall()
    }
    for src in existing_sources:
        if src in _MUSIC_SOURCES:
            graphs = _MUSIC_GRAPHS
        elif src in _VIDEO_SOURCES:
            graphs = _VIDEO_GRAPHS
        else:
            graphs = _ALL_GRAPHS
        for gid in graphs:
            conn.execute(
                "INSERT OR IGNORE INTO graph_sources (graph_id, source_name) VALUES (?, ?)",
                (gid, src),
            )

    # ── Backfill graph_feed_items from existing feed_recommendations (→ graph 1) ─
    conn.execute("""
        INSERT OR IGNORE INTO graph_feed_items (graph_id, video_id, source_video_id, added_at)
        SELECT 1, video_id, source_video_id, added_at FROM feed_recommendations
    """)

    conn.execute(
        "INSERT OR IGNORE INTO schema_version(version, applied_at) VALUES (6, ?)",
        (now,),
    )
    conn.commit()

    elapsed = time.monotonic() - t0
    logger.info("migrate_to_v6: done in %.1fs", elapsed)


def migrate_to_v7(conn) -> None:
    """Fix dangling REFERENCES graphs_v3(id) in ppr_scores and cosine_scores.

    migrate_to_graphs_v4 renamed graphs→graphs_v3, copied data to new graphs,
    then dropped graphs_v3.  This left ppr_scores and cosine_scores with a dead
    FK reference that breaks any query when foreign_keys=ON.
    Recreate both tables with the FK pointing at graphs(id).
    Only runs if schema_version < 7.
    """
    row = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()
    current = row[0] if row and row[0] else 0
    if current >= 7:
        logger.debug("migrate_to_v7: already at version %d, skipping", current)
        return

    logger.info("migrate_to_v7: fixing ppr_scores and cosine_scores FK references")
    t0 = time.monotonic()
    now = time.time()

    for table, extra_col in [
        ("ppr_scores", "spam_mass   REAL,"),
        ("cosine_scores", ""),
    ]:
        info = conn.execute(f"PRAGMA table_info({table})").fetchall()
        if not info:
            continue
        col_names = [r["name"] for r in info]
        # Check if the FK is still broken
        fk_list = conn.execute(f"PRAGMA foreign_key_list({table})").fetchall()
        bad_refs = [r for r in fk_list if r["table"] in ("graphs_v3", "graphs_v4")]
        if not bad_refs:
            logger.debug("migrate_to_v7: %s FK is already correct, skipping", table)
            continue

        # Clean up any leftover _old table from a previous failed attempt
        conn.execute(f"DROP TABLE IF EXISTS {table}_old")
        extra_ddl = f"\n            {extra_col}" if extra_col else ""
        conn.execute(f"ALTER TABLE {table} RENAME TO {table}_old")
        conn.execute(f"""
            CREATE TABLE {table} (
                video_id    TEXT NOT NULL,
                graph_id    INTEGER NOT NULL DEFAULT 1 REFERENCES graphs(id) ON DELETE CASCADE,
                score       REAL NOT NULL,
                computed_at REAL NOT NULL,{extra_ddl}
                PRIMARY KEY (video_id, graph_id)
            )
        """)
        cols = ", ".join(c for c in col_names if c in {"video_id", "graph_id", "score", "computed_at", "spam_mass"})
        conn.execute(f"INSERT INTO {table} ({cols}) SELECT {cols} FROM {table}_old")
        conn.execute(f"DROP TABLE {table}_old")
        logger.info("migrate_to_v7: recreated %s with correct FK", table)

    conn.execute(
        "INSERT OR IGNORE INTO schema_version(version, applied_at) VALUES (7, ?)",
        (now,),
    )
    conn.commit()
    logger.info("migrate_to_v7: done in %.1fs", time.monotonic() - t0)


def migrate_to_v8(conn) -> None:
    """Add converters table — user-defined named ingestion pipeline stages.

    Each converter maps one or more sources to one or more target graphs,
    with a name and description the user controls.
    Seeds the two built-in converters (video crawler, music recognition).
    Only runs if schema_version < 8.
    """
    row = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()
    current = row[0] if row and row[0] else 0
    if current >= 8:
        logger.debug("migrate_to_v8: already at version %d, skipping", current)
        return

    logger.info("migrate_to_v8: creating converters table")
    now = time.time()

    conn.execute("""
        CREATE TABLE IF NOT EXISTS converters (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            name         TEXT NOT NULL UNIQUE,
            description  TEXT NOT NULL DEFAULT '',
            content_type TEXT NOT NULL DEFAULT 'video'
                         CHECK(content_type IN ('video','music','mixed')),
            sources      TEXT NOT NULL DEFAULT '[]',
            graph_ids    TEXT NOT NULL DEFAULT '[]',
            config       TEXT NOT NULL DEFAULT '{}',
            enabled      INTEGER NOT NULL DEFAULT 1,
            created_at   REAL NOT NULL,
            updated_at   REAL NOT NULL
        )
    """)

    # Seed the two built-in converters.
    # graph_ids: Videos=3 for video; Songs=2, Albums=4, Artists=5 for music.
    # INSERT OR IGNORE so re-runs are safe.
    conn.execute(
        """INSERT OR IGNORE INTO converters
           (name, description, content_type, sources, graph_ids, enabled, created_at, updated_at)
           VALUES (?,?,?,?,?,1,?,?)""",
        (
            "Video Crawler",
            "Fetches related-video recommendations from Invidious and builds "
            "weighted video→video edges for the PPR scorer.",
            "video",
            '["invidious"]',
            '[3]',
            now, now,
        ),
    )
    conn.execute(
        """INSERT OR IGNORE INTO converters
           (name, description, content_type, sources, graph_ids, enabled, created_at, updated_at)
           VALUES (?,?,?,?,?,1,?,?)""",
        (
            "Music Recognition & Recommendations",
            "Fingerprints music in video content, enriches with metadata from "
            "Last.fm / Spotify / Deezer / iTunes / MusicBrainz / Bandcamp / Discogs, "
            "then aggregates similar-track recommendations into a confidence-weighted "
            "music graph.",
            "music",
            '["lastfm","spotify","deezer","itunes","musicbrainz","bandcamp","discogs"]',
            '[2,4,5]',
            now, now,
        ),
    )

    conn.execute(
        "INSERT OR IGNORE INTO schema_version(version, applied_at) VALUES (8, ?)",
        (now,),
    )
    conn.commit()
    logger.info("migrate_to_v8: done")


def migrate_to_v9(conn) -> None:
    """Add mapping_code column to converters table (field-level transformation spec)."""
    row = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()
    current = row[0] if row and row[0] else 0
    if current >= 9:
        logger.debug("migrate_to_v9: already at version %d, skipping", current)
        return
    logger.info("migrate_to_v9: adding mapping_code to converters")
    conn.execute("ALTER TABLE converters ADD COLUMN mapping_code TEXT NOT NULL DEFAULT '{}'")
    conn.execute(
        "INSERT OR IGNORE INTO schema_version(version, applied_at) VALUES (9, ?)",
        (time.time(),),
    )
    conn.commit()
    logger.info("migrate_to_v9: done")


def migrate_to_v10(conn) -> None:
    """Add invidious_cache table for caching raw Invidious API responses."""
    row = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()
    current = row[0] if row and row[0] else 0
    if current >= 10:
        logger.debug("migrate_to_v10: already at version %d, skipping", current)
        return
    logger.info("migrate_to_v10: creating invidious_cache table")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS invidious_cache (
            cache_key    TEXT PRIMARY KEY,
            response_json TEXT NOT NULL,
            fetched_at   REAL NOT NULL,
            expires_at   REAL NOT NULL
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_invcache_expires ON invidious_cache(expires_at)")
    conn.execute(
        "INSERT OR IGNORE INTO schema_version(version, applied_at) VALUES (10, ?)",
        (time.time(),),
    )
    conn.commit()
    logger.info("migrate_to_v10: done")


def migrate_to_v11(conn) -> None:
    """Add external_music_seeds + library_rec_results tables (yamtrack\u2192recommenderr)."""
    row = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()
    current = row[0] if row and row[0] else 0
    if current >= 11:
        logger.debug("migrate_to_v11: already at version %d, skipping", current)
        return
    logger.info("migrate_to_v11: creating external_music_seeds + library_rec_results")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS external_music_seeds (
            source     TEXT NOT NULL DEFAULT 'yamtrack',
            kind       TEXT NOT NULL CHECK(kind IN ('song','album','artist')),
            artist     TEXT NOT NULL DEFAULT '',
            album      TEXT NOT NULL DEFAULT '',
            track      TEXT NOT NULL DEFAULT '',
            score      REAL,
            updated_at REAL NOT NULL,
            PRIMARY KEY (source, kind, artist, album, track)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_ext_seeds_kind ON external_music_seeds(kind)")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS library_rec_results (
            kind        TEXT NOT NULL CHECK(kind IN ('song','album','artist')),
            artist      TEXT NOT NULL DEFAULT '',
            album       TEXT NOT NULL DEFAULT '',
            track       TEXT NOT NULL DEFAULT '',
            score       REAL NOT NULL,
            cover_art   TEXT,
            video_id    TEXT,
            sources     TEXT,
            computed_at REAL NOT NULL,
            PRIMARY KEY (kind, artist, album, track)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_librecs_kind_score ON library_rec_results(kind, score DESC)")
    conn.execute(
        "INSERT OR IGNORE INTO schema_version(version, applied_at) VALUES (11, ?)",
        (__import__("time").time(),),
    )
    conn.commit()
    logger.info("migrate_to_v11: done")


def migrate_to_v12(conn) -> None:
    """Make PPR engine config per-graph + add catalog_ppr_config.

    The old ppr_config was a single global key/value store, so one set of seed
    weights / alpha drove every graph. Split it into a per-graph table, copying
    the existing global values into every graph so behaviour is unchanged until
    each graph is tuned individually. Also add the catalog (yamtrack library)
    PPR's own config store.

    Only runs if schema_version < 12.
    """
    row = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()
    current = row[0] if row and row[0] else 0
    if current >= 12:
        logger.debug("migrate_to_v12: already at version %d, skipping", current)
        return
    logger.info("migrate_to_v12: splitting ppr_config into per-graph + catalog_ppr_config")
    t0 = time.monotonic()

    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()}

    # Capture the old global rows before rebuilding (table may be missing/global).
    old_rows: list[tuple[str, str, float]] = []
    if "ppr_config" in tables:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(ppr_config)").fetchall()}
        if "graph_id" not in cols:
            old_rows = [
                (r[0], r[1], r[2] if r[2] is not None else 0.0)
                for r in conn.execute("SELECT key, value, updated_at FROM ppr_config").fetchall()
            ]
            conn.execute("ALTER TABLE ppr_config RENAME TO ppr_config_global_old")

    conn.execute("""
        CREATE TABLE IF NOT EXISTS ppr_config (
            graph_id   INTEGER NOT NULL DEFAULT 1 REFERENCES graphs(id) ON DELETE CASCADE,
            key        TEXT NOT NULL,
            value      TEXT NOT NULL,
            updated_at REAL NOT NULL DEFAULT 0,
            PRIMARY KEY (graph_id, key)
        )
    """)

    # Copy old global values into every existing graph.
    if old_rows:
        graph_ids = [r[0] for r in conn.execute("SELECT id FROM graphs").fetchall()]
        if not graph_ids:
            graph_ids = [1]
        for gid in graph_ids:
            for key, value, updated_at in old_rows:
                conn.execute(
                    "INSERT OR IGNORE INTO ppr_config (graph_id, key, value, updated_at) VALUES (?, ?, ?, ?)",
                    (gid, key, value, updated_at),
                )
        logger.info("migrate_to_v12: copied %d global keys into %d graphs", len(old_rows), len(graph_ids))

    if "ppr_config_global_old" in {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()}:
        conn.execute("DROP TABLE ppr_config_global_old")

    conn.execute("""
        CREATE TABLE IF NOT EXISTS catalog_ppr_config (
            key        TEXT PRIMARY KEY,
            value      TEXT NOT NULL,
            updated_at REAL NOT NULL DEFAULT 0
        )
    """)

    conn.execute(
        "INSERT OR IGNORE INTO schema_version(version, applied_at) VALUES (12, ?)",
        (time.time(),),
    )
    conn.commit()
    logger.info("migrate_to_v12: done in %.1fs", time.monotonic() - t0)


def migrate_to_v13(conn) -> None:
    """Add pipeline_consumers — user-registered downstream feed readers.

    Consumers are documentary: they record which external systems read the
    recommendation feed (name / method / path / url) so the pipeline canvas can
    render the downstream edges. graph_id NULL = applies to all graphs.

    Only runs if schema_version < 13.
    """
    row = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()
    current = row[0] if row and row[0] else 0
    if current >= 13:
        logger.debug("migrate_to_v13: already at version %d, skipping", current)
        return
    logger.info("migrate_to_v13: creating pipeline_consumers")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS pipeline_consumers (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            graph_id   INTEGER REFERENCES graphs(id) ON DELETE CASCADE,
            name       TEXT NOT NULL,
            url        TEXT NOT NULL DEFAULT '',
            method     TEXT NOT NULL DEFAULT 'GET',
            path       TEXT NOT NULL DEFAULT '',
            enabled    INTEGER NOT NULL DEFAULT 1,
            created_at REAL NOT NULL DEFAULT 0
        )
    """)
    conn.execute(
        "INSERT OR IGNORE INTO schema_version(version, applied_at) VALUES (13, ?)",
        (time.time(),),
    )
    conn.commit()
    logger.info("migrate_to_v13: done")
