import type { FeedFilter, FeedItem, GraphStats, Item, Persona, PersonaSeed, PersonaScore, PprConfig, PprScore, PprSeed, Scheme, Source, WeightRule, WhyResult } from './types'

const BASE = ''  // same origin; empty → relative URLs work with Vite proxy and in production

async function req<T>(path: string, opts?: RequestInit): Promise<T> {
  const r = await fetch(`${BASE}${path}`, { headers: { 'Content-Type': 'application/json' }, ...opts })
  if (!r.ok) throw new Error(`${r.status} ${r.statusText} — ${path}`)
  return r.json() as Promise<T>
}

// ── Sources ─────────────────────────────────────────────────────────────────

export const listSources = () => req<Source[]>('/v1/sources')

export const getSource = (name: string) => req<Source>(`/v1/sources/${name}`)

export const patchSource = (name: string, body: {
  enabled?: boolean
  weight?: number
  rate_limit_per_min?: number
  credentials?: Record<string, string>
}) => req<Source>(`/v1/sources/${name}`, { method: 'PATCH', body: JSON.stringify(body) })

export const resetCircuit = (name: string) =>
  req<Source>(`/v1/sources/${name}/reset-circuit`, { method: 'POST' })

export const probeSource = (name: string) =>
  req<{ source: string; ok: boolean; detail: string }>(`/v1/sources/${name}/probe`, { method: 'POST' })

// ── Schemes + Items ──────────────────────────────────────────────────────────

export const listSchemes = () => req<Scheme[]>('/v1/items/schemes')

export const createScheme = (body: { name: string; display_name: string; description?: string; fields: Scheme['fields'] }) =>
  req<Scheme>('/v1/items/schemes', { method: 'POST', body: JSON.stringify(body) })

export const listItems = (params: { scheme?: string; q?: string; limit?: number; offset?: number }) => {
  const qs = new URLSearchParams()
  if (params.scheme) qs.set('scheme', params.scheme)
  if (params.q) qs.set('q', params.q)
  if (params.limit !== undefined) qs.set('limit', String(params.limit))
  if (params.offset !== undefined) qs.set('offset', String(params.offset))
  return req<Item[]>(`/v1/items/?${qs}`)
}

export const getItem = (id: number) => req<Item>(`/v1/items/${id}`)

// ── Admin status ─────────────────────────────────────────────────────────────

export const getStatus = () => req<Record<string, unknown>>('/admin/status')

// ── PPR ───────────────────────────────────────────────────────────────────────

export const getPprConfig = (graphId = 1) => req<PprConfig>(`/v1/ppr/config?graph_id=${graphId}`)

export const putPprConfig = (body: Partial<Omit<PprConfig, '_defaults'>>, graphId = 1) =>
  req<{ ok: boolean; updated: string[] }>('/v1/ppr/config', { method: 'PUT', body: JSON.stringify({ ...body, graph_id: graphId }) })

export const resetPprConfig = (graphId = 1) =>
  req<{ ok: boolean }>(`/v1/ppr/config/reset?graph_id=${graphId}`, { method: 'POST' })

export const recomputePpr = (body: { min_seed_rating?: number; compute_spam_mass?: boolean; graph_id?: number }) =>
  req<{ ok: boolean; elapsed_seconds: number; items: number }>('/v1/ppr/recompute', {
    method: 'POST',
    body: JSON.stringify(body),
  })

export const invalidatePpr = (graphId?: number) =>
  req<{ ok: boolean }>('/v1/ppr/invalidate', { method: 'POST', body: JSON.stringify(graphId !== undefined ? { graph_id: graphId } : {}) })

export const getPprScores = (limit = 100, graphId = 1) =>
  req<PprScore[]>(`/v1/ppr/scores?limit=${limit}&graph_id=${graphId}`)

export const getPprSeeds = (limit = 200, graphId = 1) =>
  req<PprSeed[]>(`/v1/ppr/seeds?limit=${limit}&graph_id=${graphId}`)

export const getGraphStats = (graphId = 1) => req<GraphStats>(`/v1/ppr/graph/stats?graph_id=${graphId}`)

export const getPprWhy = (videoId: string) => req<WhyResult>(`/v1/ppr/why/${encodeURIComponent(videoId)}`)

export const listWeightRules = (graphId = 1) => req<WeightRule[]>(`/v1/ppr/weight-rules?graph_id=${graphId}`)

