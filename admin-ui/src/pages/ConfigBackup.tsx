import { useRef, useState } from 'react'
import { exportPipelineYaml, importPipelineYaml } from '../lib/api'

type AppliedCounts = Record<string, number>

const EXPORT_DESCRIPTION = [
  'Pipeline config (temporal, scorer weights, diversity)',
  'PPR algorithm parameters',
  'Source enabled/weight settings (never credentials)',
  'Feed filters and weight rules',
  'User-created named graphs',
  'Custom scorer/filter module code',
  'Personas and their seeds',
]

const SECTION_LABELS: Record<string, string> = {
  pipeline_config: 'Pipeline config',
  ppr_config: 'PPR parameters',
  sources: 'Sources',
  feed_filters: 'Feed filters',
  weight_rules: 'Weight rules',
  graphs: 'Graphs',
  custom_modules: 'Custom modules',
  personas: 'Personas',
}

export default function ConfigBackup() {
  const [exporting, setExporting] = useState(false)
  const [preview, setPreview] = useState<AppliedCounts | null>(null)
  const [previewFile, setPreviewFile] = useState<File | null>(null)
  const [importing, setImporting] = useState(false)
  const [importDone, setImportDone] = useState<AppliedCounts | null>(null)
  const [error, setError] = useState<string | null>(null)
  const fileRef = useRef<HTMLInputElement>(null)
  const previewRef = useRef<HTMLInputElement>(null)

  const handleExport = async () => {
    setExporting(true); setError(null)
    try {
      const blob = await exportPipelineYaml()
      const url = URL.createObjectURL(blob)
      const a = document.createElement('a')
      a.href = url
      a.download = `pipeline-${new Date().toISOString().slice(0, 10)}.yaml`
      a.click()
      URL.revokeObjectURL(url)
    } catch (e) {
      setError(String(e))
    } finally {
      setExporting(false)
    }
  }

  const handlePreview = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0]
    if (!file) return
    setError(null); setPreview(null); setPreviewFile(null); setImportDone(null)
    try {
      const res = await importPipelineYaml(file, true)
      setPreview(res.applied)
      setPreviewFile(file)
    } catch (e) {
      setError(String(e))
    } finally {
      if (previewRef.current) previewRef.current.value = ''
    }
  }

  const handleApply = async () => {
    if (!previewFile) return
    setImporting(true); setError(null)
    try {
      const res = await importPipelineYaml(previewFile)
      setImportDone(res.applied)
      setPreview(null)
      setPreviewFile(null)
    } catch (e) {
      setError(String(e))
    } finally {
      setImporting(false)
    }
  }

  return (
    <div className="max-w-xl flex flex-col gap-6">
      <h1 className="page-title">Config Backup</h1>
      <p className="text-xs text-text-2">
        Export the full pipeline configuration as a YAML file, or restore a previous backup.
        Recommendation scores and user interaction data are never exported.
      </p>

      {error && (
        <p className="text-red-400 text-xs rounded border border-red-500/30 bg-red-500/5 px-3 py-2">{error}</p>
      )}

      {/* Export */}
      <div className="rounded border border-border bg-bg-2 p-4 flex flex-col gap-3">
        <h2 className="text-xs font-semibold uppercase tracking-wider text-text-2">Export</h2>
        <ul className="text-[11px] text-text-2 space-y-0.5 list-disc list-inside">
          {EXPORT_DESCRIPTION.map((line) => (
            <li key={line}>{line}</li>
          ))}
        </ul>
        <button
          className="btn-primary text-xs py-1.5 px-4 self-start"
          onClick={handleExport}
          disabled={exporting}
        >
          {exporting ? 'Exporting…' : '↓ Download pipeline.yaml'}
        </button>
      </div>

      {/* Restore */}
      <div className="rounded border border-border bg-bg-2 p-4 flex flex-col gap-3">
        <h2 className="text-xs font-semibold uppercase tracking-wider text-text-2">Restore</h2>
        <p className="text-[11px] text-text-2">
          Select a pipeline.yaml file to preview what will change, then apply.
          Scores and user data are never overwritten.
        </p>

        <div className="flex items-center gap-2">
          <button
            className="btn text-xs py-1.5 px-4"
            onClick={() => previewRef.current?.click()}
          >
            Choose file…
          </button>
          {previewFile && (
            <span className="text-[11px] text-text-2 truncate">{previewFile.name}</span>
          )}
          <input ref={previewRef} type="file" accept=".yaml,.yml" className="hidden" onChange={handlePreview} />
        </div>

        {/* Preview table */}
        {preview && (
          <div className="flex flex-col gap-2">
            <p className="text-[11px] text-text-2">Preview — nothing has been changed yet:</p>
            <div className="rounded border border-border overflow-hidden">
              <table className="w-full text-xs">
                <thead>
                  <tr className="border-b border-border bg-bg">
                    <th className="text-left px-3 py-1.5 font-medium text-text-2">Section</th>
                    <th className="text-right px-3 py-1.5 font-medium text-text-2">Items</th>
                  </tr>
                </thead>
                <tbody>
                  {Object.entries(preview).map(([key, count]) => (
                    <tr key={key} className="border-b border-border last:border-0">
                      <td className="px-3 py-1.5 text-text">{SECTION_LABELS[key] ?? key}</td>
                      <td className="px-3 py-1.5 text-right font-mono text-text-2">{count}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <div className="flex gap-2">
              <button
                className="btn-primary text-xs py-1.5 px-4"
                onClick={handleApply}
                disabled={importing}
              >
                {importing ? 'Applying…' : '↑ Apply restore'}
              </button>
              <button
                className="btn text-xs py-1.5 px-4"
                onClick={() => { setPreview(null); setPreviewFile(null) }}
              >
                Cancel
              </button>
            </div>
          </div>
        )}

        {/* Success */}
        {importDone && (
          <div className="rounded border border-green-500/30 bg-green-500/5 px-3 py-2 text-[11px] text-green-400">
            Restore complete —{' '}
            {Object.entries(importDone)
              .filter(([, n]) => n > 0)
              .map(([k, n]) => `${SECTION_LABELS[k] ?? k}: ${n}`)
              .join(', ') || 'nothing changed'}
          </div>
        )}
      </div>
    </div>
  )
}
