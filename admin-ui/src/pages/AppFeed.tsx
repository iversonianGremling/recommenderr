import { useEffect, useState } from 'react'
import { getPprFeed, getPprFeedStatus, invalidatePpr } from '../lib/api'
import type { FeedItem } from '../lib/types'

function Duration({ secs }: { secs: number | null }) {
  if (secs == null) return null
  const m = Math.floor(secs / 60)
  const s = secs % 60
  return <>{m}:{String(s).padStart(2, '0')}</>
}

function VideoCard({ item }: { item: FeedItem }) {
  return (
    <div className="rounded border border-border bg-bg-2 overflow-hidden flex flex-col">
      {item.thumbnail ? (
        <img src={item.thumbnail} alt="" className="w-full aspect-video object-cover" />
      ) : (
        <div className="w-full aspect-video bg-bg-3 flex items-center justify-center text-text-2 text-xs">No image</div>
      )}
      <div className="p-2 flex-1 flex flex-col gap-0.5">
        <p className="text-xs font-medium text-text line-clamp-2 leading-snug">{item.title ?? item.video_id}</p>
        {item.author && <p className="text-[10px] text-text-2 truncate">{item.author}</p>}
        <div className="mt-auto flex items-center justify-between text-[10px] text-text-2 pt-1">
          {item.score != null && <span className="font-mono">{item.score.toFixed(5)}</span>}
          <Duration secs={item.duration} />
        </div>
      </div>
    </div>
  )
}

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
        getPprFeed({ limit: 48, offset: 0, category, sort }),
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
        <div className="ml-auto flex items-center gap-2">
          <input
            className="input text-xs py-1 w-32"
            placeholder="Category…"
            value={category}
            onChange={(e) => setCategory(e.target.value)}
          />
          <select className="input text-xs py-1" value={sort} onChange={(e) => setSort(e.target.value)}>
            <option value="score">By score</option>
            <option value="date">By date</option>
            <option value="random">Random</option>
          </select>
          <button className="btn text-xs py-1 px-3" onClick={handleRefresh} disabled={refreshing}>
            {refreshing ? 'Refreshing…' : 'Invalidate cache'}
          </button>
        </div>
      </div>

      {error && <p className="text-red-500 text-sm mb-3">{error}</p>}
      {loading && <p className="text-text-2 text-sm">Loading…</p>}

      {!loading && (
        <div className="grid gap-3" style={{ gridTemplateColumns: 'repeat(auto-fill, minmax(200px, 1fr))' }}>
          {items.map((item) => (
            <VideoCard key={item.video_id} item={item} />
          ))}
          {items.length === 0 && (
            <p className="text-text-2 text-sm col-span-full">No feed items — run Recompute in the Recommendation panel first.</p>
          )}
        </div>
      )}
    </div>
  )
}
