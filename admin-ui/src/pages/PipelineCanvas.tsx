import { createContext, useCallback, useContext, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import { Link, useNavigate } from 'react-router-dom'
import {
  ReactFlow,
  Background,
  Controls,
  MiniMap,
  Handle,
  Position,
  useNodesState,
  useEdgesState,
  useViewport,
  type Node,
  type Edge,
  type NodeTypes,
  type ReactFlowInstance,
} from '@xyflow/react'
import '@xyflow/react/dist/style.css'

// Dark-theme overrides for React Flow built-in controls/minimap
const RF_DARK_CSS = `
  .react-flow__controls { box-shadow: none !important; border: 1px solid #2a2a2f !important; border-radius: 8px !important; overflow: hidden; }
  .react-flow__controls-button { background: #111113 !important; border-bottom: 1px solid #2a2a2f !important; fill: #8a8a92 !important; color: #8a8a92 !important; }
  .react-flow__controls-button:last-child { border-bottom: none !important; }
  .react-flow__controls-button:hover { background: #1a1a1e !important; fill: #ededef !important; }
  .react-flow__controls-button svg { fill: inherit !important; }
  .react-flow__minimap { background: #111113 !important; border: 1px solid #2a2a2f !important; border-radius: 8px !important; overflow: hidden; }
  .react-flow__minimap-svg { background: #111113 !important; }
  .react-flow__attribution { display: none !important; }
  .react-flow__node-output_stage { border: none !important; }
`
import {
  getPipelineStatus, listGraphs, listModules, listGraphSources, updateGraphSource,
  patchSource, resetCircuit, probeSource, putPipelineConfig,
  recomputePpr, recomputeCosine, recomputeSerendipity, recomputeModule, updateModule,
  syncSignalSource, updateSignalSource, createSignalSource, deleteSignalSource,
  getLibraryStatus, getCatalogConfig, putCatalogConfig, recomputeLibraryRecs,
  deleteGraph, createGraph, getPprFeed,
  listConsumers, createConsumer, updateConsumer, deleteConsumer,
  type Graph, type SignalSource, type CustomModule, type GraphSourceEntry,
  type LibraryStatus, type CatalogConfig, type PipelineConsumer,
} from '../lib/api'
import type { FeedItem } from '../lib/types'
import DiscoverySources from './DiscoverySources'
import IngestionConverters from './IngestionConverters'
import RecommendationGraph from './RecommendationGraph'
import Graphs from './Graphs'
import RecommendationConfig from './RecommendationConfig'
import RecommendationScores from './RecommendationScores'
import RecommendationCosine from './RecommendationCosine'
import RecommendationFilters from './RecommendationFilters'
import RecommendationWeightRules from './RecommendationWeightRules'
import PersonasList from './PersonasList'
import CustomModules from './CustomModules'
import CustomModuleEdit from './CustomModuleEdit'
import AppFeed from './AppFeed'
import AppLibraryRecs from './AppLibraryRecs'

// ---------------------------------------------------------------------------
// In-canvas modal — panels open config pages here instead of route-navigating
// away from the canvas, so the canvas stays the hub.
// ---------------------------------------------------------------------------
const ModalCtx = createContext<(content: React.ReactNode) => void>(() => {})
function useOpenModal() { return useContext(ModalCtx) }

function ModalLink({ label, render }: { label: string; render: () => React.ReactNode }) {
  const open = useOpenModal()
  return (
    <button type="button" onClick={() => open(render())} className="text-xs text-accent hover:underline text-left">
      {label}
    </button>
  )
}

function CanvasModalHost({ content, onClose }: { content: React.ReactNode; onClose: () => void }) {
  useEffect(() => {
    if (!content) return
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') onClose() }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [content, onClose])
  if (!content) return null
  return createPortal(
    <div className="fixed inset-0 z-[10000] flex items-start justify-center overflow-y-auto">
      <div className="fixed inset-0 bg-black/60 backdrop-blur-sm" onClick={onClose} />
      <div className="relative z-10 my-8 w-full max-w-5xl rounded-xl border border-border bg-bg p-6"
        style={{ boxShadow: '0 24px 64px rgba(0,0,0,0.6)' }}>
        <button onClick={onClose} className="absolute top-3 right-3 z-20 text-text-2 hover:text-text text-lg leading-none">✕</button>
        {content}
      </div>
    </div>,
    document.body,
  )
}

// Graph creation templates — used by the "+ Graph" button and the welcome state.
const GRAPH_TEMPLATES: { key: string; label: string; type: 'mixed' | 'music' | 'video'; desc: string; name: string }[] = [
  { key: 'music', label: 'Music graph', type: 'music', desc: 'Songs, albums & artists from music sources', name: 'music' },
  { key: 'video', label: 'Video graph', type: 'video', desc: 'YouTube / Invidious video recommendations', name: 'video' },
  { key: 'blank', label: 'Blank graph', type: 'mixed', desc: 'Start empty and wire sources yourself', name: 'graph' },
]

function CreateGraphForm({ onCreate }: { onCreate: (name: string, type: 'mixed' | 'music' | 'video') => Promise<void> }) {
  const [tplKey, setTplKey] = useState(GRAPH_TEMPLATES[0].key)
  const [name, setName] = useState(GRAPH_TEMPLATES[0].name)
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState<string | null>(null)
  const tpl = GRAPH_TEMPLATES.find((t) => t.key === tplKey) ?? GRAPH_TEMPLATES[0]
  const submit = async () => {
    if (!name.trim()) return
    setBusy(true); setErr(null)
    try { await onCreate(name.trim(), tpl.type) }
    catch (e) { setErr(String(e)); setBusy(false) }
  }
  return (
    <div className="flex flex-col gap-4 max-w-xl">
      <div>
        <h3 className="text-base font-semibold text-text">Create a graph</h3>
        <p className="text-xs text-text-2 mt-0.5">Each graph runs its own PPR engine over a filtered slice of the recommendation edges.</p>
      </div>
      <div className="grid grid-cols-3 gap-2">
        {GRAPH_TEMPLATES.map((t) => (
          <button key={t.key} type="button" onClick={() => { setTplKey(t.key); setName(t.name) }}
            className={`flex flex-col gap-1 rounded-lg border p-3 text-left transition-colors ${tplKey === t.key ? 'border-accent bg-accent/10' : 'border-border hover:border-text-2/40'}`}>
            <span className="text-xs font-semibold text-text">{t.label}</span>
            <span className="text-[10px] text-text-2 leading-snug">{t.desc}</span>
          </button>
        ))}
      </div>
      <label className="flex flex-col gap-1">
        <span className="text-[10px] text-text-2 uppercase tracking-wider">Name</span>
        <input className="input text-sm" value={name} autoFocus onChange={(e) => setName(e.target.value)}
          onKeyDown={(e) => { if (e.key === 'Enter') submit() }} />
      </label>
      {err && <div className="text-xs text-red-400">{err}</div>}
      <button className="btn-primary text-sm self-start" onClick={submit} disabled={busy || !name.trim()}>
        {busy ? 'Creating…' : `Create ${tpl.label.toLowerCase()}`}
      </button>
    </div>
  )
}

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

type PipelineStatus = Awaited<ReturnType<typeof getPipelineStatus>>

type PanelNode =
  | { type: 'signal_source'; data: SignalSource }
  | { type: 'add_signal_source' }
  | { type: 'content_source'; data: GraphSourceEntry }
  | { type: 'graph_build'; data: PipelineStatus; graph: Graph }
  | { type: 'scorer'; data: PipelineStatus['scorers'][number] }
  | { type: 'custom_scorer'; data: CustomModule }
  | { type: 'output_stage'; data: PipelineStatus }
  | { type: 'feed'; data: PipelineStatus }
  | { type: 'consumer'; name: string; url: string; endpoint: string; consumer?: PipelineConsumer }
  | { type: 'add_consumer'; graphId: number }
  | { type: 'library_source'; data: LibraryStatus }
  | { type: 'catalog_ppr'; data: LibraryStatus }

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function fmt(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`
  if (n >= 1_000) return `${(n / 1_000).toFixed(1)}k`
  return String(n)
}

function age(ts: number | null | undefined): string {
  if (!ts) return 'never'
  const secs = Date.now() / 1000 - ts
  if (secs < 60) return `${Math.round(secs)}s ago`
  if (secs < 3600) return `${Math.round(secs / 60)}m ago`
  if (secs < 86400) return `${Math.round(secs / 3600)}h ago`
  return `${Math.round(secs / 86400)}d ago`
}

const NODE_W = 210
const NODE_W_COMPACT = 150
// Set by buildNodesAndEdges each render so NodeShell can render a denser body.
let CANVAS_COMPACT = false

// ---------------------------------------------------------------------------
// Pipeline stage grammar — every node belongs to exactly one stage, and reads
// it via a coloured left bar + eyebrow label. Stages flow left-to-right:
//   Input → Graph → Score → Output → Feed → Consumer
// (Normalize stage is planned but has no node yet — see PIPELINE_RESTRUCTURE.md.)
// ---------------------------------------------------------------------------
type StageKey = 'input' | 'graph' | 'score' | 'output' | 'feed' | 'consumer'

const STAGE: Record<StageKey, { label: string; color: string }> = {
  input:    { label: 'Input',    color: '#6e8bff' },
  graph:    { label: 'Graph',    color: '#a78bfa' },
  score:    { label: 'Score',    color: '#fbbf24' },
  output:   { label: 'Output',   color: '#f472b6' },
  feed:     { label: 'Feed',     color: '#2dd4bf' },
  consumer: { label: 'Consumer', color: '#8a8a92' },
}

const LEGEND_ITEMS: { key: StageKey; hint: string }[] = [
  { key: 'input',    hint: 'external + user sources' },
  { key: 'graph',    hint: 'PPR graph build' },
  { key: 'score',    hint: 'ranking algorithms' },
  { key: 'output',   hint: 'diversity + filters' },
  { key: 'feed',     hint: 'ranked result' },
  { key: 'consumer', hint: 'downstream readers' },
]

function StageLegend() {
  return (
    <div className="absolute top-3 left-3 z-10 rounded-lg border border-border bg-bg-2/90 backdrop-blur px-3 py-2 select-none pointer-events-none">
      <div className="text-[9px] uppercase tracking-wider text-text-2/60 font-semibold mb-1.5">Pipeline stages</div>
      <div className="flex flex-col gap-1">
        {LEGEND_ITEMS.map(({ key, hint }) => (
          <div key={key} className="flex items-center gap-2">
            <span className="inline-block rounded-sm shrink-0" style={{ width: 8, height: 8, background: STAGE[key].color }} />
            <span className="text-[10px] text-text font-medium w-[68px]">{STAGE[key].label}</span>
            <span className="text-[9px] text-text-2/70">{hint}</span>
          </div>
        ))}
      </div>
    </div>
  )
}

// ---------------------------------------------------------------------------
// Custom Node components
// ---------------------------------------------------------------------------

// Fixed on-screen size of the read-only hover preview (independent of canvas
// zoom — it's portaled to screen space so it's always this readable).
const PREVIEW_W = 380
const PREVIEW_EST_H = 280   // generous estimate, only used to keep it on-screen
const PREVIEW_GAP = 16
const PREVIEW_SCALE = PREVIEW_W / NODE_W
const PREVIEW_CLOSE_DELAY_MS = 60    // grace period to cross the node→preview gap
// Above this canvas zoom the on-screen node is already large enough to read, so
// the hover preview is redundant and suppressed.
const PREVIEW_HIDE_ZOOM = 1.2

function NodeShell({
  title, subtitle, status, onClick, children, hasInput = true, hasOutput = true, dimmed = false, stage,
}: {
  title: string
  subtitle?: string
  stage?: StageKey
  status?: 'ok' | 'warn' | 'error' | 'off'
  onClick?: () => void
  children?: React.ReactNode
  hasInput?: boolean
  hasOutput?: boolean
  dimmed?: boolean
}) {
  const wrapRef = useRef<HTMLDivElement>(null)
  // null = unmounted; otherwise the screen-space top-left to render the preview at.
  // Hover is bridged across the node↔preview gap with a short close delay so the
  // preview can be moved into and interacted with. No fade — it appears/vanishes
  // instantly (sweeping across nodes used to leave trailing fading ghosts).
  const [preview, setPreview] = useState<{ left: number; top: number } | null>(null)
  const hideTimer = useRef<ReturnType<typeof setTimeout> | null>(null)
  const unmountTimer = useRef<ReturnType<typeof setTimeout> | null>(null)
  // Current canvas zoom — drives whether the hover preview is worth showing.
  const { zoom } = useViewport()

  const borderColor =
    status === 'error' ? 'border-red-500/70' :
    status === 'warn' ? 'border-yellow-500/60' :
    status === 'off' ? 'border-border opacity-60' :
    'border-border'

  // The card body, reused for both the in-place node and the hover preview.
  // `dense` collapses detail rows for compact mode (used by the node itself);
  // the hover preview always renders full detail regardless of compact.
  const st = stage ? STAGE[stage] : null
  const compact = CANVAS_COMPACT
  const renderCard = (dense: boolean) => (
    <div
      className={`relative overflow-hidden rounded border ${borderColor} bg-bg-2 ${dense ? 'p-2' : 'p-3'} flex flex-col gap-1.5`}
      style={{ width: dense ? NODE_W_COMPACT : NODE_W }}
    >
      {st && <div className="absolute left-0 top-0 bottom-0" style={{ width: 3, background: st.color }} />}
      {st && !dense && (
        <span className="text-[8px] font-semibold uppercase tracking-wider leading-none" style={{ color: st.color }}>
          {st.label}
        </span>
      )}
      <div className="flex items-center gap-1.5">
        {status && (
          <span className={`w-2 h-2 rounded-full flex-shrink-0 ${
            status === 'ok' ? 'bg-green-500' :
            status === 'warn' ? 'bg-yellow-400' :
            status === 'error' ? 'bg-red-500' : 'bg-text-2/30'
          }`} />
        )}
        <span className={`${dense ? 'text-[10px]' : 'text-[11px]'} font-semibold text-text leading-tight truncate`}>{title}</span>
        {subtitle && !dense && <span className="ml-auto text-[9px] text-text-2 shrink-0">{subtitle}</span>}
      </div>
      {!dense && children}
    </div>
  )
  const body = renderCard(compact)

  const clearTimers = () => {
    if (hideTimer.current) { clearTimeout(hideTimer.current); hideTimer.current = null }
    if (unmountTimer.current) { clearTimeout(unmountTimer.current); unmountTimer.current = null }
  }
  // Tidy up any pending timers if the node unmounts mid-hover.
  useEffect(() => clearTimers, [])

  // If the user zooms in past the readable threshold while a preview is open,
  // drop it — the node itself is now legible.
  useEffect(() => {
    if (zoom >= PREVIEW_HIDE_ZOOM) { clearTimers(); setPreview(null) }
  }, [zoom])

  // Decide which side / vertical offset keeps the fixed-size card on-screen,
  // mount it, then fade it in on the next frame.
  const showPreview = () => {
    // Redundant once the on-canvas node is already readable.
    if (zoom >= PREVIEW_HIDE_ZOOM) return
    clearTimers()
    const el = wrapRef.current
    if (!el) return
    const r = el.getBoundingClientRect()
    const vw = window.innerWidth
    const vh = window.innerHeight

    // Horizontal: prefer to the right of the node; flip left if it won't fit;
    // if neither fits, clamp into the viewport.
    let left = r.right + PREVIEW_GAP
    if (left + PREVIEW_W > vw - 8) {
      left = r.left - PREVIEW_GAP - PREVIEW_W
      if (left < 8) left = Math.max(8, Math.min(r.left, vw - PREVIEW_W - 8))
    }

    // Vertical: align with the node top, clamp so it stays fully visible.
    let top = r.top
    if (top + PREVIEW_EST_H > vh - 8) top = vh - PREVIEW_EST_H - 8
    if (top < 8) top = 8

    setPreview({ left, top })
  }

  // Keep the preview alive while the pointer is over the node or the preview.
  const keepPreview = () => { clearTimers() }

  // Unmount after a short grace period that lets the pointer cross the gap into
  // the preview.
  const scheduleHide = () => {
    clearTimers()
    hideTimer.current = setTimeout(() => setPreview(null), PREVIEW_CLOSE_DELAY_MS)
  }

  return (
    <div
      ref={wrapRef}
      className={`relative${dimmed ? ' opacity-40' : ''}`}
      onMouseEnter={showPreview}
      onMouseLeave={scheduleHide}
    >
      {hasInput && (
        <Handle
          type="target"
          position={Position.Left}
          style={{ background: '#6e8bff', border: '2px solid #1a1b1f', width: 8, height: 8 }}
        />
      )}
      {/* The node itself stays put; only its border reacts to hover. */}
      <div
        className="cursor-pointer rounded transition-colors hover:ring-1 hover:ring-accent/50"
        onClick={onClick}
      >
        {body}
      </div>
      {hasOutput && (
        <Handle
          type="source"
          position={Position.Right}
          style={{ background: '#6e8bff', border: '2px solid #1a1b1f', width: 8, height: 8 }}
        />
      )}
      {/* Fixed-size, zoom-independent preview, portaled to screen space. Clicking
          it opens the node's side panel (same as clicking the node); the weight
          slider inside stops propagation so it stays independently interactive. */}
      {preview && createPortal(
        <div
          className="pointer-events-auto cursor-pointer"
          onMouseEnter={keepPreview}
          onMouseLeave={scheduleHide}
          onClick={onClick}
          style={{
            position: 'fixed',
            left: preview.left,
            top: preview.top,
            width: NODE_W,
            transform: `scale(${PREVIEW_SCALE})`,
            transformOrigin: 'top left',
            zIndex: 9999,
            boxShadow: '0 10px 28px rgba(0,0,0,0.7)',
            borderRadius: 6,
          }}
        >
          {renderCard(false)}
        </div>,
        document.body,
      )}
    </div>
  )
}

function Row({ label, value }: { label: string; value: string | number }) {
  return (
    <div className="flex items-baseline justify-between gap-2">
      <span className="text-[10px] text-text-2">{label}</span>
      <span className="font-mono text-[10px] text-text">{value}</span>
    </div>
  )
}

// Which weighted thing a drag commits to.
type WeightTarget = { kind: 'source'; name: string } | { kind: 'scorer'; id: string }

// Weight → cable thickness + brightness. Weight is nominally 0–2 (1 = neutral).
// Disabled cables stay thin and grey regardless of weight.
function weightedStyle(weight: number, enabled: boolean, color = '#6e8bff') {
  if (!enabled) return { stroke: '#3a3b45', strokeWidth: 1.5, opacity: 0.55 }
  const t = Math.max(0, Math.min(2, weight)) / 2 // 0..1
  return { stroke: color, strokeWidth: 1 + t * 4, opacity: 0.32 + t * 0.68 }
}

// Small ×N caption rendered on a weighted edge.
function weightLabel(weight: number) {
  return {
    label: `×${weight.toFixed(2)}`,
    labelStyle: { fill: '#aab1c4', fontSize: 9, fontFamily: 'ui-monospace, monospace' },
    labelBgStyle: { fill: '#111113', fillOpacity: 0.85 },
    labelBgPadding: [3, 1] as [number, number],
    labelBgBorderRadius: 3,
  }
}

// Inline weight control rendered on weighted nodes. Drags the value live and
// commits on release (the cable brightness/thickness updates after rebuild).
// `nodrag` + stopPropagation keep React Flow from panning the node or the click
// from opening the side panel.
function WeightSlider({ value, onCommit, color = '#6e8bff' }: { value: number; onCommit: (v: number) => void; color?: string }) {
  const [v, setV] = useState(value)
  useEffect(() => { setV(value) }, [value])
  const commit = () => { if (Math.abs(v - value) > 1e-9) onCommit(v) }
  return (
    <div
      className="nodrag flex items-center gap-1.5 pt-0.5"
      onClick={(e) => e.stopPropagation()}
      onPointerDown={(e) => e.stopPropagation()}
    >
      <span className="text-[9px] text-text-2">w</span>
      <input
        type="range" min={0} max={2} step={0.01} value={Math.min(2, v)}
        onChange={(e) => setV(parseFloat(e.target.value))}
        onPointerUp={commit}
        onKeyUp={commit}
        className="nodrag h-1 flex-1 cursor-pointer"
        style={{ accentColor: color }}
      />
      {/* Editable readout — type any value to go arbitrarily low (or past the
          slider's 0–2 range); the slider just clamps its thumb to [0,2]. */}
      <input
        type="number" min={0} step="any" value={v}
        onChange={(e) => setV(e.target.value === '' ? 0 : parseFloat(e.target.value))}
        onBlur={commit}
        onKeyDown={(e) => { if (e.key === 'Enter') (e.target as HTMLInputElement).blur() }}
        className="nodrag w-12 bg-transparent text-[9px] font-mono tabular-nums text-right outline-none focus:bg-bg-3 rounded px-0.5"
        style={{ color }}
      />
    </div>
  )
}

function KindBadge({ kind }: { kind: string }) {
  const colors: Record<string, string> = {
    watch_history: 'bg-blue-500/20 text-blue-300',
    likes: 'bg-pink-500/20 text-pink-300',
    playlists: 'bg-purple-500/20 text-purple-300',
    custom: 'bg-orange-500/20 text-orange-300',
    api: 'bg-blue-500/20 text-blue-300',
    scraper: 'bg-orange-500/20 text-orange-300',
    extractor: 'bg-green-500/20 text-green-300',
    feed: 'bg-purple-500/20 text-purple-300',
  }
  return (
    <span className={`text-[9px] rounded px-1 py-0.5 font-medium ${colors[kind] ?? 'bg-bg text-text-2'}`}>
      {kind}
    </span>
  )
}

function ContentTypeBadge({ ct }: { ct: string }) {
  const colors: Record<string, string> = {
    mixed: 'bg-text-2/20 text-text-2',
    music: 'bg-green-500/20 text-green-300',
    video: 'bg-blue-500/20 text-blue-300',
  }
  return (
    <span className={`text-[9px] rounded px-1 py-0.5 font-medium ${colors[ct] ?? 'bg-bg text-text-2'}`}>
      {ct}
    </span>
  )
}

// Individual signal source node — pure source, no input handle
function SignalSourceNode({ data }: { data: { source: SignalSource; onSelect: () => void } }) {
  const s = data.source
  let host = s.endpoint_url
  try { host = new URL(s.endpoint_url).host } catch { /* keep raw */ }
  return (
    <NodeShell
      stage="input"
      title={s.name}
      status={!s.enabled ? 'off' : s.last_error ? 'error' : s.last_synced_at ? 'ok' : 'warn'}
      onClick={data.onSelect}
      hasInput={false}
    >
      <div className="flex items-center gap-1 flex-wrap">
        <KindBadge kind={s.kind} />
        {s.is_system && <span className="text-[9px] border border-border rounded px-0.5 text-text-2">sys</span>}
      </div>
      <div className="font-mono text-[9px] text-text-2 truncate" title={s.endpoint_url}>{host}</div>
      <Row label="synced" value={s.last_synced_at ? age(s.last_synced_at) : 'never'} />
      {s.last_count !== null && <Row label="records" value={fmt(s.last_count)} />}
    </NodeShell>
  )
}

// Content source node (one per enabled ingestion source)
function ContentSourceNode({ data }: { data: { source: GraphSourceEntry; onSelect: () => void; onWeight: (v: number) => void } }) {
  const s = data.source
  const w = s.weight_override ?? s.weight
  const status: 'ok' | 'warn' | 'error' | 'off' =
    s.circuit_open ? 'warn' :
    s.last_error ? 'error' :
    s.last_success_at ? 'ok' : 'warn'
  return (
    <NodeShell
      stage="input"
      title={s.display_name}
      status={status}
      onClick={data.onSelect}
      hasInput={false}
    >
      <KindBadge kind={s.kind} />
      {s.circuit_open && <div className="text-[9px] text-yellow-400">circuit open</div>}
      <Row label="last ok" value={age(s.last_success_at)} />
      <WeightSlider value={w} onCommit={data.onWeight} />
    </NodeShell>
  )
}

// Graph build node
function GraphBuildNode({ data }: { data: { status: PipelineStatus; graph: Graph; onSelect: () => void } }) {
  const g = data.status.graph
  return (
    <NodeShell
      stage="graph"
      title={data.graph.name}
      status={g.edges > 0 ? 'ok' : 'warn'}
      onClick={data.onSelect}
    >
      <div className="flex items-center gap-1">
        <ContentTypeBadge ct={data.graph.content_type} />
        <span className="text-[9px] text-text-2">graph</span>
      </div>
      <Row label="nodes" value={fmt(g.nodes)} />
      <Row label="edges" value={fmt(g.edges)} />
    </NodeShell>
  )
}

// Scorer node (PPR / Cosine / Serendipity)
function ScorerNode({ data }: { data: { scorer: PipelineStatus['scorers'][number]; onSelect: () => void; onWeight: (v: number) => void } }) {
  const s = data.scorer
  return (
    <NodeShell
      stage="score"
      title={s.name}
      subtitle={s.enabled ? undefined : 'off'}
      status={!s.enabled ? 'off' : s.scored > 0 ? 'ok' : 'warn'}
      onClick={data.onSelect}
    >
      <div className="text-[9px] text-text-2">{s.description}</div>
      <Row label="scored" value={fmt(s.scored)} />
      <Row label="computed" value={age(s.computed_at)} />
      <WeightSlider value={s.weight} onCommit={data.onWeight} />
    </NodeShell>
  )
}

// Custom scorer/filter module node
function CustomScorerNode({ data }: { data: { module: CustomModule; onSelect: () => void } }) {
  const m = data.module
  return (
    <NodeShell
      stage="score"
      title={m.name}
      subtitle={m.type}
      status={m.enabled ? 'ok' : 'off'}
      onClick={data.onSelect}
    >
      <div className="text-[9px] text-text-2 italic">custom {m.type}</div>
      {!m.enabled && <span className="text-[9px] text-text-2">disabled</span>}
    </NodeShell>
  )
}

// Output / diversity + filters node
function OutputNode({ data }: { data: { status: PipelineStatus; config: Record<string, number>; onSelect: () => void } }) {
  const f = data.status.filters
  const diversityEnabled = data.config['diversity.enabled']
  return (
    <NodeShell stage="output" title="Output Stage" status="ok" onClick={data.onSelect}>
      <Row label="diversity" value={diversityEnabled ? 'MMR' : 'quota'} />
      <Row label="feed filters" value={f.feed_filter_count} />
      <Row label="weight rules" value={f.weight_rule_count} />
    </NodeShell>
  )
}

// Feed output node — has input, output handle leads to consumers
function FeedOutputNode({ data }: { data: { status: PipelineStatus; onSelect: () => void } }) {
  const items = data.status.feed.items
  return (
    <NodeShell stage="feed" title="Feed Cache" status={items > 0 ? 'ok' : 'warn'} onClick={data.onSelect}>
      <div className="text-xl font-mono font-bold text-text mt-0.5">{fmt(items)}</div>
      <div className="text-[9px] text-text-2">items ready</div>
      <div className="text-[8px] text-text-2/60 mt-0.5">feed_recommendations</div>
    </NodeShell>
  )
}

// Consumer node — shows what external service reads the feed
function ConsumerNode({ data }: { data: { name: string; url: string; endpoint: string; custom?: boolean; onSelect: () => void } }) {
  let host = data.url
  try { host = new URL(data.url).host } catch { /* keep raw */ }
  return (
    <NodeShell
      stage="consumer"
      title={data.name}
      subtitle={data.custom ? 'custom' : 'consumer'}
      status="ok"
      onClick={data.onSelect}
      hasOutput={false}
    >
      <div className="font-mono text-[9px] text-text-2 truncate" title={data.url}>{host || data.url}</div>
      <div className="text-[8px] text-accent/80 font-mono truncate">{data.endpoint}</div>
    </NodeShell>
  )
}

// Library seeds source (yamtrack) — pure source, no input handle
function LibrarySourceNode({ data }: { data: { status: LibraryStatus; onSelect: () => void } }) {
  const s = data.status.seeds
  const sources = Object.keys(s.by_source)
  return (
    <NodeShell
      stage="input"
      title={sources.length ? sources.join(', ') : 'Library seeds'}
      status={s.total > 0 ? 'ok' : 'warn'}
      onClick={data.onSelect}
      hasInput={false}
    >
      <div className="flex items-center gap-1 flex-wrap">
        <span className="text-[9px] rounded px-1 py-0.5 font-medium bg-pink-500/20 text-pink-300">seeds</span>
      </div>
      <Row label="seeds" value={fmt(s.total)} />
      {Object.entries(s.by_kind).map(([k, n]) => <Row key={k} label={k} value={fmt(n)} />)}
      <Row label="synced" value={age(s.last_seed_at)} />
    </NodeShell>
  )
}

// Catalog PPR node (yamtrack-fed library recommender — its own engine)
function CatalogPprNode({ data }: { data: { status: LibraryStatus; onSelect: () => void } }) {
  const e = data.status.engine
  const r = data.status.results
  return (
    <NodeShell
      stage="graph"
      title="Catalog PPR"
      subtitle="library"
      status={e.running ? 'warn' : r.total > 0 ? 'ok' : 'warn'}
      onClick={data.onSelect}
    >
      <div className="text-[9px] text-text-2 italic">independent catalog engine</div>
      <Row label="recs" value={fmt(r.total)} />
      <Row label="computed" value={age(r.computed_at)} />
      {e.running && <div className="text-[9px] text-yellow-400">recomputing…</div>}
    </NodeShell>
  )
}

const NODE_TYPES: NodeTypes = {
  signal_source: SignalSourceNode,
  content_source: ContentSourceNode,
  graph_build: GraphBuildNode,
  scorer: ScorerNode,
  custom_scorer: CustomScorerNode,
  output_stage: OutputNode,
  feed_output: FeedOutputNode,
  consumer: ConsumerNode,
  library_source: LibrarySourceNode,
  catalog_ppr: CatalogPprNode,
}

// ---------------------------------------------------------------------------
// Slide-in panel contents
// ---------------------------------------------------------------------------

function PanelSignalSource({
  source, onReload, onDelete,
}: {
  source: SignalSource
  onReload: () => void
  onDelete: () => void
}) {
  const [mode, setMode] = useState<'view' | 'edit'>('view')
  const [syncing, setSyncing] = useState(false)
  const [syncResult, setSyncResult] = useState<string | null>(null)
  const [saving, setSaving] = useState(false)
  const [confirmDelete, setConfirmDelete] = useState(false)

  const [name, setName] = useState(source.name)
  const [kind, setKind] = useState(source.kind)
  const [url, setUrl] = useState(source.endpoint_url)
  const [converter, setConverter] = useState(source.converter)
  const [authHeader, setAuthHeader] = useState(source.auth_header ?? '')
  const [enabled, setEnabled] = useState(source.enabled)

  useEffect(() => {
    setName(source.name); setKind(source.kind); setUrl(source.endpoint_url)
    setConverter(source.converter); setAuthHeader(source.auth_header ?? '')
    setEnabled(source.enabled); setMode('view'); setConfirmDelete(false)
  }, [source.id])

  const handleSync = async () => {
    setSyncing(true); setSyncResult(null)
    try {
      const r = await syncSignalSource(source.id)
      setSyncResult(r.ok ? `Synced ${r.count ?? 0} records` : `Error: ${r.error}`)
      onReload()
    } catch (e) { setSyncResult(String(e)) }
    finally { setSyncing(false) }
  }

  const handleSave = async () => {
    setSaving(true)
    try {
      await updateSignalSource(source.id, { name, kind, endpoint_url: url, converter, auth_header: authHeader || null, enabled })
      onReload(); setMode('view')
    } catch (e) { /* ignore */ }
    finally { setSaving(false) }
  }

  const handleDelete = async () => {
    if (!confirmDelete) { setConfirmDelete(true); return }
    try { await deleteSignalSource(source.id); onDelete() } catch (e) { /* ignore */ }
  }

  if (mode === 'edit') {
    return (
      <div className="flex flex-col gap-3">
        <div className="flex items-center justify-between">
          <h3 className="text-sm font-semibold text-text">Edit source</h3>
          <button className="text-[10px] text-text-2 hover:text-text" onClick={() => setMode('view')}>cancel</button>
        </div>
        <label className="flex flex-col gap-0.5">
          <span className="text-[10px] text-text-2">Name</span>
          <input className="input text-xs" value={name} onChange={(e) => setName(e.target.value)} />
        </label>
        <label className="flex flex-col gap-0.5">
          <span className="text-[10px] text-text-2">Kind</span>
          <select className="input text-xs" value={kind} onChange={(e) => setKind(e.target.value as SignalSource['kind'])}>
            <option value="watch_history">watch_history</option>
            <option value="likes">likes</option>
            <option value="playlists">playlists</option>
            <option value="custom">custom</option>
          </select>
        </label>
        <label className="flex flex-col gap-0.5">
          <span className="text-[10px] text-text-2">Endpoint URL</span>
          <input className="input text-xs font-mono" value={url} onChange={(e) => setUrl(e.target.value)} />
        </label>
        <label className="flex flex-col gap-0.5">
          <span className="text-[10px] text-text-2">Converter</span>
          <select className="input text-xs" value={converter} onChange={(e) => setConverter(e.target.value as SignalSource['converter'])}>
            <option value="ytfront_v1">ytfront_v1</option>
            <option value="ytfront_likes_v1">ytfront_likes_v1</option>
            <option value="native">native</option>
          </select>
        </label>
        <label className="flex flex-col gap-0.5">
          <span className="text-[10px] text-text-2">Auth header</span>
          <input className="input text-xs font-mono" value={authHeader} onChange={(e) => setAuthHeader(e.target.value)} placeholder="Bearer token…" />
        </label>
        <label className="flex items-center gap-2 cursor-pointer">
          <input type="checkbox" checked={enabled} onChange={(e) => setEnabled(e.target.checked)} />
          <span className="text-xs text-text">Enabled</span>
        </label>
        <button className="btn text-xs py-1" onClick={handleSave} disabled={saving}>
          {saving ? 'Saving…' : 'Save'}
        </button>
        {!source.is_system && (
          <button
            className={`text-xs py-1 px-2 rounded border transition-colors ${confirmDelete ? 'border-red-500/60 text-red-400' : 'border-border text-text-2 hover:border-red-500/60 hover:text-red-400'}`}
            onClick={handleDelete}
          >
            {confirmDelete ? 'Confirm delete' : 'Delete source'}
          </button>
        )}
      </div>
    )
  }

  return (
    <div className="flex flex-col gap-3">
      <div className="flex items-center justify-between">
        <h3 className="text-sm font-semibold text-text">{source.name}</h3>
        <button className="text-[10px] text-accent hover:underline" onClick={() => setMode('edit')}>Edit</button>
      </div>
      <div className="flex gap-1 flex-wrap">
        <KindBadge kind={source.kind} />
        {source.is_system && <span className="text-[9px] border border-border rounded px-1 py-0.5 text-text-2">system</span>}
        {!source.enabled && <span className="text-[9px] border border-yellow-500/40 rounded px-1 py-0.5 text-yellow-400">disabled</span>}
      </div>
      <div className="flex flex-col gap-1 text-xs">
        <div className="text-text-2">Endpoint</div>
        <div className="font-mono text-[10px] bg-bg rounded px-2 py-1 break-all">{source.endpoint_url}</div>
      </div>
      <div className="flex flex-col gap-1 text-xs">
        <Row label="Converter" value={source.converter} />
        <Row label="Last synced" value={age(source.last_synced_at)} />
        {source.last_count !== null && <Row label="Records" value={fmt(source.last_count)} />}
      </div>
      {source.last_error && (
        <div className="rounded border border-red-500/30 bg-red-500/10 p-2 text-[10px] text-red-400 break-all">{source.last_error}</div>
      )}
      <button className="btn text-xs py-1" onClick={handleSync} disabled={syncing || !source.enabled}>
        {syncing ? 'Syncing…' : 'Sync now'}
      </button>
      {syncResult && <div className="text-[10px] text-text-2">{syncResult}</div>}
    </div>
  )
}

function PanelAddSignalSource({ onDone }: { onDone: () => void }) {
  const [name, setName] = useState('')
  const [kind, setKind] = useState<SignalSource['kind']>('watch_history')
  const [url, setUrl] = useState('')
  const [converter, setConverter] = useState<SignalSource['converter']>('ytfront_v1')
  const [authHeader, setAuthHeader] = useState('')
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault(); setSaving(true); setError(null)
    try {
      await createSignalSource({ name, kind, endpoint_url: url, converter, auth_header: authHeader || null, enabled: true })
      onDone()
    } catch (e) { setError(String(e)) }
    finally { setSaving(false) }
  }

  return (
    <form onSubmit={handleSubmit} className="flex flex-col gap-3">
      <h3 className="text-sm font-semibold text-text">Add Signal Source</h3>
      <p className="text-xs text-text-2">Connect an external API that provides user interaction data.</p>
      <label className="flex flex-col gap-0.5">
        <span className="text-[10px] text-text-2">Name</span>
        <input className="input text-xs" value={name} onChange={(e) => setName(e.target.value)} required />
      </label>
      <label className="flex flex-col gap-0.5">
        <span className="text-[10px] text-text-2">Kind</span>
        <select className="input text-xs" value={kind} onChange={(e) => setKind(e.target.value as SignalSource['kind'])}>
          <option value="watch_history">watch_history</option>
          <option value="likes">likes</option>
          <option value="playlists">playlists</option>
          <option value="custom">custom</option>
        </select>
      </label>
      <label className="flex flex-col gap-0.5">
        <span className="text-[10px] text-text-2">Endpoint URL</span>
        <input className="input text-xs font-mono" value={url} onChange={(e) => setUrl(e.target.value)} required placeholder="http://…" />
      </label>
      <label className="flex flex-col gap-0.5">
        <span className="text-[10px] text-text-2">Converter</span>
        <select className="input text-xs" value={converter} onChange={(e) => setConverter(e.target.value as SignalSource['converter'])}>
          <option value="ytfront_v1">ytfront_v1 — ytvideo watch history format</option>
          <option value="ytfront_likes_v1">ytfront_likes_v1 — ytvideo likes format</option>
          <option value="native">native — recommenderr native format</option>
        </select>
      </label>
      <label className="flex flex-col gap-0.5">
        <span className="text-[10px] text-text-2">Auth header (optional)</span>
        <input className="input text-xs font-mono" value={authHeader} onChange={(e) => setAuthHeader(e.target.value)} placeholder="Bearer token…" />
      </label>
      {error && <div className="text-[10px] text-red-400">{error}</div>}
      <button type="submit" className="btn text-xs py-1" disabled={saving}>
        {saving ? 'Adding…' : 'Add source'}
      </button>
    </form>
  )
}

function PanelContentSource({ source, graphId, onReload }: { source: GraphSourceEntry; graphId: number; onReload: () => void }) {
  const [weight, setWeight] = useState(String(source.weight))
  const [rateLimit, setRateLimit] = useState(String(source.rate_limit_per_min ?? ''))
  const [weightOverride, setWeightOverride] = useState(source.weight_override !== null ? String(source.weight_override) : '')
  const [inGraph, setInGraph] = useState(source.in_graph)
  const [saving, setSaving] = useState(false)
  const [probing, setProbing] = useState(false)
  const [probeResult, setProbeResult] = useState<string | null>(null)
  const [resetting, setResetting] = useState(false)

  useEffect(() => {
    setWeight(String(source.weight))
    setRateLimit(String(source.rate_limit_per_min ?? ''))
    setWeightOverride(source.weight_override !== null ? String(source.weight_override) : '')
    setInGraph(source.in_graph)
  }, [source.name, source.weight, source.rate_limit_per_min, source.weight_override, source.in_graph])

  const handleGlobalToggle = async () => {
    setSaving(true)
    try { await patchSource(source.name, { enabled: !source.enabled }); onReload() }
    catch (e) { /* ignore */ } finally { setSaving(false) }
  }

  const handleGlobalSave = async () => {
    setSaving(true)
    try {
      const w = parseFloat(weight)
      const rl = rateLimit !== '' ? parseInt(rateLimit) : null
      await patchSource(source.name, {
        ...(isNaN(w) ? {} : { weight: w }),
        ...(rl !== null && !isNaN(rl) ? { rate_limit_per_min: rl } : {}),
      })
      onReload()
    } catch (e) { /* ignore */ } finally { setSaving(false) }
  }

  const handleGraphSave = async () => {
    setSaving(true)
    try {
      const wo = weightOverride !== '' ? parseFloat(weightOverride) : null
      await updateGraphSource(graphId, source.name, {
        in_graph: inGraph,
        weight_override: wo !== null && !isNaN(wo) ? wo : null,
      })
      onReload()
    } catch (e) { /* ignore */ } finally { setSaving(false) }
  }

  const handleProbe = async () => {
    setProbing(true); setProbeResult(null)
    try {
      const r = await probeSource(source.name)
      setProbeResult(r.ok ? `OK — ${r.detail}` : `Failed — ${r.detail}`)
    } catch (e) { setProbeResult(String(e)) }
    finally { setProbing(false) }
  }

  const handleResetCircuit = async () => {
    setResetting(true)
    try { await resetCircuit(source.name); onReload() }
    catch (e) { /* ignore */ } finally { setResetting(false) }
  }

  return (
    <div className="flex flex-col gap-3">
      <div className="flex items-center gap-2">
        <h3 className="text-sm font-semibold text-text">{source.display_name}</h3>
        <KindBadge kind={source.kind} />
      </div>
      {source.circuit_open && (
        <div className="rounded border border-yellow-500/40 bg-yellow-500/10 p-2 text-xs text-yellow-400 flex items-center justify-between">
          Circuit open — temporarily disabled
          <button className="underline text-[10px]" onClick={handleResetCircuit} disabled={resetting}>
            {resetting ? '…' : 'Reset'}
          </button>
        </div>
      )}
      {source.last_error && (
        <div className="rounded border border-red-500/30 bg-red-500/10 p-2 text-[10px] text-red-400 break-all">{source.last_error}</div>
      )}
      <div className="flex flex-col gap-1 text-xs">
        <Row label="Last success" value={age(source.last_success_at)} />
        <Row label="Failure streak" value={source.failure_streak} />
      </div>

      <hr className="border-border" />
      <div className="text-[10px] text-text-2 uppercase tracking-wider font-semibold">This Graph</div>
      <label className="flex items-center gap-2 cursor-pointer">
        <input type="checkbox" checked={inGraph} onChange={(e) => setInGraph(e.target.checked)} />
        <span className="text-xs text-text">Feed data into this graph</span>
      </label>
      {inGraph && (
        <label className="flex flex-col gap-0.5">
          <span className="text-[10px] text-text-2">Weight override (blank = use global)</span>
          <input className="input text-xs" type="number" step="any" min="0" value={weightOverride}
            onChange={(e) => setWeightOverride(e.target.value)} placeholder={String(source.weight)} />
        </label>
      )}
      <button className="btn text-xs py-1" onClick={handleGraphSave} disabled={saving}>
        {saving ? 'Saving…' : 'Save graph settings'}
      </button>

      <hr className="border-border" />
      <div className="text-[10px] text-text-2 uppercase tracking-wider font-semibold">Global</div>
      <label className="flex flex-col gap-0.5">
        <span className="text-[10px] text-text-2">Weight</span>
        <input className="input text-xs" type="number" step="any" min="0" value={weight} onChange={(e) => setWeight(e.target.value)} />
      </label>
      <label className="flex flex-col gap-0.5">
        <span className="text-[10px] text-text-2">Rate limit (req/min, blank = unlimited)</span>
        <input className="input text-xs" type="number" min="1" value={rateLimit} onChange={(e) => setRateLimit(e.target.value)} placeholder="unlimited" />
      </label>
      <div className="flex gap-2">
        <button className="btn text-xs py-1 px-3" onClick={handleGlobalSave} disabled={saving}>
          {saving ? 'Saving…' : 'Save'}
        </button>
        <button className="btn text-xs py-1 px-3" onClick={handleGlobalToggle} disabled={saving}>
          {source.enabled ? 'Disable' : 'Enable'}
        </button>
      </div>
      <button className="btn text-xs py-1" onClick={handleProbe} disabled={probing}>
        {probing ? 'Probing…' : 'Probe source'}
      </button>
      {probeResult && (
        <div className={`text-[10px] ${probeResult.startsWith('OK') ? 'text-green-400' : 'text-red-400'}`}>{probeResult}</div>
      )}
      <div className="flex flex-col gap-1 items-start">
        <ModalLink label="All sources →" render={() => <DiscoverySources />} />
        <ModalLink label="Converters →" render={() => <IngestionConverters />} />
      </div>
    </div>
  )
}

function PanelGraphBuild({
  status, graph, onReload, onClose,
}: {
  status: PipelineStatus
  graph: Graph
  onReload: () => void
  onClose: () => void
}) {
  const [recomputing, setRecomputing] = useState(false)
  const [deleting, setDeleting] = useState(false)
  const [confirmDelete, setConfirmDelete] = useState(false)
  const canDelete = graph.id > 3  // built-ins (Mixed/Songs/Videos) are protected

  const handleRecompute = async () => {
    setRecomputing(true)
    try {
      await recomputePpr({ graph_id: graph.id })
      onReload()
    } catch (e) {
      // ignore
    } finally {
      setRecomputing(false)
    }
  }

  const handleDelete = async () => {
    if (!confirmDelete) { setConfirmDelete(true); return }
    setDeleting(true)
    try {
      await deleteGraph(graph.id)
      onClose()
      onReload()
    } catch (e) { /* ignore */ } finally { setDeleting(false) }
  }

  return (
    <div className="flex flex-col gap-3">
      <div className="flex items-center gap-2">
        <h3 className="text-sm font-semibold text-text">{graph.name}</h3>
        <ContentTypeBadge ct={graph.content_type} />
      </div>
      <p className="text-xs text-text-2">The recommendation graph for this pipeline. Edges connect related content items.</p>
      <div className="flex flex-col gap-1">
        <Row label="Nodes" value={fmt(status.graph.nodes)} />
        <Row label="Edges" value={fmt(status.graph.edges)} />
      </div>
      <button className="btn text-xs py-1" onClick={handleRecompute} disabled={recomputing}>
        {recomputing ? 'Recomputing…' : 'Recompute PPR'}
      </button>
      <ModalLink label="View graph visualization →" render={() => <RecommendationGraph />} />
      <ModalLink label="Manage graphs →" render={() => <Graphs />} />
      {canDelete && (
        <button
          className={`text-xs py-1 px-2 rounded border transition-colors mt-1 ${confirmDelete ? 'border-red-500/60 text-red-400' : 'border-border text-text-2 hover:border-red-500/60 hover:text-red-400'}`}
          onClick={handleDelete}
          disabled={deleting}
        >
          {deleting ? 'Deleting…' : confirmDelete ? 'Confirm delete graph' : 'Delete graph'}
        </button>
      )}
    </div>
  )
}

function PanelScorer({
  scorer, graphId, onReload,
}: {
  scorer: PipelineStatus['scorers'][number]
  graphId: number
  onReload: () => void
}) {
  const [recomputing, setRecomputing] = useState(false)
  const [saving, setSaving] = useState(false)
  const [weight, setWeight] = useState(String(scorer.weight))
  const [enabled, setEnabled] = useState(scorer.enabled)

  useEffect(() => {
    setWeight(String(scorer.weight)); setEnabled(scorer.enabled)
  }, [scorer.id, scorer.weight, scorer.enabled])

  const handleRecompute = async () => {
    setRecomputing(true)
    try {
      if (scorer.id === 'ppr') await recomputePpr({ graph_id: graphId })
      else if (scorer.id === 'cosine') await recomputeCosine({ graph_id: graphId })
      else if (scorer.id === 'serendipity') await recomputeSerendipity(graphId)
      onReload()
    } catch (e) { /* ignore */ } finally { setRecomputing(false) }
  }

  const handleSave = async () => {
    setSaving(true)
    try {
      const w = parseFloat(weight)
      await putPipelineConfig({
        [`scorer.${scorer.id}.enabled`]: enabled ? 1 : 0,
        [`scorer.${scorer.id}.weight`]: isNaN(w) ? scorer.weight : w,
      }, graphId)
      onReload()
    } catch (e) { /* ignore */ } finally { setSaving(false) }
  }

  const scoreLink: Record<string, string> = { ppr: '/scoring/scores', cosine: '/scoring/cosine' }

  return (
    <div className="flex flex-col gap-3">
      <h3 className="text-sm font-semibold text-text">{scorer.name}</h3>
      <p className="text-xs text-text-2">{scorer.description}</p>
      <div className="flex flex-col gap-1">
        <Row label="Scored items" value={fmt(scorer.scored)} />
        <Row label="Last computed" value={age(scorer.computed_at)} />
      </div>
      <hr className="border-border" />
      <label className="flex items-center gap-2 cursor-pointer">
        <input type="checkbox" checked={enabled} onChange={(e) => setEnabled(e.target.checked)} />
        <span className="text-xs text-text">Enabled</span>
      </label>
      <label className="flex flex-col gap-0.5">
        <span className="text-[10px] text-text-2">Blend weight (contribution to final score)</span>
        <input className="input text-xs" type="number" step="0.05" min="0" max="2" value={weight} onChange={(e) => setWeight(e.target.value)} />
      </label>
      <div className="flex gap-2">
        <button className="btn text-xs py-1 px-3" onClick={handleSave} disabled={saving}>{saving ? 'Saving…' : 'Save'}</button>
        <button className="btn text-xs py-1 px-3" onClick={handleRecompute} disabled={recomputing || !enabled}>
          {recomputing ? 'Recomputing…' : 'Recompute'}
        </button>
      </div>
      {scorer.id === 'ppr' && (
        <ModalLink label="Edit engine params (this graph) →" render={() => <RecommendationConfig graphId={graphId} />} />
      )}
      {(scorer.id === 'ppr' || scorer.id === 'cosine') && (
        <ModalLink label="View scores →"
          render={() => (scorer.id === 'cosine' ? <RecommendationCosine graphId={graphId} /> : <RecommendationScores graphId={graphId} />)} />
      )}
    </div>
  )
}

function PanelCustomScorer({ module: m, onReload }: { module: CustomModule; onReload: () => void }) {
  const [recomputing, setRecomputing] = useState(false)
  const handleRecompute = async () => {
    setRecomputing(true)
    try { await recomputeModule(m.id); onReload() } catch (e) { /* ignore */ }
    finally { setRecomputing(false) }
  }
  return (
    <div className="flex flex-col gap-3">
      <h3 className="text-sm font-semibold text-text">{m.name}</h3>
      <p className="text-xs text-text-2">Custom {m.type} module — runs Python code in the scoring pipeline.</p>
      <div className="flex flex-col gap-1">
        <Row label="Type" value={m.type} />
        <Row label="Enabled" value={m.enabled ? 'yes' : 'no'} />
      </div>
      {m.type === 'scorer' && (
        <button className="btn text-xs py-1" onClick={handleRecompute} disabled={recomputing || !m.enabled}>
          {recomputing ? 'Recomputing…' : 'Recompute'}
        </button>
      )}
      <ModalLink label="Edit module →" render={() => <CustomModuleEdit moduleId={m.id} />} />
    </div>
  )
}

function PanelOutput({ status, config, graphId, onReload }: { status: PipelineStatus; config: Record<string, number>; graphId: number; onReload: () => void }) {
  const [diversityEnabled, setDiversityEnabled] = useState(Boolean(config['diversity.enabled']))
  const [lambda, setLambda] = useState(String(config['diversity.lambda'] ?? 0.7))
  const [maxPerChannel, setMaxPerChannel] = useState(String(config['diversity.max_per_channel'] ?? 3))
  const [saving, setSaving] = useState(false)

  useEffect(() => {
    setDiversityEnabled(Boolean(config['diversity.enabled']))
    setLambda(String(config['diversity.lambda'] ?? 0.7))
    setMaxPerChannel(String(config['diversity.max_per_channel'] ?? 3))
  }, [config])

  const handleSave = async () => {
    setSaving(true)
    try {
      await putPipelineConfig({
        'diversity.enabled': diversityEnabled ? 1 : 0,
        'diversity.lambda': parseFloat(lambda) || 0.7,
        'diversity.max_per_channel': parseInt(maxPerChannel) || 3,
      }, graphId)
      onReload()
    } catch (e) { /* ignore */ } finally { setSaving(false) }
  }

  return (
    <div className="flex flex-col gap-3">
      <h3 className="text-sm font-semibold text-text">Output Stage</h3>
      <div className="flex flex-col gap-1 text-xs">
        <Row label="Feed filters" value={status.filters.feed_filter_count} />
        <Row label="Weight rules" value={status.filters.weight_rule_count} />
      </div>
      <hr className="border-border" />
      <label className="flex items-center gap-2 cursor-pointer">
        <input type="checkbox" checked={diversityEnabled} onChange={(e) => setDiversityEnabled(e.target.checked)} />
        <span className="text-xs text-text">MMR diversity re-ranking</span>
      </label>
      {diversityEnabled && (
        <label className="flex flex-col gap-0.5">
          <span className="text-[10px] text-text-2">Lambda — relevance vs diversity (0–1)</span>
          <input className="input text-xs" type="number" step="0.05" min="0" max="1" value={lambda} onChange={(e) => setLambda(e.target.value)} />
        </label>
      )}
      <label className="flex flex-col gap-0.5">
        <span className="text-[10px] text-text-2">Max per channel</span>
        <input className="input text-xs" type="number" min="1" value={maxPerChannel} onChange={(e) => setMaxPerChannel(e.target.value)} />
      </label>
      <button className="btn text-xs py-1" onClick={handleSave} disabled={saving}>{saving ? 'Saving…' : 'Save'}</button>
      <div className="flex flex-col gap-1 mt-1">
        <ModalLink label="Feed filters →" render={() => <RecommendationFilters graphId={graphId} />} />
        <ModalLink label="Weight rules →" render={() => <RecommendationWeightRules graphId={graphId} />} />
        <ModalLink label="Personas →" render={() => <PersonasList />} />
      </div>
    </div>
  )
}

function PanelFeed({ status, graphId }: { status: PipelineStatus; graphId: number }) {
  const [items, setItems] = useState<FeedItem[] | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const preview = async () => {
    setLoading(true); setError(null)
    try {
      const r = await getPprFeed({ graph_id: graphId, limit: 10, sort: 'score' })
      setItems(r.items)
    } catch (e) { setError(String(e)) } finally { setLoading(false) }
  }

  return (
    <div className="flex flex-col gap-3">
      <h3 className="text-sm font-semibold text-text">Feed</h3>
      <p className="text-xs text-text-2">Pre-computed recommendation feed served to the frontend application.</p>
      <div className="text-3xl font-mono font-bold text-text">{fmt(status.feed.items)}</div>
      <div className="text-xs text-text-2">items ready</div>
      <button className="btn text-xs py-1" onClick={preview} disabled={loading}>
        {loading ? 'Loading…' : 'Preview ranked output'}
      </button>
      {error && <div className="text-[10px] text-red-400 break-all">{error}</div>}
      {items && (
        <div className="flex flex-col gap-1.5 max-h-72 overflow-y-auto">
          {items.length === 0 && <div className="text-[10px] text-text-2 italic">empty</div>}
          {items.map((it, i) => (
            <div key={it.video_id} className="flex items-center gap-2 rounded border border-border bg-bg p-1.5">
              <span className="text-[10px] text-text-2 w-4 text-right shrink-0">{i + 1}</span>
              {it.thumbnail && <img src={it.thumbnail} alt="" className="w-10 h-7 object-cover rounded shrink-0" />}
              <div className="min-w-0 flex-1">
                <div className="text-[11px] text-text truncate">{it.title ?? it.video_id}</div>
                <div className="text-[9px] text-text-2 truncate">{it.author ?? ''}</div>
              </div>
              {it.score != null && <span className="font-mono text-[9px] text-text-2 shrink-0">{it.score.toFixed(3)}</span>}
            </div>
          ))}
        </div>
      )}
      <ModalLink label="Browse feed →" render={() => <AppFeed />} />
    </div>
  )
}

function PanelLibrarySource({ status }: { status: LibraryStatus }) {
  const s = status.seeds
  return (
    <div className="flex flex-col gap-3">
      <h3 className="text-sm font-semibold text-text">Library Seeds</h3>
      <p className="text-xs text-text-2">
        External music taste profile synced into recommenderr (e.g. from yamtrack). These
        seeds drive the independent Catalog PPR engine.
      </p>
      <div className="flex flex-col gap-1 text-xs">
        <Row label="Total seeds" value={fmt(s.total)} />
        {Object.entries(s.by_kind).map(([k, n]) => <Row key={k} label={k} value={fmt(n)} />)}
        <Row label="Last synced" value={age(s.last_seed_at)} />
      </div>
      <div className="flex flex-col gap-1 text-xs">
        <div className="text-text-2">Sources</div>
        {Object.entries(s.by_source).map(([src, n]) => (
          <div key={src} className="font-mono text-[10px] bg-bg rounded px-2 py-1 flex justify-between">
            <span>{src}</span><span>{fmt(n)}</span>
          </div>
        ))}
      </div>
      <ModalLink label="View library recs →" render={() => <AppLibraryRecs />} />
    </div>
  )
}

function CatalogSlider({ label, value, min, max, step, description, onChange }: {
  label: string; value: number; min: number; max: number; step: number; description?: string; onChange: (v: number) => void
}) {
  return (
    <div className="flex flex-col gap-0.5">
      <div className="flex items-center justify-between">
        <span className="text-[10px] text-text-2">{label}</span>
        <span className="font-mono text-[10px] text-text">{value % 1 === 0 ? value : value.toFixed(2)}</span>
      </div>
      <input type="range" min={min} max={max} step={step} value={value}
        onChange={(e) => onChange(parseFloat(e.target.value))} className="w-full accent-accent" />
      {description && <p className="text-[9px] text-text-2">{description}</p>}
    </div>
  )
}

function PanelCatalogPpr({ status, onReload }: { status: LibraryStatus; onReload: () => void }) {
  const [cfg, setCfg] = useState<CatalogConfig | null>(null)
  const [dirty, setDirty] = useState<Partial<Omit<CatalogConfig, '_defaults'>>>({})
  const [saving, setSaving] = useState(false)
  const [recomputing, setRecomputing] = useState(false)
  const [msg, setMsg] = useState<string | null>(null)

  useEffect(() => { getCatalogConfig().then(setCfg).catch(() => {}) }, [])

  const get = (k: keyof Omit<CatalogConfig, '_defaults'>) =>
    (k in dirty ? dirty[k] : cfg?.[k]) ?? 0
  const set = (k: keyof Omit<CatalogConfig, '_defaults'>, v: number) => setDirty((p) => ({ ...p, [k]: v }))

  const handleSave = async () => {
    if (!Object.keys(dirty).length) return
    setSaving(true); setMsg(null)
    try { await putCatalogConfig(dirty); const fresh = await getCatalogConfig(); setCfg(fresh); setDirty({}); setMsg('Saved.') }
    catch (e) { setMsg(String(e)) } finally { setSaving(false) }
  }

  const handleRecompute = async () => {
    setRecomputing(true); setMsg(null)
    try {
      if (Object.keys(dirty).length) { await putCatalogConfig(dirty); setDirty({}) }
      await recomputeLibraryRecs()
      setMsg('Recompute started — refresh in a moment.')
      onReload()
    } catch (e) { setMsg(String(e)) } finally { setRecomputing(false) }
  }

  return (
    <div className="flex flex-col gap-3">
      <h3 className="text-sm font-semibold text-text">Catalog PPR</h3>
      <p className="text-xs text-text-2">
        Independent Personalized PageRank over your music library catalog (separate from the
        per-graph PPR). Expands seeds via related artists/albums, then ranks the catalog.
      </p>
      <div className="flex flex-col gap-1 text-xs">
        <Row label="Recs produced" value={fmt(status.results.total)} />
        {Object.entries(status.results.by_kind).map(([k, n]) => <Row key={k} label={k} value={fmt(n)} />)}
        <Row label="Last computed" value={age(status.results.computed_at)} />
        {status.engine.last_error && (
          <div className="rounded border border-red-500/30 bg-red-500/10 p-2 text-[10px] text-red-400 break-all mt-1">
            {status.engine.last_error}
          </div>
        )}
      </div>

      {cfg && (
        <>
          <hr className="border-border" />
          <div className="text-[10px] text-text-2 uppercase tracking-wider font-semibold">Engine params</div>
          <CatalogSlider label="Alpha (damping)" value={get('alpha')} min={0.01} max={0.5} step={0.01}
            description="Restart probability of the catalog walk." onChange={(v) => set('alpha', v)} />
          <CatalogSlider label="Album seed cap" value={get('album_seed_cap')} min={5} max={150} step={5}
            description="Distinct seed artists expanded from album seeds." onChange={(v) => set('album_seed_cap', v)} />
          <CatalogSlider label="Song seed cap" value={get('song_seed_cap')} min={5} max={100} step={5}
            onChange={(v) => set('song_seed_cap', v)} />
          <CatalogSlider label="Related per artist" value={get('related_per_artist')} min={2} max={20} step={1}
            onChange={(v) => set('related_per_artist', v)} />
          <CatalogSlider label="Albums per artist" value={get('albums_per_artist')} min={1} max={12} step={1}
            onChange={(v) => set('albums_per_artist', v)} />
          <CatalogSlider label="Song recs from albums" value={get('song_recs_from_albums')} min={5} max={100} step={5}
            onChange={(v) => set('song_recs_from_albums', v)} />
        </>
      )}

      <div className="flex gap-2">
        <button className="btn text-xs py-1 px-3" onClick={handleSave} disabled={saving || !Object.keys(dirty).length}>
          {saving ? 'Saving…' : 'Save'}
        </button>
        <button className="btn text-xs py-1 px-3" onClick={handleRecompute} disabled={recomputing}>
          {recomputing ? 'Recomputing…' : 'Save & Recompute'}
        </button>
      </div>
      {msg && <div className="text-[10px] text-text-2">{msg}</div>}
      <ModalLink label="View library recs →" render={() => <AppLibraryRecs />} />
    </div>
  )
}

// ---------------------------------------------------------------------------
// Right-click context menu
// ---------------------------------------------------------------------------
// A single menu row. Uses a div (not <button>) so it isn't hit by the global
// button base styles, and renders as a clean command-menu item.
function PanelConsumerForm({
  graphId, existing, onDone,
}: {
  graphId: number
  existing?: PipelineConsumer
  onDone: () => void
}) {
  const METHODS = ['GET', 'POST', 'PUT', 'PATCH', 'DELETE']
  const [name, setName] = useState(existing?.name ?? '')
  const [method, setMethod] = useState(existing?.method ?? 'GET')
  const [path, setPath] = useState(existing?.path ?? '')
  const [url, setUrl] = useState(existing?.url ?? '')
  const [scope, setScope] = useState<'all' | 'graph'>(
    existing ? (existing.graph_id === null ? 'all' : 'graph') : 'graph',
  )
  const [saving, setSaving] = useState(false)
  const [deleting, setDeleting] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const submit = async (e: React.FormEvent) => {
    e.preventDefault(); setSaving(true); setError(null)
    const graph_id = scope === 'all' ? null : graphId
    try {
      if (existing) {
        await updateConsumer(existing.id, { name, method, path, url, graph_id })
      } else {
        await createConsumer({ name, method, path, url, graph_id })
      }
      onDone()
    } catch (e) { setError(String(e)) }
    finally { setSaving(false) }
  }

  const handleDelete = async () => {
    if (!existing) return
    if (!confirm(`Delete consumer "${existing.name}"?`)) return
    setDeleting(true); setError(null)
    try { await deleteConsumer(existing.id); onDone() }
    catch (e) { setError(String(e)) }
    finally { setDeleting(false) }
  }

  return (
    <form onSubmit={submit} className="flex flex-col gap-3">
      <h3 className="text-sm font-semibold text-text">{existing ? 'Edit consumer' : 'Add consumer endpoint'}</h3>
      <p className="text-xs text-text-2">
        Register a downstream system that reads this pipeline's feed. Documentary only —
        recommenderr records it for the canvas, it does not push the feed anywhere.
      </p>
      <label className="flex flex-col gap-0.5">
        <span className="text-[10px] text-text-2">Name</span>
        <input className="input text-xs" value={name} onChange={(e) => setName(e.target.value)} required placeholder="e.g. mobile app" />
      </label>
      <div className="grid grid-cols-[5rem_1fr] gap-2">
        <label className="flex flex-col gap-0.5">
          <span className="text-[10px] text-text-2">Method</span>
          <select className="input text-xs" value={method} onChange={(e) => setMethod(e.target.value)}>
            {METHODS.map((m) => <option key={m} value={m}>{m}</option>)}
          </select>
        </label>
        <label className="flex flex-col gap-0.5">
          <span className="text-[10px] text-text-2">Path</span>
          <input className="input text-xs font-mono" value={path} onChange={(e) => setPath(e.target.value)} placeholder="/v1/recommendations/feed" />
        </label>
      </div>
      <label className="flex flex-col gap-0.5">
        <span className="text-[10px] text-text-2">Host / base URL (optional)</span>
        <input className="input text-xs font-mono" value={url} onChange={(e) => setUrl(e.target.value)} placeholder="http://…" />
      </label>
      <label className="flex flex-col gap-0.5">
        <span className="text-[10px] text-text-2">Scope</span>
        <select className="input text-xs" value={scope} onChange={(e) => setScope(e.target.value as 'all' | 'graph')}>
          <option value="graph">This graph only</option>
          <option value="all">All graphs</option>
        </select>
      </label>
      {error && <div className="text-[10px] text-red-400">{error}</div>}
      <button type="submit" className="btn text-xs py-1" disabled={saving}>
        {saving ? 'Saving…' : existing ? 'Save changes' : 'Add consumer'}
      </button>
      {existing && (
        <button type="button" className="text-xs text-red-400 hover:text-red-300 self-start" onClick={handleDelete} disabled={deleting}>
          {deleting ? 'Deleting…' : 'Delete consumer'}
        </button>
      )}
    </form>
  )
}

function MenuItem({
  onClick, label, hint, tone = 'default', leading,
}: {
  onClick?: () => void
  label: string
  hint?: string
  tone?: 'default' | 'accent' | 'dim'
  leading?: React.ReactNode
}) {
  const interactive = tone !== 'dim'
  return (
    <div
      role="button"
      tabIndex={interactive ? 0 : -1}
      onClick={interactive ? onClick : undefined}
      className={[
        'flex items-center gap-2 rounded-md px-2 py-1.5 text-[13px] select-none transition-colors',
        tone === 'accent' ? 'text-accent hover:bg-accent/10 cursor-pointer'
          : tone === 'dim' ? 'text-text-2/40 cursor-default'
          : 'text-text-2 hover:bg-bg-3 hover:text-text cursor-pointer',
      ].join(' ')}
    >
      {leading}
      <span className="flex-1 truncate">{label}</span>
      {hint && <span className="text-[10px] text-text-2/60 shrink-0">{hint}</span>}
    </div>
  )
}

function MenuLabel({ children }: { children: React.ReactNode }) {
  return <div className="px-2 pt-2 pb-1 text-[10px] font-semibold uppercase tracking-wider text-text-2/50">{children}</div>
}

const MENU_W = 248

function ContextMenu({
  x, y,
  status, contentSources, modules, graphId,
  onClose, onOpenPanel, onReload,
}: {
  x: number
  y: number
  status: PipelineStatus
  contentSources: GraphSourceEntry[]
  modules: CustomModule[]
  graphId: number
  onClose: () => void
  onOpenPanel: (p: PanelNode) => void
  onReload: () => void
}) {
  const openModal = useOpenModal()
  const ref = useRef<HTMLDivElement>(null)
  const [pos, setPos] = useState({ left: x, top: y })

  // Flip / clamp into the viewport once the real size is known.
  useLayoutEffect(() => {
    const el = ref.current
    if (!el) return
    const r = el.getBoundingClientRect()
    let left = x
    let top = y
    if (left + r.width > window.innerWidth - 8) left = Math.max(8, window.innerWidth - r.width - 8)
    if (top + r.height > window.innerHeight - 8) top = Math.max(8, window.innerHeight - r.height - 8)
    setPos({ left, top })
  }, [x, y])

  const disabledSignals = status.signal_sources.filter((s) => !s.enabled)
  const disabledContent = contentSources.filter((s) => !s.enabled)
  const offModules = modules.filter((m) => !m.enabled)
  const onModules = modules.filter((m) => Boolean(m.enabled))

  const enableContent = async (name: string) => {
    try { await patchSource(name, { enabled: true }); onReload() } catch { /* ignore */ }
    onClose()
  }
  const enableSignal = async (id: number) => {
    try { await updateSignalSource(id, { enabled: true }); onReload() } catch { /* ignore */ }
    onClose()
  }
  const enableModule = async (id: number) => {
    try { await updateModule(id, { enabled: true }); onReload() } catch { /* ignore */ }
    onClose()
  }
  const resetPositions = () => {
    localStorage.removeItem('pipeline-canvas-positions')
    window.location.reload()
  }
  const plus = <span className="text-accent text-sm leading-none w-3 text-center shrink-0">+</span>

  return createPortal(
    <>
      {/* backdrop: closes on any outside click / right-click */}
      <div
        className="fixed inset-0 z-[9998]"
        onClick={onClose}
        onContextMenu={(e) => { e.preventDefault(); onClose() }}
      />
      <div
        ref={ref}
        className="fixed z-[9999] rounded-lg border border-border bg-bg-2 p-1.5 select-none max-h-[80vh] overflow-y-auto"
        style={{ left: pos.left, top: pos.top, width: MENU_W, boxShadow: '0 12px 40px rgba(0,0,0,0.6)' }}
        onContextMenu={(e) => e.preventDefault()}
      >
        <MenuLabel>User signals</MenuLabel>
        <MenuItem tone="accent" leading={plus} label="Add signal source"
          onClick={() => { onOpenPanel({ type: 'add_signal_source' }); onClose() }} />
        {disabledSignals.map((s) => (
          <MenuItem key={s.id} label={s.name} hint="enable" onClick={() => enableSignal(s.id)} />
        ))}

        <div className="my-1 mx-1 border-t border-border" />

        <MenuLabel>Content sources</MenuLabel>
        {disabledContent.length === 0 ? (
          <div className="px-2 py-1.5 text-[12px] italic text-text-2/40">all active</div>
        ) : (
          disabledContent.map((s) => (
            <MenuItem key={s.name} label={s.display_name} hint="enable" onClick={() => enableContent(s.name)} />
          ))
        )}

        <div className="my-1 mx-1 border-t border-border" />

        <MenuLabel>Modules</MenuLabel>
        <MenuItem tone="accent" leading={plus} label="Create module"
          onClick={() => { onClose(); openModal(<CustomModules />) }} />
        {offModules.map((m) => (
          <MenuItem key={m.id} label={m.name} hint="add" onClick={() => enableModule(m.id)} />
        ))}
        {onModules.map((m) => (
          <MenuItem key={m.id} tone="dim" label={m.name} hint="active" />
        ))}

        <div className="my-1 mx-1 border-t border-border" />

        <MenuLabel>Consumers</MenuLabel>
        <MenuItem tone="accent" leading={plus} label="Add consumer endpoint"
          onClick={() => { onOpenPanel({ type: 'add_consumer', graphId }); onClose() }} />

        <div className="my-1 mx-1 border-t border-border" />

        <MenuItem label="Reset node positions" onClick={resetPositions} />
      </div>
    </>,
    document.body,
  )
}

// ---------------------------------------------------------------------------
// Slide-in Panel
// ---------------------------------------------------------------------------

function SlidePanel({
  panel, status, contentSources, graphs, selectedGraphId, config, libraryStatus, onClose, onReload,
}: {
  panel: PanelNode | null
  status: PipelineStatus | null
  contentSources: GraphSourceEntry[]
  graphs: Graph[]
  selectedGraphId: number
  config: Record<string, number>
  libraryStatus: LibraryStatus | null
  onClose: () => void
  onReload: () => void
}) {
  if (!panel || !status) return null

  let content: React.ReactNode = null
  if (panel.type === 'signal_source') {
    // Look up live data so panel stays fresh after sync/edit
    const live = status.signal_sources.find((s) => s.id === panel.data.id) ?? panel.data
    content = <PanelSignalSource source={live} onReload={onReload} onDelete={() => { onReload(); onClose() }} />
  } else if (panel.type === 'add_signal_source') {
    content = <PanelAddSignalSource onDone={() => { onReload(); onClose() }} />
  } else if (panel.type === 'content_source') {
    const live = contentSources.find((s) => s.name === panel.data.name) ?? panel.data
    content = <PanelContentSource source={live} graphId={selectedGraphId} onReload={onReload} />
  } else if (panel.type === 'graph_build') {
    content = <PanelGraphBuild status={panel.data} graph={panel.graph} onReload={onReload} onClose={onClose} />
  } else if (panel.type === 'scorer') {
    content = <PanelScorer scorer={panel.data} graphId={selectedGraphId} onReload={onReload} />
  } else if (panel.type === 'custom_scorer') {
    content = <PanelCustomScorer module={panel.data} onReload={onReload} />
  } else if (panel.type === 'output_stage') {
    content = <PanelOutput status={panel.data} config={config} graphId={selectedGraphId} onReload={onReload} />
  } else if (panel.type === 'feed') {
    content = <PanelFeed status={panel.data} graphId={selectedGraphId} />
  } else if (panel.type === 'library_source') {
    content = <PanelLibrarySource status={libraryStatus ?? panel.data} />
  } else if (panel.type === 'catalog_ppr') {
    content = <PanelCatalogPpr status={libraryStatus ?? panel.data} onReload={onReload} />
  } else if (panel.type === 'consumer') {
    content = panel.consumer ? (
      <PanelConsumerForm graphId={selectedGraphId} existing={panel.consumer} onDone={() => { onReload(); onClose() }} />
    ) : (
      <div className="flex flex-col gap-3">
        <h3 className="text-sm font-semibold text-text">{panel.name}</h3>
        <p className="text-xs text-text-2">Built-in system that reads the recommendation feed from this pipeline.</p>
        <div className="flex flex-col gap-1 text-xs">
          <div className="text-text-2">Host</div>
          <div className="font-mono text-[10px] bg-bg rounded px-2 py-1 break-all">{panel.url}</div>
        </div>
        <Row label="API call" value={panel.endpoint} />
      </div>
    )
  } else if (panel.type === 'add_consumer') {
    content = <PanelConsumerForm graphId={panel.graphId} onDone={() => { onReload(); onClose() }} />
  }

  return (
    <div
      className="absolute top-0 right-0 h-full w-80 bg-bg-2 border-l border-border z-10 flex flex-col overflow-hidden"
      style={{ boxShadow: '-4px 0 16px rgba(0,0,0,0.4)' }}
    >
      <div className="flex items-center justify-between px-4 py-3 border-b border-border shrink-0">
        <span className="text-xs font-semibold text-text-2 uppercase tracking-wider">Details</span>
        <button className="text-text-2 hover:text-text text-lg leading-none" onClick={onClose}>✕</button>
      </div>
      <div className="flex-1 overflow-y-auto px-4 py-4">
        {content}
      </div>
    </div>
  )
}

// ---------------------------------------------------------------------------
// Graph selector tabs
// ---------------------------------------------------------------------------

function GraphTabs({
  graphs, selectedId, onChange, onCreate,
}: {
  graphs: Graph[]
  selectedId: number
  onChange: (id: number) => void
  onCreate: () => void
}) {
  const CT_ICON: Record<string, string> = { music: 'Mu', video: 'V', album: 'Al', artist: 'Ar' }
  const visible = graphs.filter((g) => g.content_type !== 'mixed')
  return (
    <div className="flex items-center gap-1 px-4 py-2 border-b border-border bg-bg-2 shrink-0 overflow-x-auto">
      <span className="text-[10px] text-text-2 mr-2 shrink-0">Graph:</span>
      {visible.map((g) => (
        <button
          key={g.id}
          onClick={() => onChange(g.id)}
          className={`flex items-center gap-1 px-2 py-1 rounded text-xs shrink-0 transition-colors ${
            selectedId === g.id
              ? 'bg-accent/20 text-accent border border-accent/40'
              : 'text-text-2 hover:text-text hover:bg-bg'
          }`}
        >
          <span className="text-[8px] bg-bg rounded px-0.5 font-mono">{CT_ICON[g.content_type] ?? g.content_type}</span>
          {g.name}
        </button>
      ))}
      <button
        onClick={onCreate}
        className="ml-1 flex items-center gap-1 px-2 py-1 rounded text-xs shrink-0 text-accent hover:bg-accent/10 border border-dashed border-accent/40 transition-colors"
        title="Create a new graph"
      >
        + Graph
      </button>
    </div>
  )
}

// ---------------------------------------------------------------------------
// Build React Flow nodes + edges from status
// ---------------------------------------------------------------------------

const POSITIONS_KEY = 'pipeline-canvas-positions'

function loadPositions(): Record<string, { x: number; y: number }> {
  try {
    return JSON.parse(localStorage.getItem(POSITIONS_KEY) ?? '{}')
  } catch {
    return {}
  }
}

function savePositions(nodes: Node[]) {
  const map: Record<string, { x: number; y: number }> = {}
  for (const n of nodes) {
    map[n.id] = n.position
  }
  localStorage.setItem(POSITIONS_KEY, JSON.stringify(map))
}

// COL / ROW_GAP live inside buildNodesAndEdges (compact-aware).

function buildNodesAndEdges(
  status: PipelineStatus,
  graphs: Graph[],
  selectedGraphId: number,
  modules: CustomModule[],
  contentSources: GraphSourceEntry[],
  onSelect: (p: PanelNode) => void,
  savedPositions: Record<string, { x: number; y: number }>,
  libraryStatus: LibraryStatus | null,
  customConsumers: PipelineConsumer[],
  compact: boolean,
  onWeight: (target: WeightTarget, value: number) => void,
): { nodes: Node[]; edges: Edge[] } {
  CANVAS_COMPACT = compact
  const ROW_GAP = compact ? 104 : 160
  const COL = compact
    ? { signals: 0, content: 205, graph: 400, scorers: 600, output: 800, feed: 970, consumer: 1140 }
    : { signals: 0, content: 330, graph: 650, scorers: 970, output: 1290, feed: 1560, consumer: 1830 }
  const nodes: Node[] = []
  const edges: Edge[] = []
  const pos = (id: string, defaultX: number, defaultY: number) =>
    savedPositions[id] ?? { x: defaultX, y: defaultY }

  const selectedGraph = graphs.find((g) => g.id === selectedGraphId) ?? graphs[0]

  const edgeStyle = (enabled: boolean) => ({
    stroke: enabled ? '#6e8bff' : '#3a3b45',
    strokeWidth: 1.5,
  })

  // ── Signal source nodes (user interaction data — no aggregate, each connects directly to graph)
  const signalSources = status.signal_sources ?? []
  signalSources.forEach((s, i) => {
    const id = `signal_src_${s.id}`
    nodes.push({
      id,
      type: 'signal_source',
      position: pos(id, COL.signals, i * ROW_GAP),
      data: { source: s, onSelect: () => onSelect({ type: 'signal_source', data: s }) },
    })
  })
  const signalColH = Math.max(signalSources.length * ROW_GAP, ROW_GAP)

  // ── Content source nodes: only sources in this graph that are globally enabled
  const activeSources = contentSources.filter((s) => s.in_graph && s.enabled)
  activeSources.forEach((s, i) => {
    const id = `content_src_${s.name}`
    nodes.push({
      id,
      type: 'content_source',
      position: pos(id, COL.content, i * ROW_GAP),
      data: { source: s, onSelect: () => onSelect({ type: 'content_source', data: s }), onWeight: (v: number) => onWeight({ kind: 'source', name: s.name }, v) },
    })
  })
  const contentColH = Math.max(activeSources.length * ROW_GAP, ROW_GAP)

  const midY = Math.max(signalColH, contentColH) / 2 - 40

  // ── Graph build node
  if (selectedGraph) {
    const gbId = 'graph_build'
    nodes.push({
      id: gbId,
      type: 'graph_build',
      position: pos(gbId, COL.graph, midY),
      data: { status, graph: selectedGraph, onSelect: () => onSelect({ type: 'graph_build', data: status, graph: selectedGraph }) },
    })

    // Signal sources → graph (PPR seeds, purple)
    signalSources.forEach((s) => {
      edges.push({
        id: `e_signal_src_${s.id}_gb`,
        source: `signal_src_${s.id}`,
        target: gbId,
        type: 'smoothstep',
        animated: s.enabled,
        style: { stroke: s.enabled ? '#a78bfa' : '#3a3b45', strokeWidth: 1.5 },
      })
    })

    // Content sources → graph
    activeSources.forEach((s) => {
      const w = s.weight_override ?? s.weight
      edges.push({
        id: `e_content_src_${s.name}_gb`,
        source: `content_src_${s.name}`,
        target: gbId,
        type: 'smoothstep',
        animated: true,
        style: weightedStyle(w, true),
        ...weightLabel(w),
      })
    })

    // ── Scorers
    const allScorers = status.scorers
    const customScorers = modules.filter((m) => m.type === 'scorer' && m.enabled)
    const totalScorers = allScorers.length + customScorers.length
    const scorerStartY = midY - ((totalScorers - 1) * ROW_GAP) / 2

    allScorers.forEach((scorer, i) => {
      const id = `scorer_${scorer.id}`
      nodes.push({
        id,
        type: 'scorer',
        position: pos(id, COL.scorers, scorerStartY + i * ROW_GAP),
        data: { scorer, onSelect: () => onSelect({ type: 'scorer', data: scorer }), onWeight: (v: number) => onWeight({ kind: 'scorer', id: scorer.id }, v) },
      })
      edges.push({
        id: `e_gb_${id}`,
        source: gbId,
        target: id,
        type: 'smoothstep',
        animated: scorer.enabled,
        style: edgeStyle(scorer.enabled),
      })
    })

    customScorers.forEach((m, i) => {
      const id = `custom_scorer_${m.id}`
      nodes.push({
        id,
        type: 'custom_scorer',
        position: pos(id, COL.scorers, scorerStartY + (allScorers.length + i) * ROW_GAP),
        data: { module: m, onSelect: () => onSelect({ type: 'custom_scorer', data: m }) },
      })
      edges.push({
        id: `e_gb_${id}`,
        source: gbId,
        target: id,
        type: 'smoothstep',
        animated: Boolean(m.enabled),
        style: edgeStyle(Boolean(m.enabled)),
      })
    })

    // ── Output node (centered vertically relative to scorers)
    const outId = 'output_stage'
    nodes.push({
      id: outId,
      type: 'output_stage',
      position: pos(outId, COL.output, midY),
      data: { status, config: status.config, onSelect: () => onSelect({ type: 'output_stage', data: status }) },
    })

    // Connect all scorer nodes → output
    allScorers.forEach((scorer) => {
      edges.push({
        id: `e_scorer_${scorer.id}_out`,
        source: `scorer_${scorer.id}`,
        target: outId,
        type: 'smoothstep',
        animated: scorer.enabled,
        style: weightedStyle(scorer.weight, scorer.enabled),
        ...weightLabel(scorer.weight),
      })
    })
    customScorers.forEach((m) => {
      edges.push({
        id: `e_custom_scorer_${m.id}_out`,
        source: `custom_scorer_${m.id}`,
        target: outId,
        type: 'smoothstep',
        animated: Boolean(m.enabled),
        style: edgeStyle(Boolean(m.enabled)),
      })
    })

    // ── Feed output node
    const feedId = 'feed'
    nodes.push({
      id: feedId,
      type: 'feed_output',
      position: pos(feedId, COL.feed, midY),
      data: { status, onSelect: () => onSelect({ type: 'feed', data: status }) },
    })
    edges.push({
      id: 'e_out_feed',
      source: outId,
      target: feedId,
      type: 'smoothstep',
      animated: true,
      style: edgeStyle(true),
    })

    // ── Consumer nodes — external systems that read the feed
    const ytvSystemSource = signalSources.find((s) => s.is_system)
    let ytvOrigin = ''
    if (ytvSystemSource?.endpoint_url) {
      try { ytvOrigin = new URL(ytvSystemSource.endpoint_url).origin } catch { /* skip */ }
    }
    const feedSlug = selectedGraph?.name.toLowerCase() ?? ''
    const consumers = [
      { id: 'consumer_api', name: 'Feed API', url: 'recommenderr', endpoint: `POST /v1/feed/${feedSlug}` },
      ...(ytvOrigin ? [{ id: 'consumer_ytv', name: 'ytvideo', url: ytvOrigin, endpoint: 'GET /v1/recommendations/feed' }] : []),
    ]
    consumers.forEach((c, i) => {
      nodes.push({
        id: c.id,
        type: 'consumer',
        position: pos(c.id, COL.consumer, midY + i * ROW_GAP),
        data: { name: c.name, url: c.url, endpoint: c.endpoint, onSelect: () => onSelect({ type: 'consumer', name: c.name, url: c.url, endpoint: c.endpoint }) },
      })
      edges.push({
        id: `e_feed_${c.id}`,
        source: feedId,
        target: c.id,
        type: 'smoothstep',
        animated: true,
        style: edgeStyle(true),
      })
    })

    // ── Custom (user-registered) consumers — appended below the built-ins
    const builtinConsumerCount = consumers.length
    customConsumers.forEach((c, i) => {
      const nodeId = `consumer_custom_${c.id}`
      const endpoint = `${c.method} ${c.path}`.trim()
      nodes.push({
        id: nodeId,
        type: 'consumer',
        position: pos(nodeId, COL.consumer, midY + (builtinConsumerCount + i) * ROW_GAP),
        data: {
          name: c.name, url: c.url, endpoint, custom: true,
          onSelect: () => onSelect({ type: 'consumer', name: c.name, url: c.url, endpoint, consumer: c }),
        },
      })
      edges.push({
        id: `e_feed_${nodeId}`,
        source: feedId,
        target: nodeId,
        type: 'smoothstep',
        animated: c.enabled,
        style: edgeStyle(c.enabled),
      })
    })

    // ── Library lane (yamtrack → Catalog PPR → library recs).
    // Only meaningful for music graphs; this is a parallel, independent engine.
    if (selectedGraph.content_type === 'music' && libraryStatus) {
      const baseY = Math.max(signalColH, contentColH) + ROW_GAP * 0.5

      const libSeedId = 'library_seeds'
      nodes.push({
        id: libSeedId,
        type: 'library_source',
        position: pos(libSeedId, COL.signals, baseY),
        data: { status: libraryStatus, onSelect: () => onSelect({ type: 'library_source', data: libraryStatus }) },
      })

      const catId = 'catalog_ppr'
      nodes.push({
        id: catId,
        type: 'catalog_ppr',
        position: pos(catId, COL.graph, baseY),
        data: { status: libraryStatus, onSelect: () => onSelect({ type: 'catalog_ppr', data: libraryStatus }) },
      })
      edges.push({
        id: 'e_libseeds_catppr',
        source: libSeedId,
        target: catId,
        type: 'smoothstep',
        animated: true,
        style: { stroke: '#f0a5c5', strokeWidth: 1.5 },
      })

      const libConsumerId = 'consumer_library'
      nodes.push({
        id: libConsumerId,
        type: 'consumer',
        position: pos(libConsumerId, COL.consumer, baseY),
        data: {
          name: 'Library Recs',
          url: 'recommenderr',
          endpoint: 'GET /v1/music/recommendations/library',
          onSelect: () => onSelect({ type: 'consumer', name: 'Library Recs', url: 'recommenderr', endpoint: 'GET /v1/music/recommendations/library' }),
        },
      })
      edges.push({
        id: 'e_catppr_libconsumer',
        source: catId,
        target: libConsumerId,
        type: 'smoothstep',
        animated: true,
        style: { stroke: '#f0a5c5', strokeWidth: 1.5 },
      })
    }
  }

  return { nodes, edges }
}

// ---------------------------------------------------------------------------
// Main PipelineCanvas component
// ---------------------------------------------------------------------------

export default function PipelineCanvas() {
  const [graphs, setGraphs] = useState<Graph[]>([])
  const [selectedGraphId, setSelectedGraphId] = useState(0)  // 0 = uninitialized
  const [status, setStatus] = useState<PipelineStatus | null>(null)
  const [modules, setModules] = useState<CustomModule[]>([])
  const [contentSources, setContentSources] = useState<GraphSourceEntry[]>([])
  const [libraryStatus, setLibraryStatus] = useState<LibraryStatus | null>(null)
  const [consumers, setConsumers] = useState<PipelineConsumer[]>([])
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)
  const [panel, setPanel] = useState<PanelNode | null>(null)
  const [contextMenu, setContextMenu] = useState<{ x: number; y: number } | null>(null)
  const [savedPositions, setSavedPositions] = useState<Record<string, { x: number; y: number }>>(loadPositions)
  const [compact, setCompact] = useState<boolean>(() => { try { return localStorage.getItem('pipeline-compact') === '1' } catch { return false } })
  const [modalContent, setModalContent] = useState<React.ReactNode>(null)
  const containerRef = useRef<HTMLDivElement>(null)
  const rfRef = useRef<ReactFlowInstance | null>(null)

  const [nodes, setNodes, onNodesChange] = useNodesState<Node>([])
  const [edges, setEdges, onEdgesChange] = useEdgesState<Edge>([])

  const reload = useCallback(async () => {
    try {
      const gs = await listGraphs()
      setGraphs(gs)
      // Auto-select first non-mixed graph on first load, or recover if the
      // selected graph no longer exists (e.g. just deleted).
      let gid = selectedGraphId
      if ((gid === 0 || !gs.some((g) => g.id === gid)) && gs.length > 0) {
        gid = gs.find((g) => g.content_type !== 'mixed')?.id ?? gs[0].id
        setSelectedGraphId(gid)
        setLoading(false)
        return  // effect will re-run with the new gid
      }
      const [st, mods, srcs, lib, cons] = await Promise.all([
        getPipelineStatus(gid),
        listModules(),
        listGraphSources(gid),
        getLibraryStatus().catch(() => null),
        listConsumers(gid).catch(() => [] as PipelineConsumer[]),
      ])
      setStatus(st)
      setModules(mods)
      setContentSources(srcs)
      setLibraryStatus(lib)
      setConsumers(cons)
      setError(null)
    } catch (e) {
      setError(String(e))
    } finally {
      setLoading(false)
    }
  }, [selectedGraphId])

  useEffect(() => { reload() }, [reload])

  // Drag-to-weight: commit a node's new weight, then rebuild so the cable updates.
  const handleWeight = useCallback(async (target: WeightTarget, value: number) => {
    try {
      if (target.kind === 'source') {
        await updateGraphSource(selectedGraphId, target.name, { in_graph: true, weight_override: value })
      } else {
        await putPipelineConfig({ [`scorer.${target.id}.weight`]: value }, selectedGraphId)
      }
      reload()
    } catch { /* ignore */ }
  }, [selectedGraphId, reload])

  useEffect(() => {
    if (!status) return
    const { nodes: n, edges: e } = buildNodesAndEdges(
      status, graphs, selectedGraphId, modules, contentSources,
      (p) => setPanel(p),
      savedPositions,
      libraryStatus,
      consumers,
      compact,
      handleWeight,
    )
    setNodes(n)
    setEdges(e)
  }, [status, graphs, selectedGraphId, modules, contentSources, savedPositions, libraryStatus, consumers, compact, handleWeight]) // eslint-disable-line

  const handleNodesChange = useCallback((changes: Parameters<typeof onNodesChange>[0]) => {
    onNodesChange(changes)
    const hasDrag = changes.some((c) => c.type === 'position' && !('dragging' in c && c.dragging))
    if (hasDrag) {
      setNodes((ns) => {
        savePositions(ns)
        setSavedPositions(loadPositions())
        return ns
      })
    }
  }, [onNodesChange, setNodes])

  const config = useMemo(() => ({ ...(status?.config ?? {}) }), [status])

  const fit = useCallback((delay = 60) => {
    setTimeout(() => { try { rfRef.current?.fitView({ padding: 0.15, duration: 300 }) } catch { /* ignore */ } }, delay)
  }, [])

  const onTidy = useCallback(() => {
    try { localStorage.removeItem(POSITIONS_KEY) } catch { /* ignore */ }
    setSavedPositions({})
    fit(80)
  }, [fit])

  const toggleCompact = useCallback(() => {
    setCompact((c) => {
      const next = !c
      try { localStorage.setItem('pipeline-compact', next ? '1' : '0') } catch { /* ignore */ }
      return next
    })
    fit(120)
  }, [fit])

  const handleCreateGraph = useCallback(async (name: string, type: 'mixed' | 'music' | 'video') => {
    const g = await createGraph({ name, content_type: type })
    setModalContent(null)
    setSelectedGraphId(g.id)
  }, [])

  const openCreateGraph = useCallback(() => {
    setModalContent(<CreateGraphForm onCreate={handleCreateGraph} />)
  }, [handleCreateGraph])

  const nonMixedGraphs = graphs.filter((g) => g.content_type !== 'mixed')

  return (
    <ModalCtx.Provider value={setModalContent}>
    <div className="-mx-5 -my-5 flex flex-col overflow-hidden" style={{ height: '100vh' }}>
      <div className="flex items-center justify-between px-5 py-3 border-b border-border bg-bg shrink-0">
        <h1 className="text-sm font-semibold text-text">Pipeline</h1>
        <div className="flex items-center gap-3">
          {error && <span className="text-xs text-red-400">{error}</span>}
          <button className="btn text-xs py-1 px-3" onClick={onTidy} title="Reset to the tidy stage layout">Tidy</button>
          <button
            className={`btn text-xs py-1 px-3${compact ? ' border-accent text-accent' : ''}`}
            onClick={toggleCompact}
            title="Toggle compact node density"
          >
            {compact ? 'Compact ✓' : 'Compact'}
          </button>
          <button
            className={`btn text-xs py-1 px-3${contextMenu ? ' opacity-60' : ''}`}
            onClick={(e) => {
              if (contextMenu) { setContextMenu(null); return }
              const btn = (e.currentTarget as HTMLElement).getBoundingClientRect()
              setContextMenu({ x: Math.max(8, btn.right - MENU_W), y: btn.bottom + 6 })
            }}
          >
            + Add node
          </button>
          <button className="btn text-xs py-1 px-3" onClick={reload} disabled={loading}>
            {loading ? 'Loading…' : 'Refresh'}
          </button>
        </div>
      </div>

      {graphs.length > 0 && (
        <GraphTabs
          graphs={graphs}
          selectedId={selectedGraphId}
          onChange={(id) => { setSelectedGraphId(id); setPanel(null) }}
          onCreate={openCreateGraph}
        />
      )}

      <div className="flex flex-1 overflow-hidden">
      <div
        ref={containerRef}
        className="flex-1 relative overflow-hidden"
        onWheel={(e) => e.stopPropagation()}
        onClick={() => contextMenu && setContextMenu(null)}
      >
        <style>{RF_DARK_CSS}</style>
        <StageLegend />
        <ReactFlow
          nodes={nodes}
          edges={edges}
          onNodesChange={handleNodesChange}
          onEdgesChange={onEdgesChange}
          nodeTypes={NODE_TYPES}
          onInit={(inst) => { rfRef.current = inst }}
          fitView
          fitViewOptions={{ padding: 0.15 }}
          minZoom={0.3}
          maxZoom={2}
          proOptions={{ hideAttribution: true }}
          onPaneContextMenu={(e) => {
            e.preventDefault()
            setContextMenu({ x: e.clientX, y: e.clientY })
          }}
          onPaneClick={() => setContextMenu(null)}
          onNodeClick={() => setContextMenu(null)}
          onNodeMouseEnter={(_, node) => setNodes((ns) => ns.map((n) => n.id === node.id ? { ...n, zIndex: 1 } : n))}
          onNodeMouseLeave={(_, node) => setNodes((ns) => ns.map((n) => n.id === node.id ? { ...n, zIndex: 0 } : n))}
        >
          <Background color="#1f1f24" gap={20} size={1} />
          <Controls className="bg-bg-2 border border-border rounded" />
          <MiniMap
            className="bg-bg-2 border border-border rounded"
            nodeColor="#3a3a40"
            maskColor="rgba(0,0,0,0.5)"
            pannable
            zoomable
          />
        </ReactFlow>

        {contextMenu && status && (
          <ContextMenu
            x={contextMenu.x}
            y={contextMenu.y}
            status={status}
            contentSources={contentSources}
            modules={modules}
            graphId={selectedGraphId}
            onClose={() => setContextMenu(null)}
            onOpenPanel={(p) => { setPanel(p); setContextMenu(null) }}
            onReload={reload}
          />
        )}

        {panel && status && (
          <SlidePanel
            panel={panel}
            status={status}
            contentSources={contentSources}
            graphs={graphs}
            selectedGraphId={selectedGraphId}
            config={config}
            libraryStatus={libraryStatus}
            onClose={() => setPanel(null)}
            onReload={reload}
          />
        )}

        {!loading && nonMixedGraphs.length === 0 && (
          <div className="absolute inset-0 z-20 flex items-center justify-center bg-bg/85 backdrop-blur-sm p-6">
            <div className="w-full max-w-xl rounded-xl border border-border bg-bg-2 p-6 shadow-2xl">
              <CreateGraphForm onCreate={handleCreateGraph} />
            </div>
          </div>
        )}
      </div>
      </div>
    </div>
    <CanvasModalHost content={modalContent} onClose={() => setModalContent(null)} />
    </ModalCtx.Provider>
  )
}
