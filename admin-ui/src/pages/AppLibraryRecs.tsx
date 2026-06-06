import { useEffect, useState } from 'react'
import { getLibraryRecs, recomputeLibraryRecs } from '../lib/api'
import type { LibraryRec, LibraryRecs } from '../lib/api'

const PLACEHOLDER =
  'data:image/svg+xml;utf8,' +
  encodeURIComponent(
    '<svg xmlns="http://www.w3.org/2000/svg" width="48" height="48"><rect width="48" height="48" fill="#222"/></svg>',
  )

function RecCard({ rec, kind }: { rec: LibraryRec; kind: 'album' | 'artist' | 'song' }) {
  const title = kind === 'album' ? rec.album : kind === 'song' ? rec.track : rec.artist
  const sub = kind === 'artist' ? rec.sources || '' : rec.artist
  return (
    <div className="flex items-center gap-3 rounded px-2 py-1.5 hover:bg-bg-2">
      <img
        src={rec.cover_art || PLACEHOLDER}
        alt=""
        className="h-10 w-10 flex-shrink-0 rounded object-cover"
        onError={(e) => ((e.target as HTMLImageElement).src = PLACEHOLDER)}
      />
      <div className="min-w-0 flex-1">
        <div className="truncate text-[13px] text-text">{title || '—'}</div>
        <div className="truncate text-[11px] text-text-2">{sub}</div>
      </div>
      <div className="flex-shrink-0 text-[11px] tabular-nums text-text-2">{rec.score.toFixed(3)}</div>
    </div>
  )
}

function Column({ label, kind, recs }: { label: string; kind: 'album' | 'artist' | 'song'; recs: LibraryRec[] }) {
  return (
    <div className="flex-1">
      <div className="nav-section-label mb-2">
        {label} <span className="text-text-2">({recs.length})</span>
      </div>
      <div className="space-y-0.5">
        {recs.length === 0 ? (
          <div className="px-2 py-4 text-[12px] text-text-2">No recommendations yet.</div>
        ) : (
          recs.map((r, i) => <RecCard key={`${kind}-${i}`} rec={r} kind={kind} />)
        )}
      </div>
    </div>
  )
}

export default function AppLibraryRecs() {
  const [data, setData] = useState<LibraryRecs | null>(null)
  const [loading, setLoading] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const load = async () => {
    setLoading(true)
    setError(null)
    try {
      setData(await getLibraryRecs(50))
    } catch (e) {
      setError(String(e))
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    load()
  }, [])

  // While a recompute is running, poll until it settles.
  useEffect(() => {
    if (!data?.state.running) return
    const t = setInterval(load, 4000)
    return () => clearInterval(t)
  }, [data?.state.running])

  const handleRecompute = async () => {
    setBusy(true)
    try {
      await recomputeLibraryRecs()
      await load()
    } catch (e) {
      setError(String(e))
    } finally {
      setBusy(false)
    }
  }

  const computedAgo =
    data?.computed_at != null
      ? `${Math.round((Date.now() / 1000 - data.computed_at) / 60)} min ago`
      : 'never'

  return (
    <div>
      <div className="mb-4 flex items-center justify-between">
        <div>
          <h1 className="text-[15px] font-semibold text-text">Library Recommendations</h1>
          <p className="text-[12px] text-text-2">
            Weighted PPR over your synced library (score ≥ 7). Computed {computedAgo}.
            {data?.state.running && ' — recomputing…'}
            {data?.state.last_error && (
              <span className="text-red-400"> — error: {data.state.last_error}</span>
            )}
          </p>
        </div>
        <button
          onClick={handleRecompute}
          disabled={busy || data?.state.running}
          className="rounded border border-border bg-bg-2 px-3 py-1.5 text-[12px] text-text hover:bg-border disabled:opacity-50"
        >
          {busy || data?.state.running ? 'Recomputing…' : 'Recompute'}
        </button>
      </div>

      {error && <div className="mb-3 text-[12px] text-red-400">{error}</div>}
      {loading && !data && <div className="text-[12px] text-text-2">Loading…</div>}

      {data && (
        <div className="flex gap-6">
          <Column label="Albums" kind="album" recs={data.albums} />
          <Column label="Artists" kind="artist" recs={data.artists} />
          <Column label="Songs" kind="song" recs={data.songs} />
        </div>
      )}
    </div>
  )
}
