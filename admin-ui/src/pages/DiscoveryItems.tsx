import { useEffect, useState } from 'react'
import type { Item, Scheme } from '../lib/types'
import { listItems, listSchemes } from '../lib/api'
import { ItemTable, normalizeItem } from '../components/ItemTable'

const PAGE = 100

export default function DiscoveryItems() {
  const [schemes, setSchemes] = useState<Scheme[]>([])
  const [selectedScheme, setSelectedScheme] = useState<string>('')
  const [serverQ, setServerQ] = useState('')
  const [inputQ, setInputQ] = useState('')
  const [items, setItems] = useState<Item[]>([])
  const [loading, setLoading] = useState(false)
  const [offset, setOffset] = useState(0)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    listSchemes()
      .then((s) => {
        setSchemes(s)
        if (s.length > 0) setSelectedScheme(s[0].name)
      })
      .catch((e) => setError(String(e)))
  }, [])

  useEffect(() => {
    if (!selectedScheme) return
    setLoading(true)
    setError(null)
    listItems({ scheme: selectedScheme, q: serverQ || undefined, limit: PAGE, offset })
      .then(setItems)
      .catch((e) => setError(String(e)))
      .finally(() => setLoading(false))
  }, [selectedScheme, serverQ, offset])

  const normalized = items.map((i) => normalizeItem(i as unknown as Record<string, unknown>))

  const pageToolbar = (
    <div className="flex items-center gap-2 flex-wrap">
      <select
        value={selectedScheme}
        onChange={(e) => { setSelectedScheme(e.target.value); setOffset(0) }}
        className="input text-xs py-1"
      >
        {schemes.map((s) => (
          <option key={s.name} value={s.name}>{s.display_name}</option>
        ))}
      </select>
      <form
        className="flex items-center gap-1"
        onSubmit={(e) => { e.preventDefault(); setServerQ(inputQ); setOffset(0) }}
      >
        <input
          value={inputQ}
          onChange={(e) => setInputQ(e.target.value)}
          placeholder="Server search…"
          className="input text-xs py-1 w-40"
        />
        <button type="submit" className="btn text-xs py-1 px-2">Go</button>
        {serverQ && (
          <button
            type="button"
            className="btn text-xs py-1 px-2"
            onClick={() => { setServerQ(''); setInputQ(''); setOffset(0) }}
          >
            Clear
          </button>
        )}
      </form>
      <div className="flex items-center gap-1">
        <button
          className="btn text-xs py-1 px-2"
          disabled={offset === 0}
          onClick={() => setOffset((o) => Math.max(0, o - PAGE))}
        >
          ←
        </button>
        <span className="text-[10px] text-text-2 whitespace-nowrap">
          {offset + 1}–{offset + items.length}
        </span>
        <button
          className="btn text-xs py-1 px-2"
          disabled={items.length < PAGE}
          onClick={() => setOffset((o) => o + PAGE)}
        >
          →
        </button>
      </div>
    </div>
  )

  return (
    <div>
      <h1 className="page-title mb-4">Items</h1>

      {error && (
        <div className="rounded border border-red-500/30 bg-red-500/10 px-4 py-3 text-sm text-red-300 mb-3">
          {error}
        </div>
      )}
      {loading && <p className="text-text-2 text-sm mb-2">Loading…</p>}

      <ItemTable
        items={normalized}
        defaultColumns={['thumbnail', 'title', 'id', 'scheme', 'duration', 'added_at']}
        storageKey="discovery-items"
        toolbar={pageToolbar}
        emptyMessage="No items found."
      />
    </div>
  )
}
