import { useState } from 'react'
import { rawSearch } from '../lib/api'

const SOURCES = [
  'lastfm', 'spotify', 'deezer', 'itunes',
  'musicbrainz', 'discogs', 'bandcamp', 'invidious',
]

export default function DiscoveryRaw() {
  const [source, setSource] = useState('deezer')
  const [q, setQ] = useState('')
  const [result, setResult] = useState<unknown>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [elapsed, setElapsed] = useState<number | null>(null)

  async function run(e: React.FormEvent) {
    e.preventDefault()
    if (!q.trim()) return
    setLoading(true)
    setError(null)
    setResult(null)
    const t0 = Date.now()
    try {
      const data = await rawSearch(source, q.trim())
      setResult(data)
      setElapsed(Date.now() - t0)
    } catch (err: unknown) {
      setError(String(err))
    } finally {
      setLoading(false)
    }
  }

  return (
    <div className="max-w-3xl">
      <div className="mb-5">
        <h1 className="text-xl font-semibold text-text">Raw Query</h1>
        <p className="mt-0.5 text-sm text-text-2">Fire a search against any source and inspect the raw response.</p>
      </div>

      <form onSubmit={run} className="flex items-center gap-3 mb-5 flex-wrap">
        <select value={source} onChange={e => setSource(e.target.value)} className="text-sm">
          {SOURCES.map(s => <option key={s} value={s}>{s}</option>)}
        </select>
        <input
          value={q}
          onChange={e => setQ(e.target.value)}
          placeholder="Search query…"
          className="text-sm flex-1 min-w-[200px]"
        />
        <button type="submit" disabled={loading} className="btn-primary">
          {loading ? <span className="spinner-sm" /> : 'Run'}
        </button>
      </form>

      {error && (
        <div className="mb-4 rounded border border-red-500/30 bg-red-500/10 px-4 py-3 text-sm text-red-300">
          {error}
        </div>
      )}

      {result !== null && (
        <div className="surface p-4">
          <div className="mb-2 flex items-center justify-between">
            <span className="text-[12px] text-text-2">
              {Array.isArray(result) ? `${result.length} results` : typeof result}
              {elapsed != null && ` — ${elapsed}ms`}
            </span>
          </div>
          <pre className="overflow-auto rounded bg-bg-3 p-3 text-[12px] text-text max-h-[60vh]">
            {JSON.stringify(result, null, 2)}
          </pre>
        </div>
      )}
    </div>
  )
}
