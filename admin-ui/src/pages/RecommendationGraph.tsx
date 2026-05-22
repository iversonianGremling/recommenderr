import { useCallback, useEffect, useRef, useState } from 'react'
import ForceGraph2D from 'react-force-graph-2d'
import type { GraphStats } from '../lib/types'

// ── Types ─────────────────────────────────────────────────────────────────────

type Mode = 'top' | 'ego' | 'channel'
type Direction = 'in' | 'out' | 'both'
type NodeType = 'target' | 'source' | 'center' | 'scored' | 'neighbor' | 'channel'

interface GNode {
  id: string
  label: string
  author?: string | null
  thumbnail?: string | null
  score?: number | null
  edge_weight?: number | null
  type: NodeType
  // injected by force-graph at runtime
  x?: number; y?: number; vx?: number; vy?: number; fx?: number; fy?: number
}

interface GEdge {
  source: string
  target: string
  weight: number
  edge_count?: number
}

interface SubgraphResponse {
  nodes: GNode[]
  edges: GEdge[]
  meta: { mode: string; node_count: number; edge_count: number; center?: string }
}

// ── Colour helpers ────────────────────────────────────────────────────────────

const NODE_COLORS: Record<NodeType, string> = {
  center:   '#ffffff',
  target:   '#60a5fa',  // blue  — top PPR recommended
  scored:   '#a78bfa',  // purple — ego node that also has a PPR score
  source:   '#fb923c',  // orange — seed / watched videos
  neighbor: '#6b7280',  // grey  — unscored ego neighbours
  channel:  '#34d399',  // green — channel-level nodes
}

function hashColor(s: string): string {
  let h = 0
  for (let i = 0; i < s.length; i++) h = (Math.imul(31, h) + s.charCodeAt(i)) | 0
  const hue = Math.abs(h) % 360
  return `hsl(${hue},65%,60%)`
}

function nodeColor(n: GNode): string {
  if (n.type === 'channel') return hashColor(n.label)
  return NODE_COLORS[n.type] ?? '#6b7280'
}

function nodeSize(n: GNode): number {
  if (n.score != null) return Math.max(2, Math.log1p(n.score * 1e5) * 1.5)
  if (n.edge_weight != null) return Math.max(1.5, Math.log1p(n.edge_weight) * 1.2)
  if (n.type === 'center') return 8
  return 2.5
}

function linkWidth(e: GEdge): number {
  return Math.max(0.3, Math.min(4, (e.weight ?? 1) * 1.5))
}

// ── Stats card ────────────────────────────────────────────────────────────────

function StatsCard({ stats }: { stats: GraphStats }) {
  return (
    <div className="flex gap-4 text-xs text-text-2 mb-4">
      <span>Full graph: <strong className="text-text">{stats.nodes.toLocaleString()}</strong> nodes</span>
      <span><strong className="text-text">{stats.edges.toLocaleString()}</strong> edges</span>
      <span>density <strong className="text-text">{stats.density.toExponential(1)}</strong></span>
      <span><strong className="text-text">{stats.scored_nodes.toLocaleString()}</strong> scored</span>
    </div>
  )
}

// ── Hover tooltip ─────────────────────────────────────────────────────────────

interface TooltipProps {
  node: GNode
  x: number
  y: number
  onEgo: (id: string) => void
}

