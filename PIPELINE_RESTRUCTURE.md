# Recommenderr Pipeline — Restructure Plan

Status: proposed (2026-06-04). Companion to the "quick wins" already shipped on
branch `feat/independent-ppr-pipeline`.

## Problem

The admin pipeline grew feature-by-feature without a unifying information
architecture. Symptoms:

- One node component (`NodeShell`) renders 7 conceptually different kinds of node
  (signal source, content source, graph, scorer, output stage, feed, consumer),
  so "Songs" (a graph) and "Output Stage" (a post-processing step) look identical
  and read as peers.
- Configuration is scattered across three locations reachable only by clicking the
  right node: PPR *engine* params (`/scoring/ppr`), PPR/cosine/serendipity *blend
  weights* (`PanelScorer`), and the *output stage* (diversity/filters).
- Inputs are inconsistently mutable: signal sources are addable, content sources
  are code-declared (enable-only), consumers are hardcoded — with no UI cue
  explaining which is which.
- The ingestion **normalize/translate** step (converters + `mapping_executor`) is
  invisible: most sources run on passthrough, so the stage that makes external
  data graph-ready isn't represented on the canvas at all.
- Optional scorers (cosine/serendipity) had a stale-score bug and no discoverable
  controls.

## Target information architecture — five explicit stages

Make the pipeline read left-to-right as five labeled stages, each with a distinct
node style (color/border/icon) and a clear "can I add here?" rule:

1. **INPUTS**
   - *Signal sources* (user behavior: watch history, ratings, playlists) — addable.
   - *Content sources* (Last.fm, Invidious, Bandcamp, …) — code-declared; UI is
     enable + configure (weight, credentials) only. Badge them "code-defined" so
     the read-only-ness is obvious, not a mystery.
2. **NORMALIZE** (new, surfaced)
   - The converters layer as a real stage. Show per-source whether a converter is
     attached or it's running passthrough. This is the "translator" — it maps raw
     API JSON into the uniform record shape PPR needs to build edges/nodes.
3. **GRAPHS**
   - One node per content-type graph (Songs / Videos / Albums / Artists). Title
     should read as "Songs graph", not bare "Songs".
4. **SCORE**
   - PPR (primary) + optional Cosine / Serendipity + custom modules. Surface
     enabled-state and blend weight inline on the node, not only in the panel.
5. **OUTPUT → FEED → CONSUMERS**
   - Output stage (MMR diversity, max-per-channel, feed filters, weight rules) →
     Feed (precomputed ranked list) → Consumers (downstream readers).

### Node grammar
Assign each stage a visual token (left border color + small stage glyph):
inputs / normalize / graph / score / output / consumer. A legend strip across the
top of the canvas. This alone resolves most of the "I don't know what each module
does" confusion without moving any logic.

### Config consolidation
Keep the per-node panels (they're good), but add a single "Scoring" summary panel
on the graph node that shows all scorers + weights together, so the three PPR-knob
locations are at least cross-linked and legible as one system. Document the three
intentionally-separate knobs in the panel copy.

## Concrete work items

### A. Addable consumer endpoints (was a requested "quick win"; needs real design)
Consumers are currently hardcoded display nodes. To let the user register extra
downstream endpoints properly:
- Backend: `pipeline_consumers` table `(id, graph_id NULLABLE, name, url, method,
  path, enabled, created_at)`; CRUD router at `/v1/pipeline/consumers`. `graph_id`
  NULL = applies to all graphs.
- Canvas: render registered consumers after the built-in ones; "Add consumer
  endpoint" in the +Add context menu opens a panel (name / method / url / path);
  edit + delete on custom consumer nodes.
- Normalize all consumer labels to `METHOD /path` (built-ins included). Decide
  whether consumers are purely documentary or whether recommenderr should actually
  push the feed to registered URLs (probably documentary for now — note it).

### B. Cosine / Serendipity — DONE (auto-recompute) + remaining polish
- [shipped] `feed_cache._do_refresh` now recomputes cosine/serendipity when enabled
  so the blend doesn't use stale scores.
- Remaining: surface enable+weight on the scorer node face; add a "blend preview"
  showing how on/off changes the top-N; serendipity copy noting it suits niche
  catalogs.

### C. Make the Normalize stage real
- Add converter-attached / passthrough status to each content source node.
- Link content-source node → its converter in `IngestionConverters`.

### D. HTTP method consistency
- Backend conventions are already sane (GET read, PUT config write, POST action
  with body). The inconsistency was display labels — finish normalizing them and
  document the convention in one place.

## Phasing
1. **P1 (done this session):** CustomModules back button + type toggle; cosine/
   serendipity auto-recompute; ytvideo consumer label verb.
2. **P2:** Node grammar (stage colors + legend) — pure visual, no logic moves.
   Lowest risk, highest clarity payoff.
3. **P3:** Addable consumers (table + CRUD + canvas).
4. **P4:** Surface Normalize stage; consolidate scoring summary panel.
5. **P5:** Scorer-node inline controls + blend preview.
