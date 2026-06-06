import { useEffect, useState } from 'react'
import { getPprScores, getPprSeeds, getPprWhy } from '../lib/api'
import type { PprScore, PprSeed, WhyResult } from '../lib/types'
import { ItemTable, normalizeItem } from '../components/ItemTable'
import type { NormalizedItem } from '../components/ItemTable'
import GraphSelector from '../components/GraphSelector'

function WhyDrawer({ videoId, onClose }: { videoId: string; onClose: () => void }) {
  const [data, setData] = useState<WhyResult | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    getPprWhy(videoId)
      .then(setData)
      .catch((e) => setError(String(e)))
  }, [videoId])

  return (
    <>
      <div className="fixed inset-0 z-30 bg-black/20" onClick={onClose} />
      <div className="fixed inset-y-0 right-0 z-40 w-96 border-l border-border bg-bg overflow-y-auto shadow-lg">
        <div className="flex items-center justify-between border-b border-border p-4">
          <h2 className="text-sm font-semibold text-text">Why recommended?</h2>
          <button className="text-text-2 hover:text-text text-lg leading-none" onClick={onClose}>×</button>
        </div>
        <div className="p-4 space-y-3 text-sm">
          {error && <p className="text-red-500">{error}</p>}
          {!data && !error && <p className="text-text-2">Loading…</p>}
          {data && (
            <>
              <div>
                <p className="font-medium text-text">{data.title ?? videoId}</p>
                {data.author && <p className="text-text-2 text-xs">{data.author as string}</p>}
              </div>
              {data.score !== undefined && (
                <p className="text-text-2">
                  Score: <span className="font-mono text-text">{Number(data.score).toFixed(6)}</span>
                </p>
              )}
              {data.contributions && (data.contributions as unknown[]).length > 0 ? (
                <div>
                  <p className="font-medium text-text mb-2">Contributions</p>
                  <div className="space-y-1.5">
                    {(data.contributions as Array<{ source: string; weight: number; reason: string }>).map((c, i) => (
                      <div key={i} className="rounded border border-border p-2">
                        <div className="flex items-center justify-between">
                          <span className="font-mono text-xs text-text-2">{c.source}</span>
                          <span className="font-mono text-xs text-accent">{Number(c.weight).toFixed(4)}</span>
                        </div>
                        {c.reason && <p className="text-xs text-text-2 mt-0.5">{c.reason}</p>}
                      </div>
                    ))}
                  </div>
                </div>
              ) : (
                <pre className="text-xs text-text-2 overflow-auto">{JSON.stringify(data, null, 2)}</pre>
              )}
            </>
          )}
        </div>
      </div>
    </>
  )
}

type Tab = 'scores' | 'seeds'

export default function RecommendationScores({ graphId: initGraphId }: { graphId?: number } = {}) {
  const [tab, setTab] = useState<Tab>('scores')
  const [scores, setScores] = useState<PprScore[]>([])
  const [seeds, setSeeds] = useState<PprSeed[]>([])
  const [limit, setLimit] = useState(100)
  const [graphId, setGraphId] = useState(initGraphId ?? 1)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [whyId, setWhyId] = useState<string | null>(null)

  useEffect(() => {
    setLoading(true)
    setError(null)
    const p = tab === 'scores'
      ? getPprScores(limit, graphId).then(setScores)
      : getPprSeeds(limit).then(setSeeds)
    p.catch((e) => setError(String(e))).finally(() => setLoading(false))
  }, [tab, limit, graphId])

  const scoreItems = scores.map((s) => normalizeItem(s as unknown as Record<string, unknown>))
  const seedItems = seeds.map((s) =>
    normalizeItem({ ...s, video_id: s.video_id } as Record<string, unknown>)
  )

  const tabToolbar = (
    <div className="flex items-center gap-2">
      <div className="flex rounded border border-border overflow-hidden">
        {(['scores', 'seeds'] as Tab[]).map((t) => (
          <button
            key={t}
            className={`px-3 py-1 text-xs capitalize ${tab === t ? 'bg-accent text-white' : 'text-text-2 hover:text-text'}`}
            onClick={() => setTab(t)}
          >
            {t}
          </button>
        ))}
      </div>
      <select
        className="input text-xs py-1"
        value={limit}
        onChange={(e) => setLimit(Number(e.target.value))}
      >
        {[50, 100, 200, 500].map((n) => (
          <option key={n} value={n}>{n} rows</option>
        ))}
      </select>
      {tab === 'scores' && (
        <GraphSelector value={graphId} onChange={setGraphId} />
      )}
    </div>
  )

  return (
    <div>
      <h1 className="page-title mb-4">PPR Scores</h1>

      {error && <p className="text-red-500 text-sm mb-3">{error}</p>}
      {loading && <p className="text-text-2 text-sm">Loading…</p>}

      {!loading && tab === 'scores' && (
        <ItemTable
          items={scoreItems}
          defaultColumns={['thumbnail', 'title', 'score', 'effective_score', 'spam_mass', 'computed_at']}
          storageKey="ppr-scores"
          toolbar={tabToolbar}
          actions={(item: NormalizedItem) => (
            <button className="btn text-xs py-0.5 px-2" onClick={() => setWhyId(item.id)}>
              Why?
            </button>
          )}
          emptyMessage="No scores — run Recompute first."
        />
      )}

      {!loading && tab === 'seeds' && (
        <ItemTable
          items={seedItems}
          defaultColumns={['thumbnail', 'title', 'weight', 'reasons']}
          storageKey="ppr-seeds"
          toolbar={tabToolbar}
          emptyMessage="No seeds — watch or rate some videos first."
        />
      )}

      {whyId && <WhyDrawer videoId={whyId} onClose={() => setWhyId(null)} />}
    </div>
  )
}
