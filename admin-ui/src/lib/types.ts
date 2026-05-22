export interface Source {
  name: string
  display_name: string
  kind: 'api' | 'scraper' | 'extractor' | 'feed'
  enabled: number  // 0 | 1
  weight: number
  rate_limit_per_min: number | null
  last_success_at: number | null
  last_error_at: number | null
  last_error: string | null
  failure_streak: number
  circuit_open: boolean
  circuit_open_until_seconds: number
  env_vars: string[]
  credential_status: Record<string, boolean>
}

export interface SchemeField {
  name: string
  type: 'text' | 'url-image' | 'duration' | 'date' | 'number' | 'enum'
  label: string
  required?: boolean
}

export interface Scheme {
  name: string
  display_name: string
  description: string | null
  fields: SchemeField[]
}

export interface Item {
  id: number
  scheme: string
  external_id: string
  metadata: Record<string, unknown>
  added_at: number
  aliases?: Array<{ alias_scheme: string; alias_external_id: string }>
}

export interface PprConfig {
  watch_base: number
  playlist_base: number
  feed_rec_base: number
  alpha: number
  min_seed_rating: number
  compute_spam_mass: number
  _defaults: Record<string, number>
}

export interface PprScore {
  video_id: string
  score: number
  spam_mass: number | null
  computed_at: number | null
  title: string | null
  author: string | null
}

export interface PprSeed {
  video_id: string
  weight: number
  title: string | null
  author: string | null
  reasons: string[]
}

export interface GraphStats {
  nodes: number
  edges: number
  density: number
  scored_nodes: number
}

export interface WeightRule {
  id: number
  rule_type: string
  match_value: string
  multiplier: number
  created_at: number
}

export interface FeedFilter {
  id: number
  filter_type: string
  match_value: string
  created_at: number
}

export interface Persona {
  id: number
  name: string
  description: string | null
  scheme: string
  alpha: number
  min_seed_rating: number
  created_at: number
  updated_at: number
  version: number
  seed_count: number
  job_status: 'pending' | 'running' | 'done' | 'error' | null
  last_run_at: number | null
  last_error: string | null
  job_next_run?: number | null
}

export interface PersonaSeed {
  item_id: number
  scheme: string
  external_id: string
  weight: number
  title: string | null
  author: string | null
}

export interface PersonaScore {
  video_id: string
  score: number
  spam_mass: number | null
  computed_at: number
  title: string | null
  author: string | null
  thumbnail: string | null
  duration: number | null
}

export interface FeedItem {
  video_id: string
  title: string | null
  author: string | null
  author_id: string | null
  thumbnail: string | null
  duration: number | null
  score: number | null
  category: string | null
}

export interface WhyResult {
  video_id: string
  title?: string
  author?: string
  score?: number
  contributions?: Array<{
    source: string
    weight: number
    reason: string
  }>
  [key: string]: unknown
}
