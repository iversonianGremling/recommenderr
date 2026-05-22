import { useEffect, useState } from 'react'
import { listFeedFilters, addFeedFilter, deleteFeedFilter } from '../lib/api'
import type { FeedFilter } from '../lib/types'

const FILTER_TYPES = ['channel_id', 'channel_name', 'keyword', 'video_id']

export default function RecommendationFilters() {
  const [filters, setFilters] = useState<FeedFilter[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [form, setForm] = useState({ filter_type: 'channel_id', match_value: '' })
  const [submitting, setSubmitting] = useState(false)

  const load = () => {
    setLoading(true)
    listFeedFilters()
      .then(setFilters)
      .catch((e) => setError(String(e)))
      .finally(() => setLoading(false))
  }

  useEffect(load, [])

  const handleAdd = async (e: React.FormEvent) => {
    e.preventDefault()
    if (!form.match_value.trim()) {
      setError('match_value required')
      return
    }
    setSubmitting(true)
    setError(null)
    try {
      await addFeedFilter({ filter_type: form.filter_type, match_value: form.match_value.trim() })
      setForm((f) => ({ ...f, match_value: '' }))
      load()
    } catch (e) {
      setError(String(e))
    } finally {
      setSubmitting(false)
    }
  }

  const handleDelete = async (id: number) => {
    try {
      await deleteFeedFilter(id)
      setFilters((f) => f.filter((x) => x.id !== id))
    } catch (e) {
      setError(String(e))
    }
  }

  return (
    <div className="max-w-2xl">
      <h1 className="page-title mb-4">Feed Filters</h1>
      <p className="text-xs text-text-2 mb-4">
        Permanently block channels, keywords, or individual videos from the feed. Invalidates feed cache on change.
      </p>

      <form onSubmit={handleAdd} className="mb-5 flex flex-wrap gap-2 items-end">
        <div>
          <label className="label">Type</label>
          <select
            className="input text-xs"
            value={form.filter_type}
            onChange={(e) => setForm((f) => ({ ...f, filter_type: e.target.value }))}
          >
            {FILTER_TYPES.map((t) => <option key={t}>{t}</option>)}
          </select>
        </div>
        <div className="flex-1 min-w-48">
          <label className="label">Match value</label>
          <input
            className="input w-full"
            placeholder="channel ID, keyword, or video ID"
            value={form.match_value}
            onChange={(e) => setForm((f) => ({ ...f, match_value: e.target.value }))}
          />
        </div>
        <button className="btn-primary" type="submit" disabled={submitting}>Block</button>
      </form>

      {error && <p className="text-red-500 text-sm mb-3">{error}</p>}
      {loading && <p className="text-text-2 text-sm">Loading…</p>}

      {!loading && (
        <div className="rounded border border-border overflow-auto">
          <table className="w-full text-xs">
            <thead className="bg-bg-2">
              <tr>
                <th className="th text-left">Type</th>
                <th className="th text-left">Match</th>
                <th className="th"></th>
              </tr>
            </thead>
            <tbody>
              {filters.map((f) => (
                <tr key={f.id} className="tr">
                  <td className="td font-mono text-text-2">{f.filter_type}</td>
                  <td className="td font-medium text-text">{f.match_value}</td>
                  <td className="td text-right">
                    <button className="btn text-xs py-0.5 px-2 text-red-400 hover:text-red-600" onClick={() => handleDelete(f.id)}>
                      Remove
                    </button>
                  </td>
                </tr>
              ))}
              {filters.length === 0 && (
                <tr><td colSpan={3} className="td text-center text-text-2">No filters — all channels/videos visible.</td></tr>
              )}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}