export const addWeightRule = (body: { rule_type: string; match_value: string; multiplier: number; graph_id?: number }) =>
  req<{ ok: boolean }>('/v1/ppr/weight-rules', { method: 'POST', body: JSON.stringify(body) })

export const deleteWeightRule = (id: number, graphId = 1) =>
  req<{ ok: boolean }>(`/v1/ppr/weight-rules/${id}?graph_id=${graphId}`, { method: 'DELETE' })

export const listFeedFilters = (graphId = 1) => req<FeedFilter[]>(`/v1/ppr/feed-filters?graph_id=${graphId}`)

export const addFeedFilter = (body: { filter_type: string; match_value: string; graph_id?: number }) =>
  req<{ ok: boolean }>('/v1/ppr/feed-filters', { method: 'POST', body: JSON.stringify(body) })

export const deleteFeedFilter = (id: number, graphId = 1) =>
  req<{ ok: boolean }>(`/v1/ppr/feed-filters/${id}?graph_id=${graphId}`, { method: 'DELETE' })

// ── Personas ──────────────────────────────────────────────────────────────────

export const listPersonas = () => req<Persona[]>('/v1/personas')

export const createPersona = (body: { name: string; description?: string; alpha?: number; min_seed_rating?: number }) =>
  req<Persona>('/v1/personas', { method: 'POST', body: JSON.stringify(body) })

export const getPersona = (id: number) => req<Persona>(`/v1/personas/${id}`)

export const patchPersona = (id: number, body: Partial<Pick<Persona, 'name' | 'description' | 'alpha' | 'min_seed_rating'>>) =>
  req<Persona>(`/v1/personas/${id}`, { method: 'PATCH', body: JSON.stringify(body) })

export const deletePersona = (id: number) =>
  fetch(`/v1/personas/${id}`, { method: 'DELETE' })

export const getPersonaSeeds = (id: number) => req<PersonaSeed[]>(`/v1/personas/${id}/seeds`)

export const setPersonaSeeds = (id: number, seeds: Array<{ scheme: string; external_id: string; weight?: number }>, merge = false) =>
  req<{ ok: boolean; seed_count: number }>(`/v1/personas/${id}/seeds`, {
    method: 'POST',
    body: JSON.stringify({ seeds, merge }),
  })

export const deletePersonaSeed = (personaId: number, itemId: number) =>
  fetch(`/v1/personas/${personaId}/seeds/${itemId}`, { method: 'DELETE' })

export const getPersonaScores = (id: number, limit = 100) =>
  req<PersonaScore[]>(`/v1/personas/${id}/scores?limit=${limit}`)

export const recomputePersona = (id: number) =>
  req<{ ok: boolean; scored: number; elapsed_seconds: number }>(`/v1/personas/${id}/recompute`, { method: 'POST' })

// ── Feed ──────────────────────────────────────────────────────────────────────

export const getPprFeed = (params: {
  limit?: number; offset?: number; category?: string; sort?: string;
  persona_id?: number | null; graph_id?: number;
}) => {
  return req<{ items: FeedItem[]; total: number }>('/v1/ppr/feed', {
    method: 'POST',
    body: JSON.stringify({
      limit: params.limit ?? 50,
      offset: params.offset ?? 0,
      category: params.category ?? '',
      sort: params.sort ?? 'score',
      persona_id: params.persona_id ?? null,
      graph_id: params.graph_id ?? 1,
    }),
  })
}

export const getUserSignals = () =>
  req<{
    watch_history: number; rated_videos: number; blocked_videos: number;
    rated_channels: number; blocked_channels: number; playlists: number;
    playlist_items: number; rated_albums: number;
  }>('/v1/ppr/user-signals')

export const autoGeneratePersonas = (params: {
  top_keywords?: number; min_videos_per_keyword?: number;
  max_seeds_per_persona?: number; min_watch_or_rating?: boolean;
} = {}) =>
  req<{ created: string[]; skipped: string[]; total_created: number }>(
    '/v1/personas/auto-generate', { method: 'POST', body: JSON.stringify(params) }
  )

export const getPprFeedStatus = (graphId = 1) => req<{ items: number; age_seconds: number | null; is_refreshing: boolean }>(`/v1/ppr/feed/status?graph_id=${graphId}`)

// ── Raw search proxy ─────────────────────────────────────────────────────────

