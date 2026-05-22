import type { FeedFilter, GraphStats, Item, PprConfig, PprScore, PprSeed, Scheme, Source, WeightRule, WhyResult } from './types'

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

export const getPprConfig = () => req<PprConfig>('/v1/ppr/config')

export const putPprConfig = (body: Partial<Omit<PprConfig, '_defaults'>>) =>
  req<{ ok: boolean; updated: string[] }>('/v1/ppr/config', { method: 'PUT', body: JSON.stringify(body) })

export const resetPprConfig = () =>
  req<{ ok: boolean }>('/v1/ppr/config/reset', { method: 'POST' })

export const recomputePpr = (body: { min_seed_rating?: number; compute_spam_mass?: boolean }) =>
  req<{ ok: boolean; elapsed_seconds: number; items: number }>('/v1/ppr/recompute', {
    method: 'POST',
    body: JSON.stringify(body),
  })

export const invalidatePpr = () =>
  req<{ ok: boolean }>('/v1/ppr/invalidate', { method: 'POST' })

export const getPprScores = (limit = 100) =>
  req<PprScore[]>(`/v1/ppr/scores?limit=${limit}`)

export const getPprSeeds = (limit = 200) =>
  req<PprSeed[]>(`/v1/ppr/seeds?limit=${limit}`)

export const getGraphStats = () => req<GraphStats>('/v1/ppr/graph/stats')

export const getPprWhy = (videoId: string) => req<WhyResult>(`/v1/ppr/why/${encodeURIComponent(videoId)}`)

export const listWeightRules = () => req<WeightRule[]>('/v1/ppr/weight-rules')

export const addWeightRule = (body: { rule_type: string; match_value: string; multiplier: number }) =>
  req<{ ok: boolean }>('/v1/ppr/weight-rules', { method: 'POST', body: JSON.stringify(body) })

export const deleteWeightRule = (id: number) =>
  req<{ ok: boolean }>(`/v1/ppr/weight-rules/${id}`, { method: 'DELETE' })

export const listFeedFilters = () => req<FeedFilter[]>('/v1/ppr/feed-filters')

export const addFeedFilter = (body: { filter_type: string; match_value: string }) =>
  req<{ ok: boolean }>('/v1/ppr/feed-filters', { method: 'POST', body: JSON.stringify(body) })

export const deleteFeedFilter = (id: number) =>
  req<{ ok: boolean }>(`/v1/ppr/feed-filters/${id}`, { method: 'DELETE' })

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
