import type { Item, Scheme } from '../lib/types'

interface Props {
  scheme: Scheme
  items: Item[]
  onRowClick?: (item: Item) => void
}

function renderCell(value: unknown, type: string): string {
  if (value == null) return '—'
  switch (type) {
    case 'duration': {
      const secs = Number(value)
      if (!secs) return '—'
      const m = Math.floor(secs / 60)
      const s = secs % 60
      return `${m}:${String(s).padStart(2, '0')}`
    }
    case 'date':
      return new Date(Number(value) * 1000).toLocaleDateString()
    case 'number':
      return Number(value).toLocaleString()
    default:
      return String(value)
  }
}

export default function SchemeAwareTable({ scheme, items, onRowClick }: Props) {
  const cols = scheme.fields.slice(0, 6)  // show up to 6 columns

  return (
    <div className="overflow-x-auto">
      <table className="w-full border-collapse text-[13px]">
        <thead>
          <tr className="border-b border-border">
            {cols.map(f => (
              <th key={f.name} className="py-2 px-3 text-left text-[11px] font-semibold uppercase tracking-wider text-text-2">
                {f.label}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {items.length === 0 && (
            <tr>
              <td colSpan={cols.length} className="py-6 text-center text-text-2">No items</td>
            </tr>
          )}
          {items.map(item => (
            <tr
              key={item.id}
              onClick={() => onRowClick?.(item)}
              className={`border-b border-border/50 transition-colors ${onRowClick ? 'cursor-pointer hover:bg-bg-3' : ''}`}
            >
              {cols.map(f => {
                const val = item.metadata[f.name]
                return (
                  <td key={f.name} className="py-2 px-3 text-text max-w-[260px] truncate">
                    {f.type === 'url-image' && val
                      ? <img src={String(val)} alt="" className="h-8 w-14 object-cover rounded" />
                      : renderCell(val, f.type)}
                  </td>
                )
              })}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}
