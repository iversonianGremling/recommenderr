import type { Item, Scheme, Source } from './types'

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
