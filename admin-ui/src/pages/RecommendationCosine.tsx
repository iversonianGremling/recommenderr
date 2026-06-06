import { useEffect, useState } from 'react'
import { getCosineScores, recomputeCosine } from '../lib/api'
import { ItemTable, normalizeItem } from '../components/ItemTable'
import GraphSelector from '../components/GraphSelector'

type CosineScore = {
  video_id: string
  score: number
  computed_at: number
  title: string | null
  author: string | null
  thumbnail: string | null
  duration: number | null
}

export default function RecommendationCosine({ graphId: initGraphId }: { graphId?: number } = {}) {
  const [scores, setScores] = useState<CosineScore[]>([])
  const [limit, setLimit] = useState(100)
  const [graphId, setGraphId] = useState(initGraphId ?? 1)
  const [loading, setLoading] = useState(false)
  const [recomputing, setRecomputing] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [lastResult, setLastResult] = useState<{ scored: number; elapsed_seconds: number } | null>(null)

  const load = (lim = limit, gid = graphId) => {
    setLoading(true)
    setError(null)
    getCosineScores(lim, gid)
      .then(setScores)
      .catch((e) => setError(String(e)))
      .finally(() => setLoading(false))
  }

  useEffect(() => { load(limit, graphId) }, [limit, graphId])

  const handleRecompute = async () => {
    setRecomputing(true)
    setError(null)
    try {
      const res = await recomputeCosine({})
      setLastResult({ scored: res.scored, elapsed_seconds: res.elapsed_seconds })
      load()
    } catch (e) {
      setError(String(e))
    } finally {
      setRecomputing(false)
    }
  }

  const items = scores.map((s) =>
    normalizeItem(s as unknown as Record<string, unknown>)
  )

  const toolbar = (
    <div className="flex items-center gap-2">
      <button
        className="btn text-xs py-1 px-3"
        onClick={handleRecompute}
        disabled={recomputing}
      >
        {recomputing ? 'Computing…' : 'Recompute'}
      </button>
      <select
        className="input text-xs py-1"
        value={limit}
        onChange={(e) => setLimit(Number(e.target.value))}
      >
        {[50, 100, 200, 500].map((n) => (
          <option key={n} value={n}>{n} rows</option>
        ))}
      </select>
      <GraphSelector value={graphId} onChange={setGraphId} />
    </div>
  )

  return (
    <div>
      <h1 className="page-title mb-1">Cosine Similarity Scores</h1>
      <p className="text-text-2 text-xs mb-4">
        One-hop neighborhood overlap with your seeds — different signal from PPR (direct co-recommendation vs. random walk).
      </p>

      {lastResult && (
        <p className="text-xs text-text-2 mb-3">
          Scored {lastResult.scored} videos in {lastResult.elapsed_seconds}s
        </p>
      )}
      {error && <p className="text-red-500 text-sm mb-3">{error}</p>}
      {loading && <p className="text-text-2 text-sm">Loading…</p>}

      {!loading && (
        <ItemTable
          items={items}
          defaultColumns={['thumbnail', 'title', 'score', 'computed_at']}
          storageKey="cosine-scores"
          toolbar={toolbar}
          emptyMessage="No cosine scores — click Recompute to generate."
        />
      )}
    </div>
  )
}
