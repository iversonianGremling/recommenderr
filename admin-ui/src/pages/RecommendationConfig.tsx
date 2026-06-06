import { useEffect, useState } from 'react'
import { useSearchParams, Link } from 'react-router-dom'
import { getPprConfig, putPprConfig, resetPprConfig, recomputePpr, getGraphStats } from '../lib/api'
import type { PprConfig, GraphStats } from '../lib/types'
import GraphSelector from '../components/GraphSelector'

const CONFIG_LABELS: Record<keyof Omit<PprConfig, '_defaults'>, { label: string; min: number; max: number; step: number; description: string }> = {
  watch_base: { label: 'Watch base', min: 0, max: 1, step: 0.001, description: 'Seed weight for passively watched videos' },
  playlist_base: { label: 'Playlist base', min: 0, max: 10, step: 0.1, description: 'Seed weight for playlist membership' },
  feed_rec_base: { label: 'Feed rec base', min: 0, max: 5, step: 0.1, description: 'Base for rated feed-only videos' },
  alpha: { label: 'Alpha (damping)', min: 0.01, max: 0.5, step: 0.01, description: 'Restart probability (higher = more personalized)' },
  min_seed_rating: { label: 'Min seed rating', min: 0, max: 10, step: 1, description: '0 = all signals; >0 = strict mode, only rated content' },
  compute_spam_mass: { label: 'Spam mass', min: 0, max: 1, step: 1, description: '1 = compute spam penalty, 0 = skip' },
}

// Friendly presets for the most-tuned knob: how much passive watch history
// influences recommendations. Maps to watch_base. Lower = ratings/playlists dominate.
const WATCH_INFLUENCE_PRESETS: { label: string; value: number; hint: string }[] = [
  { label: 'Off', value: 0, hint: 'Ratings only' },
  { label: 'Low', value: 0.003, hint: 'Mostly ratings' },
  { label: 'Normal', value: 0.01, hint: 'Default' },
  { label: 'High', value: 0.03, hint: 'Watch-led' },
]

