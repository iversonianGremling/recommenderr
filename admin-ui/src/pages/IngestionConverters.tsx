import { useEffect, useState } from 'react'
import {
  listConverters, createConverter, updateConverter, deleteConverter,
  listSources, listGraphs,
  type Converter, type Graph,
} from '../lib/api'
import type { Source } from '../lib/types'

// ── helpers ────────────────────────────────────────────────────────────────

function fmt(n: number | null | undefined): string {
  if (n == null) return '—'
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`
  if (n >= 1_000) return `${(n / 1_000).toFixed(1)}k`
  return String(n)
}

function age(ts: number | null | undefined): string {
  if (!ts) return 'never'
  const s = Date.now() / 1000 - ts
  if (s < 60) return `${Math.round(s)}s ago`
  if (s < 3600) return `${Math.round(s / 60)}m ago`
  if (s < 86400) return `${Math.round(s / 3600)}h ago`
  return `${Math.round(s / 86400)}d ago`
}

const CT_BADGE: Record<string, string> = {
  video: 'bg-green-500/15 text-green-400',
  music: 'bg-purple-500/15 text-purple-400',
  mixed: 'bg-blue-500/15 text-blue-400',
}

// ── mapping visual ─────────────────────────────────────────────────────────

type OpType = 'passthrough' | 'rename' | 'merge' | 'transform' | 'delete'

interface Operation {
  type: OpType
  from?: string | string[]
  to?: string
  field?: string
  via?: string
}

interface MappingSpec {
  operations?: Operation[]
}

const EXAMPLE_MAPPING = `{
  "operations": [
    { "type": "passthrough", "from": "video_id", "to": "id" },
    { "type": "rename", "from": "video_title", "to": "title" },
    { "type": "merge", "from": ["author", "channel_name"], "to": "artist" },
    { "type": "transform", "from": ["duration_s", "fps"], "to": "duration_info", "via": "normalize" },
    { "type": "delete", "field": "raw_html" }
  ]
}`

const OP_LABEL: Record<OpType, string> = {
  passthrough: '',
  rename: 'rename',
  merge: 'merge',
  transform: '',
  delete: 'delete',
}

const OP_COLOR: Record<OpType, string> = {
  passthrough: 'text-text-2',
  rename: 'text-blue-400 border-blue-500/30',
  merge: 'text-purple-400 border-purple-500/30',
  transform: 'text-yellow-400 border-yellow-500/30',
  delete: 'text-red-400 border-red-500/30',
}

function FieldChip({ name, dim, accent }: { name: string; dim?: boolean; accent?: boolean }) {
  return (
    <span className={`inline-block font-mono text-[10px] px-1.5 py-0.5 rounded border whitespace-nowrap
      ${dim ? 'border-red-500/30 text-red-400/50 line-through' : accent ? 'border-accent/40 text-accent' : 'border-border text-text'}`}>
      {name}
    </span>
  )
}

function OperationRow({ op }: { op: Operation }) {
  const inputs: string[] = op.type === 'delete'
    ? [op.field ?? '?']
    : Array.isArray(op.from) ? op.from : [op.from ?? '?']
  const multi = inputs.length > 1
  const label = op.type === 'transform' ? (op.via ?? 'transform') : OP_LABEL[op.type]

  return (
    <div className="flex items-stretch gap-2 py-0.5">
      {/* Input chips */}
      <div className="flex flex-col gap-1 items-end justify-center min-w-[100px]">
        {inputs.map((f, i) => (
          <FieldChip key={i} name={f} dim={op.type === 'delete'} />
        ))}
      </div>

      {/* Bracket for multi-input */}
      {multi && (
        <div className="flex flex-col w-2 self-stretch my-0.5">
          <div className="flex-1 border-r border-t border-border/40 rounded-tr" />
          <div className="flex-1 border-r border-b border-border/40 rounded-br" />
        </div>
      )}

      {/* Connector + label */}
      <div className="flex items-center gap-1 self-center">
        <span className="text-text-3/60 text-[10px] font-mono">──</span>
        {label && (
          <span className={`text-[9px] px-1 py-0.5 rounded border bg-bg-3 ${OP_COLOR[op.type]}`}>
            {label}
          </span>
        )}
        {op.type === 'delete'
          ? <span className="text-red-400/70 text-[10px] font-mono">──✗</span>
          : <span className="text-text-3/60 text-[10px] font-mono">──→</span>
        }
      </div>

      {/* Output chip */}
      {op.type !== 'delete' && op.to && (
        <div className="flex items-center self-center">
          <FieldChip name={op.to} accent />
        </div>
      )}
    </div>
  )
}

function MappingVisual({ code }: { code: string }) {
  let parsed: MappingSpec = {}
  let parseError = ''
  try {
    parsed = JSON.parse(code || '{}')
  } catch (e) {
    parseError = String(e)
  }

  const ops = parsed.operations ?? []

  if (parseError) {
    return <div className="text-red-400 text-[10px] font-mono py-1">{parseError}</div>
  }
  if (ops.length === 0) {
    return (
      <div className="text-text-3 text-xs italic py-2">
        No operations defined. Add an <code className="bg-bg-3 px-1 rounded">"operations"</code> array to the JSON.
      </div>
    )
  }

  return (
    <div className="space-y-0.5">
      {ops.map((op, i) => <OperationRow key={i} op={op} />)}
    </div>
  )
}

// ── converter form ─────────────────────────────────────────────────────────

interface FormState {
  name: string
  description: string
  content_type: 'video' | 'music' | 'mixed'
  sources: string[]
  graph_ids: number[]
  mapping_code: string
  enabled: boolean
}

const EMPTY_FORM: FormState = {
  name: '',
  description: '',
  content_type: 'video',
  sources: [],
  graph_ids: [],
  mapping_code: '{}',
  enabled: true,
}

function converterToForm(c: Converter): FormState {
  return {
    name: c.name,
    description: c.description,
    content_type: c.content_type,
    sources: c.sources,
    graph_ids: c.graph_ids,
    mapping_code: c.mapping_code || '{}',
    enabled: c.enabled,
  }
}

interface ConverterFormProps {
  initial: FormState
  availableSources: Source[]
  availableGraphs: Graph[]
  submitting: boolean
  error: string | null
  onSubmit: (form: FormState) => void
  onCancel: () => void
  submitLabel: string
}

function ConverterForm({
  initial, availableSources, availableGraphs,
  submitting, error, onSubmit, onCancel, submitLabel,
}: ConverterFormProps) {
  const [form, setForm] = useState<FormState>(initial)
  const [mappingTab, setMappingTab] = useState<'code' | 'visual'>('code')

  function toggleSource(name: string) {
    setForm(f => ({
      ...f,
      sources: f.sources.includes(name)
        ? f.sources.filter(s => s !== name)
        : [...f.sources, name],
    }))
  }

  function toggleGraph(id: number) {
    setForm(f => ({
      ...f,
      graph_ids: f.graph_ids.includes(id)
        ? f.graph_ids.filter(g => g !== id)
        : [...f.graph_ids, id],
    }))
  }

  return (
    <div className="space-y-4">
      {error && (
        <div className="rounded border border-red-500/30 bg-red-500/10 px-3 py-2 text-xs text-red-300">{error}</div>
      )}

      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
        <div>
          <label className="label">Name</label>
          <input
            className="input w-full"
            value={form.name}
            onChange={e => setForm(f => ({ ...f, name: e.target.value }))}
            placeholder="e.g. YouTube Video Crawler"
            autoFocus
          />
        </div>
        <div>
          <label className="label">Content type</label>
          <select
            className="input w-full"
            value={form.content_type}
            onChange={e => setForm(f => ({ ...f, content_type: e.target.value as FormState['content_type'] }))}
          >
            <option value="video">Video</option>
            <option value="music">Music</option>
            <option value="mixed">Mixed</option>
          </select>
        </div>
      </div>

      <div>
        <label className="label">Description</label>
        <textarea
          className="input w-full resize-none"
          rows={2}
          value={form.description}
          onChange={e => setForm(f => ({ ...f, description: e.target.value }))}
          placeholder="What does this converter do? Which APIs does it call?"
        />
      </div>

      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
        <div>
          <label className="label">Sources</label>
          <div className="rounded border border-border bg-bg p-2 space-y-1 max-h-48 overflow-y-auto">
            {availableSources.map(s => (
              <label key={s.name} className="flex items-center gap-2 cursor-pointer py-0.5 hover:bg-bg-2 px-1 rounded text-xs">
                <input
                  type="checkbox"
                  checked={form.sources.includes(s.name)}
                  onChange={() => toggleSource(s.name)}
                  className="shrink-0"
                />
                <span className="text-text">{s.display_name}</span>
                <span className="text-text-3 ml-auto">{s.kind}</span>
              </label>
            ))}
          </div>
        </div>

        <div>
          <label className="label">Target graphs</label>
          <div className="rounded border border-border bg-bg p-2 space-y-1 max-h-48 overflow-y-auto">
            {availableGraphs.filter(g => g.content_type !== 'mixed').map(g => (
              <label key={g.id} className="flex items-center gap-2 cursor-pointer py-0.5 hover:bg-bg-2 px-1 rounded text-xs">
                <input
                  type="checkbox"
                  checked={form.graph_ids.includes(g.id)}
                  onChange={() => toggleGraph(g.id)}
                  className="shrink-0"
                />
                <span className="text-text">{g.name}</span>
                <span className="text-text-3 ml-auto">{g.content_type}</span>
              </label>
            ))}
          </div>
        </div>
      </div>

      {/* Mapping code + visual */}
      <div>
        <div className="flex items-center justify-between mb-1.5">
          <label className="label mb-0">Field mapping</label>
          <div className="flex gap-0.5 bg-bg-2 rounded border border-border p-0.5">
            <button
              type="button"
              onClick={() => setMappingTab('code')}
              className={`text-[10px] px-2 py-0.5 rounded transition-colors ${mappingTab === 'code' ? 'bg-bg-3 text-text' : 'text-text-2 hover:text-text'}`}
            >
              Code
            </button>
            <button
              type="button"
              onClick={() => setMappingTab('visual')}
              className={`text-[10px] px-2 py-0.5 rounded transition-colors ${mappingTab === 'visual' ? 'bg-bg-3 text-text' : 'text-text-2 hover:text-text'}`}
            >
              Visual
            </button>
          </div>
        </div>

        {mappingTab === 'code' ? (
          <div className="relative">
            <textarea
              className="input w-full font-mono text-[11px] resize-y leading-relaxed"
              rows={12}
              value={form.mapping_code}
              onChange={e => setForm(f => ({ ...f, mapping_code: e.target.value }))}
              spellCheck={false}
              placeholder={EXAMPLE_MAPPING}
            />
            <button
              type="button"
              onClick={() => setForm(f => ({ ...f, mapping_code: EXAMPLE_MAPPING }))}
              className="absolute top-2 right-2 text-[9px] text-text-3 hover:text-text bg-bg-3 px-1.5 py-0.5 rounded border border-border"
            >
              load example
            </button>
          </div>
        ) : (
          <div className="rounded border border-border bg-bg p-3 min-h-[120px]">
            <MappingVisual code={form.mapping_code} />
          </div>
        )}
      </div>

      <div className="flex items-center gap-2">
        <label className="flex items-center gap-2 text-xs cursor-pointer">
          <input
            type="checkbox"
            checked={form.enabled}
            onChange={e => setForm(f => ({ ...f, enabled: e.target.checked }))}
          />
          <span className="text-text">Enabled</span>
        </label>
      </div>

      <div className="flex gap-2">
        <button
          className="btn-primary text-xs"
          disabled={submitting || !form.name.trim()}
          onClick={() => onSubmit(form)}
        >
          {submitting ? 'Saving…' : submitLabel}
        </button>
        <button className="btn text-xs" onClick={onCancel}>Cancel</button>
      </div>
    </div>
  )
}

// ── converter card ─────────────────────────────────────────────────────────

interface ConverterCardProps {
  converter: Converter
  sources: Source[]
  graphs: Graph[]
  onEdit: () => void
  onDelete: () => void
}

function ConverterCard({ converter: c, sources, graphs, onEdit, onDelete }: ConverterCardProps) {
  const [mappingOpen, setMappingOpen] = useState(false)
  const [mappingTab, setMappingTab] = useState<'code' | 'visual'>('visual')

  const graphNames = c.graph_ids
    .map(id => graphs.find(g => g.id === id)?.name ?? `#${id}`)
    .join(', ')

  const usedSources = sources.filter(s => c.sources.includes(s.name))

  const statRows: Array<{ label: string; value: string; accent?: boolean }> =
    c.content_type === 'video'
      ? [
          { label: 'Queue pending', value: fmt(c.stats?.queue_pending), accent: (c.stats?.queue_pending ?? 0) > 0 },
          { label: 'Queue done', value: fmt(c.stats?.queue_done) },
          { label: 'Queue failed', value: fmt(c.stats?.queue_failed), accent: (c.stats?.queue_failed ?? 0) > 0 },
          { label: 'Edges total', value: fmt(c.stats?.edges_total) },
          { label: 'Last crawled', value: age(c.stats?.last_crawled_at) },
        ]
      : [
          { label: 'Jobs pending', value: fmt(c.stats?.jobs_pending), accent: (c.stats?.jobs_pending ?? 0) > 0 },
          { label: 'Jobs done', value: fmt(c.stats?.jobs_done) },
          { label: 'Jobs errors', value: fmt(c.stats?.jobs_errors), accent: (c.stats?.jobs_errors ?? 0) > 0 },
          { label: 'Library', value: fmt(c.stats?.library_total) },
          { label: 'Recognized', value: `${fmt(c.stats?.recognized_music)} / ${fmt(c.stats?.recognized_total)}` },
          { label: 'Last job', value: age(c.stats?.last_job_at) },
        ]

  let hasMappingOps = false
  try {
    const p: MappingSpec = JSON.parse(c.mapping_code || '{}')
    hasMappingOps = (p.operations?.length ?? 0) > 0
  } catch { /* invalid json */ }

  return (
    <div className={`surface p-4 ${!c.enabled ? 'opacity-60' : ''}`}>
      <div className="flex items-start justify-between gap-3 mb-2">
        <div className="min-w-0">
          <div className="flex items-center gap-2 flex-wrap">
            <h2 className="font-semibold text-text">{c.name}</h2>
            <span className={`tag text-[10px] ${CT_BADGE[c.content_type] ?? 'bg-bg-3 text-text-2'}`}>
              {c.content_type}
            </span>
            {!c.enabled && <span className="tag bg-bg-3 text-text-3 text-[10px]">disabled</span>}
          </div>
          {c.description && (
            <p className="mt-1 text-xs text-text-2">{c.description}</p>
          )}
        </div>
        <div className="flex gap-1.5 shrink-0">
          <button className="btn text-xs py-0.5 px-2" onClick={onEdit}>Edit</button>
          <button
            className="btn text-xs py-0.5 px-2 text-red-400 hover:text-red-300"
            onClick={onDelete}
          >Delete</button>
        </div>
      </div>

      <div className="grid grid-cols-1 gap-3 sm:grid-cols-3 mt-3 text-xs">
        <div>
          <div className="text-[10px] uppercase tracking-wider text-text-2 font-medium mb-1">Sources</div>
          {usedSources.length === 0
            ? <span className="text-text-3">none</span>
            : usedSources.map(s => (
              <div key={s.name} className="flex items-center gap-1 py-0.5">
                <span className={`w-1.5 h-1.5 rounded-full shrink-0 ${s.enabled ? (s.circuit_open ? 'bg-yellow-400' : 'bg-accent2') : 'bg-bg-3'}`} />
                <span className="text-text">{s.display_name}</span>
              </div>
            ))
          }
        </div>

        <div>
          <div className="text-[10px] uppercase tracking-wider text-text-2 font-medium mb-1">Target graphs</div>
          {c.graph_ids.length === 0
            ? <span className="text-text-3">none</span>
            : <span className="text-text">{graphNames}</span>
          }
        </div>

        <div>
          <div className="text-[10px] uppercase tracking-wider text-text-2 font-medium mb-1">Stats</div>
          {statRows.map(({ label, value, accent }) => (
            <div key={label} className="flex justify-between gap-2 py-0.5">
              <span className="text-text-2">{label}</span>
              <span className={`font-mono ${accent ? 'text-accent' : 'text-text'}`}>{value}</span>
            </div>
          ))}
        </div>
      </div>

      {/* Mapping section */}
      <div className="mt-3 border-t border-border/40 pt-3">
        <button
          onClick={() => setMappingOpen(v => !v)}
          className="flex items-center gap-1.5 text-[10px] text-text-2 hover:text-text transition-colors"
        >
          <span className={`transition-transform ${mappingOpen ? 'rotate-90' : ''}`}>▶</span>
          Field mapping
          {hasMappingOps && !mappingOpen && (
            <span className="text-text-3">— {(() => { try { return (JSON.parse(c.mapping_code) as MappingSpec).operations?.length } catch { return 0 } })()} operations</span>
          )}
        </button>

        {mappingOpen && (
          <div className="mt-2">
            <div className="flex gap-0.5 bg-bg-2 rounded border border-border p-0.5 w-fit mb-2">
              <button
                onClick={() => setMappingTab('visual')}
                className={`text-[10px] px-2 py-0.5 rounded transition-colors ${mappingTab === 'visual' ? 'bg-bg-3 text-text' : 'text-text-2 hover:text-text'}`}
              >
                Visual
              </button>
              <button
                onClick={() => setMappingTab('code')}
                className={`text-[10px] px-2 py-0.5 rounded transition-colors ${mappingTab === 'code' ? 'bg-bg-3 text-text' : 'text-text-2 hover:text-text'}`}
              >
                Code
              </button>
            </div>

            {mappingTab === 'visual' ? (
              <div className="rounded border border-border bg-bg p-3">
                <MappingVisual code={c.mapping_code} />
              </div>
            ) : (
              <pre className="rounded border border-border bg-bg p-3 text-[11px] font-mono text-text-2 overflow-x-auto whitespace-pre-wrap break-all">
                {(() => { try { return JSON.stringify(JSON.parse(c.mapping_code || '{}'), null, 2) } catch { return c.mapping_code } })()}
              </pre>
            )}
          </div>
        )}
      </div>
    </div>
  )
}

