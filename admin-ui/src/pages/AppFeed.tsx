import { useEffect, useState } from 'react'
import { getPprFeed, getPprFeedStatus, invalidatePpr, listPersonas } from '../lib/api'
import type { FeedItem, Persona } from '../lib/types'
import { ItemTable, normalizeItem } from '../components/ItemTable'
import GraphSelector from '../components/GraphSelector'

const CONTENT_MODES = [
  { value: 'all', label: 'All' },
  { value: 'video', label: 'Video' },
  { value: 'music', label: 'Music' },
]

// Moods: keyword shortcuts that get passed as category filters
const MOODS = [
  { value: '', label: 'Any mood' },
  { value: 'chill', label: 'Chill' },
  { value: 'tutorial', label: 'Tutorial' },
  { value: 'gaming', label: 'Gaming' },
  { value: 'music', label: 'Music' },
  { value: 'programming', label: 'Programming' },
  { value: 'linux', label: 'Linux' },
  { value: 'anime', label: 'Anime' },
  { value: 'review', label: 'Review' },
]

export default function AppFeed() {
  const [items, setItems] = useState<FeedItem[]>([])
  const [status, setStatus] = useState<{ items: number; age_seconds: number | null; is_refreshing: boolean } | null>(null)
  const [personas, setPersonas] = useState<Persona[]>([])
  const [personaId, setPersonaId] = useState<number | null>(null)
  const [graphId, setGraphId] = useState(1)
  const [category, setCategory] = useState('')
  const [mood, setMood] = useState('')
  const [sort, setSort] = useState('score')
  const [loading, setLoading] = useState(false)
  const [refreshing, setRefreshing] = useState(false)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    listPersonas().then(setPersonas).catch(() => {})
  }, [])

  const effectiveCategory = mood || category

  const load = async () => {
    setLoading(true)
    setError(null)
    try {
      const [feed, st] = await Promise.all([
        getPprFeed({
          limit: 100, offset: 0,
          category: effectiveCategory,
          sort,
          persona_id: personaId,
          graph_id: graphId,
        }),
        getPprFeedStatus(),
      ])
      setItems(feed.items)
      setStatus(st)
    } catch (e) {
      setError(String(e))
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => { load() }, [personaId, graphId, effectiveCategory, sort])

  const handleRefresh = async () => {
    setRefreshing(true)
    try {
      await invalidatePpr()
      await load()
    } finally {
      setRefreshing(false)
    }
  }

  const normalized = items.map((i) => normalizeItem(i as unknown as Record<string, unknown>))

  const feedToolbar = (
    <div className="flex items-center gap-2 flex-wrap">
      {/* Persona selector */}
      <select
        className="input text-xs py-1"
        value={personaId ?? ''}
        onChange={(e) => setPersonaId(e.target.value ? Number(e.target.value) : null)}
      >
        <option value="">Global feed</option>
        {personas.map((p) => (
          <option key={p.id} value={p.id}>{p.name}</option>
        ))}
      </select>

      {/* Graph selector */}
      <GraphSelector value={graphId} onChange={setGraphId} />

      {/* Mood picker */}
      <select
        className="input text-xs py-1"
        value={mood}
        onChange={(e) => { setMood(e.target.value); setCategory('') }}
      >
        {MOODS.map((m) => (
          <option key={m.value} value={m.value}>{m.label}</option>
        ))}
      </select>

      {/* Free-text category */}
      {!mood && (
        <input
          className="input text-xs py-1 w-28"
          placeholder="Category…"
          value={category}
          onChange={(e) => setCategory(e.target.value)}
        />
      )}

      {/* Sort */}
      <select className="input text-xs py-1" value={sort} onChange={(e) => setSort(e.target.value)}>
        <option value="score">By score</option>
        <option value="date">By date</option>
        <option value="title">By title</option>
        <option value="channel">By channel</option>
        <option value="duration">By duration</option>
        <option value="spam_mass">By spam mass</option>
      </select>

      <button className="btn text-xs py-1 px-3" onClick={handleRefresh} disabled={refreshing}>
        {refreshing ? 'Refreshing…' : 'Invalidate cache'}
      </button>
    </div>
  )

  const contextLabel = personaId
    ? `Persona: ${personas.find((p) => p.id === personaId)?.name ?? personaId}`
    : 'Global'

  return (
    <div>
      <div className="flex items-center gap-3 mb-4 flex-wrap">
        <h1 className="page-title">Feed</h1>
        <span className="text-xs rounded bg-bg-3 border border-border px-2 py-0.5 text-text-2">
          {contextLabel}
        </span>
        {status && (
          <span className="text-xs text-text-2">
            {status.items.toLocaleString()} cached
            {status.age_seconds != null && ` · ${Math.round(status.age_seconds)}s old`}
            {status.is_refreshing && ' · refreshing…'}
          </span>
        )}
      </div>

      {error && <p className="text-red-500 text-sm mb-3">{error}</p>}
      {loading && <p className="text-text-2 text-sm">Loading…</p>}

      {!loading && (
        <ItemTable
          items={normalized}
          defaultColumns={['thumbnail', 'title', 'score', 'duration', 'category', 'published_at']}
          storageKey="app-feed"
          withGrid
          toolbar={feedToolbar}
          emptyMessage="No feed items — run Recompute in the Recommendation panel first."
        />
      )}
    </div>
  )
}
