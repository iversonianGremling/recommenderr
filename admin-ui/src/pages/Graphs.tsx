import { useEffect, useState } from 'react'
import { createGraph, deleteGraph, listGraphs, recomputeGraph, type Graph } from '../lib/api'

const CONTENT_TYPE_LABELS: Record<string, string> = {
  mixed: 'Mixed (all)',
  music: 'Music only',
  video: 'Video only',
}

const CONTENT_TYPE_BADGE: Record<string, string> = {
  mixed: 'bg-blue-500/15 text-blue-400',
  music: 'bg-purple-500/15 text-purple-400',
  video: 'bg-green-500/15 text-green-400',
}

function ts(epoch: number | null) {
  if (!epoch) return '—'
  return new Date(epoch * 1000).toLocaleString()
}

export default function Graphs() {
  const [graphs, setGraphs] = useState<Graph[]>([])
  const [loading, setLoading] = useState(true)
  const [recomputing, setRecomputing] = useState<number | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [creating, setCreating] = useState(false)
  const [newName, setNewName] = useState('')
  const [newType, setNewType] = useState<'mixed' | 'music' | 'video'>('mixed')

  const load = () =>
    listGraphs()
      .then(setGraphs)
      .catch((e) => setError(String(e)))
      .finally(() => setLoading(false))

  useEffect(() => { load() }, [])

  const handleCreate = async () => {
    if (!newName.trim()) return
    try {
      await createGraph({ name: newName.trim(), content_type: newType })
      setNewName('')
      setCreating(false)
      load()
    } catch (e) {
      setError(String(e))
    }
  }

  const handleDelete = async (id: number) => {
    if (!confirm('Delete this graph and all its PPR/cosine scores?')) return
    try {
      await deleteGraph(id)
      load()
    } catch (e) {
      setError(String(e))
    }
  }

  const handleRecompute = async (id: number) => {
    setRecomputing(id)
    setError(null)
    try {
      const res = await recomputeGraph(id)
      alert(`Done in ${res.elapsed_seconds}s — ${res.cosine_scored} cosine scores`)
      load()
    } catch (e) {
      setError(String(e))
    } finally {
      setRecomputing(null)
    }
  }

  return (
    <div className="max-w-4xl">
      <div className="flex items-center justify-between mb-4">
        <h1 className="page-title">Graphs</h1>
        <button className="btn-primary text-xs" onClick={() => setCreating(true)}>
          + New graph
        </button>
      </div>
      <p className="text-xs text-text-2 mb-4">
        Each graph runs PPR on a filtered subset of recommendation_edges. Built-in graphs (1–3)
        cannot be deleted. Recompute triggers PPR + cosine scoring for that graph's content type.
      </p>

      {error && <p className="text-red-400 text-xs mb-3">{error}</p>}

      {creating && (
        <div className="mb-4 rounded border border-border bg-bg-2 p-4 flex gap-3 items-end">
          <div className="flex-1">
            <label className="label">Name</label>
            <input className="input w-full" value={newName} onChange={(e) => setNewName(e.target.value)}
              placeholder="e.g. music-v2" autoFocus onKeyDown={(e) => e.key === 'Enter' && handleCreate()} />
          </div>
          <div>
            <label className="label">Content type</label>
            <select className="input" value={newType} onChange={(e) => setNewType(e.target.value as typeof newType)}>
              <option value="mixed">Mixed (all edges)</option>
              <option value="music">Music only</option>
              <option value="video">Video only</option>
            </select>
          </div>
          <button className="btn-primary" onClick={handleCreate}>Create</button>
          <button className="btn" onClick={() => setCreating(false)}>Cancel</button>
        </div>
      )}

      {loading ? (
        <p className="text-text-2 text-sm">Loading…</p>
      ) : (
        <div className="rounded border border-border overflow-hidden">
          <table className="w-full text-xs">
            <thead className="bg-bg-2">
              <tr>
                <th className="th text-left w-8">ID</th>
                <th className="th text-left">Name</th>
                <th className="th text-left">Content type</th>
                <th className="th text-right">PPR scores</th>
                <th className="th text-right">Cosine scores</th>
                <th className="th text-right">Last computed</th>
                <th className="th text-right"></th>
              </tr>
            </thead>
            <tbody>
              {graphs.map((g) => (
                <tr key={g.id} className="tr">
                  <td className="td text-text-2">{g.id}</td>
                  <td className="td font-medium text-text">{g.name}</td>
                  <td className="td">
                    <span className={`inline-block rounded px-1.5 py-0.5 text-[10px] font-medium ${CONTENT_TYPE_BADGE[g.content_type]}`}>
                      {CONTENT_TYPE_LABELS[g.content_type]}
                    </span>
                  </td>
                  <td className="td text-right font-mono">{g.ppr_count.toLocaleString()}</td>
                  <td className="td text-right font-mono">{g.cosine_count.toLocaleString()}</td>
                  <td className="td text-right text-text-2">{ts(g.ppr_computed_at)}</td>
                  <td className="td text-right">
                    <div className="flex gap-1.5 justify-end">
                      <button
                        className="btn text-xs py-0.5 px-2"
                        disabled={recomputing === g.id}
                        onClick={() => handleRecompute(g.id)}
                      >
                        {recomputing === g.id ? 'Computing…' : 'Recompute'}
                      </button>
                      {g.id > 3 && (
                        <button
                          className="btn text-xs py-0.5 px-2 text-red-400 hover:text-red-300"
                          onClick={() => handleDelete(g.id)}
                        >
                          Delete
                        </button>
                      )}
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}
