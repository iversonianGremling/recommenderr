import { useEffect, useState } from 'react'
import { getPprConfig, putPprConfig, resetPprConfig, recomputePpr, getGraphStats } from '../lib/api'
import type { PprConfig, GraphStats } from '../lib/types'

const CONFIG_LABELS: Record<keyof Omit<PprConfig, '_defaults'>, { label: string; min: number; max: number; step: number; description: string }> = {
  watch_base: { label: 'Watch base', min: 0, max: 1, step: 0.001, description: 'Seed weight for passively watched videos' },
  playlist_base: { label: 'Playlist base', min: 0, max: 10, step: 0.1, description: 'Seed weight for playlist membership' },
  feed_rec_base: { label: 'Feed rec base', min: 0, max: 5, step: 0.1, description: 'Base for rated feed-only videos' },
  alpha: { label: 'Alpha (damping)', min: 0.01, max: 0.5, step: 0.01, description: 'Restart probability (higher = more personalized)' },
  min_seed_rating: { label: 'Min seed rating', min: 0, max: 10, step: 1, description: '0 = all signals; >0 = strict mode, only rated content' },
  compute_spam_mass: { label: 'Spam mass', min: 0, max: 1, step: 1, description: '1 = compute spam penalty, 0 = skip' },
}

export default function RecommendationConfig() {
  const [config, setConfig] = useState<PprConfig | null>(null)
  const [stats, setStats] = useState<GraphStats | null>(null)
  const [dirty, setDirty] = useState<Partial<Omit<PprConfig, '_defaults'>>>({})
  const [saving, setSaving] = useState(false)
  const [recomputing, setRecomputing] = useState(false)
  const [lastResult, setLastResult] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    getPprConfig().then(setConfig).catch((e) => setError(String(e)))
    getGraphStats().then(setStats).catch(() => {})
  }, [])

  const current = config ? { ...config, ...dirty } : null

  const handleChange = (key: keyof Omit<PprConfig, '_defaults'>, val: number) => {
    setDirty((d) => ({ ...d, [key]: val }))
  }

  const handleSave = async () => {
    if (!Object.keys(dirty).length) return
    setSaving(true)
    setError(null)
    try {
      await putPprConfig(dirty)
      const fresh = await getPprConfig()
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
      await resetPprConfig()
      const fresh = await getPprConfig()
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
      if (Object.keys(dirty).length) await putPprConfig(dirty)
      const res = await recomputePpr({
        min_seed_rating: current?.min_seed_rating ?? 0,
        compute_spam_mass: (current?.compute_spam_mass ?? 1) > 0,
      })
      const fresh = await getPprConfig()
      setConfig(fresh)
      setDirty({})
      setLastResult(`Recomputed in ${res.elapsed_seconds}s — ${res.items} items scored.`)
    } catch (e) {
      setError(String(e))
    } finally {
      setRecomputing(false)
    }
  }

  if (!config) return <div className="text-text-2 text-sm">{error ?? 'Loading…'}</div>

  return (
    <div className="max-w-xl">
      <h1 className="page-title mb-1">PPR Config</h1>
      {stats && (
        <div className="mb-5 flex gap-4 text-xs text-text-2">
          <span>{stats.nodes.toLocaleString()} nodes</span>
          <span>{stats.edges.toLocaleString()} edges</span>
          <span>{stats.scored_nodes.toLocaleString()} scored</span>
          <span>density {stats.density.toExponential(2)}</span>
        </div>
      )}

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
    </div>
  )
}