function NodeTooltip({ node, x, y, onEgo }: TooltipProps) {
  return (
    <div
      className="fixed z-50 rounded border border-border bg-bg shadow-lg p-2.5 text-xs max-w-56 pointer-events-none"
      style={{ left: x + 14, top: y - 10 }}
    >
      <p className="font-medium text-text leading-snug mb-0.5 line-clamp-2">{node.label}</p>
      {node.author && <p className="text-text-2 mb-1 truncate">{node.author}</p>}
      <div className="flex flex-col gap-0.5 text-text-2">
        {node.score != null && <span>Score: <code className="text-accent">{node.score.toFixed(6)}</code></span>}
        {node.edge_weight != null && <span>Edge mass: <code className="text-accent">{node.edge_weight.toFixed(2)}</code></span>}
        <span>Type: <code>{node.type}</code></span>
        <span className="font-mono text-[10px] break-all text-text-2/60">{node.id}</span>
      </div>
      <button
        className="mt-2 w-full rounded border border-border text-[10px] py-0.5 text-text-2 hover:text-text pointer-events-auto"
        onMouseDown={(e) => { e.preventDefault(); onEgo(node.id) }}
      >
        Explore ego graph →
      </button>
    </div>
  )
}

// ── Legend ────────────────────────────────────────────────────────────────────

const LEGEND_ITEMS: { color: string; label: string }[] = [
  { color: NODE_COLORS.target,   label: 'Top recommended' },
  { color: NODE_COLORS.source,   label: 'Source / watched' },
  { color: NODE_COLORS.center,   label: 'Ego centre' },
  { color: NODE_COLORS.scored,   label: 'Scored neighbour' },
  { color: NODE_COLORS.neighbor, label: 'Unscored neighbour' },
  { color: NODE_COLORS.channel,  label: 'Channel (hashed)' },
]

// ── Main component ────────────────────────────────────────────────────────────

