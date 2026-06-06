import { useEffect, useState } from 'react'
import type { Source } from '../lib/types'
import { listSources, listGraphSources, updateGraphSource, type GraphSourceEntry } from '../lib/api'
import SourceHealthCard from '../components/SourceHealthCard'
import GraphSelector from '../components/GraphSelector'

export default function DiscoverySources() {
  const [sources, setSources] = useState<Source[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [selectedGraphId, setSelectedGraphId] = useState(0)
  const [graphSources, setGraphSources] = useState<GraphSourceEntry[]>([])
  const [toggling, setToggling] = useState<string | null>(null)

  useEffect(() => {
    setLoading(true)
    listSources()
      .then(setSources)
      .catch(e => setError(String(e)))
      .finally(() => setLoading(false))
  }, [])

  useEffect(() => {
    if (selectedGraphId > 0) {
      listGraphSources(selectedGraphId).then(setGraphSources).catch(() => {})
    }
  }, [selectedGraphId])

  function handleUpdate(updated: Source) {
    setSources(prev => prev.map(s => s.name === updated.name ? updated : s))
  }

  async function toggleGraphSource(sourceName: string, currentInGraph: boolean) {
    if (!selectedGraphId) return
    setToggling(sourceName)
    try {
      await updateGraphSource(selectedGraphId, sourceName, { in_graph: !currentInGraph })
      listGraphSources(selectedGraphId).then(setGraphSources)
    } catch (e) {
      setError(String(e))
    } finally {
      setToggling(null)
    }
  }

  const graphSourceMap = Object.fromEntries(graphSources.map(gs => [gs.name, gs]))
  const enabled = sources.filter(s => s.enabled)
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
        <div className="flex items-center gap-3">
          <GraphSelector value={selectedGraphId} onChange={setSelectedGraphId} />
          <button onClick={() => listSources().then(setSources)} className="text-[12px]">
            Refresh
          </button>
        </div>
      </div>

      {error && <div className="rounded border border-red-500/30 bg-red-500/10 px-4 py-3 text-sm text-red-300 mb-4">{error}</div>}

      {/* Per-graph membership table */}
      {selectedGraphId > 0 && !loading && (
        <div className="mb-6">
          <h2 className="text-xs font-medium text-text-2 uppercase tracking-wider mb-2">Graph membership</h2>
          <div className="rounded border border-border overflow-hidden">
            <table className="w-full text-xs">
              <thead className="bg-bg-2">
                <tr>
                  <th className="th text-left">Source</th>
                  <th className="th text-left">Kind</th>
                  <th className="th text-center">In graph</th>
                  <th className="th text-right">Weight override</th>
                </tr>
              </thead>
              <tbody>
                {sources.map(s => {
                  const gs = graphSourceMap[s.name]
                  const inGraph = gs?.in_graph ?? false
                  return (
                    <tr key={s.name} className="tr">
                      <td className="td">
                        <span className="font-medium text-text">{s.display_name}</span>
                        <span className="ml-1.5 text-text-3">{s.name}</span>
                      </td>
                      <td className="td text-text-2">{s.kind}</td>
                      <td className="td text-center">
                        <button
                          className={`text-[11px] px-2 py-0.5 rounded transition-colors ${inGraph ? 'bg-accent2/20 text-accent2 hover:bg-accent2/30' : 'bg-bg-3 text-text-2 hover:bg-bg-3/70'}`}
                          disabled={toggling === s.name}
                          onClick={() => toggleGraphSource(s.name, inGraph)}
                        >
                          {toggling === s.name ? '…' : inGraph ? 'In graph' : 'Excluded'}
                        </button>
                      </td>
                      <td className="td text-right">
                        {gs?.weight_override != null
                          ? <span className="text-text">{gs.weight_override.toFixed(2)}</span>
                          : <span className="text-text-3">global ({s.weight.toFixed(2)})</span>}
                      </td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          </div>
        </div>
      )}

      {loading && <div className="text-text-2">Loading…</div>}

      {!loading && !error && (
        <div>
          <h2 className="text-xs font-medium text-text-2 uppercase tracking-wider mb-2">Global health</h2>
          <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-3">
            {sources.map(s => (
              <SourceHealthCard key={s.name} source={s} onUpdate={handleUpdate} />
            ))}
          </div>
        </div>
      )}
    </div>
  )
}
