import { useState } from 'react'
import type { Source } from '../lib/types'
import { patchSource, probeSource, resetCircuit } from '../lib/api'

interface Props {
  source: Source
  onUpdate: (updated: Source) => void
}

export default function SourceHealthCard({ source: s, onUpdate }: Props) {
  const [loading, setLoading] = useState(false)
  const [probeResult, setProbeResult] = useState<{ ok: boolean; detail: string } | null>(null)

  async function toggle() {
    setLoading(true)
    try {
      const updated = await patchSource(s.name, { enabled: !s.enabled })
      onUpdate(updated)
    } finally {
      setLoading(false)
    }
  }

  async function probe() {
    setLoading(true)
    setProbeResult(null)
    try {
      const result = await probeSource(s.name)
      setProbeResult(result)
      onUpdate(await fetch(`/v1/sources/${s.name}`).then(r => r.json()))
    } catch (e: unknown) {
      setProbeResult({ ok: false, detail: String(e) })
    } finally {
      setLoading(false)
    }
  }

  async function reset() {
    setLoading(true)
    try {
      const updated = await resetCircuit(s.name)
      onUpdate(updated)
    } finally {
      setLoading(false)
    }
  }

  const kindColor: Record<string, string> = {
    api: 'bg-blue-500/20 text-blue-300',
    scraper: 'bg-yellow-500/20 text-yellow-300',
    extractor: 'bg-purple-500/20 text-purple-300',
    feed: 'bg-green-500/20 text-green-300',
  }

  const lastOk = s.last_success_at ? new Date(s.last_success_at * 1000).toLocaleString() : '—'
  const lastErr = s.last_error_at ? new Date(s.last_error_at * 1000).toLocaleString() : '—'

  return (
    <div className={`surface p-4 flex flex-col gap-3 ${!s.enabled ? 'opacity-60' : ''}`}>
      {/* Header */}
      <div className="flex items-start justify-between gap-2">
        <div>
          <div className="flex items-center gap-1.5">
            <span className="font-semibold text-text">{s.display_name}</span>
            <span className={`tag ${kindColor[s.kind] ?? 'bg-bg-3 text-text-2'}`}>{s.kind}</span>
            {s.circuit_open && (
              <span className="tag bg-red-500/20 text-red-300">circuit open</span>
            )}
          </div>
          <div className="mt-0.5 text-[11px] text-text-2">{s.name}</div>
        </div>
        <button
          onClick={toggle}
          disabled={loading}
          className={s.enabled ? 'btn-ghost text-[11px]' : 'btn-primary text-[11px]'}
        >
          {s.enabled ? 'Disable' : 'Enable'}
        </button>
      </div>

      {/* Stats */}
      <div className="grid grid-cols-2 gap-x-4 gap-y-1 text-[12px]">
        <Stat label="Weight" value={s.weight.toFixed(2)} />
        <Stat label="Rate limit" value={s.rate_limit_per_min != null ? `${s.rate_limit_per_min}/min` : '—'} />
        <Stat label="Failures" value={String(s.failure_streak)} accent={s.failure_streak > 0} />
        {s.circuit_open && (
          <Stat label="Resets in" value={`${s.circuit_open_until_seconds}s`} accent />
        )}
        <Stat label="Last OK" value={lastOk} />
        {s.last_error && <Stat label="Last error" value={s.last_error} accent />}
      </div>

      {/* Credentials */}
      {s.env_vars.length > 0 && (
        <div className="text-[11px] text-text-2">
          {s.env_vars.map(v => (
            <span key={v} className="mr-2">
              {v}: <span className={s.credential_status[v] ? 'text-accent2' : 'text-red-400'}>
                {s.credential_status[v] ? 'set' : 'missing'}
              </span>
            </span>
          ))}
        </div>
      )}

      {/* Actions */}
      <div className="flex items-center gap-2 flex-wrap">
        <button onClick={probe} disabled={loading} className="text-[11px]">
          {loading ? <span className="spinner-sm" /> : 'Probe'}
        </button>
        {s.circuit_open && (
          <button onClick={reset} disabled={loading} className="text-[11px] btn-primary">
            Reset circuit
          </button>
        )}
        {probeResult && (
          <span className={`text-[11px] ${probeResult.ok ? 'text-accent2' : 'text-red-400'}`}>
            {probeResult.ok ? 'OK' : `Failed: ${probeResult.detail}`}
          </span>
        )}
      </div>
    </div>
  )
}

function Stat({ label, value, accent }: { label: string; value: string; accent?: boolean }) {
  return (
    <div className="flex items-baseline gap-1">
      <span className="text-text-2">{label}:</span>
      <span className={accent ? 'text-accent' : 'text-text'}>{value}</span>
    </div>
  )
}
