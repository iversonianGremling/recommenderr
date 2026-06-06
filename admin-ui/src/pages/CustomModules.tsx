import { useEffect, useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { listModules, createModule, deleteModule, type CustomModule } from '../lib/api'

export default function CustomModules() {
  const navigate = useNavigate()
  const [modules, setModules] = useState<CustomModule[]>([])
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [creating, setCreating] = useState(false)
  const [newName, setNewName] = useState('')
  const [newType, setNewType] = useState<'scorer' | 'filter'>('scorer')

  const load = () => {
    setLoading(true)
    listModules()
      .then(setModules)
      .catch((e) => setError(String(e)))
      .finally(() => setLoading(false))
  }

  useEffect(() => { load() }, [])

  const handleCreate = async () => {
    if (!newName.trim()) return
    setCreating(true)
    setError(null)
    try {
      await createModule({ name: newName.trim(), type: newType })
      setNewName('')
      load()
    } catch (e) {
      setError(String(e))
    } finally {
      setCreating(false)
    }
  }

  const handleDelete = async (m: CustomModule) => {
    if (!confirm(`Delete module "${m.name}"?`)) return
    await deleteModule(m.id)
    load()
  }

  const scorers = modules.filter((m) => m.type === 'scorer')
  const filters = modules.filter((m) => m.type === 'filter')

  return (
    <div>
      <button className="text-text-2 hover:text-text text-sm mb-3" onClick={() => navigate('/pipeline')}>← Pipeline</button>
      <h1 className="page-title mb-1">Custom Modules</h1>
      <p className="text-text-2 text-xs mb-4">
        Sandboxed Python that runs in the scoring pipeline. A <span className="font-mono">scorer</span> assigns
        each candidate a number; a <span className="font-mono">filter</span> drops or reorders candidates.
      </p>

      {error && <p className="text-red-500 text-sm mb-3">{error}</p>}

      {/* Create form */}
      <div className="flex items-center gap-2 mb-6">
        <input
          className="input text-sm py-1 w-48"
          placeholder="Module name"
          value={newName}
          onChange={(e) => setNewName(e.target.value)}
          onKeyDown={(e) => e.key === 'Enter' && handleCreate()}
        />
        {/* Type toggle (was a dropdown) */}
        <div className="inline-flex rounded border border-border overflow-hidden text-sm">
          {(['scorer', 'filter'] as const).map((t) => (
            <button
              key={t}
              type="button"
              onClick={() => setNewType(t)}
              className={`px-3 py-1 capitalize transition-colors ${
                newType === t ? 'bg-accent text-white' : 'bg-bg-2 text-text-2 hover:text-text'
              }`}
            >
              {t}
            </button>
          ))}
        </div>
        <button className="btn text-sm py-1 px-3" onClick={handleCreate} disabled={creating || !newName.trim()}>
          {creating ? 'Creating…' : 'Create'}
        </button>
      </div>

      {loading && <p className="text-text-2 text-sm">Loading…</p>}

      {!loading && (
        <div className="space-y-6">
          {[{ label: 'Scorers', items: scorers }, { label: 'Filters', items: filters }].map(({ label, items }) => (
            <div key={label}>
              <div className="text-xs uppercase tracking-wider text-text-2 font-medium mb-2">{label}</div>
              {items.length === 0 ? (
                <p className="text-text-2 text-sm">None yet.</p>
              ) : (
                <div className="space-y-1">
                  {items.map((m) => (
                    <div key={m.id} className="flex items-center gap-3 rounded border border-border bg-bg-2 px-3 py-2">
                      <span className={`h-1.5 w-1.5 rounded-full flex-shrink-0 ${m.enabled ? 'bg-green-500' : 'bg-text-2'}`} />
                      <span className="flex-1 text-sm text-text font-medium">{m.name}</span>
                      <span className="text-[10px] text-text-2 font-mono">{m.type}</span>
                      <Link to={`/modules/${m.id}`} className="btn text-xs py-0.5 px-2">Edit</Link>
                      <button className="text-xs text-red-400 hover:text-red-300" onClick={() => handleDelete(m)}>Delete</button>
                    </div>
                  ))}
                </div>
              )}
            </div>
          ))}
        </div>
      )}
    </div>
  )
}
