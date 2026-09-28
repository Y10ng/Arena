export interface WorkerTemplate {
  id: string
  name: string
  owner: string
  tenant_worker_ids: string[]
  worker_names: string[]
  created_at: string
}

export interface CreateWorkerTemplateRequest {
  name: string
  tenant_worker_ids: string[]
}

export interface UpdateWorkerTemplateRequest {
  name?: string
  tenant_worker_ids?: string[]
}
