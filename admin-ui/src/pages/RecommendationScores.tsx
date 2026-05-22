import { useEffect, useState } from 'react'
import { getPprScores, getPprSeeds, getPprWhy } from '../lib/api'
import type { PprScore, PprSeed, WhyResult } from '../lib/types'

function WhyDrawer({ videoId, onClose }: { videoId: string; onClose: () => void }) {
  const [data, setData] = useState<WhyResult | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    getPprWhy(videoId)
      .then(setData)
      .catch((e) => setError(String(e)))
  }, [videoId])

  return (
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
              <p className="text-text-2">Score: <span className="font-mono text-text">{Number(data.score).toFixed(6)}</span></p>
            )}
            {data.contributions && data.contributions.length > 0 && (
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
            )}
            {(!data.contributions || data.contributions.length === 0) && (
              <div className="text-xs text-text-2">
                <pre className="overflow-auto">{JSON.stringify(data, null, 2)}</pre>
              </div>
            )}
          </>
        )}
      </div>
    </div>
  )
}

type Tab = 'scores' | 'seeds'

export default function RecommendationScores() {
  const [tab, setTab] = useState<Tab>('scores')
  const [scores, setScores] = useState<PprScore[]>([])
  const [seeds, setSeeds] = useState<PprSeed[]>([])
  const [limit, setLimit] = useState(100)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [whyId, setWhyId] = useState<string | null>(null)

  useEffect(() => {
    setLoading(true)
    setError(null)
    const p = tab === 'scores'
      ? getPprScores(limit).then(setScores)
      : getPprSeeds(limit).then(setSeeds)
    p.catch((e) => setError(String(e))).finally(() => setLoading(false))
  }, [tab, limit])

  return (
    <div className="max-w-4xl">
      <div className="flex items-center justify-between mb-4">
        <h1 className="page-title">PPR Scores</h1>
        <div className="flex items-center gap-3">
          <div className="flex rounded border border-border">
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
        </div>
      </div>

      {error && <p className="text-red-500 text-sm mb-3">{error}</p>}
      {loading && <p className="text-text-2 text-sm">Loading…</p>}

      {tab === 'scores' && !loading && (
        <div className="overflow-auto rounded border border-border">
          <table className="w-full text-xs">
            <thead className="bg-bg-2">
              <tr>
                <th className="th text-left">Video</th>
                <th className="th text-right">Score</th>
                <th className="th text-right">Spam mass</th>
                <th className="th"></th>
              </tr>
            </thead>
            <tbody>
              {scores.map((s) => (
                <tr key={s.video_id} className="tr">
                  <td className="td">
                    <p className="font-medium text-text truncate max-w-xs">{s.title ?? s.video_id}</p>
                    {s.author && <p className="text-text-2">{s.author}</p>}
                  </td>
                  <td className="td text-right font-mono">{s.score.toFixed(6)}</td>
                  <td className="td text-right font-mono">{s.spam_mass != null ? s.spam_mass.toFixed(4) : '—'}</td>
                  <td className="td text-right">
                    <button className="btn text-xs py-0.5 px-2" onClick={() => setWhyId(s.video_id)}>
                      Why?
                    </button>
                  </td>
                </tr>
              ))}
              {scores.length === 0 && (
                <tr><td colSpan={4} className="td text-center text-text-2">No scores — run Recompute first.</td></tr>
              )}
            </tbody>
          </table>
        </div>
      )}

      {tab === 'seeds' && !loading && (
        <div className="overflow-auto rounded border border-border">
          <table className="w-full text-xs">
            <thead className="bg-bg-2">
              <tr>
                <th className="th text-left">Video</th>
                <th className="th text-right">Weight</th>
                <th className="th text-left">Reasons</th>
              </tr>
            </thead>
            <tbody>
              {seeds.map((s) => (
                <tr key={s.video_id} className="tr">
                  <td className="td">
                    <p className="font-medium text-text truncate max-w-xs">{s.title ?? s.video_id}</p>
                    {s.author && <p className="text-text-2">{s.author}</p>}
                  </td>
                  <td className="td text-right font-mono">{s.weight.toFixed(4)}</td>
                  <td className="td text-text-2">{s.reasons.join(', ') || '—'}</td>
                </tr>
              ))}
              {seeds.length === 0 && (
                <tr><td colSpan={3} className="td text-center text-text-2">No seeds — watch or rate some videos first.</td></tr>
              )}
            </tbody>
          </table>
        </div>
      )}

      {whyId && <WhyDrawer videoId={whyId} onClose={() => setWhyId(null)} />}
      {whyId && <div className="fixed inset-0 z-30 bg-black/20" onClick={() => setWhyId(null)} />}
    </div>
  )
}
