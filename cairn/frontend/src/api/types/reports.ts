export interface ReportInventoryItem {
  artifact_id: string
  project_id: string
  project_title: string
  task_run_id: string
  artifact_uri: string
  artifact_sha256: string
  artifact_size: number
  created_at: string
  template_key: string
  template_version: number | null
  packet_version?: number | null
  output_contract_version?: number | null
  report_data_schema_version?: number | null
  template_digest_sha256: string
  report_policy_version: number
  report_execution_plan_sha256: string
  report_prompt_digest_sha256: string
  input_snapshot_artifact_id: string
  input_snapshot_sha256: string
  completeness_rule_version: number
}

export interface ReportInventoryPage {
  items: ReportInventoryItem[]
  total: number
  page: number
  page_size: number
}

export interface ReportInventoryFilters {
  q?: string
  project_id?: string
  template_key?: string
  date_from?: string
  date_to?: string
  page?: number
  page_size?: number
}
