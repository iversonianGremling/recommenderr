import { useEffect, useState } from 'react'
import { useParams, useNavigate } from 'react-router-dom'
import { getPersona, getPersonaScores } from '../lib/api'
import type { Persona, PersonaScore } from '../lib/types'
import { ItemTable, normalizeItem } from '../components/ItemTable'

export default function PersonaScores() {
  const { id } = useParams<{ id: string }>()
  const pid = Number(id)
  const navigate = useNavigate()

  const [persona, setPersona] = useState<Persona | null>(null)
  const [scores, setScores] = useState<PersonaScore[]>([])
  const [limit, setLimit] = useState(100)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    setLoading(true)
    Promise.all([getPersona(pid), getPersonaScores(pid, limit)])
      .then(([p, s]) => { setPersona(p); setScores(s) })
      .catch((e) => setError(String(e)))
      .finally(() => setLoading(false))
  }, [pid, limit])

  if (loading) return <div className="text-text-2 text-sm">Loading…</div>
  if (error) return <div className="text-red-500 text-sm">{error}</div>

  const items = scores.map((s) => normalizeItem(s as unknown as Record<string, unknown>))

  const headerToolbar = (
    <div className="flex items-center gap-2">
      <button className="btn text-xs py-0.5 px-2" onClick={() => navigate('/personas')}>
        ← Personas
      </button>
      <button className="btn text-xs py-0.5 px-2" onClick={() => navigate(`/personas/${pid}/edit`)}>
        Edit
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
    </div>
  )

  return (
    <div>
      <h1 className="page-title mb-4">{persona?.name ?? `Persona ${pid}`} — Scores</h1>
      <ItemTable
        items={items}
        defaultColumns={['thumbnail', 'title', 'score', 'spam_mass', 'duration', 'computed_at']}
        storageKey="persona-scores"
        toolbar={headerToolbar}
        emptyMessage="No scores yet — add seeds and run Recompute."
      />
    </div>
  )
}