export const rawSearch = async (source: string, q: string): Promise<unknown> => {
  const endpointMap: Record<string, string> = {
    lastfm: `/v1/music/search?q=${encodeURIComponent(q)}&sources=lastfm`,
    spotify: `/v1/music/search?q=${encodeURIComponent(q)}&sources=spotify`,
    deezer: `/v1/music/search?q=${encodeURIComponent(q)}&sources=deezer`,
    itunes: `/v1/music/search?q=${encodeURIComponent(q)}&sources=itunes`,
    musicbrainz: `/v1/music/search?q=${encodeURIComponent(q)}&sources=musicbrainz`,
    discogs: `/v1/music/search?q=${encodeURIComponent(q)}&sources=discogs`,
    bandcamp: `/v1/music/search?q=${encodeURIComponent(q)}&sources=bandcamp`,
    invidious: `/v1/invidious/search?q=${encodeURIComponent(q)}`,
  }
  const path = endpointMap[source]
  if (!path) throw new Error(`No raw endpoint for source: ${source}`)
  return req(path)
}

// ── Cosine scorer ─────────────────────────────────────────────────────────────

export const recomputeCosine = (body: { min_seed_rating?: number; graph_id?: number }) =>
  req<{ ok: boolean; scored: number; elapsed_seconds: number }>('/v1/ppr/cosine/recompute', {
    method: 'POST',
    body: JSON.stringify(body),
  })

export const getCosineScores = (limit = 100, graphId = 1) =>
  req<Array<{ video_id: string; score: number; computed_at: number; title: string | null; author: string | null; thumbnail: string | null; duration: number | null }>>(`/v1/ppr/cosine/scores?limit=${limit}&graph_id=${graphId}`)


// ── Signal Sources ──────────────────────────────────────────────────────────

export interface SignalSource {
  id: number
  name: string
  kind: 'watch_history' | 'likes' | 'playlists' | 'custom'
  endpoint_url: string
  converter: 'ytfront_v1' | 'ytfront_likes_v1' | 'native'
  auth_header: string | null
  enabled: boolean
  is_system: boolean
  created_at: number
  last_synced_at: number | null
  last_count: number | null
  last_error: string | null
}

export const listSignalSources = () => req<SignalSource[]>('/v1/signal-sources')

export const createSignalSource = (body: Omit<SignalSource, 'id' | 'is_system' | 'created_at' | 'last_synced_at' | 'last_count' | 'last_error'>) =>
  req<SignalSource>('/v1/signal-sources', { method: 'POST', body: JSON.stringify(body) })

export const updateSignalSource = (id: number, body: Partial<Pick<SignalSource, 'name' | 'kind' | 'endpoint_url' | 'converter' | 'auth_header' | 'enabled'>>) =>
  req<SignalSource>(`/v1/signal-sources/${id}`, { method: 'PATCH', body: JSON.stringify(body) })

export const deleteSignalSource = (id: number) =>
  fetch(`/v1/signal-sources/${id}`, { method: 'DELETE' })

export const syncSignalSource = (id: number) =>
  req<{ ok: boolean; count?: number; error?: string }>(`/v1/signal-sources/${id}/sync`, { method: 'POST' })

// ── Pipeline consumers (documentary downstream feed readers) ─────────────

export interface PipelineConsumer {
  id: number
  graph_id: number | null
  name: string
  url: string
  method: string
  path: string
  enabled: boolean
  created_at: number
}

export const listConsumers = (graphId?: number) =>
  req<PipelineConsumer[]>(`/v1/pipeline/consumers${graphId !== undefined ? `?graph_id=${graphId}` : ''}`)

export const createConsumer = (body: {
  name: string; url?: string; method?: string; path?: string; graph_id?: number | null; enabled?: boolean
}) => req<PipelineConsumer>('/v1/pipeline/consumers', { method: 'POST', body: JSON.stringify(body) })

export const updateConsumer = (id: number, body: Partial<{
  name: string; url: string; method: string; path: string; graph_id: number | null; enabled: boolean
}>) => req<PipelineConsumer>(`/v1/pipeline/consumers/${id}`, { method: 'PATCH', body: JSON.stringify(body) })

export const deleteConsumer = (id: number) =>
  req<{ ok: boolean; deleted: number }>(`/v1/pipeline/consumers/${id}`, { method: 'DELETE' })

