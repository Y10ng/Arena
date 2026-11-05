import type { ConsumptionOverview } from './consumption'
import type { Vulnerability } from '../client'
import type { ProjectSummary } from './projects'

export interface OverviewResponse {
  generated_at: string
  consumption?: ConsumptionOverview
  projects: {
    total: number
    by_status: Partial<Record<ProjectSummary['status'], number>>
  }
  vulnerabilities: {
    total: number
    by_severity: Partial<Record<Vulnerability['severity'], number>>
    by_status: Partial<Record<Vulnerability['status'], number>>
  }
  project_previews: Pick<ProjectSummary, 'id' | 'title' | 'status' | 'created_at' | 'consumption'>[]
  vulnerability_previews: (Pick<
    Vulnerability,
    'id' | 'project_id' | 'title' | 'severity' | 'status' | 'created_at'
  > & { project_title: string })[]
}
