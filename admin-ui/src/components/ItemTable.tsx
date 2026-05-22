import { useState, useMemo, useEffect, useRef } from 'react'

// ── Normalized item shape ─────────────────────────────────────────────────────

export interface NormalizedItem {
  id: string
  scheme?: string
  item_id?: number
  title?: string | null
  author?: string | null
  author_id?: string | null
  thumbnail?: string | null
  duration?: number | null
  score?: number | null
  effective_score?: number | null
  spam_mass?: number | null
  effective_rating?: number | null
  category?: string | null
  published_at?: number | null
  added_at?: number | null
  computed_at?: number | null
  weight?: number | null
  reasons?: string[] | null
  source?: string | null
  _raw: Record<string, unknown>
}

// eslint-disable-next-line @typescript-eslint/no-explicit-any
export function normalizeItem(raw: Record<string, any>): NormalizedItem {
  const meta = (raw.metadata as Record<string, unknown> | null) ?? {}
  return {
    id: String(raw.video_id ?? raw.external_id ?? raw.id ?? ''),
    scheme: raw.scheme as string | undefined,
    item_id: raw.item_id as number | undefined,
    title: (raw.title ?? meta.title ?? null) as string | null,
    author: (raw.author ?? meta.author ?? null) as string | null,
    author_id: (raw.author_id ?? meta.author_id ?? null) as string | null,
    thumbnail: (raw.thumbnail ?? meta.thumbnail ?? null) as string | null,
    duration: (raw.duration ?? meta.duration ?? null) as number | null,
    score: (raw.ppr_score ?? raw.score ?? null) as number | null,
    effective_score: (raw.effective_ppr_score ?? null) as number | null,
    spam_mass: (raw.spam_mass ?? null) as number | null,
    effective_rating: (raw.effective_rating ?? null) as number | null,
    category: (raw.category ?? null) as string | null,
    published_at: (raw.published_at ?? (meta.published_at ?? null)) as number | null,
    added_at: (raw.added_at ?? null) as number | null,
    computed_at: (raw.computed_at ?? null) as number | null,
    weight: (raw.weight ?? null) as number | null,
    reasons: (raw.reasons ?? null) as string[] | null,
    source: (raw.source_video_title ?? null) as string | null,
    _raw: raw,
  }
}

// ── Helpers ───────────────────────────────────────────────────────────────────

function fmtDuration(secs: number): string {
  const h = Math.floor(secs / 3600)
  const m = Math.floor((secs % 3600) / 60)
  const s = secs % 60
  if (h > 0) return `${h}:${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`
  return `${m}:${String(s).padStart(2, '0')}`
}

function fmtDate(ts: number): string {
  return new Date(ts * 1000).toLocaleDateString()
}

function fmtDateTime(ts: number): string {
  return new Date(ts * 1000).toLocaleString()
}

// ── Column definitions ────────────────────────────────────────────────────────

export type ColumnKey =
  | 'thumbnail' | 'title' | 'id' | 'author' | 'scheme'
  | 'duration' | 'score' | 'effective_score' | 'spam_mass' | 'effective_rating'
  | 'category' | 'published_at' | 'added_at' | 'computed_at'
  | 'weight' | 'reasons' | 'source'

type SortDir = 'asc' | 'desc'

interface ColDef {
  key: ColumnKey
  label: string
  thClass?: string
  sortVal?: (item: NormalizedItem) => number | string | null | undefined
  render: (item: NormalizedItem) => React.ReactNode
}