export default function RecommendationGraph() {
  const [stats, setStats] = useState<GraphStats | null>(null)
  const [mode, setMode] = useState<Mode>('top')
  const [limit, setLimit] = useState(150)
  const [center, setCenter] = useState('')
  const [centerInput, setCenterInput] = useState('')
  const [direction, setDirection] = useState<Direction>('in')
  const [minWeight, setMinWeight] = useState(0)
  const [graph, setGraph] = useState<SubgraphResponse | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [hoveredNode, setHoveredNode] = useState<GNode | null>(null)
  const [cursor, setCursor] = useState({ x: 0, y: 0 })

  const containerRef = useRef<HTMLDivElement>(null)
  const [dims, setDims] = useState({ w: 800, h: 560 })

  // Resize observer
  useEffect(() => {
    if (!containerRef.current) return
    const obs = new ResizeObserver((entries) => {
      const { width } = entries[0].contentRect
      setDims({ w: Math.max(400, width), h: Math.max(400, Math.round(width * 0.55)) })
    })
    obs.observe(containerRef.current)
    return () => obs.disconnect()
  }, [])

  // Load global stats once
  useEffect(() => {
    fetch('/v1/ppr/graph/stats')
      .then((r) => r.json())
      .then(setStats)
      .catch(() => {})
  }, [])

  const fetchGraph = useCallback(async (m: Mode, opts: {
    limit: number; center?: string; direction?: Direction; minWeight?: number
  }) => {
    setLoading(true)
    setError(null)
    setHoveredNode(null)
    const params = new URLSearchParams({
      mode: m,
      limit: String(opts.limit),
      min_weight: String(opts.minWeight ?? 0),
    })
    if (m === 'ego' && opts.center) params.set('center', opts.center)
    if (m === 'ego') params.set('direction', opts.direction ?? 'in')
    try {
      const r = await fetch(`/v1/ppr/graph/subgraph?${params}`)
      if (!r.ok) { const e = await r.json(); throw new Error(e.detail ?? 'Error') }
      const data: SubgraphResponse = await r.json()
      setGraph(data)
    } catch (e) {
      setError(String(e))
    } finally {
      setLoading(false)
    }
  }, [])

  const handleLoad = () => {
    if (mode === 'ego' && !center) { setError('Enter a video ID or title to explore.'); return }
    fetchGraph(mode, { limit, center, direction, minWeight })
  }

  const handleEgo = (id: string) => {
    setMode('ego')
    setCenter(id)
    setCenterInput(id)
    setHoveredNode(null)
    fetchGraph('ego', { limit, center: id, direction, minWeight })
  }

  // Canvas node rendering: circle + label for high-score nodes
  const nodeCanvasObject = useCallback((node: object, ctx: CanvasRenderingContext2D, globalScale: number) => {
    const n = node as GNode
    const r = nodeSize(n)
    const x = n.x ?? 0
    const y = n.y ?? 0

    ctx.beginPath()
    ctx.arc(x, y, r, 0, 2 * Math.PI)
    ctx.fillStyle = nodeColor(n)
    ctx.fill()

    if (n.type === 'center') {
      ctx.strokeStyle = '#fff'
      ctx.lineWidth = 1.5 / globalScale
      ctx.stroke()
    }

    const labelScale = 4
    if (globalScale > labelScale || n.type === 'center') {
      const label = n.label.length > 28 ? n.label.slice(0, 27) + '…' : n.label
      ctx.font = `${Math.max(2.5, 4 / globalScale)}px sans-serif`
      ctx.fillStyle = 'rgba(255,255,255,0.85)'
      ctx.textAlign = 'center'
      ctx.fillText(label, x, y + r + 4 / globalScale)
    }
  }, [])

  const graphData = graph
    ? { nodes: graph.nodes, links: graph.edges.map((e) => ({ ...e, source: e.source, target: e.target })) }
    : { nodes: [], links: [] }

  return (
    <div>
      <h1 className="page-title mb-1">Recommendation Graph</h1>
      {stats && <StatsCard stats={stats} />}

      {/* Controls */}
      <div className="rounded border border-border bg-bg-2 p-3 mb-4 flex flex-wrap gap-3 items-end text-xs">

        {/* Mode */}
        <div>
          <p className="label mb-1">Mode</p>
          <div className="flex rounded border border-border overflow-hidden">
            {(['top', 'ego', 'channel'] as Mode[]).map((m) => (
              <button
                key={m}
                className={`px-3 py-1 capitalize ${mode === m ? 'bg-accent text-white' : 'text-text-2 hover:text-text'}`}
                onClick={() => setMode(m)}
              >
                {m === 'top' ? 'Top Feed' : m === 'ego' ? 'Ego Graph' : 'Channels'}
              </button>
            ))}
          </div>
        </div>

        {/* Limit */}
        <div className="w-36">
          <p className="label mb-1">Max nodes: <strong className="text-text">{limit}</strong></p>
          <input
            type="range" min={30} max={400} step={10} value={limit}
            onChange={(e) => setLimit(Number(e.target.value))}
            className="w-full accent-accent"
          />
        </div>

        {/* Min weight (channel + top) */}
        {mode !== 'ego' && (
          <div className="w-36">
            <p className="label mb-1">Min edge weight: <strong className="text-text">{minWeight}</strong></p>
            <input
              type="range" min={0} max={20} step={1} value={minWeight}
              onChange={(e) => setMinWeight(Number(e.target.value))}
              className="w-full accent-accent"
            />
          </div>
        )}

        {/* Ego centre + direction */}
        {mode === 'ego' && (
          <>
            <div className="flex-1 min-w-48">
              <p className="label mb-1">Centre video ID</p>
              <input
                className="input w-full text-xs py-1 font-mono"
                placeholder="video_id from scores…"
                value={centerInput}
                onChange={(e) => { setCenterInput(e.target.value); setCenter(e.target.value) }}
                onKeyDown={(e) => e.key === 'Enter' && handleLoad()}
              />
            </div>
            <div>
              <p className="label mb-1">Direction</p>
              <div className="flex rounded border border-border overflow-hidden">
                {(['in', 'out', 'both'] as Direction[]).map((d) => (
                  <button
                    key={d}
                    className={`px-3 py-1 ${direction === d ? 'bg-accent text-white' : 'text-text-2 hover:text-text'}`}
                    onClick={() => setDirection(d)}
                  >
                    {d === 'in' ? '← In' : d === 'out' ? 'Out →' : '↔ Both'}
                  </button>
                ))}
              </div>
            </div>
          </>
        )}

        <button className="btn-primary self-end" onClick={handleLoad} disabled={loading}>
          {loading ? 'Loading…' : 'Load graph'}
        </button>

        {graph && (
          <span className="self-end text-text-2">
            {graph.meta.node_count} nodes · {graph.meta.edge_count} edges
          </span>
        )}
      </div>

      {error && <p className="text-red-500 text-sm mb-3">{error}</p>}

      {/* Mode explainers */}
      {!graph && !loading && (
        <div className="rounded border border-border bg-bg-2 p-5 text-sm text-text-2 space-y-2">
          <p><strong className="text-text">Top Feed</strong> — renders the top-N PPR-scored videos (blue) alongside the source/watched videos (orange) that drove their scores. Reveals which watched content is "pulling" the recommendations.</p>
          <p><strong className="text-text">Ego Graph</strong> — BFS neighbourhood around any video. <em>In</em> shows what recommends it (why is it scored?), <em>Out</em> shows what it generates (what does watching it lead to?), <em>Both</em> for the full local structure.</p>
          <p><strong className="text-text">Channels</strong> — collapses all 191k videos into their channels. Edge weight is the sum of recommendation weights between every pair of channels. Reveals which content communities feed each other.</p>
          <p className="text-[11px] opacity-60 pt-1">Hover nodes for details · click "Explore ego graph" in the tooltip to re-center · node size = PPR score magnitude</p>
        </div>
      )}

      {/* Legend */}
      {graph && graph.meta.node_count > 0 && (
        <div className="flex flex-wrap gap-3 text-[10px] text-text-2 mb-2">
          {LEGEND_ITEMS.map((item) => (
            <span key={item.label} className="flex items-center gap-1.5">
              <span className="inline-block w-2.5 h-2.5 rounded-full flex-shrink-0" style={{ background: item.color }} />
              {item.label}
            </span>
          ))}
          <span className="ml-2 opacity-60">· size ∝ PPR score · edge width ∝ weight · scroll to zoom · drag to pan</span>
        </div>
      )}

      {/* Graph canvas */}
      <div
        ref={containerRef}
        className="relative rounded border border-border bg-[#0d0d0f] overflow-hidden"
        onMouseMove={(e) => setCursor({ x: e.clientX, y: e.clientY })}
      >
        {graph && graph.meta.node_count > 0 ? (
          <ForceGraph2D
            width={dims.w}
            height={dims.h}
            graphData={graphData}
            nodeId="id"
            nodeVal={nodeSize as (n: object) => number}
            nodeColor={nodeColor as (n: object) => string}
            nodeCanvasObject={nodeCanvasObject}
            nodeCanvasObjectMode={() => 'replace'}
            linkWidth={linkWidth as (l: object) => number}
            linkDirectionalArrowLength={3}
            linkDirectionalArrowRelPos={1}
            linkColor={() => 'rgba(255,255,255,0.12)'}
            onNodeHover={(node) => setHoveredNode(node ? node as GNode : null)}
            onNodeClick={(node) => handleEgo((node as GNode).id)}
            cooldownTicks={120}
            d3AlphaDecay={0.02}
            d3VelocityDecay={0.3}
            backgroundColor="#0d0d0f"
          />
        ) : graph && graph.meta.node_count === 0 ? (
          <div className="flex items-center justify-center text-text-2 text-sm" style={{ height: dims.h }}>
            No graph data — run Recompute to build recommendation edges first.
          </div>
        ) : (
          <div className="flex items-center justify-center text-text-2 text-sm" style={{ height: dims.h }}>
            {loading ? 'Building subgraph…' : 'Choose a mode and click Load graph.'}
          </div>
        )}
      </div>

      {hoveredNode && (
        <NodeTooltip node={hoveredNode} x={cursor.x} y={cursor.y} onEgo={handleEgo} />
      )}
    </div>
  )
}
