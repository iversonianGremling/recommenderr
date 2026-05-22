import { useEffect, useState } from 'react'
import { useParams, useNavigate } from 'react-router-dom'
import { getPersona, getPersonaScores } from '../lib/api'
import type { Persona, PersonaScore } from '../lib/types'

function Duration({ secs }: { secs: number | null }) {
  if (secs == null) return null
  const m = Math.floor(secs / 60)
  const s = secs % 60
  return <>{m}:{String(s).padStart(2, '0')}</>
}

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

  return (
    <div className="max-w-4xl">
      <div className="flex items-center gap-3 mb-4">
        <button className="btn text-xs py-0.5 px-2" onClick={() => navigate('/personas')}>← Personas</button>
        <h1 className="page-title">{persona?.name ?? `Persona ${pid}`} — Scores</h1>
        <div className="ml-auto flex items-center gap-2">
          <button className="btn text-xs py-0.5 px-2" onClick={() => navigate(`/personas/${pid}/edit`)}>Edit</button>
          <select className="input text-xs py-1" value={limit} onChange={(e) => setLimit(Number(e.target.value))}>
            {[50, 100, 200, 500].map((n) => <option key={n} value={n}>{n} rows</option>)}
          </select>
        </div>
      </div>

      {scores.length === 0 ? (
        <p className="text-text-2 text-sm">No scores yet — add seeds and run Recompute.</p>
      ) : (
        <div className="overflow-auto rounded border border-border">
          <table className="w-full text-xs">
            <thead className="bg-bg-2">
              <tr>
                <th className="th">Thumbnail</th>
                <th className="th text-left">Video</th>
                <th className="th text-right">Score</th>
                <th className="th text-right">Duration</th>
              </tr>
            </thead>
            <tbody>
              {scores.map((s) => (
                <tr key={s.video_id} className="tr">
                  <td className="td w-16">
                    {s.thumbnail && <img src={s.thumbnail} alt="" className="h-9 w-16 object-cover rounded" />}
                  </td>
                  <td className="td">
                    <p className="font-medium text-text truncate max-w-sm">{s.title ?? s.video_id}</p>
                    {s.author && <p className="text-text-2">{s.author}</p>}
                  </td>
                  <td className="td text-right font-mono">{s.score.toFixed(6)}</td>
                  <td className="td text-right font-mono text-text-2"><Duration secs={s.duration} /></td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}