const COL_DEFS: ColDef[] = [
  {
    key: 'thumbnail',
    label: 'Thumb',
    thClass: 'w-[68px]',
    render: (i) =>
      i.thumbnail ? (
        <img src={i.thumbnail} alt="" className="h-9 w-[60px] object-cover rounded" />
      ) : (
        <div className="h-9 w-[60px] rounded bg-bg-3 flex items-center justify-center text-text-2 text-[9px]">
          no img
        </div>
      ),
  },
  {
    key: 'title',
    label: 'Title / Channel',
    sortVal: (i) => (i.title ?? i.id).toLowerCase(),
    render: (i) => (
      <div className="min-w-0">
        <p className="font-medium text-text leading-snug" style={{ maxWidth: '28rem', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
          {i.title ?? <span className="font-mono text-text-2 text-[10px]">{i.id}</span>}
        </p>
        {i.author && (
          <p className="text-text-2 text-[10px]" style={{ maxWidth: '28rem', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
            {i.author}
          </p>
        )}
      </div>
    ),
  },
  {
    key: 'id',
    label: 'ID',
    sortVal: (i) => i.id,
    render: (i) => (
      <code className="font-mono text-[10px] text-text-2 select-all whitespace-nowrap">{i.id}</code>
    ),
  },
  {
    key: 'author',
    label: 'Channel',
    sortVal: (i) => (i.author ?? '').toLowerCase(),
    render: (i) =>
      i.author ? (
        <span className="block text-text-2" style={{ maxWidth: '14rem', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
          {i.author}
        </span>
      ) : (
        <span className="text-text-2">—</span>
      ),
  },
  {
    key: 'scheme',
    label: 'Scheme',
    sortVal: (i) => i.scheme ?? '',
    render: (i) =>
      i.scheme ? (
        <code className="font-mono text-[10px]">{i.scheme}</code>
      ) : (
        <span className="text-text-2">—</span>
      ),
  },
  {
    key: 'duration',
    label: 'Duration',
    thClass: 'text-right',
    sortVal: (i) => i.duration ?? -1,
    render: (i) => (
      <span className="font-mono block text-right whitespace-nowrap">
        {i.duration != null ? fmtDuration(i.duration) : <span className="text-text-2">—</span>}
      </span>
    ),
  },
  {
    key: 'score',
    label: 'Score',
    thClass: 'text-right',
    sortVal: (i) => i.score ?? -Infinity,
    render: (i) => (
      <span className="font-mono block text-right">
        {i.score != null ? i.score.toFixed(6) : <span className="text-text-2">—</span>}
      </span>
    ),
  },
  {
    key: 'effective_score',
    label: 'Eff. score',
    thClass: 'text-right',
    sortVal: (i) => i.effective_score ?? -Infinity,
    render: (i) => (
      <span className="font-mono block text-right">
        {i.effective_score != null ? i.effective_score.toFixed(6) : <span className="text-text-2">—</span>}
      </span>
    ),
  },
  {
    key: 'spam_mass',
    label: 'Spam',
    thClass: 'text-right',
    sortVal: (i) => i.spam_mass ?? -1,
    render: (i) => (
      <span className="font-mono block text-right">
        {i.spam_mass != null ? i.spam_mass.toFixed(4) : <span className="text-text-2">—</span>}
      </span>
    ),
  },
  {
    key: 'effective_rating',
    label: 'Rating',
    thClass: 'text-right',
    sortVal: (i) => i.effective_rating ?? -1,
    render: (i) => (
      <span className="block text-right">
        {i.effective_rating != null ? String(i.effective_rating) : <span className="text-text-2">—</span>}
      </span>
    ),
  },
  {
    key: 'category',
    label: 'Category',
    sortVal: (i) => i.category ?? '',
    render: (i) =>
      i.category ? (
        <span className="text-text-2" style={{ maxWidth: '10rem', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', display: 'block' }}>
          {i.category}
        </span>
      ) : (
        <span className="text-text-2">—</span>
      ),
  },
  {
    key: 'published_at',
    label: 'Published',
    thClass: 'text-right',
    sortVal: (i) => i.published_at ?? 0,
    render: (i) => (
      <span
        title={i.published_at ? fmtDateTime(i.published_at) : ''}
        className="text-text-2 block text-right whitespace-nowrap"
      >
        {i.published_at ? fmtDate(i.published_at) : '—'}
      </span>
    ),
  },
  {
    key: 'added_at',
    label: 'Added',
    thClass: 'text-right',
    sortVal: (i) => i.added_at ?? 0,
    render: (i) => (
      <span
        title={i.added_at ? fmtDateTime(i.added_at) : ''}
        className="text-text-2 block text-right whitespace-nowrap"
      >
        {i.added_at ? fmtDate(i.added_at) : '—'}
      </span>
    ),
  },
  {
    key: 'computed_at',
    label: 'Computed',
    thClass: 'text-right',
    sortVal: (i) => i.computed_at ?? 0,
    render: (i) => (
      <span
        title={i.computed_at ? fmtDateTime(i.computed_at) : ''}
        className="text-text-2 block text-right whitespace-nowrap"
      >
        {i.computed_at ? fmtDate(i.computed_at) : '—'}
      </span>
    ),
  },
  {
    key: 'weight',
    label: 'Weight',
    thClass: 'text-right',
    sortVal: (i) => i.weight ?? -1,
    render: (i) => (
      <span className="font-mono text-accent block text-right">
        {i.weight != null ? `×${i.weight.toFixed(2)}` : <span className="text-text-2">—</span>}
      </span>
    ),
  },
  {
    key: 'reasons',
    label: 'Reasons',
    render: (i) => (
      <span
        className="text-text-2"
        style={{ maxWidth: '20rem', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', display: 'block' }}
      >
        {i.reasons?.length ? i.reasons.join(', ') : '—'}
      </span>
    ),
  },
  {
    key: 'source',
    label: 'Source video',
    sortVal: (i) => (i.source ?? '').toLowerCase(),
    render: (i) => (
      <span
        className="text-text-2 text-[10px]"
        style={{ maxWidth: '20rem', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', display: 'block' }}
      >
        {i.source ?? '—'}
      </span>
    ),
  },
]

const COL_MAP = Object.fromEntries(COL_DEFS.map((c) => [c.key, c])) as Record<ColumnKey, ColDef>

// ── ItemTable ─────────────────────────────────────────────────────────────────

export interface ItemTableProps {
  items: NormalizedItem[]
  defaultColumns: ColumnKey[]
  storageKey: string
  actions?: (item: NormalizedItem) => React.ReactNode
  emptyMessage?: string
  withGrid?: boolean
  toolbar?: React.ReactNode
}

export function ItemTable({
  items,
  defaultColumns,
  storageKey,
  actions,
  emptyMessage = 'No items.',
  withGrid = false,
  toolbar: extraToolbar,
}: ItemTableProps) {
  const storedColsKey = `itemtable:${storageKey}:cols`
  const storedViewKey = `itemtable:${storageKey}:view`

  const [visibleCols, setVisibleCols] = useState<ColumnKey[]>(() => {
    try {
      const s = localStorage.getItem(storedColsKey)
      if (s) return JSON.parse(s) as ColumnKey[]
    } catch { /* ignore */ }
    return defaultColumns
  })

  const [viewMode, setViewMode] = useState<'table' | 'grid'>(() => {
    if (!withGrid) return 'table'
    return (localStorage.getItem(storedViewKey) as 'table' | 'grid') ?? 'grid'
  })

  const [showColPicker, setShowColPicker] = useState(false)
  const [filter, setFilter] = useState('')
  const [sortCol, setSortCol] = useState<ColumnKey | null>(null)
  const [sortDir, setSortDir] = useState<SortDir>('desc')
  const pickerRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    localStorage.setItem(storedColsKey, JSON.stringify(visibleCols))
  }, [visibleCols, storedColsKey])

  useEffect(() => {
    if (withGrid) localStorage.setItem(storedViewKey, viewMode)
  }, [viewMode, storedViewKey, withGrid])

  // Close column picker on outside click
  useEffect(() => {
    if (!showColPicker) return
    const handler = (e: MouseEvent) => {
      if (pickerRef.current && !pickerRef.current.contains(e.target as Node)) {
        setShowColPicker(false)
      }
    }
    document.addEventListener('mousedown', handler)
    return () => document.removeEventListener('mousedown', handler)
  }, [showColPicker])

  const handleSortCol = (key: ColumnKey) => {
    if (sortCol === key) {
      setSortDir((d) => (d === 'asc' ? 'desc' : 'asc'))
    } else {
      setSortCol(key)
      setSortDir('desc')
    }
  }

  const toggleCol = (key: ColumnKey) => {
    setVisibleCols((cols) =>
      cols.includes(key) ? cols.filter((c) => c !== key) : [...cols, key]
    )
  }

  const filtered = useMemo(() => {
    const q = filter.trim().toLowerCase()
    if (!q) return items
    return items.filter((i) => {
      const haystack = [i.title, i.id, i.author, i.scheme, i.category, i.source, i.reasons?.join(' ')]
        .filter(Boolean)
        .join(' ')
        .toLowerCase()
      return haystack.includes(q)
    })
  }, [items, filter])

  const sorted = useMemo(() => {
    if (!sortCol) return filtered
    const def = COL_MAP[sortCol]
    if (!def?.sortVal) return filtered
    return [...filtered].sort((a, b) => {
      const va = def.sortVal!(a) ?? ''
      const vb = def.sortVal!(b) ?? ''
      const cmp =
        typeof va === 'number' && typeof vb === 'number'
          ? va - vb
          : String(va).localeCompare(String(vb))
      return sortDir === 'asc' ? cmp : -cmp
    })
  }, [filtered, sortCol, sortDir])

  const activeCols = visibleCols.filter((k) => COL_MAP[k]).map((k) => COL_MAP[k])

  const toolbar = (
    <div className="flex items-center gap-2 mb-2 flex-wrap">
      <input
        className="input text-xs py-1 w-48"
        placeholder="Filter…"
        value={filter}
        onChange={(e) => setFilter(e.target.value)}
      />
      {extraToolbar}
      <div className="ml-auto flex items-center gap-2">
        <span className="text-[10px] text-text-2">
          {filter && sorted.length !== items.length
            ? `${sorted.length} / ${items.length}`
            : `${items.length} items`}
        </span>
        <div className="relative" ref={pickerRef}>
          <button
            className="btn text-xs py-1 px-2"
            onClick={() => setShowColPicker((v) => !v)}
          >
            Columns ▾
          </button>
          {showColPicker && (
            <div className="absolute right-0 top-full mt-1 z-50 rounded border border-border bg-bg shadow-xl p-3 w-60">
              <p className="text-[10px] font-semibold text-text-2 uppercase mb-2">Visible columns</p>
              <div className="grid grid-cols-2 gap-x-4 gap-y-1.5">
                {COL_DEFS.map((col) => (
                  <label key={col.key} className="flex items-center gap-1.5 cursor-pointer">
                    <input
                      type="checkbox"
                      checked={visibleCols.includes(col.key)}
                      onChange={() => toggleCol(col.key)}
                      className="accent-accent"
                    />
                    <span className="text-[11px] text-text">{col.label}</span>
                  </label>
                ))}
              </div>
              <div className="mt-3 border-t border-border pt-2 flex gap-3">
                <button
                  className="text-[10px] text-text-2 hover:text-text"
                  onClick={() => setVisibleCols(defaultColumns)}
                >
                  Reset defaults
                </button>
                <button
                  className="text-[10px] text-text-2 hover:text-text"
                  onClick={() => setVisibleCols(COL_DEFS.map((c) => c.key))}
                >
                  Show all
                </button>
              </div>
            </div>
          )}
        </div>
        {withGrid && (
          <div className="flex rounded border border-border overflow-hidden">
            {(['grid', 'table'] as const).map((m) => (
              <button
                key={m}
                title={m === 'grid' ? 'Grid view' : 'Table view'}
                className={`px-2.5 py-1 text-sm leading-none ${viewMode === m ? 'bg-accent text-white' : 'text-text-2 hover:text-text'}`}
                onClick={() => setViewMode(m)}
              >
                {m === 'grid' ? '⊞' : '≡'}
              </button>
            ))}
          </div>
        )}
      </div>
    </div>
  )

  if (items.length === 0) {
    return (
      <>
        {toolbar}
        <p className="text-text-2 text-sm">{emptyMessage}</p>
      </>
    )
  }

  if (viewMode === 'grid') {
    return (
      <>
        {toolbar}
        <div className="grid gap-3" style={{ gridTemplateColumns: 'repeat(auto-fill, minmax(180px, 1fr))' }}>
          {sorted.map((item) => (
            <GridCard key={`${item.id}-${item.item_id ?? ''}`} item={item} actions={actions} />
          ))}
          {sorted.length === 0 && filter && (
            <p className="text-text-2 text-sm col-span-full">No results for "{filter}"</p>
          )}
        </div>
      </>
    )
  }

  return (
    <>
      {toolbar}
      <div className="overflow-auto rounded border border-border">
        <table className="w-full text-xs">
          <thead className="bg-bg-2">
            <tr>
              {activeCols.map((col) => (
                <th
                  key={col.key}
                  className={`th select-none ${col.thClass ?? ''} ${col.sortVal ? 'cursor-pointer hover:text-text' : ''}`}
                  onClick={() => col.sortVal && handleSortCol(col.key)}
                >
                  {col.label}
                  {sortCol === col.key && (
                    <span className="ml-1 text-accent">{sortDir === 'asc' ? '↑' : '↓'}</span>
                  )}
                </th>
              ))}
              {actions && <th className="th w-24"></th>}
            </tr>
          </thead>
          <tbody>
            {sorted.map((item) => (
              <tr key={`${item.id}-${item.item_id ?? ''}`} className="tr">
                {activeCols.map((col) => (
                  <td key={col.key} className="td align-middle">
                    {col.render(item)}
                  </td>
                ))}
                {actions && (
                  <td className="td text-right align-middle">{actions(item)}</td>
                )}
              </tr>
            ))}
            {sorted.length === 0 && filter && (
              <tr>
                <td
                  colSpan={activeCols.length + (actions ? 1 : 0)}
                  className="td text-center text-text-2"
                >
                  No results for "{filter}"
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
    </>
  )
}

// ── Grid card ─────────────────────────────────────────────────────────────────

function GridCard({
  item,
  actions,
}: {
  item: NormalizedItem
  actions?: (i: NormalizedItem) => React.ReactNode
}) {
  return (
    <div className="rounded border border-border bg-bg-2 overflow-hidden flex flex-col">
      {item.thumbnail ? (
        <img src={item.thumbnail} alt="" className="w-full aspect-video object-cover" />
      ) : (
        <div className="w-full aspect-video bg-bg-3 flex items-center justify-center text-text-2 text-[10px]">
          no img
        </div>
      )}
      <div className="p-2 flex-1 flex flex-col gap-0.5">
        <p className="text-xs font-medium text-text leading-snug line-clamp-2">
          {item.title ?? <span className="font-mono text-[10px] text-text-2">{item.id}</span>}
        </p>
        {item.author && <p className="text-[10px] text-text-2 truncate">{item.author}</p>}
        <div className="mt-auto flex items-center justify-between text-[10px] text-text-2 pt-1 gap-1 flex-wrap">
          {item.score != null && <span className="font-mono">{item.score.toFixed(5)}</span>}
          {item.duration != null && <span>{fmtDuration(item.duration)}</span>}
        </div>
        {actions && <div className="mt-1.5">{actions(item)}</div>}
      </div>
    </div>
  )
}
