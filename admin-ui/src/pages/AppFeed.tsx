import { useEffect, useState } from 'react'
import { getPprFeed, getPprFeedStatus, invalidatePpr } from '../lib/api'
import type { FeedItem } from '../lib/types'
import { ItemTable, normalizeItem } from '../components/ItemTable'

export default function AppFeed() {
  const [items, setItems] = useState<FeedItem[]>([])
  const [status, setStatus] = useState<{ items: number; age_seconds: number | null; is_refreshing: boolean } | null>(null)
  const [category, setCategory] = useState('')
  const [sort, setSort] = useState('score')
  const [loading, setLoading] = useState(false)
  const [refreshing, setRefreshing] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const load = async () => {
    setLoading(true)
    setError(null)
    try {
      const [feed, st] = await Promise.all([
        getPprFeed({ limit: 100, offset: 0, category, sort }),
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

  useEffect(() => { load() }, [category, sort])

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
      <input
        className="input text-xs py-1 w-32"
        placeholder="Category…"
        value={category}
        onChange={(e) => setCategory(e.target.value)}
      />
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

  return (
    <div>
      <div className="flex items-center gap-3 mb-4 flex-wrap">
        <h1 className="page-title">Feed</h1>
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