// ── page ───────────────────────────────────────────────────────────────────

type EditingState =
  | { mode: 'none' }
  | { mode: 'create' }
  | { mode: 'edit'; converter: Converter }

export default function IngestionConverters() {
  const [converters, setConverters] = useState<Converter[]>([])
  const [sources, setSources] = useState<Source[]>([])
  const [graphs, setGraphs] = useState<Graph[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [editing, setEditing] = useState<EditingState>({ mode: 'none' })
  const [formError, setFormError] = useState<string | null>(null)
  const [submitting, setSubmitting] = useState(false)

  function load() {
    setLoading(true)
    Promise.all([listConverters(), listSources(), listGraphs()])
      .then(([cv, sr, gr]) => {
        setConverters(cv.converters)
        setSources(sr)
        setGraphs(gr)
      })
      .catch(e => setError(String(e)))
      .finally(() => setLoading(false))
  }

  useEffect(load, [])

  async function handleCreate(form: FormState) {
    setSubmitting(true)
    setFormError(null)
    try {
      const created = await createConverter({
        name: form.name,
        description: form.description,
        content_type: form.content_type,
        sources: form.sources,
        graph_ids: form.graph_ids,
        config: {},
        mapping_code: form.mapping_code,
        enabled: form.enabled,
      })
      setConverters(prev => [...prev, created])
      setEditing({ mode: 'none' })
    } catch (e: unknown) {
      setFormError(String(e))
    } finally {
      setSubmitting(false)
    }
  }

  async function handleUpdate(id: number, form: FormState) {
    setSubmitting(true)
    setFormError(null)
    try {
      const updated = await updateConverter(id, {
        name: form.name,
        description: form.description,
        content_type: form.content_type,
        sources: form.sources,
        graph_ids: form.graph_ids,
        mapping_code: form.mapping_code,
        enabled: form.enabled,
      })
      setConverters(prev => prev.map(c => c.id === id ? { ...updated, stats: c.stats } : c))
      setEditing({ mode: 'none' })
    } catch (e: unknown) {
      setFormError(String(e))
    } finally {
      setSubmitting(false)
    }
  }

  async function handleDelete(c: Converter) {
    if (!confirm(`Delete converter "${c.name}"?`)) return
    try {
      await deleteConverter(c.id)
      setConverters(prev => prev.filter(x => x.id !== c.id))
    } catch (e: unknown) {
      setError(String(e))
    }
  }

  return (
    <div className="max-w-3xl">
      <div className="mb-5 flex items-center justify-between">
        <div>
          <h1 className="text-xl font-semibold text-text">Converters</h1>
          <p className="mt-0.5 text-sm text-text-2">
            Named ingestion pipeline stages — each one normalizes data from its sources into recommendation edges for its target graphs.
          </p>
        </div>
        <div className="flex gap-2">
          <button onClick={load} className="text-[12px]">Refresh</button>
          {editing.mode === 'none' && (
            <button
              className="btn-primary text-xs"
              onClick={() => { setFormError(null); setEditing({ mode: 'create' }) }}
            >
              + New converter
            </button>
          )}
        </div>
      </div>

      {error && (
        <div className="rounded border border-red-500/30 bg-red-500/10 px-4 py-3 text-sm text-red-300 mb-4">{error}</div>
      )}

      {editing.mode === 'create' && (
        <div className="surface p-4 mb-4">
          <h2 className="text-sm font-medium text-text mb-3">New converter</h2>
          <ConverterForm
            initial={EMPTY_FORM}
            availableSources={sources}
            availableGraphs={graphs}
            submitting={submitting}
            error={formError}
            submitLabel="Create"
            onSubmit={handleCreate}
            onCancel={() => setEditing({ mode: 'none' })}
          />
        </div>
      )}

      {loading ? (
        <div className="text-text-2 text-sm">Loading…</div>
      ) : converters.length === 0 ? (
        <div className="text-text-2 text-sm">No converters yet. Create one to define an ingestion pipeline stage.</div>
      ) : (
        <div className="space-y-3">
          {converters.map(c => (
            editing.mode === 'edit' && editing.converter.id === c.id ? (
              <div key={c.id} className="surface p-4">
                <h2 className="text-sm font-medium text-text mb-3">Edit "{c.name}"</h2>
                <ConverterForm
                  initial={converterToForm(c)}
                  availableSources={sources}
                  availableGraphs={graphs}
                  submitting={submitting}
                  error={formError}
                  submitLabel="Save changes"
                  onSubmit={form => handleUpdate(c.id, form)}
                  onCancel={() => setEditing({ mode: 'none' })}
                />
              </div>
            ) : (
              <ConverterCard
                key={c.id}
                converter={c}
                sources={sources}
                graphs={graphs}
                onEdit={() => { setFormError(null); setEditing({ mode: 'edit', converter: c }) }}
                onDelete={() => handleDelete(c)}
              />
            )
          ))}
        </div>
      )}

      <div className="mt-6 text-xs text-text-3">
        Feed endpoints: <code className="bg-bg-3 px-1 rounded">POST /v1/feed/videos</code>,{' '}
        <code className="bg-bg-3 px-1 rounded">/v1/feed/songs</code>,{' '}
        <code className="bg-bg-3 px-1 rounded">/v1/feed/albums</code>,{' '}
        <code className="bg-bg-3 px-1 rounded">/v1/feed/artists</code>
      </div>
    </div>
  )
}
