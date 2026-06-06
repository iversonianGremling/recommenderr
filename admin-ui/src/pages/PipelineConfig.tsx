import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { getPipelineConfig, putPipelineConfig } from '../lib/api'
import GraphSelector from '../components/GraphSelector'

type Cfg = Record<string, number>

function SliderField({
  label, value, min, max, step = 0.01, description, onChange, disabled,
}: {
  label: string; value: number; min: number; max: number; step?: number
  description?: string; onChange: (v: number) => void; disabled?: boolean
}) {
  return (
    <div className="flex flex-col gap-1">
      <div className="flex items-center justify-between">
        <span className="text-xs text-text">{label}</span>
        <span className="font-mono text-xs text-text w-12 text-right">
          {value % 1 === 0 ? value : value.toFixed(2)}
        </span>
      </div>
      <input
        type="range" min={min} max={max} step={step}
        value={value}
        disabled={disabled}
        onChange={(e) => onChange(parseFloat(e.target.value))}
        className="w-full accent-accent"
      />
      {description && <p className="text-[10px] text-text-2">{description}</p>}
    </div>
  )
}

function Toggle({ label, description, value, onChange }: {
  label: string; description?: string; value: boolean; onChange: (v: boolean) => void
}) {
  return (
    <div className="flex flex-col gap-0.5">
      <div className="flex items-center justify-between">
        <span className="text-xs text-text">{label}</span>
        <button
          onClick={() => onChange(!value)}
          className={`relative inline-flex h-4 w-8 items-center rounded-full transition-colors ${value ? 'bg-accent' : 'bg-border'}`}
        >
          <span className={`inline-block h-3 w-3 transform rounded-full bg-white transition-transform ${value ? 'translate-x-4' : 'translate-x-0.5'}`} />
        </button>
      </div>
      {description && <p className="text-[10px] text-text-2">{description}</p>}
    </div>
  )
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div className="rounded border border-border bg-bg-2 p-4 flex flex-col gap-4">
      <h2 className="text-xs font-semibold uppercase tracking-wider text-text-2">{title}</h2>
      {children}
    </div>
  )
}

export default function PipelineConfig() {
  const [graphId, setGraphId] = useState(0)
  const [cfg, setCfg] = useState<Cfg>({})
  const [dirty, setDirty] = useState<Cfg>({})
  const [saving, setSaving] = useState(false)
  const [saved, setSaved] = useState(false)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    if (!graphId) return
    setCfg({}); setDirty({})
    getPipelineConfig(graphId)
      .then((c) => {
        const { _defaults: _, ...rest } = c as Cfg & { _defaults: Cfg }
        setCfg(rest)
      })
      .catch((e) => setError(String(e)))
  }, [graphId])

  const get = (key: string, fallback = 0) =>
    key in dirty ? dirty[key] : (cfg[key] ?? fallback)

  const set = (key: string, v: number) => setDirty((p) => ({ ...p, [key]: v }))

  const handleSave = async () => {
    if (!Object.keys(dirty).length) return
    setSaving(true); setError(null); setSaved(false)
    try {
      await putPipelineConfig(dirty, graphId)
      setCfg((c) => ({ ...c, ...dirty }))
      setDirty({})
      setSaved(true)
      setTimeout(() => setSaved(false), 2000)
    } catch (e) {
      setError(String(e))
    } finally {
      setSaving(false)
    }
  }

  const hasDirty = Object.keys(dirty).length > 0

  return (
    <div className="max-w-xl flex flex-col gap-5">
      <div className="flex items-center justify-between">
        <h1 className="page-title">Pipeline Config</h1>
        <div className="flex items-center gap-3">
          <GraphSelector value={graphId} onChange={setGraphId} />
          {saved && <span className="text-xs text-green-400">Saved</span>}
          <button
            className="btn-primary text-xs py-1 px-4"
            onClick={handleSave}
            disabled={saving || !hasDirty || !graphId}
          >
            {saving ? 'Saving…' : 'Save'}
          </button>
        </div>
      </div>

      {error && <p className="text-red-500 text-sm">{error}</p>}

      <Section title="Temporal">
        <SliderField
          label={`Recency half-life${get('temporal.recency_halflife_days') === 0 ? ' (off)' : ' (days)'}`}
          value={get('temporal.recency_halflife_days')}
          min={0} max={180} step={1}
          description="Decay older watch history. 0 = uniform. Higher = older watches weighted less as seeds."
          onChange={(v) => set('temporal.recency_halflife_days', v)}
        />
      </Section>

      <Section title="Scorer Blend Weights">
        <p className="text-[10px] text-text-2 -mt-2">
          How much each scorer contributes to the final blended ranking. The PPR
          engine&apos;s own seed weights &amp; damping live in{' '}
          <Link to={`/scoring/ppr?graph=${graphId}`} className="text-accent hover:underline">PPR Engine</Link>.
        </p>
        <SliderField
          label="PPR blend weight"
          value={get('scorer.ppr.weight', 1)}
          min={0} max={3} step={0.05}
          description="PPR score contribution in blended ranking (not the engine's internal params)."
          onChange={(v) => set('scorer.ppr.weight', v)}
        />
        <Toggle
          label="Cosine scorer"
          description="Content-similarity scorer. Complements PPR with metadata overlap."
          value={!!get('scorer.cosine.enabled')}
          onChange={(v) => set('scorer.cosine.enabled', v ? 1 : 0)}
        />
        <SliderField
          label="Cosine weight"
          value={get('scorer.cosine.weight', 0.5)}
          min={0} max={3} step={0.05}
          disabled={!get('scorer.cosine.enabled')}
          onChange={(v) => set('scorer.cosine.weight', v)}
        />
        <Toggle
          label="Serendipity scorer"
          description="score = ppr × (1 − cosine_norm). Rewards multi-hop, low-similarity discoveries."
          value={!!get('scorer.serendipity.enabled')}
          onChange={(v) => set('scorer.serendipity.enabled', v ? 1 : 0)}
        />
        <SliderField
          label="Serendipity weight"
          value={get('scorer.serendipity.weight', 0.3)}
          min={0} max={3} step={0.05}
          disabled={!get('scorer.serendipity.enabled')}
          onChange={(v) => set('scorer.serendipity.weight', v)}
        />
      </Section>

      <Section title="Diversity">
        <Toggle
          label="MMR diversity"
          description="Maximal Marginal Relevance. Replaces per-channel quota with a global diversity penalty."
          value={!!get('diversity.enabled')}
          onChange={(v) => set('diversity.enabled', v ? 1 : 0)}
        />
        <SliderField
          label="Lambda (relevance ↔ diversity)"
          value={get('diversity.lambda', 0.7)}
          min={0} max={1} step={0.05}
          description="1 = pure relevance, 0 = pure diversity."
          disabled={!get('diversity.enabled')}
          onChange={(v) => set('diversity.lambda', v)}
        />
        <SliderField
          label="Max items per channel"
          value={get('diversity.max_per_channel', 3)}
          min={1} max={10} step={1}
          disabled={!get('diversity.enabled')}
          onChange={(v) => set('diversity.max_per_channel', v)}
        />
      </Section>
    </div>
  )
}