export const getPipelineStatus = (graphId = 1) =>
  req<{
    config: Record<string, number>
    user_signals: { watch_history: number; rated_videos: number; rated_channels: number; playlist_items: number }
    signal_sources: SignalSource[]
    sources: { total: number; enabled: number; circuit_open: number; names: string[] }
    graph: { nodes: number; edges: number }
    scorers: Array<{ id: string; name: string; description: string; scored: number; computed_at: number | null; enabled: boolean; weight: number }>
    filters: { feed_filter_count: number; weight_rule_count: number }
    feed: { items: number }
  }>(`/v1/ppr/pipeline/status?graph_id=${graphId}`)

export const getPipelineConfig = (graphId = 1) =>
  req<Record<string, number> & { _defaults: Record<string, number> }>(`/v1/ppr/pipeline/config?graph_id=${graphId}`)

export const putPipelineConfig = (updates: Record<string, number>, graphId = 1) =>
  req<{ ok: boolean; updated: string[] }>('/v1/ppr/pipeline/config', {
    method: 'PUT', body: JSON.stringify({ updates, graph_id: graphId })
  })

export const recomputeSerendipity = (graphId = 1) =>
  req<{ ok: boolean; scored: number; elapsed_seconds: number }>('/v1/ppr/pipeline/serendipity/recompute', {
    method: 'POST', body: JSON.stringify({ graph_id: graphId })
  })

export const getSerendipityScores = (limit = 100, graphId = 1) =>
  req<Array<{ video_id: string; score: number; computed_at: number; title: string | null; author: string | null }>>(`/v1/ppr/pipeline/serendipity/scores?limit=${limit}&graph_id=${graphId}`)

// ── Custom modules ────────────────────────────────────────────────────────────

export type CustomModule = {
  id: number
  name: string
  type: 'scorer' | 'filter'
  code: string
  enabled: number
  created_at: number
  updated_at: number
}

export const listModules = () => req<CustomModule[]>('/v1/modules')

export const createModule = (body: { name: string; type: string; code?: string; enabled?: boolean }) =>
  req<CustomModule>('/v1/modules', { method: 'POST', body: JSON.stringify(body) })

export const getModule = (id: number) => req<CustomModule>(`/v1/modules/${id}`)

export const updateModule = (id: number, body: { name?: string; code?: string; enabled?: boolean }) =>
  req<CustomModule>(`/v1/modules/${id}`, { method: 'PUT', body: JSON.stringify(body) })

export const deleteModule = (id: number) =>
  fetch(`/v1/modules/${id}`, { method: 'DELETE' })

export const testModule = (id: number, limit = 20) =>
  req<{ ok: boolean; error?: string; elapsed_seconds: number; results: Record<string, unknown>[] }>(
    `/v1/modules/${id}/test`,
    { method: 'POST', body: JSON.stringify({ limit }) }
  )

export const recomputeModule = (id: number) =>
  req<{ ok: boolean; scored: number; elapsed_seconds: number }>(`/v1/modules/${id}/recompute`, { method: 'POST' })

export const getModuleScores = (id: number, limit = 100) =>
  req<Array<{ video_id: string; score: number; computed_at: number; title: string | null; author: string | null }>>(`/v1/modules/${id}/scores?limit=${limit}`)

// ── Graphs ──────────────────────────────────────────────────────────────────

export interface Graph {
  id: number
  name: string
  content_type: 'mixed' | 'music' | 'video' | 'album' | 'artist'
  config_json: string | null
  created_at: number
  ppr_count: number
  ppr_computed_at: number | null
  cosine_count: number
}

export const listGraphs = () => req<Graph[]>('/v1/graphs')

export const createGraph = (body: { name: string; content_type: string; config_json?: string }) =>
  req<Graph>('/v1/graphs', { method: 'POST', body: JSON.stringify(body) })

export const updateGraph = (id: number, body: { name?: string; config_json?: string }) =>
  req<Graph>(`/v1/graphs/${id}`, { method: 'PATCH', body: JSON.stringify(body) })

export const deleteGraph = (id: number) =>
  fetch(`/v1/graphs/${id}`, { method: 'DELETE' })

export const recomputeGraph = (id: number, body: { min_seed_rating?: number; compute_spam_mass?: boolean } = {}) =>
  req<{ ok: boolean; graph_id: number; content_type: string; cosine_scored: number; elapsed_seconds: number }>(
    `/v1/graphs/${id}/recompute`, { method: 'POST', body: JSON.stringify(body) }
  )

// ── Graph Sources ────────────────────────────────────────────────────────────

export interface GraphSourceEntry extends Source {
  in_graph: boolean
  weight_override: number | null
}

