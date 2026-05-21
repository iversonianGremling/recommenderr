# recommenderr Admin UI Overhaul

## Context

`recommenderr` (Proxmox LXC ct134, port 9001) is the central recommendation/aggregator service in the 3-app split. Its current admin UI is a 34-line static HTML scaffold at `/opt/recommenderr/admin-ui/index.html` — none of the backend's capabilities are exposed. The user wants a real UI organized around a three-layer mental model — **Discovery / Recommendation / Application** — and a content abstraction so the engine isn't bound to YouTube videos (must also handle music, and later movies, IG-style feeds, etc.).

The recommender is single-user: there's no real collaborative filtering. We simulate CF via **personas** — synthetic seed bundles that act as topic-sensitive PageRank teleport vectors (Haveliwala 2002). PPR's existing algorithm is the right shape; nothing in `compute_ppr` cares what nodes represent.

**Confirmed scope decisions:**
- Frontend (React + TS + Vite + Tailwind, mirroring `/opt/ytfrontend/frontend/`) **and** the backend additions it needs.
- Four pre-built schemes: `yt_video`, `music_track`, `music_album`, `music_artist`. Users can add more via UI by declaring fields — items go into a generic JSON-backed table.
- **Separate `personas` table** (not reusing `categories`). Personas are seed bundles, not content tags.
- Defer alternative scorers (cosine / ItemRank) to v2. Expose PPR well first.

## Architecture summary

- **Items abstraction**: new `items(id, scheme, external_id, metadata_json, added_at)` + `schemes(name, display_name, fields_json, …)` + `item_aliases(item_id, alias_scheme, alias_external_id)` for cross-scheme dedup.
- **PPR keys go composite TEXT**: `"yt_video:abc123"`, `"music_album:bandcamp/foo"`. `recommendation_edges` columns rename to `source_node_key` / `target_node_key`; `ppr_scores.video_id` → `node_key`. The hot loop in `compute_ppr` (`ppr_engine.py:420-451`) is already string-keyed — only seed-building and JOINs need to learn the new shape. **`persona_seeds.item_id` is the one place we use surrogate FK**, resolved to a composite key at the engine boundary.
- **Source registry**: declarative dict in code (only code adds new sources) + DB-backed `sources` table for state (enabled, weight, credential overrides, rate-limit, circuit breaker, last error). Replaces scattered `os.getenv` reads and the ad-hoc circuit globals in `radio_service.py:46-80`.
- **Personas worker** modeled after `category_recs_worker` (`services/category_recs.py:90-138, 348-404`). Start with one worker to avoid SQLite write contention.
- **Frontend**: single `SchemeAwareTable` + single `ItemDetailDrawer`, both parameterized by `scheme.fields_json`. Adding a scheme is a backend data change, no UI code change.

## Phased delivery

Six phases. Each is independently shippable and gated on its own tests + interop smoke.

### Phase A — Schemes + items table (backend, no UI change)
Additive schema. Bump `SCHEMA_VERSION` to 2 in `backend/main.py:21`.

