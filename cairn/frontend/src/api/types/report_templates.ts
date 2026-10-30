export interface ReportTemplateCatalogEntry {
  key: string
  name: string
  description: string
  digest_sha256: string
  compatibility_profiles: string[]
  is_default: boolean
  supports_single_vulnerability: boolean
  validation_error: string | null
}

export interface ReportTemplate {
  key: string
  name: string
  description: string
  markdown_content: string | null
  digest_sha256: string | null
  compatibility_profiles: string[]
  is_default: boolean
  supports_single_vulnerability: boolean
  validation_error: string | null
  created_at: string
  updated_at: string
}

export interface UpdateReportTemplateRequest {
  name: string
  description?: string
  markdown_content: string
  compatibility_profiles?: string[]
}

export interface CreateReportTemplateRequest extends UpdateReportTemplateRequest {
  key?: string | null
}
