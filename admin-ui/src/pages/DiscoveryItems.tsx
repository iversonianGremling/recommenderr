import { useEffect, useState } from 'react'
import type { Item, Scheme } from '../lib/types'
import { listItems, listSchemes } from '../lib/api'
import SchemeAwareTable from '../components/SchemeAwareTable'

const PAGE = 50

export default function DiscoveryItems() {
  const [schemes, setSchemes] = useState<Scheme[]>([])
  const [selectedScheme, setSelectedScheme] = useState<string>('')
  const [q, setQ] = useState('')
  const [inputQ, setInputQ] = useState('')
  const [items, setItems] = useState<Item[]>([])
  const [loading, setLoading] = useState(false)
  const [offset, setOffset] = useState(0)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    listSchemes().then(s => {
      setSchemes(s)
      if (s.length > 0) setSelectedScheme(s[0].name)
    }).catch(e => setError(String(e)))
  }, [])

  useEffect(() => {
    if (!selectedScheme) return
    setLoading(true)
    setError(null)
    listItems({ scheme: selectedScheme, q: q || undefined, limit: PAGE, offset })
      .then(setItems)
      .catch(e => setError(String(e)))
      .finally(() => setLoading(false))
  }, [selectedScheme, q, offset])

  const scheme = schemes.find(s => s.name === selectedScheme)

  return (
    <div>
      <div className="mb-4 flex items-center gap-3 flex-wrap">
        <h1 className="text-xl font-semibold text-text mr-2">Items</h1>
        <select
          value={selectedScheme}
          onChange={e => { setSelectedScheme(e.target.value); setOffset(0) }}
          className="text-sm"
        >
          {schemes.map(s => <option key={s.name} value={s.name}>{s.display_name}</option>)}
        </select>
        <form
          onSubmit={e => { e.preventDefault(); setQ(inputQ); setOffset(0) }}
          className="flex items-center gap-2"
        >
          <input
            value={inputQ}
            onChange={e => setInputQ(e.target.value)}
            placeholder="Search…"
            className="text-sm w-48"
          />
          <button type="submit" className="text-[12px]">Search</button>
          {q && <button type="button" onClick={() => { setQ(''); setInputQ('') }} className="text-[12px]">Clear</button>}
        </form>
      </div>

      {error && <div className="rounded border border-red-500/30 bg-red-500/10 px-4 py-3 text-sm text-red-300 mb-3">{error}</div>}

      <div className="surface">
        {loading
          ? <div className="py-8 text-center text-text-2">Loading…</div>
          : scheme
            ? <SchemeAwareTable scheme={scheme} items={items} />
            : null}
      </div>

      {/* Pagination */}
      <div className="mt-3 flex items-center gap-3 text-[13px] text-text-2">
        <button onClick={() => setOffset(o => Math.max(0, o - PAGE))} disabled={offset === 0} className="text-[12px]">← Prev</button>
        <span>Showing {offset + 1}–{offset + items.length}</span>
        <button onClick={() => setOffset(o => o + PAGE)} disabled={items.length < PAGE} className="text-[12px]">Next →</button>
      </div>
    </div>
  )
}
