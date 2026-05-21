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
