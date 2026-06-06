import { useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { listPersonas, createPersona, deletePersona, recomputePersona, autoGeneratePersonas } from '../lib/api'
import type { Persona } from '../lib/types'

function statusDot(status: Persona['job_status']) {
  if (status === 'running') return <span className="inline-block h-2 w-2 rounded-full bg-yellow-400" title="running" />
  if (status === 'error')   return <span className="inline-block h-2 w-2 rounded-full bg-red-500" title="error" />
  if (status === 'done')    return <span className="inline-block h-2 w-2 rounded-full bg-green-500" title="done" />
  return <span className="inline-block h-2 w-2 rounded-full bg-border" title="pending" />
}

export default function PersonasList() {
  const [personas, setPersonas] = useState<Persona[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [creating, setCreating] = useState(false)
  const [autoGenerating, setAutoGenerating] = useState(false)
  const [autoResult, setAutoResult] = useState<string | null>(null)
  const [newName, setNewName] = useState('')
  const navigate = useNavigate()

  const load = () => {
    setLoading(true)
    listPersonas()
      .then(setPersonas)
      .catch((e) => setError(String(e)))
      .finally(() => setLoading(false))
  }

  useEffect(load, [])

  const handleCreate = async (e: React.FormEvent) => {
    e.preventDefault()
    if (!newName.trim()) return
    setCreating(true)
    try {
      const p = await createPersona({ name: newName.trim() })
      setNewName('')
      navigate(`/personas/${p.id}/edit`)
    } catch (e) {
      setError(String(e))
    } finally {
      setCreating(false)
    }
  }

  const handleDelete = async (id: number, name: string) => {
    if (!confirm(`Delete persona "${name}"? This removes all seeds and scores.`)) return
    await deletePersona(id)
    setPersonas((ps) => ps.filter((p) => p.id !== id))
  }

  const handleRunNow = async (id: number) => {
    try {
      setPersonas((ps) => ps.map((p) => p.id === id ? { ...p, job_status: 'running' } : p))
      const res = await recomputePersona(id)
      setPersonas((ps) => ps.map((p) => p.id === id ? { ...p, job_status: 'done', seed_count: p.seed_count } : p))
    } catch (e) {
      setError(String(e))
    }
    load()
  }

  const handleAutoGenerate = async () => {
    setAutoGenerating(true)
    setAutoResult(null)
    setError(null)
    try {
      const res = await autoGeneratePersonas({ top_keywords: 20, min_videos_per_keyword: 3 })
      setAutoResult(`Created ${res.total_created} personas: ${res.created.join(', ') || 'none'} · Skipped: ${res.skipped.length}`)
      load()
    } catch (e) {
      setError(String(e))
    } finally {
      setAutoGenerating(false)
    }
  }

  return (
    <div className="max-w-3xl">
      <div className="flex items-center justify-between mb-4">
        <h1 className="page-title">Personas</h1>
        <button
          className="btn text-xs py-1 px-3"
          onClick={handleAutoGenerate}
          disabled={autoGenerating}
          title="Generate personas from top video keywords"
        >
          {autoGenerating ? 'Generating…' : '⚡ Auto-generate from keywords'}
        </button>
      </div>
      <p className="text-xs text-text-2 mb-4">
        Personas are named seed bundles. Each runs its own PPR pass and produces a ranked list of recommendations tuned to that interest profile.
        Auto-generate creates one persona per top keyword using your watched/rated videos as seeds.
      </p>

      {autoResult && (
        <p className="text-xs text-green-400 mb-3 rounded bg-green-400/10 border border-green-400/20 px-3 py-2">{autoResult}</p>
      )}

      <form onSubmit={handleCreate} className="mb-6 flex gap-2">
        <input
          className="input flex-1"
          placeholder="New persona name…"
          value={newName}
          onChange={(e) => setNewName(e.target.value)}
        />
        <button className="btn-primary" type="submit" disabled={creating || !newName.trim()}>
          Create
        </button>
      </form>

      {error && <p className="text-red-500 text-sm mb-3">{error}</p>}
      {loading && <p className="text-text-2 text-sm">Loading…</p>}

      {!loading && personas.length === 0 && (
        <p className="text-text-2 text-sm">No personas yet. Create one above.</p>
      )}

      <div className="space-y-2">
        {personas.map((p) => (
          <div key={p.id} className="rounded border border-border bg-bg-2 p-3 flex items-center gap-3">
            <div className="flex-1 min-w-0">
              <div className="flex items-center gap-2">
                {statusDot(p.job_status)}
                <span className="font-medium text-sm text-text">{p.name}</span>
                <span className="text-xs text-text-2">{p.seed_count} seeds</span>
                {p.job_status === 'error' && p.last_error && (
                  <span className="text-xs text-red-400 truncate max-w-xs" title={p.last_error}>
                    {p.last_error}
                  </span>
                )}
              </div>
              {p.description && <p className="text-xs text-text-2 mt-0.5">{p.description}</p>}
              <p className="text-[10px] text-text-2 mt-0.5 opacity-60">
                α={p.alpha} · min_rating={p.min_seed_rating}
                {p.last_run_at && ` · last run ${new Date(p.last_run_at * 1000).toLocaleString()}`}
              </p>
            </div>
            <div className="flex items-center gap-2 flex-shrink-0">
              <button className="btn text-xs py-0.5 px-2" onClick={() => navigate(`/personas/${p.id}/scores`)}>
                Scores
              </button>
              <button className="btn text-xs py-0.5 px-2" onClick={() => handleRunNow(p.id)}>
                Run now
              </button>
              <button className="btn-primary text-xs py-0.5 px-2" onClick={() => navigate(`/personas/${p.id}/edit`)}>
                Edit
              </button>
              <button className="btn text-xs py-0.5 px-2 text-red-400 hover:text-red-600" onClick={() => handleDelete(p.id, p.name)}>
                Delete
              </button>
            </div>
          </div>
        ))}
      </div>
    </div>
  )
}
