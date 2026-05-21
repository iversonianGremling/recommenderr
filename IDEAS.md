# Future ideas

## Visual pipeline builder

A canvas where the recommendation pipeline is represented as a directed graph of nodes.
Each node is a stage: Source → Candidate Generation → Scoring → Filtering → Surface.
Users connect nodes with edges to compose pipelines; multiple pipelines can coexist (one per persona or surface).
Nodes are configurable in-place (click a Source node → set weight, enable/disable, inspect raw output).
The canvas renders a live preview of what the top-N output looks like for the current pipeline config.

## Drag-and-drop stage ordering

Within a pipeline, stages can be reordered by drag. Ordering matters when:
- Multiple scorers are chained (first-pass PPR → re-rank by freshness → re-rank by source weight)
- Filters are applied at different depths (pre-filter before PPR runs vs. post-filter on ranked output)
- Fallback chains (if source A returns 0 results, fall through to source B)

Pairs naturally with the pipeline builder canvas but can also be surfaced as a simpler ordered list if full canvas is too heavy for v1.5.

## DSL for recommendation logic

A small declarative language for expressing pipeline logic without writing Python. Rough shape:

```
pipeline music_discovery:
  sources:
    - lastfm   weight=0.85
    - deezer   weight=0.90
    - bandcamp weight=0.70 rate_limit=5/min

  score:
    algorithm: ppr
    alpha: 0.15
    seeds: persona("avant-garde") + playlist("liked")

  filter:
    exclude keyword("tutorial", "reaction")
    exclude watched_in_last(days=7)
    min_score: 0.001

  surface: feed limit=100
```

Key properties:
- Declarative, not imperative — describes WHAT, not HOW
- Each pipeline is a named, versioned artifact; can be diffed and rolled back
- Pipelines can import from each other (`extends: base_music`)
- Evaluator is Python-side; DSL compiles to the same internal pipeline objects the UI uses
- Long-term: users paste DSL into the UI and see immediate ranked output; pipelines are stored as text in the DB

## Why not yet

- DSL + visual builder need multiple scorers to be meaningful — PPR-only makes the scoring node trivial
- Pipeline builder adds significant frontend surface area (canvas library, node editor)
- Right order: expose PPR fully (v1) → wire 1-2 alternative scorers empirically (v2) → visual builder + DSL have something to compose (v3)
