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

export const getPprFeed = (params: { limit?: number; offset?: number; category?: string; sort?: string }) => {
  return req<{ items: FeedItem[]; total: number }>('/v1/ppr/feed', {
    method: 'POST',
    body: JSON.stringify({ limit: params.limit ?? 50, offset: params.offset ?? 0, category: params.category ?? '', sort: params.sort ?? 'score' }),
  })
}

export const getPprFeedStatus = () => req<{ items: number; age_seconds: number | null; is_refreshing: boolean }>('/v1/ppr/feed/status')

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
