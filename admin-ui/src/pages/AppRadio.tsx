import { useState } from 'react'

interface RadioTrack {
  video_id: string
  title?: string
  author?: string
  thumbnail?: string
  lengthSeconds?: number
}

interface RadioResult {
  tracks: RadioTrack[]
  seed_track?: string
  seed_artist?: string
}

function Duration({ secs }: { secs: number | null | undefined }) {
  if (secs == null) return null
  const m = Math.floor(secs / 60)
  const s = secs % 60
  return <>{m}:{String(s).padStart(2, '0')}</>
}

export default function AppRadio() {
  const [query, setQuery] = useState('')
  const [suggestions, setSuggestions] = useState<Array<{ track: string; artist: string }>>([])
  const [selected, setSelected] = useState<{ track: string; artist: string } | null>(null)
  const [result, setResult] = useState<RadioResult | null>(null)
  const [loading, setLoading] = useState(false)
  const [searching, setSearching] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const handleSearch = async () => {
    if (!query.trim()) return
    setSearching(true)
    setSuggestions([])
    setSelected(null)
    setResult(null)
    try {
      const r = await fetch(`/v1/radio/search?q=${encodeURIComponent(query)}`).then((r) => r.json()) as Array<{ track: string; artist: string }>
      setSuggestions(r)
    } catch (e) {
      setError(String(e))
    } finally {
      setSearching(false)
    }
  }

  const handleStartRadio = async (track: string, artist: string) => {
    setSelected({ track, artist })
    setLoading(true)
    setError(null)
    setResult(null)
    try {
      const r = await fetch('/v1/radio', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ seeds: [{ track, artist }], limit: 20 }),
      }).then((r) => r.json()) as RadioResult
      setResult(r)
    } catch (e) {
      setError(String(e))
    } finally {
      setLoading(false)
    }
  }

  return (
    <div className="max-w-3xl">
      <h1 className="page-title mb-1">Radio</h1>
      <p className="text-xs text-text-2 mb-4">
        Pick a track or artist to seed the radio. Playback links open in ytfrontend — the admin UI is not a player.
      </p>

      <div className="flex gap-2 mb-4">
        <input
          className="input flex-1"
          placeholder="Search track or artist…"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          onKeyDown={(e) => e.key === 'Enter' && handleSearch()}
        />
        <button className="btn-primary" onClick={handleSearch} disabled={searching}>
          {searching ? 'Searching…' : 'Search'}
        </button>
      </div>

      {error && <p className="text-red-500 text-sm mb-3">{error}</p>}

      {suggestions.length > 0 && !selected && (
        <div className="mb-4 rounded border border-border overflow-hidden">
          {suggestions.map((s, i) => (
            <button
              key={i}
              className="w-full text-left px-3 py-2 text-xs hover:bg-bg-3 border-b border-border last:border-0 flex items-center gap-2"
              onClick={() => handleStartRadio(s.track, s.artist)}
            >
              <span className="font-medium text-text">{s.track}</span>
              <span className="text-text-2">{s.artist}</span>
            </button>
          ))}
        </div>
      )}

      {selected && (
        <div className="mb-4 text-xs text-text-2">
          Seeding from: <span className="font-medium text-text">{selected.track}</span> — {selected.artist}
        </div>
      )}

      {loading && <p className="text-text-2 text-sm">Building radio queue…</p>}

      {result && result.tracks && (
        <div className="rounded border border-border overflow-hidden">
          <table className="w-full text-xs">
            <thead className="bg-bg-2">
              <tr>
                <th className="th w-12"></th>
                <th className="th text-left">Track</th>
                <th className="th text-right">Duration</th>
                <th className="th text-right">Play</th>
              </tr>
            </thead>
            <tbody>
              {result.tracks.map((t, i) => (
                <tr key={t.video_id} className="tr">
                  <td className="td text-center text-text-2">{i + 1}</td>
                  <td className="td">
                    <div className="flex items-center gap-2">
                      {t.thumbnail && <img src={t.thumbnail} alt="" className="h-8 w-14 object-cover rounded flex-shrink-0" />}
                      <div>
                        <p className="font-medium text-text truncate max-w-xs">{t.title ?? t.video_id}</p>
                        {t.author && <p className="text-text-2">{t.author}</p>}
                      </div>
                    </div>
                  </td>
                  <td className="td text-right font-mono text-text-2"><Duration secs={t.lengthSeconds} /></td>
                  <td className="td text-right">
                    <a
                      href={`/watch?v=${t.video_id}`}
                      target="_blank"
                      rel="noreferrer"
                      className="btn text-xs py-0.5 px-2"
                    >
                      ▶
                    </a>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}
