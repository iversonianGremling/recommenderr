import { useEffect, useState } from 'react'
import type { Source } from '../lib/types'
import { listSources } from '../lib/api'
import SourceHealthCard from '../components/SourceHealthCard'

export default function DiscoverySources() {
  const [sources, setSources] = useState<Source[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    setLoading(true)
    listSources()
      .then(setSources)
      .catch(e => setError(String(e)))
      .finally(() => setLoading(false))
  }, [])

  function handleUpdate(updated: Source) {
    setSources(prev => prev.map(s => s.name === updated.name ? updated : s))
  }

  const enabled = sources.filter(s => s.enabled)
  const disabled = sources.filter(s => !s.enabled)
  const circuitOpen = sources.filter(s => s.circuit_open)

  return (
    <div>
      <div className="mb-5 flex items-center justify-between">
        <div>
          <h1 className="text-xl font-semibold text-text">Sources</h1>
          <p className="mt-0.5 text-sm text-text-2">
            {sources.length} declared — {enabled.length} enabled
            {circuitOpen.length > 0 && <span className="ml-2 text-red-400">{circuitOpen.length} circuit open</span>}
          </p>
        </div>
        <button onClick={() => listSources().then(setSources)} className="text-[12px]">
          Refresh
        </button>
      </div>

      {loading && <div className="text-text-2">Loading…</div>}
      {error && <div className="rounded border border-red-500/30 bg-red-500/10 px-4 py-3 text-sm text-red-300">{error}</div>}

      {!loading && !error && (
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-3">
          {sources.map(s => (
            <SourceHealthCard key={s.name} source={s} onUpdate={handleUpdate} />
          ))}
        </div>
      )}
    </div>
  )
}
