import { useEffect, useRef, useState } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import { getModule, updateModule, testModule, recomputeModule, getModuleScores, type CustomModule } from '../lib/api'
import { ItemTable, normalizeItem } from '../components/ItemTable'

function age(ts: number): string {
  const s = Date.now() / 1000 - ts
  if (s < 60) return `${Math.round(s)}s ago`
  if (s < 3600) return `${Math.round(s / 60)}m ago`
  return `${Math.round(s / 3600)}h ago`
}

export default function CustomModuleEdit({ moduleId: propModuleId }: { moduleId?: number } = {}) {
  const { id } = useParams<{ id: string }>()
  const navigate = useNavigate()
  const moduleId = propModuleId ?? Number(id)

  const [module, setModule] = useState<CustomModule | null>(null)
  const [code, setCode] = useState('')
  const [name, setName] = useState('')
  const [enabled, setEnabled] = useState(true)
  const [saving, setSaving] = useState(false)
  const [testing, setTesting] = useState(false)
  const [recomputing, setRecomputing] = useState(false)
  const [testResult, setTestResult] = useState<{ ok: boolean; error?: string; elapsed_seconds: number; results: Record<string, unknown>[] } | null>(null)
  const [scores, setScores] = useState<Array<Record<string, unknown>>>([])
  const [tab, setTab] = useState<'editor' | 'scores'>('editor')
  const [error, setError] = useState<string | null>(null)
  const textareaRef = useRef<HTMLTextAreaElement>(null)

  useEffect(() => {
    getModule(moduleId)
      .then((m) => {
        setModule(m)
        setCode(m.code)
        setName(m.name)
        setEnabled(!!m.enabled)
      })
      .catch((e) => setError(String(e)))
  }, [moduleId])

  const handleSave = async () => {
    setSaving(true)
    setError(null)
    try {
      const updated = await updateModule(moduleId, { name, code, enabled })
      setModule(updated)
    } catch (e) {
      setError(String(e))
    } finally {
      setSaving(false)
    }
  }

  const handleTest = async () => {
    setTesting(true)
    setTestResult(null)
    setError(null)
    try {
      // Save first so the test uses current code
      await updateModule(moduleId, { code })
      const res = await testModule(moduleId, 20)
      setTestResult(res)
    } catch (e) {
      setError(String(e))
    } finally {
      setTesting(false)
    }
  }

  const handleRecompute = async () => {
    setRecomputing(true)
    setError(null)
    try {
      await updateModule(moduleId, { code })
      await recomputeModule(moduleId)
      if (tab === 'scores') {
        const s = await getModuleScores(moduleId, 100)
        setScores(s as unknown as Array<Record<string, unknown>>)
      }
    } catch (e) {
      setError(String(e))
    } finally {
      setRecomputing(false)
    }
  }

  useEffect(() => {
    if (tab === 'scores') {
      getModuleScores(moduleId, 100)
        .then((s) => setScores(s as unknown as Array<Record<string, unknown>>))
        .catch((e) => setError(String(e)))
    }
  }, [tab, moduleId])

  // Tab key in textarea inserts 4 spaces
  const handleTabKey = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === 'Tab') {
      e.preventDefault()
      const ta = textareaRef.current!
      const start = ta.selectionStart
      const end = ta.selectionEnd
      const newCode = code.substring(0, start) + '    ' + code.substring(end)
      setCode(newCode)
      requestAnimationFrame(() => {
        ta.selectionStart = ta.selectionEnd = start + 4
      })
    }
  }

  if (!module && !error) return <p className="text-text-2 text-sm">Loading…</p>
  if (error && !module) return <p className="text-red-500 text-sm">{error}</p>

  const scoreItems = scores.map((s) => normalizeItem(s))
  const entryFn = module?.type === 'scorer' ? 'score(candidates)' : 'filter_items(items)'

  return (
    <div className="flex flex-col gap-4 h-full">
      {/* Header */}
      <div className="flex items-center gap-3">
        <button className="text-text-2 hover:text-text text-sm" onClick={() => navigate('/modules')}>← Modules</button>
        <input
          className="input text-sm py-1 w-48"
          value={name}
          onChange={(e) => setName(e.target.value)}
        />
        <label className="flex items-center gap-1.5 text-xs text-text-2 cursor-pointer select-none">
          <input type="checkbox" checked={enabled} onChange={(e) => setEnabled(e.target.checked)} className="h-3 w-3" />
          Enabled
        </label>
        <span className="text-[10px] text-text-2 rounded border border-border px-1.5 py-0.5">{module?.type}</span>
        <div className="flex-1" />
        <div className="flex items-center gap-2">
          {module?.type === 'scorer' && (
            <button className="btn text-xs py-1 px-3" onClick={handleRecompute} disabled={recomputing}>
              {recomputing ? 'Running…' : 'Recompute'}
            </button>
          )}
          <button className="btn text-xs py-1 px-3" onClick={handleTest} disabled={testing}>
            {testing ? 'Testing…' : 'Test'}
          </button>
          <button className="btn text-xs py-1 px-3 bg-accent text-white hover:bg-accent/80" onClick={handleSave} disabled={saving}>
            {saving ? 'Saving…' : 'Save'}
          </button>
        </div>
      </div>

      {error && <p className="text-red-500 text-xs">{error}</p>}

      {/* Tabs */}
      <div className="flex gap-1 border-b border-border">
        {(['editor', ...(module?.type === 'scorer' ? ['scores'] : [])] as const).map((t) => (
          <button
            key={t}
            onClick={() => setTab(t as 'editor' | 'scores')}
            className={`px-3 py-1.5 text-xs capitalize border-b-2 -mb-px ${tab === t ? 'border-accent text-text' : 'border-transparent text-text-2 hover:text-text'}`}
          >
            {t}
          </button>
        ))}
      </div>

      {tab === 'editor' && (
        <div className="flex gap-4 flex-1 min-h-0">
          {/* Code editor */}
          <div className="flex-1 flex flex-col gap-2 min-h-0">
            <div className="text-[10px] text-text-2">
              Define <code className="font-mono bg-bg px-1 rounded">{entryFn}</code>
              {module?.type === 'scorer' ? ' → dict[video_id, float]' : ' → list'}
            </div>
            <textarea
              ref={textareaRef}
              className="flex-1 font-mono text-xs bg-bg-2 border border-border rounded p-3 resize-none focus:outline-none focus:border-accent text-text leading-relaxed"
              value={code}
              onChange={(e) => setCode(e.target.value)}
              onKeyDown={handleTabKey}
              spellCheck={false}
              style={{ minHeight: '300px' }}
            />
          </div>

          {/* Test output */}
          {testResult && (
            <div className="w-80 flex flex-col gap-2 min-h-0">
              <div className={`text-xs font-medium ${testResult.ok ? 'text-green-400' : 'text-red-400'}`}>
                {testResult.ok ? `Test passed in ${testResult.elapsed_seconds}s` : 'Test failed'}
              </div>
              {testResult.error && <p className="text-red-400 text-xs font-mono">{testResult.error}</p>}
              {testResult.ok && (
                <div className="flex-1 overflow-y-auto space-y-1">
                  {testResult.results.map((r, i) => (
                    <div key={i} className="text-xs border border-border rounded px-2 py-1.5 bg-bg-2">
                      <div className="text-text truncate">{String(r.title || r.video_id)}</div>
                      <div className="flex gap-3 text-text-2 mt-0.5">
                        {module?.type === 'scorer' && (
                          <>
                            <span>ppr: {Number(r.ppr_score || 0).toFixed(5)}</span>
                            <span className="text-accent">→ {Number(r.module_score || 0).toFixed(5)}</span>
                          </>
                        )}
                      </div>
                    </div>
                  ))}
                </div>
              )}
            </div>
          )}
        </div>
      )}

      {tab === 'scores' && (
        <ItemTable
          items={scoreItems}
          defaultColumns={['thumbnail', 'title', 'score', 'computed_at']}
          storageKey={`module-scores-${moduleId}`}
          emptyMessage="No scores yet — click Recompute."
        />
      )}
    </div>
  )
}