export const listGraphSources = (graphId: number) =>
  req<GraphSourceEntry[]>(`/v1/graphs/${graphId}/sources`)

export const updateGraphSource = (graphId: number, sourceName: string, body: { in_graph: boolean; weight_override?: number | null }) =>
  req<{ ok: boolean }>(`/v1/graphs/${graphId}/sources/${encodeURIComponent(sourceName)}`, {
    method: 'PUT',
    body: JSON.stringify(body),
  })

// ── Converters ───────────────────────────────────────────────────────────────

export interface Converter {
  id: number
  name: string
  description: string
  content_type: 'video' | 'music' | 'mixed'
  sources: string[]
  graph_ids: number[]
  config: Record<string, unknown>
  mapping_code: string
  enabled: boolean
  created_at: number
  updated_at: number
  stats?: Record<string, number | null>
}

export const listConverters = () =>
  req<{ converters: Converter[] }>('/v1/pipeline/converters')

export const createConverter = (body: Omit<Converter, 'id' | 'created_at' | 'updated_at' | 'stats'>) =>
  req<Converter>('/v1/pipeline/converters', { method: 'POST', body: JSON.stringify(body) })

export const updateConverter = (id: number, body: Partial<Omit<Converter, 'id' | 'created_at' | 'updated_at' | 'stats'>>) =>
  req<Converter>(`/v1/pipeline/converters/${id}`, { method: 'PATCH', body: JSON.stringify(body) })

export const deleteConverter = (id: number) =>
  req<{ ok: boolean }>(`/v1/pipeline/converters/${id}`, { method: 'DELETE' })

export const populateCrawlQueue = () =>
  req<{ ok: boolean; added: number | null }>('/v1/crawl/populate', { method: 'POST' })

// ── Pipeline export ───────────────────────────────────────────────────────────

/** Fetch pipeline YAML as a Blob for file download. */
export const exportPipelineYaml = async (): Promise<Blob> => {
  const r = await fetch('/v1/pipeline/export')
  if (!r.ok) throw new Error(`Export failed: ${r.status}`)
  return r.blob()
}

/** Upload a pipeline.yaml file. Pass dryRun=true to preview without writing. */
export const importPipelineYaml = async (
  file: File, dryRun = false
): Promise<{ ok: boolean; dry_run: boolean; applied: Record<string, number> }> => {
  const form = new FormData()
  form.append('file', file)
  const url = `/v1/pipeline/import${dryRun ? '?dry_run=true' : ''}`
  const r = await fetch(url, { method: 'POST', body: form })
  if (!r.ok) {
    const msg = await r.text().catch(() => String(r.status))
    throw new Error(msg)
  }
  return r.json()
}

// ── Library recommendations (from external seeds, e.g. yamtrack) ──────────────

export interface LibraryRec {
  artist: string
  album: string
  track: string
  score: number
  cover_art: string | null
  video_id: string | null
  sources: string | null
  computed_at: number
}

export interface LibraryRecs {
  albums: LibraryRec[]
  artists: LibraryRec[]
  songs: LibraryRec[]
  computed_at: number | null
  state: { running: boolean; last_error: string | null }
}

export const getLibraryRecs = (limit = 50) =>
  req<LibraryRecs>(`/v1/music/recommendations/library?limit=${limit}`)

export const recomputeLibraryRecs = () =>
  req<{ status: string }>('/v1/music/recommendations/library/recompute', { method: 'POST' })

// ── Catalog (library) PPR — its own independent engine, fed by yamtrack ───────

export interface LibraryStatus {
  seeds: { total: number; by_source: Record<string, number>; by_kind: Record<string, number>; last_seed_at: number | null }
  results: { by_kind: Record<string, number>; total: number; computed_at: number | null }
  engine: { running: boolean; last_computed: number; last_error: string | null }
}

export interface CatalogConfig {
  alpha: number
  album_seed_cap: number
  song_seed_cap: number
  related_per_artist: number
  albums_per_artist: number
  song_recs_from_albums: number
  _defaults: Omit<CatalogConfig, '_defaults'>
}

export const getLibraryStatus = () => req<LibraryStatus>('/v1/music/library/status')

export const getCatalogConfig = () => req<CatalogConfig>('/v1/music/library/config')

export const putCatalogConfig = (body: Partial<Omit<CatalogConfig, '_defaults'>>) =>
  req<{ ok: boolean; updated: string[] }>('/v1/music/library/config', { method: 'PUT', body: JSON.stringify(body) })
