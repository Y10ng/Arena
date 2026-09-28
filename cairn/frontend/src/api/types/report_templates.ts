export interface ReportTemplateCatalogEntry {
  key: string
  name: string
  description: string
  version: number
  digest_sha256: string
  compatibility_profiles: string[]
}

export interface ReportTemplate {
  key: string
  name: string
  description: string
  latest_version: number | null
  created_at: string
  archived_at: string | null
}

export interface CreateReportTemplateRequest {
  key: string
  name: string
  description?: string
}

export interface ReportTemplateVersion {
  template_key: string
  version: number
  digest_sha256: string
  message: string | null
  created_by: string | null
  created_at: string
}

export interface ReportTemplateVersionDetail extends ReportTemplateVersion {
  markdown_content: string
}

export interface CreateReportTemplateVersionRequest {
  markdown_content: string
  message?: string | null
}
