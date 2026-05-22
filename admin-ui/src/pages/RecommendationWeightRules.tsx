import { useEffect, useState } from 'react'
import { listWeightRules, addWeightRule, deleteWeightRule } from '../lib/api'
import type { WeightRule } from '../lib/types'

const RULE_TYPES = ['keyword', 'channel_id', 'channel_name', 'genre', 'category', 'attribute']

export default function RecommendationWeightRules() {
  const [rules, setRules] = useState<WeightRule[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [form, setForm] = useState({ rule_type: 'keyword', match_value: '', multiplier: '2' })
  const [submitting, setSubmitting] = useState(false)

  const load = () => {
    setLoading(true)
    listWeightRules()
      .then(setRules)
      .catch((e) => setError(String(e)))
      .finally(() => setLoading(false))
  }

  useEffect(load, [])

  const handleAdd = async (e: React.FormEvent) => {
    e.preventDefault()
    const mult = parseFloat(form.multiplier)
    if (!form.match_value.trim() || isNaN(mult) || mult <= 0) {
      setError('match_value required and multiplier must be > 0')
      return
    }
    setSubmitting(true)
    setError(null)
    try {
      await addWeightRule({ rule_type: form.rule_type, match_value: form.match_value.trim(), multiplier: mult })
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
      await deleteWeightRule(id)
      setRules((r) => r.filter((x) => x.id !== id))
    } catch (e) {
      setError(String(e))
    }
  }

  return (
    <div className="max-w-2xl">
      <h1 className="page-title mb-4">Weight Rules</h1>
      <p className="text-xs text-text-2 mb-4">
        Boost or suppress recommendations by keyword, channel, genre, category, or attribute. Multiplier &gt; 1 boosts, &lt; 1 suppresses. Invalidates feed cache on change.
      </p>

      <form onSubmit={handleAdd} className="mb-5 flex flex-wrap gap-2 items-end">
        <div>
          <label className="label">Type</label>
          <select
            className="input text-xs"
            value={form.rule_type}
            onChange={(e) => setForm((f) => ({ ...f, rule_type: e.target.value }))}
          >
            {RULE_TYPES.map((t) => <option key={t}>{t}</option>)}
          </select>
        </div>
        <div className="flex-1 min-w-32">
          <label className="label">Match value</label>
          <input
            className="input w-full"
            placeholder="e.g. cooking, UCxxxxxx"
            value={form.match_value}
            onChange={(e) => setForm((f) => ({ ...f, match_value: e.target.value }))}
          />
        </div>
        <div className="w-24">
          <label className="label">Multiplier</label>
          <input
            className="input w-full"
            type="number"
            min="0.01"
            step="0.1"
            value={form.multiplier}
            onChange={(e) => setForm((f) => ({ ...f, multiplier: e.target.value }))}
          />
        </div>
        <button className="btn-primary" type="submit" disabled={submitting}>Add</button>
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
                <th className="th text-right">Multiplier</th>
                <th className="th"></th>
              </tr>
            </thead>
            <tbody>
              {rules.map((r) => (
                <tr key={r.id} className="tr">
                  <td className="td font-mono text-text-2">{r.rule_type}</td>
                  <td className="td font-medium text-text">{r.match_value}</td>
                  <td className="td text-right font-mono">
                    <span className={r.multiplier > 1 ? 'text-green-500' : r.multiplier < 1 ? 'text-red-400' : ''}>
                      ×{r.multiplier}
                    </span>
                  </td>
                  <td className="td text-right">
                    <button className="btn text-xs py-0.5 px-2 text-red-400 hover:text-red-600" onClick={() => handleDelete(r.id)}>
                      Remove
                    </button>
                  </td>
                </tr>
              ))}
              {rules.length === 0 && (
                <tr><td colSpan={4} className="td text-center text-text-2">No rules yet.</td></tr>
              )}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}
