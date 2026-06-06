import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import {
  getPipelineStatus, getPipelineConfig,
  recomputePpr, recomputeCosine, recomputeSerendipity,
  recomputeModule, listModules, type CustomModule,
} from '../lib/api'

type PipelineStatus = {
  config: Record<string, number>
  user_signals?: { watch_history: number; rated_videos: number; rated_channels: number; playlist_items: number }
  sources: { total: number; enabled: number; circuit_open: number; names: string[] }
  graph: { nodes: number; edges: number }
  scorers: Array<{ id: string; name: string; description: string; scored: number; computed_at: number | null; enabled: boolean; weight: number }>
  filters: { feed_filter_count: number; weight_rule_count: number }
  feed: { items: number }
}

function fmt(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`
  if (n >= 1_000) return `${(n / 1_000).toFixed(1)}k`
  return String(n)
}

function age(ts: number | null): string {
  if (!ts) return 'never'
  const secs = Date.now() / 1000 - ts
  if (secs < 60) return `${Math.round(secs)}s ago`
  if (secs < 3600) return `${Math.round(secs / 60)}m ago`
  return `${Math.round(secs / 3600)}h ago`
}

function Arrow() {
  return <div className="flex items-center self-center text-text-2 text-lg px-0.5 select-none">→</div>
}

function LaneLabel({ children }: { children: React.ReactNode }) {
  return (
    <div className="text-[9px] uppercase tracking-widest text-text-2 font-semibold mb-1 px-0.5 opacity-60">
      {children}
    </div>
  )
}

function StageCard({
  label, to, children, status,
}: {
  label: string; to?: string; children: React.ReactNode; status?: 'ok' | 'warn' | 'error'
}) {
  const border = status === 'error' ? 'border-red-500'
    : status === 'warn' ? 'border-yellow-500' : 'border-border'
  const inner = (
    <div className={`rounded border ${border} bg-bg-2 p-3 min-w-[120px] flex flex-col gap-1`}>
      <div className="text-[10px] uppercase tracking-wider text-text-2 font-medium">{label}</div>
      {children}
      {to && (
        <div className="mt-1">
          <span className="text-[9px] text-accent">configure →</span>
        </div>
      )}
    </div>
  )
  if (to) {
    return <Link to={to} className="hover:opacity-80 transition-opacity">{inner}</Link>
  }
  return inner
}

function Stat({ label, value }: { label: string; value: string | number }) {
  return (
    <div className="flex items-baseline justify-between gap-2">
      <span className="text-[11px] text-text-2">{label}</span>
      <span className="font-mono text-xs text-text">{value}</span>
    </div>
  )
}

function ScorerCard({
  scorer, onRecompute, recomputing,
}: {
  scorer: PipelineStatus['scorers'][number]; onRecompute: () => void; recomputing: boolean
}) {
  return (
    <div className="rounded border border-border bg-bg-2 p-3 flex flex-col gap-1.5 min-w-[145px]">
      <div className="text-[10px] uppercase tracking-wider text-text-2 font-medium flex items-center gap-1">
        {scorer.name}
        {!scorer.enabled && <span className="text-[9px] border border-border rounded px-0.5 opacity-60">off</span>}
      </div>
      <div className="text-[10px] text-text-2 leading-tight">{scorer.description}</div>
      <Stat label="scored" value={fmt(scorer.scored)} />
      <Stat label="computed" value={age(scorer.computed_at)} />
      <button
        className="btn text-[10px] py-0.5 mt-0.5"
        onClick={onRecompute}
        disabled={recomputing}
      >
        {recomputing ? '…' : 'Recompute'}
      </button>
    </div>
  )
}

function CustomModuleCard({
  m, onRecompute, recomputing,
}: {
  m: CustomModule; onRecompute: () => void; recomputing: boolean
}) {
  return (
    <div className="rounded border border-dashed border-border bg-bg-2 p-3 flex flex-col gap-1.5 min-w-[145px]">
      <div className="flex items-center justify-between gap-1">
        <div className="text-[10px] uppercase tracking-wider text-text-2 font-medium">{m.name}</div>
        <Link to={`/modules/${m.id}`} className="text-[9px] text-accent hover:underline">edit</Link>
      </div>
      <div className="text-[10px] text-text-2">custom {m.type}</div>
      {!m.enabled && <span className="text-[9px] text-text-2 border border-border rounded px-1 self-start">off</span>}
      {m.type === 'scorer' && (
        <button className="btn text-[10px] py-0.5 mt-0.5" onClick={onRecompute} disabled={recomputing || !m.enabled}>
          {recomputing ? '…' : 'Recompute'}
        </button>
      )}
    </div>
  )
}

export default function PipelineDashboard() {
  const [data, setData] = useState<PipelineStatus | null>(null)
  const [cfg, setCfg] = useState<Record<string, number>>({})
  const [modules, setModules] = useState<CustomModule[]>([])
  const [error, setError] = useState<string | null>(null)
  const [recomputingPpr, setRecomputingPpr] = useState(false)
  const [recomputingCosine, setRecomputingCosine] = useState(false)
  const [recomputingSeren, setRecomputingSeren] = useState(false)
  const [recomputingModule, setRecomputingModule] = useState<number | null>(null)

  const load = () => {
    getPipelineStatus().then(setData).catch((e) => setError(String(e)))
    getPipelineConfig().then((c) => {
      const { _defaults: _, ...rest } = c as Record<string, number> & { _defaults: Record<string, number> }
      setCfg(rest)
    }).catch(() => {})
    listModules().then(setModules).catch(() => {})
  }

  useEffect(() => { load() }, [])

  const handlePprRecompute = async () => {
    setRecomputingPpr(true)
    try { await recomputePpr({}); load() } catch (e) { setError(String(e)) }
    finally { setRecomputingPpr(false) }
  }
  const handleCosineRecompute = async () => {
    setRecomputingCosine(true)
    try { await recomputeCosine({}); load() } catch (e) { setError(String(e)) }
    finally { setRecomputingCosine(false) }
  }
  const handleSerenRecompute = async () => {
    setRecomputingSeren(true)
    try { await recomputeSerendipity(); load() } catch (e) { setError(String(e)) }
    finally { setRecomputingSeren(false) }
  }
  const handleModuleRecompute = async (id: number) => {
    setRecomputingModule(id)
    try { await recomputeModule(id); load() } catch (e) { setError(String(e)) }
    finally { setRecomputingModule(null) }
  }

  const getRecomputeForScorer = (id: string) => {
    if (id === 'ppr') return handlePprRecompute
    if (id === 'cosine') return handleCosineRecompute
    if (id === 'serendipity') return handleSerenRecompute
    return async () => {}
  }
  const getRecomputingForScorer = (id: string) => {
    if (id === 'ppr') return recomputingPpr
    if (id === 'cosine') return recomputingCosine
    if (id === 'serendipity') return recomputingSeren
    return false
  }

  const srcStatus = data ? (data.sources.circuit_open > 0 ? 'warn' : 'ok') : undefined
  const customScorers = modules.filter((m) => m.type === 'scorer')
  const customFilters = modules.filter((m) => m.type === 'filter')
  const recencyDays = cfg['temporal.recency_halflife_days'] ?? 0

  return (
    <div>
      <div className="flex items-center justify-between mb-5">
        <h1 className="page-title">Pipeline Overview</h1>
        <button className="btn text-xs py-1 px-3" onClick={load}>Refresh</button>
      </div>

      {error && <p className="text-red-500 text-sm mb-3">{error}</p>}
      {!data && !error && <p className="text-text-2 text-sm">Loading…</p>}

      {data && (
        <div className="flex flex-col gap-6">

          {/* ── Lane 1: Ingestion ── */}
          <div>
            <LaneLabel>Ingestion</LaneLabel>
            <div className="flex items-start gap-1 overflow-x-auto pb-1">
              <StageCard label="User Signals">
                <Stat label="watched" value={fmt(data.user_signals?.watch_history ?? 0)} />
                <Stat label="rated videos" value={data.user_signals?.rated_videos ?? 0} />
                <Stat label="rated channels" value={data.user_signals?.rated_channels ?? 0} />
                <Stat label="playlist items" value={fmt(data.user_signals?.playlist_items ?? 0)} />
                <div className="mt-1">
                  <Link to="/scoring/scores" className="text-[9px] text-accent">→ seeds</Link>
                </div>
              </StageCard>

              <Arrow />

              <StageCard label="Temporal" to="/pipeline/config">
                <Stat label="recency half-life" value={recencyDays === 0 ? 'off' : `${recencyDays}d`} />
              </StageCard>

              <Arrow />

              <StageCard label="Sources" to="/ingestion/sources" status={srcStatus}>
                <Stat label="enabled" value={data.sources.enabled} />
                <Stat label="circuits open" value={data.sources.circuit_open} />
                <div className="flex flex-wrap gap-0.5 mt-1">
                  {data.sources.names.map((n) => (
                    <span key={n} className="text-[9px] bg-bg rounded px-1 py-0.5 text-text-2">{n}</span>
                  ))}
                </div>
              </StageCard>
            </div>
          </div>

          {/* ── Lane 2: Graph & Scoring ── */}
          <div>
            <LaneLabel>Graph &amp; Scoring</LaneLabel>
            <div className="flex items-start gap-1 overflow-x-auto pb-1">
              <StageCard label="Graph" to="/scoring/graph">
                <Stat label="nodes" value={fmt(data.graph.nodes)} />
                <Stat label="edges" value={fmt(data.graph.edges)} />
              </StageCard>

              <Arrow />

              <div className="flex flex-col gap-2">
                <div className="flex items-center gap-1 px-0.5">
                  <span className="text-[9px] uppercase tracking-wider text-text-2">Scorers</span>
                  <Link to="/modules" className="text-[9px] text-accent ml-auto">+ add</Link>
                </div>
                <div className="flex items-start gap-2">
                  {data.scorers.map((scorer) => (
                    <ScorerCard
                      key={scorer.id}
                      scorer={scorer}
                      onRecompute={getRecomputeForScorer(scorer.id)}
                      recomputing={getRecomputingForScorer(scorer.id)}
                    />
                  ))}
                  {customScorers.map((m) => (
                    <CustomModuleCard
                      key={m.id}
                      m={m}
                      onRecompute={() => handleModuleRecompute(m.id)}
                      recomputing={recomputingModule === m.id}
                    />
                  ))}
                </div>
              </div>
            </div>
          </div>

          {/* ── Lane 3: Output ── */}
          <div>
            <LaneLabel>Output</LaneLabel>
            <div className="flex items-start gap-1 overflow-x-auto pb-1">
              <StageCard label="Diversity" to="/pipeline/config">
                <Stat label="mode" value={cfg['diversity.enabled'] ? 'MMR' : 'quota'} />
                {cfg['diversity.enabled'] ? (
                  <Stat label="λ" value={(cfg['diversity.lambda'] ?? 0.7).toFixed(2)} />
                ) : null}
              </StageCard>

              <Arrow />

              <div className="flex flex-col gap-2">
                <div className="flex items-center gap-1 px-0.5">
                  <span className="text-[9px] uppercase tracking-wider text-text-2">Filters</span>
                  <Link to="/modules" className="text-[9px] text-accent ml-auto">+ add</Link>
                </div>
                <div className="flex items-start gap-2">
                  <StageCard label="Standard" to="/output/filters">
                    <Stat label="feed filters" value={data.filters.feed_filter_count} />
                    <Stat label="weight rules" value={data.filters.weight_rule_count} />
                  </StageCard>
                  {customFilters.map((m) => (
                    <CustomModuleCard
                      key={m.id}
                      m={m}
                      onRecompute={() => {}}
                      recomputing={false}
                    />
                  ))}
                </div>
              </div>

              <Arrow />

              <StageCard label="Feed" to="/app/feed">
                <Stat label="items" value={fmt(data.feed.items)} />
              </StageCard>
            </div>
          </div>

          {data.sources.circuit_open > 0 && (
            <div className="rounded border border-yellow-500/40 bg-yellow-500/5 p-3 text-xs text-yellow-400">
              {data.sources.circuit_open} source circuit{data.sources.circuit_open > 1 ? 's' : ''} open —{' '}
              <Link to="/ingestion/sources" className="underline">check Sources</Link>.
            </div>
          )}
        </div>
      )}
    </div>
  )
}
