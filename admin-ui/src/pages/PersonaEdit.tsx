import { useEffect, useState } from 'react'
import { useParams, useNavigate } from 'react-router-dom'
import { getPersona, patchPersona, getPersonaSeeds, deletePersonaSeed, setPersonaSeeds, recomputePersona } from '../lib/api'
import type { Persona, PersonaSeed } from '../lib/types'
import { ItemTable, normalizeItem } from '../components/ItemTable'
import type { NormalizedItem } from '../components/ItemTable'

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
  const [seedResults, setSeedResults] = useState<NormalizedItem[]>([])
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

  const handleRemoveSeed = async (item: NormalizedItem) => {
    if (item.item_id == null) return
    await deletePersonaSeed(pid, item.item_id)
    setSeeds((s) => s.filter((x) => x.item_id !== item.item_id))
  }

  const handleSeedSearch = async () => {
    if (!seedQuery.trim()) return
    setSeedSearching(true)
    setSeedResults([])
    try {
      const inv = await fetch(`/v1/invidious/search?q=${encodeURIComponent(seedQuery)}&type=video`)
        .then((r) => r.json())
        .catch(() => []) as Array<{ videoId: string; title: string; author: string; lengthSeconds?: number; videoThumbnails?: Array<{ url: string }> }>
      const results = Array.isArray(inv)
        ? inv.slice(0, 10).map((v) =>
            normalizeItem({
              video_id: v.videoId,
              title: v.title,
              author: v.author,
              duration: v.lengthSeconds ?? null,
              thumbnail: v.videoThumbnails?.[0]?.url ?? null,
            })
          )
        : []
      setSeedResults(results)
    } catch (e) {
      setError(String(e))
    } finally {
      setSeedSearching(false)
    }
  }

  const handleAddVideoSeed = async (item: NormalizedItem, weight = 1.0) => {
    if (seeds.some((s) => s.external_id === item.id)) return
    setError(null)
    try {
      await setPersonaSeeds(pid, [{ scheme: 'yt_video', external_id: item.id, weight }], true)
      await load()
    } catch (e) {
      setError(String(e))
    }
  }

  if (loading) return <div className="text-text-2 text-sm">Loading…</div>
  if (!persona) return <div className="text-red-500 text-sm">{error ?? 'Persona not found'}</div>

  const seedItems = seeds.map((s) =>
    normalizeItem({
      video_id: s.external_id,
      external_id: s.external_id,
      item_id: s.item_id,
      scheme: s.scheme,
      title: s.title,
      author: s.author,
      weight: s.weight,
    })
  )

  return (
    <div>
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
            <input
              className="input w-full"
              value={form.name}
              onChange={(e) => setForm((f) => ({ ...f, name: e.target.value }))}
            />
          </div>
          <div className="col-span-2">
            <label className="label">Description</label>
            <input
              className="input w-full"
              value={form.description}
              onChange={(e) => setForm((f) => ({ ...f, description: e.target.value }))}
              placeholder="Optional"
            />
          </div>
          <div>
            <label className="label">Alpha (0.01–0.99)</label>
            <input
              className="input w-full"
              type="number"
              min="0.01"
              max="0.99"
              step="0.01"
              value={form.alpha}
              onChange={(e) => setForm((f) => ({ ...f, alpha: e.target.value }))}
            />
          </div>
          <div>
            <label className="label">Min seed rating (0 = all)</label>
            <input
              className="input w-full"
              type="number"
              min="0"
              max="10"
              step="1"
              value={form.min_seed_rating}
              onChange={(e) => setForm((f) => ({ ...f, min_seed_rating: e.target.value }))}
            />
          </div>
        </div>
        <div className="mt-3 flex gap-2 items-center flex-wrap">
          <button className="btn-primary" onClick={handleSave} disabled={saving}>
            {saving ? 'Saving…' : 'Save'}
          </button>
          <button className="btn-accent" onClick={handleRecompute} disabled={recomputing}>
            {recomputing ? 'Recomputing…' : 'Save & Recompute'}
          </button>
          <button className="btn text-xs py-0.5 px-2" onClick={() => navigate(`/personas/${pid}/scores`)}>
            View scores
          </button>
          {lastResult && <span className="text-xs text-text-2">{lastResult}</span>}
        </div>
      </div>

      {/* Seeds */}
      <div className="rounded border border-border bg-bg-2 p-4 mb-4">
        <h2 className="text-xs font-semibold text-text-2 uppercase tracking-wide mb-3">
          Seeds ({seeds.length})
        </h2>

        <ItemTable
          items={seedItems}
          defaultColumns={['thumbnail', 'title', 'scheme', 'weight']}
          storageKey="persona-seeds"
          actions={(item) => (
            <button
              className="text-red-400 hover:text-red-500 text-xs"
              onClick={() => handleRemoveSeed(item)}
            >
              Remove
            </button>
          )}
          emptyMessage="No seeds yet — search below to add some."
        />

        {/* Search to add seeds */}
        <div className="mt-4 border-t border-border pt-4">
          <h3 className="text-xs font-semibold text-text-2 uppercase tracking-wide mb-2">Add seeds</h3>
          <div className="flex gap-2 mb-3">
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
            <ItemTable
              items={seedResults}
              defaultColumns={['thumbnail', 'title', 'duration']}
              storageKey="persona-seed-search"
              actions={(item) => {
                const already = seeds.some((s) => s.external_id === item.id)
                return (
                  <button
                    className={`btn text-xs py-0.5 px-2 ${already ? 'opacity-50 cursor-not-allowed' : ''}`}
                    disabled={already}
                    onClick={() => !already && handleAddVideoSeed(item)}
                  >
                    {already ? 'Added' : 'Add'}
                  </button>
                )
              }}
            />
          )}
        </div>
      </div>
    </div>
  )
}
