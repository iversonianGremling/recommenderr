import { useEffect, useState } from 'react'
import { listGraphs, type Graph } from '../lib/api'

interface Props {
  value: number
  onChange: (id: number) => void
  className?: string
  showLabel?: boolean
}

export default function GraphSelector({ value, onChange, className = '', showLabel = true }: Props) {
  const [graphs, setGraphs] = useState<Graph[]>([])

  useEffect(() => {
    listGraphs().then((gs) => {
      const visible = gs.filter((g) => g.content_type !== 'mixed')
      setGraphs(visible)
      // Auto-select first graph if nothing selected yet
      if (value === 0 && visible.length > 0) {
        onChange(visible[0].id)
      }
    }).catch(() => {})
  }, []) // eslint-disable-line react-hooks/exhaustive-deps

  if (graphs.length === 0) return null

  return (
    <div className={`flex items-center gap-2 ${className}`}>
      {showLabel && <span className="text-[10px] text-text-2 shrink-0">Graph:</span>}
      <select
        className="input text-xs"
        value={value}
        onChange={(e) => onChange(Number(e.target.value))}
      >
        {graphs.map((g) => (
          <option key={g.id} value={g.id}>
            {g.name} — {g.ppr_count.toLocaleString()} scores
          </option>
        ))}
      </select>
    </div>
  )
}