- **New tables** in `backend/schema.sql`: `schemes`, `items`, `item_aliases`, **and** `ppr_config` (it's read by `routers/ppr.py:29` but never declared — latent bug, fix here).
- **New module** `backend/db/items.py`: `upsert_item`, `get_item`, `resolve_alias`, `list_schemes`, `register_scheme`.
- **New module** `backend/services/migration.py::migrate_to_items_v1()`: idempotent via `INSERT OR IGNORE + UNIQUE(scheme, external_id)`. Invoked once from `init_db()` after `executescript`. Backfills:
  - `video_metadata` → `items(scheme='yt_video')` (join `feed_recommendations`/`watch_history`/`playlist_videos` for title/author/thumbnail).
  - `music_library` → `items(scheme='music_track')` + alias to the matching `yt_video`.
  - `album_ratings` → `items(scheme='music_album')`.
  - `artist_follows` → `items(scheme='music_artist')`.
- **Pre-declared schemes**: insert 4 rows with `fields_json` field lists (`title/author/duration/thumbnail` for yt_video; `track/artist/album/isrc/...` for music_track; etc.).
- **Optional triggers** on `INSERT INTO video_metadata` / `music_library` to keep `items` mirrored (use `INSERT OR IGNORE`, idempotent).
- **Endpoints** (new router `backend/routers/items.py`, mount `/v1/items`): `GET /schemes`, `GET /?scheme=&q=&limit=`, `GET /{item_id}`.

**Gate**: interop suite green; `GET /v1/items/schemes` returns 4 entries on a fresh DB.

### Phase A.5 — Composite-key migration for graph (defer until Phase D is in flight)
- Add transition logic to `ppr_engine.py`: bare `video_id` reads as implicit `"yt_video:abc"`.
- One-shot `UPDATE recommendation_edges SET source_video_id = 'yt_video:' || source_video_id WHERE source_video_id NOT LIKE '%:%'` (same for target, ppr_scores).
- `ALTER TABLE … RENAME COLUMN source_video_id TO source_node_key` (SQLite ≥3.25). **Take a DB backup first** — SQLite RENAME is irreversible.
- `ppr_engine.get_seed_weights()` (`services/ppr_engine.py:102-404`): wrap video-id outputs as `f"yt_video:{vid}"`; add `_load_music_seeds()` for album/artist seeds.

**Gate**: PPR recompute completes in <2s on production DB; feed renders identical top-20 to pre-migration.

### Phase B — Source registry + per-source health
- **New table** `sources` (name PK, enabled, weight, credentials_json, rate_limit_per_min, last_success_at, last_error_at, last_error, failure_streak, circuit_open_until, metadata_json).
- **New module** `backend/services/source_registry.py` with module-level `SOURCES_DECL` dict (lastfm, spotify, deezer, itunes, musicbrainz, bandcamp, discogs, invidious, ytdlp, youtube_rss). Public API: `get_credential`, `is_available`, `mark_success`, `mark_failure`, `with_source` decorator.
- **Refactor call sites** (minimal-invasive — wrap, don't rewrite):
  - `services/music_client.py:487, 546-547, 757` — `os.getenv` → `get_credential`; wrap public coroutines with `@with_source("lastfm")` etc.
  - `services/invidious_client.py` — wrap `api_get/post/delete`; pull `INVIDIOUS_URL` from registry.
  - `services/radio_service.py:46-80` — **delete** `_ytdlp_failed_until`/`_invidious_failed_until` globals; replace `_ytdlp_available()`/`_invidious_available()` with `source_registry.is_available(...)`.
  - `services/music_recommendations.py:30-35` — replace hardcoded `SOURCE_WEIGHTS` dict with cached `get_source_weight(name)`.
  - `services/ytdlp_service.py`, `services/bandcamp_*.py` — register and route env reads.
- **Endpoints** (new router `backend/routers/sources.py`, `/v1/sources`): `GET /`, `GET /{name}`, `PATCH /{name}`, `POST /{name}/reset-circuit`, `POST /{name}/probe`. Credentials are write-only over the wire (`{has_value: true}` on read).

**Gate**: `/v1/music/search` + `/v1/radio` still functional; `GET /v1/sources` returns ≥7 entries; disabling Spotify causes `lastfm_search_track` to return `[]` without hitting httpx.

### Phase C — React SPA bootstrap + Discovery panel
- **New tree** at `/opt/recommenderr/admin-ui/`: `package.json`, `vite.config.ts` (`base: '/admin/'`, `build.outDir: 'dist'`), `tsconfig.json`, `tailwind.config.js`, `postcss.config.js`, `index.html` (Vite entry, replaces scaffold), `src/`. Clone template files from `/opt/ytfrontend/frontend/`.
- **`src/` structure**:
  - `main.tsx`, `App.tsx`, `index.css`
  - `lib/api.ts` (typed fetch wrappers, injects `Authorization: Bearer` from auth store), `lib/types.ts`, `lib/schemeRenderers.ts`
  - `stores/auth.ts`, `stores/sources.ts`, `stores/schemes.ts`, `stores/pprStatus.ts`
  - `components/Layout.tsx`, `ItemCard.tsx`, `ItemDetailDrawer.tsx`, `SchemeAwareTable.tsx`, `SourceHealthCard.tsx`, `ItemSearchPicker.tsx`
  - `pages/DiscoveryItems.tsx`, `DiscoverySources.tsx`, `DiscoveryRaw.tsx`
- **`backend/routers/admin.py`** rewrite: mount `StaticFiles(directory=admin-ui/dist/assets)` at `/admin/assets`; catch-all `GET /admin/{path:path}` → `dist/index.html` (SPA fallback). Add `Depends(require_admin_token)` (new in `backend/auth.py`, reads `ADMIN_TOKEN`, exempt if unset for dev).
- **Admin-scoped JSON** under `/admin/api/*`: proxies to `/v1/*` with admin auth instead of service token; one new endpoint `GET /admin/api/raw/{source}/{kind}/{query}` that fires a raw client call and returns the unprocessed response (debug surface).
- **Scheme-aware render**: `SchemeAwareTable` columns driven by `scheme.fields_json` (`{name, type, label}`). Field types: `text | url-image | duration | date | number | enum`. Per-scheme custom renderers via `customRenderers["${scheme}.${field}"]` escape hatch in `lib/schemeRenderers.ts`. Adding a scheme is purely data — no UI code change.

**Gate**: `npm ci && npm run build` succeeds; SPA loads at `http://ct134:9001/admin/`; Discovery shows items from all 4 schemes; source health card reflects real state.

### Phase D — Recommendation panel
Backend already exposes most knobs (`backend/routers/ppr.py` has `/config`, `/feed`, `/why/{id}`, `/for-source/{id}`, `/explore`, `/scores`, `/recompute`, `/invalidate`, `/weight-rules`). Add the few missing pieces, then build the UI:

- **New endpoints** in `routers/ppr.py`: `GET /v1/ppr/seeds?limit=200` (current seed weights + reasons), `GET /v1/ppr/graph/stats` (node/edge counts, density).
- **New CRUD** for `feed_filters` (table already declared `schema.sql:291`, read by `ppr_engine.py:355-394`): `GET/POST/DELETE /v1/ppr/feed-filters`. Add `get_feed_filters/add_feed_filter/delete_feed_filter` to `backend/db/__init__.py` (mirror `get_weight_rules` lines 448-469).
- **Fix weight-rule validator** `ppr.py:286`: extend allowed `rule_type` to include `genre|category|attribute` (engine already reads them at `ppr_engine.py:281-283`).
- **Frontend pages**: `RecommendationConfig.tsx` (sliders bound to `PPR_CONFIG_DEFAULTS`, Save/Reset/Recompute), `RecommendationScores.tsx` (paginated, "Why" drawer calling `/v1/ppr/why/{id}`), `RecommendationWeightRules.tsx`, `RecommendationFilters.tsx`, `RecommendationExplore.tsx` (seed picker reusing `ItemSearchPicker`), `RecommendationGraph.tsx` (stats card; force-graph stub for v2).
- **Cache invalidation**: every mutation endpoint must set `feed_cache._snapshot.computed_at = 0.0` (pattern at `ppr.py:75-77`).

**Gate**: every PPR behavior previously only DB-poked is UI-driveable; "Why" drawer correctly attributes a watched + rated + playlist-member video.

### Phase E — Personas
Phase A's `items` table must be live. Composite-key migration (A.5) should be in if you want music personas; otherwise yt_video-only personas work without it.

- **New tables**:
  ```
  personas(id PK, name UNIQUE, description, scheme, alpha, min_seed_rating, created_at, updated_at, version INTEGER)
  persona_seeds(persona_id FK CASCADE, item_id FK CASCADE, weight, PK(persona_id,item_id))
  persona_scores(persona_id, item_id, score, spam_mass, computed_at, PK(persona_id,item_id))
  persona_jobs(persona_id PK FK CASCADE, status, last_run_at, next_run_at, last_error)
  ```
  Index `persona_scores(persona_id, score DESC)`. `version` is bumped on every seed change → worker aborts persist if `current_version != claimed_version`.
- **Engine**: new `ppr_engine.compute_persona_ppr(persona_id, alpha=None)`. Loads `persona_seeds` JOIN `items` to resolve composite node_keys; reuses `build_graph()` + `compute_ppr()` unchanged; persists top-N to `persona_scores`.
- **Worker** `backend/services/persona_worker.py` cloned from `services/category_recs.py` lines 90-104 (`_ensure_jobs_for_all_personas`), 107-138 (`pick_next_job` with `BEGIN IMMEDIATE`), 348-404 (worker loop). `PERSONA_REFRESH_HOURS = 6`. **Register exactly one worker** in `backend/main.py:lifespan()` — SQLite write contention bites at 5+ concurrent writers.
- **Endpoints** (new router `backend/routers/personas.py`, `/v1/personas`): list, create, get, patch, delete (cascades), `POST /{id}/seeds` (replace or `?merge=1`), `DELETE /{id}/seeds/{item_id}`, `GET /{id}/scores`, `POST /{id}/recompute` (synchronous via `run_in_threadpool`). Seed API accepts `{scheme, external_id}` and resolves to `item_id` server-side; 404 if item unknown.
- **Frontend pages**: `PersonasList.tsx` (table + "Run now"), `PersonaEdit.tsx` (name/description/alpha + seed picker via `ItemSearchPicker`), `PersonaScores.tsx` (reuses `RecommendationScores` table).

**Gate**: one persona created end-to-end, scores visible in UI; worker picks up dirty personas on the refresh cadence.

### Phase F — Application surfaces
No schema changes. Pure frontend over existing endpoints.

- **Pages**: `AppFeed.tsx` (calls `/v1/ppr/feed`, video grid, category filter, "Refresh cache" button), `AppRadio.tsx` (seed picker using existing `GET /v1/ppr/track-search` typeahead at `ppr.py:316`, posts to `/v1/radio`, links out to ytfrontend for actual playback — admin UI is not a player), `AppSurfaces.tsx` (index page describing each surface: Feed / Radio / Categories / Personas).
- **Optional polish**: `GET /v1/radio/cache/stats` (count `radio_graph_cache` rows by `seed_key`, age distribution).

**Gate**: user drives Feed + Radio without curl.

## Critical files to read first

In rough order:

1. `/opt/recommenderr/backend/services/ppr_engine.py` — whole file. Focus on `get_seed_weights()` (102-404), `compute_ppr()` (420-451), `update_ppr_scores()` (475-560), `explain_recommendation()` (596-684), `explore_from_seeds()` (686-811).
2. `/opt/recommenderr/backend/services/category_recs.py` lines 90-138, 348-404 — **persona worker is a near-clone**. Copy verbatim.
3. `/opt/recommenderr/backend/schema.sql` — full 358 lines. Especially `recommendation_edges` (53), `ppr_scores` (63), `weight_rules` (118), `attributes` (132), `feed_filters` (291).
4. `/opt/recommenderr/backend/routers/ppr.py` — model for `run_in_threadpool` + `feed_cache` invalidation pattern.
5. `/opt/recommenderr/backend/services/music_recommendations.py` lines 30-35 — `SOURCE_WEIGHTS` dict (becomes registry-backed).
6. `/opt/recommenderr/backend/services/radio_service.py` lines 46-80 — ad-hoc circuit breakers (delete in Phase B).
7. `/opt/recommenderr/backend/services/music_client.py` lines 487, 546-547, 757 — env reads to route through registry; full surface (126-1003) gets `@with_source` decorators.
8. `/opt/recommenderr/backend/services/feed_cache.py` — module-level mutable snapshot; every PPR mutation invalidates via `_snapshot.computed_at = 0.0`.
9. `/opt/recommenderr/backend/db/__init__.py` — `get_db`, `save_recommendations` (162), `get_ppr_feed` (326), `get_weight_rules/add_weight_rule/delete_weight_rule` (448-469).
10. `/opt/recommenderr/backend/main.py:38-58` — worker registration in `lifespan()`.
11. `/opt/ytfrontend/frontend/` — `package.json`, `vite.config.ts`, `tsconfig.json`, `tailwind.config.js`, `src/main.tsx`, `src/lib/api.ts`. Templates to clone.
12. `/opt/yt-platform/tests/interop/conftest.py` — multi-service test harness with `DISABLE_EXTERNAL_APIS=1`, `DISABLE_WORKERS=1`.
13. `/opt/recommenderr/backend/tests/conftest.py` — `tmp_db` fixture pattern.

## Reusable functions (don't rewrite)

- `compute_ppr(graph, seeds, alpha)` — `services/ppr_engine.py:420-451`. Already generic over string keys.
- `build_graph()` — `services/ppr_engine.py:21`. Reads `recommendation_edges`.
- `pick_next_job()` with `BEGIN IMMEDIATE` — `services/category_recs.py:107-138`. Exact pattern for persona worker.
- `get_weight_rules`/`add_weight_rule`/`delete_weight_rule` — `backend/db/__init__.py:448-469`. Mirror for `feed_filters`.
- `feed_cache._snapshot` invalidation — `routers/ppr.py:75-77`. Reuse on every new mutation endpoint.
- `require_service_token` — `backend/auth.py`. Pattern for new `require_admin_token`.

## Risks and gotchas

1. **PPR cost scales with |V|+|E|**. 4 schemes may 5-10× node count. If recompute exceeds 5s post-A.5, loosen `tol` in `compute_ppr` or cache `in_edges` at module level (currently rebuilt per call at line 428).
2. **Don't cascade on `items` from per-scheme tables**. Items should outlive `video_metadata` so PPR doesn't lose nodes. Triggers are one-way insert-only.
3. **SQLite write contention**. 4 category workers + 1 persona worker is already the safe ceiling. Don't multi-worker personas without benchmarks.
4. **`recommendation_edges` rename is irreversible**. Back up DB before Phase A.5. SQLite RENAME COLUMN is in-place.
5. **nginx routing for `/admin/`**. Confirm `/etc/nginx/sites-enabled/yt-platform` passes `/admin/*` through to ct134:9001 and doesn't strip the prefix. Vite `base: '/admin/'` depends on it.
6. **Admin auth + `LISTEN_HOST`**. `main.py:21` defaults to `0.0.0.0`. Once admin gains write endpoints, an unset `ADMIN_TOKEN` = open admin on the LAN. Plan: enforce token OR bind to localhost when token empty.
7. **`weight_rules` validator drift**. `ppr.py:286` allows only `keyword|channel_id|channel_name`; engine reads `genre|category|attribute` too. Extend validator in Phase D.
8. **Persona worker thrash**. Rapid seed edits race the worker. The `personas.version` integer + version-check at persist time handles it.
9. **`ppr_config` table missing from `schema.sql`** but read by `routers/ppr.py:29` — Phase A's additions fix this latent bug. Verify prod DB was hand-patched (likely, since endpoint works today).
10. **Credentials must be write-only over the wire**. Never include `credentials_json` in `GET /v1/sources` responses — return `{has_value: true}` instead.
11. **Node toolchain on ct134**. Verify `pct exec 134 -- node --version` ≥ 18 (Vite 6 requires it). Install via `apt install -y nodejs npm` if missing.
12. **Item-alias collisions**. A `music_track` and a `yt_video` describing the same physical thing produce two `items` rows linked by alias — intended. Drill-in UI must show both perspectives when an alias exists.

## Verification

Per-phase tests live in `/opt/recommenderr/backend/tests/{unit,integration}/`. Cross-phase regressions caught by `cd /opt/yt-platform && make test-interop` and `bash /opt/yt-platform/scripts/smoke.sh` (run after every phase).

- **Phase A**: `test_items_migration.py` — fresh DB, seed video_metadata/music_library, run `migrate_to_items_v1`, assert counts + alias linkage. `test_items_router.py` — `GET /v1/items/schemes` returns 4. Manual: `pct exec 134 -- curl -s localhost:9001/v1/items/schemes | jq`.
- **Phase A.5**: `test_ppr_composite_keys.py` — produce identical top-20 feed before/after migration on a fixture DB.
- **Phase B**: unit (registry state machine: disable, circuit-open, reset). Integration (`PATCH /v1/sources/lastfm {enabled:false}` then assert `lastfm_search_track` returns `[]` without firing httpx, via `httpx_mock`). Interop: `GET /v1/sources` reachable cross-service.
- **Phase C**: build (`npm ci && npm run build` produces `dist/index.html` + `dist/assets/*`). Backend integration (`test_admin_spa.py`: `GET /admin/recommendation/config` returns SPA HTML). Manual: open browser, walk Discovery.
- **Phase D**: `test_ppr_config_roundtrip`, `test_weight_rule_lifecycle`, `test_feed_filter_blocks`. Manual: add keyword rule, recompute, observe ranking shift.
- **Phase E**: unit (`compute_persona_ppr` on 3-node fixture). Integration (create persona, add 3 seeds, recompute, scores ranked). Worker test (stand up persona_worker on ephemeral DB, mark 2 dirty, observe state transitions).
- **Phase F**: manual smoke — feed renders ≥10 items, radio with known seed returns ≥5 tracks (gate on `DISABLE_EXTERNAL_APIS`).