export default function RecommendationConfig({ graphId: initGraphId }: { graphId?: number } = {}) {
  const [searchParams] = useSearchParams()
  const initialGraph = initGraphId || Number(searchParams.get('graph')) || 0
  const [graphId, setGraphId] = useState(initialGraph)
  const [config, setConfig] = useState<PprConfig | null>(null)
  const [stats, setStats] = useState<GraphStats | null>(null)
  const [dirty, setDirty] = useState<Partial<Omit<PprConfig, '_defaults'>>>({})
  const [saving, setSaving] = useState(false)
  const [recomputing, setRecomputing] = useState(false)
  const [lastResult, setLastResult] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    if (!graphId) return
    setConfig(null); setStats(null); setDirty({}); setLastResult(null)
    getPprConfig(graphId).then(setConfig).catch((e) => setError(String(e)))
    getGraphStats(graphId).then(setStats).catch(() => {})
  }, [graphId])

  const current = config ? { ...config, ...dirty } : null

  const handleChange = (key: keyof Omit<PprConfig, '_defaults'>, val: number) => {
    setDirty((d) => ({ ...d, [key]: val }))
  }

  const handleSave = async () => {
    if (!Object.keys(dirty).length) return
    setSaving(true)
    setError(null)
    try {
      await putPprConfig(dirty, graphId)
      const fresh = await getPprConfig(graphId)
      setConfig(fresh)
      setDirty({})
      setLastResult('Saved.')
    } catch (e) {
      setError(String(e))
    } finally {
      setSaving(false)
    }
  }

  const handleReset = async () => {
    setSaving(true)
    setError(null)
    try {
      await resetPprConfig(graphId)
      const fresh = await getPprConfig(graphId)
      setConfig(fresh)
      setDirty({})
      setLastResult('Reset to defaults.')
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
      if (Object.keys(dirty).length) await putPprConfig(dirty, graphId)
      const res = await recomputePpr({
        graph_id: graphId,
        min_seed_rating: current?.min_seed_rating ?? 0,
        compute_spam_mass: (current?.compute_spam_mass ?? 1) > 0,
      })
      const fresh = await getPprConfig(graphId)
      setConfig(fresh)
      setDirty({})
      setLastResult(`Recomputed in ${res.elapsed_seconds}s — ${res.items} items scored.`)
    } catch (e) {
      setError(String(e))
    } finally {
      setRecomputing(false)
    }
  }

  return (
    <div className="max-w-xl">
      <div className="flex items-center justify-between mb-1">
        <h1 className="page-title">PPR Engine <span className="text-text-2 font-normal">· per graph</span></h1>
        <GraphSelector value={graphId} onChange={setGraphId} />
      </div>
      <p className="mb-4 text-[11px] text-text-2">
        Each graph runs its own independent Personalized PageRank — these seed weights &amp;
        damping apply only to the selected graph. The <em>blend weight</em> (how much PPR
        counts vs cosine/serendipity) lives in{' '}
        <Link to="/pipeline/config" className="text-accent hover:underline">Pipeline Config</Link>.
      </p>

      {!graphId && <div className="text-text-2 text-sm">Select a graph…</div>}
      {graphId && !config && <div className="text-text-2 text-sm">{error ?? 'Loading…'}</div>}

      {graphId && config && (
        <>
          {stats && (
            <div className="mb-5 flex gap-4 text-xs text-text-2">
              <span>{stats.nodes.toLocaleString()} nodes</span>
              <span>{stats.edges.toLocaleString()} edges</span>
              <span>{stats.scored_nodes.toLocaleString()} scored</span>
              <span>density {stats.density.toExponential(2)}</span>
            </div>
          )}

          <div className="mb-5 rounded-lg border border-border bg-bg-2 p-4">
            <div className="flex items-center justify-between">
              <h2 className="text-sm font-semibold text-text">Watch-history influence</h2>
              <span className="font-mono text-[11px] text-text-2">watch_base {Number(current?.watch_base ?? 0).toFixed(3)}</span>
            </div>
            <p className="mb-3 mt-0.5 text-[11px] text-text-2">
              How much your <em>passively watched</em> videos steer recommendations. If recs feel
              scattered, lower this so your highly-rated videos and playlists drive the feed instead.
            </p>
            <div className="grid grid-cols-4 gap-2">
              {WATCH_INFLUENCE_PRESETS.map((p) => {
                const active = Math.abs((current?.watch_base ?? 0) - p.value) < 0.0005
                return (
                  <button
                    key={p.label}
                    type="button"
                    onClick={() => handleChange('watch_base', p.value)}
                    className={`rounded-md border px-2 py-2 text-center transition-colors ${active ? 'border-accent bg-accent/10 text-accent' : 'border-border bg-bg-3 text-text-2 hover:text-text'}`}
                  >
                    <span className="block text-[13px] font-medium">{p.label}</span>
                    <span className="mt-0.5 block text-[10px] leading-tight opacity-80">{p.hint}</span>
                  </button>
                )
              })}
            </div>
            <p className="mt-2 text-[11px] text-text-2">Pick a level, then <strong>Save &amp; Recompute</strong> below. Advanced seed weights are still available underneath.</p>
          </div>

          <div className="space-y-5">
            {(Object.keys(CONFIG_LABELS) as Array<keyof typeof CONFIG_LABELS>).map((key) => {
              const meta = CONFIG_LABELS[key]
              const val = current?.[key] ?? config._defaults[key] ?? 0
              const isChanged = key in dirty
              const defaultVal = config._defaults[key]
              return (
                <div key={key}>
                  <div className="flex items-center justify-between">
                    <label className="text-sm font-medium text-text">
                      {meta.label}
                      {isChanged && <span className="ml-1.5 text-[10px] text-accent">modified</span>}
                    </label>
                    <span className="font-mono text-sm text-text">{Number(val).toFixed(meta.step < 0.01 ? 4 : meta.step < 1 ? 2 : 0)}</span>
                  </div>
                  <p className="mb-1.5 text-[11px] text-text-2">{meta.description} — default: {defaultVal}</p>
                  <input
                    type="range"
                    min={meta.min}
                    max={meta.max}
                    step={meta.step}
                    value={val}
                    onChange={(e) => handleChange(key, parseFloat(e.target.value))}
                    className="w-full accent-accent"
                  />
                </div>
              )
            })}
          </div>

          <div className="mt-6 flex items-center gap-3">
            <button className="btn-primary" onClick={handleSave} disabled={saving || !Object.keys(dirty).length}>
              {saving ? 'Saving…' : 'Save'}
            </button>
            <button className="btn" onClick={handleReset} disabled={saving}>
              Reset defaults
            </button>
            <button className="btn-accent" onClick={handleRecompute} disabled={recomputing}>
              {recomputing ? 'Recomputing…' : 'Save & Recompute'}
            </button>
          </div>

          {lastResult && <p className="mt-3 text-sm text-text-2">{lastResult}</p>}
          {error && <p className="mt-3 text-sm text-red-500">{error}</p>}
        </>
      )}
    </div>
  )
}
