import { useEffect, useState } from 'react'
import { useParams, useNavigate } from 'react-router-dom'
import { getPersona, patchPersona, getPersonaSeeds, deletePersonaSeed, setPersonaSeeds, recomputePersona } from '../lib/api'
import type { Persona, PersonaSeed } from '../lib/types'

export default function PersonaEdit() {
  const { id } = useParams<{ id: string }>()
  const pid = Number(id)
  const navigate = useNavigate()

  const [persona, setPersona] = useState<Persona | null>(null)
  const [seeds, setSeeds] = useState<PersonaSeed[]>([])
  const [form, setForm] = useState({ name: '', description: '', alpha: '0.15', min_seed_rating: '0' })
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)
  const [recomputing, setRecomputing] = useState(false)
  const [lastResult, setLastResult] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [seedQuery, setSeedQuery] = useState('')
  const [seedResults, setSeedResults] = useState<Array<{ video_id: string; title?: string; author?: string }>>([])
  const [seedSearching, setSeedSearching] = useState(false)

  const load = async () => {
    setLoading(true)
    try {
      const [p, s] = await Promise.all([getPersona(pid), getPersonaSeeds(pid)])
      setPersona(p)
      setSeeds(s)
      setForm({
        name: p.name,
        description: p.description ?? '',
        alpha: String(p.alpha),
        min_seed_rating: String(p.min_seed_rating),
      })
    } catch (e) {
      setError(String(e))
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => { load() }, [pid])

  const handleSave = async () => {
    setSaving(true)
    setError(null)
    try {
      await patchPersona(pid, {
        name: form.name.trim(),
        description: form.description,
        alpha: parseFloat(form.alpha),
        min_seed_rating: parseInt(form.min_seed_rating),
      })
      setLastResult('Saved.')
    } catch (e) {
      setError(String(e))
    } finally {
      setSaving(false)
    }
  }

  const handleRecompute = async () => {
    setRecomputing(true)
    setError(null)
    setLastResult(null)
    try {
      const res = await recomputePersona(pid)
      setLastResult(`Recomputed in ${res.elapsed_seconds}s — ${res.scored} items scored.`)
      await load()
    } catch (e) {
      setError(String(e))
    } finally {
      setRecomputing(false)
    }
  }

  const handleRemoveSeed = async (itemId: number) => {
    await deletePersonaSeed(pid, itemId)
    setSeeds((s) => s.filter((x) => x.item_id !== itemId))
  }

  const handleSeedSearch = async () => {
    if (!seedQuery.trim()) return
    setSeedSearching(true)
    setSeedResults([])
    try {
      const res = await fetch(`/v1/ppr/track-search?q=${encodeURIComponent(seedQuery)}`).then((r) => r.json()) as Array<{ track: string; artist: string }>
      // Also search invidious for video seeds
      const inv = await fetch(`/v1/invidious/search?q=${encodeURIComponent(seedQuery)}&type=video`).then((r) => r.json()).catch(() => []) as Array<{ videoId: string; title: string; author: string }>
      const vidResults = Array.isArray(inv) ? inv.slice(0, 5).map((v) => ({ video_id: v.videoId, title: v.title, author: v.author })) : []
      setSeedResults(vidResults)
    } catch (e) {
      setError(String(e))
    } finally {
      setSeedSearching(false)
    }
  }

  const handleAddVideoSeed = async (videoId: string, weight = 1.0) => {
    if (seeds.some((s) => s.external_id === videoId)) return
    setError(null)
    try {
      await setPersonaSeeds(pid, [{ scheme: 'yt_video', external_id: videoId, weight }], true)
      await load()
    } catch (e) {
      setError(String(e))
    }
  }

  if (loading) return <div className="text-text-2 text-sm">Loading…</div>
  if (!persona) return <div className="text-red-500 text-sm">{error ?? 'Persona not found'}</div>

  return (
    <div className="max-w-2xl">
      <div className="flex items-center gap-3 mb-5">
        <button className="btn text-xs py-0.5 px-2" onClick={() => navigate('/personas')}>← Personas</button>
        <h1 className="page-title">{persona.name}</h1>
      </div>

      {error && <p className="text-red-500 text-sm mb-3">{error}</p>}

      {/* Settings */}
      <div className="rounded border border-border bg-bg-2 p-4 mb-4">
        <h2 className="text-xs font-semibold text-text-2 uppercase tracking-wide mb-3">Settings</h2>
        <div className="grid grid-cols-2 gap-3">
          <div className="col-span-2">
            <label className="label">Name</label>
            <input className="input w-full" value={form.name} onChange={(e) => setForm((f) => ({ ...f, name: e.target.value }))} />
          </div>
          <div className="col-span-2">
            <label className="label">Description</label>
            <input className="input w-full" value={form.description} onChange={(e) => setForm((f) => ({ ...f, description: e.target.value }))} placeholder="Optional" />
          </div>
          <div>
            <label className="label">Alpha (0.01–0.99)</label>
            <input className="input w-full" type="number" min="0.01" max="0.99" step="0.01" value={form.alpha} onChange={(e) => setForm((f) => ({ ...f, alpha: e.target.value }))} />
          </div>
          <div>
            <label className="label">Min seed rating (0 = all)</label>
            <input className="input w-full" type="number" min="0" max="10" step="1" value={form.min_seed_rating} onChange={(e) => setForm((f) => ({ ...f, min_seed_rating: e.target.value }))} />
          </div>
        </div>
        <div className="mt-3 flex gap-2 items-center">
          <button className="btn-primary" onClick={handleSave} disabled={saving}>{saving ? 'Saving…' : 'Save'}</button>
          <button className="btn-accent" onClick={handleRecompute} disabled={recomputing}>{recomputing ? 'Recomputing…' : 'Save & Recompute'}</button>
          <button className="btn text-xs py-0.5 px-2" onClick={() => navigate(`/personas/${pid}/scores`)}>View scores</button>
          {lastResult && <span className="text-xs text-text-2">{lastResult}</span>}
        </div>
      </div>

      {/* Seeds */}
      <div className="rounded border border-border bg-bg-2 p-4 mb-4">
        <h2 className="text-xs font-semibold text-text-2 uppercase tracking-wide mb-3">Seeds ({seeds.length})</h2>

        {seeds.length > 0 && (
          <div className="space-y-1.5 mb-4">
            {seeds.map((s) => (
              <div key={s.item_id} className="flex items-center gap-2 rounded border border-border px-2.5 py-1.5">
                <span className="font-mono text-[10px] text-text-2">{s.scheme}</span>
                <span className="flex-1 text-xs text-text truncate">{s.title ?? s.external_id}</span>
                {s.author && <span className="text-[10px] text-text-2 truncate max-w-24">{s.author}</span>}
                <span className="font-mono text-[10px] text-accent">×{s.weight}</span>
                <button className="text-red-400 hover:text-red-600 text-xs" onClick={() => handleRemoveSeed(s.item_id)}>✕</button>
              </div>
            ))}
          </div>
        )}

        {/* Search to add seeds */}
        <div className="flex gap-2">
          <input
            className="input flex-1 text-xs"
            placeholder="Search videos to add as seeds…"
            value={seedQuery}
            onChange={(e) => setSeedQuery(e.target.value)}
            onKeyDown={(e) => e.key === 'Enter' && handleSeedSearch()}
          />
          <button className="btn text-xs" onClick={handleSeedSearch} disabled={seedSearching}>
            {seedSearching ? '…' : 'Search'}
          </button>
        </div>
        {seedResults.length > 0 && (
          <div className="mt-2 space-y-1">
            {seedResults.map((r) => (
              <div key={r.video_id} className="flex items-center gap-2 rounded border border-border px-2.5 py-1.5">
                <span className="flex-1 text-xs text-text truncate">{r.title ?? r.video_id}</span>
                {r.author && <span className="text-[10px] text-text-2 truncate max-w-24">{r.author}</span>}
                <button
                  className="btn text-xs py-0.5 px-2"
                  disabled={seeds.some((s) => s.external_id === r.video_id)}
                  onClick={() => handleAddVideoSeed(r.video_id)}
                >
                  {seeds.some((s) => s.external_id === r.video_id) ? 'Added' : 'Add'}
                </button>
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  )
}
