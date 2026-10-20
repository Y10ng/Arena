import type {
  AssessmentReportFinalization,
  AssessmentReportGenerationRequest,
  CoverageCell,
  CreateAdminUserRequest,
  CreateReportTemplateRequest,
  UpdateReportTemplateRequest,
  CreateProjectRequest,
  CreateProjectResourceRequest,
  CreateProjectHintRequest,
  CreateTargetRequest,
  CreateWorkerTemplateRequest,
  DispatcherConfigDocument,
  DispatcherConnectivityAccepted,
  DispatcherConnectivityTest,
  DispatcherControlCollection,
  DispatcherCredentialStatus,
  DispatcherManagedWorkerUpdate,
  DispatcherModel,
  DispatcherProvider,
  DispatcherProviderUpdate,
  DispatcherWorkerConfig,
  DispatcherYamlDocument,
  EvidenceRef,
  EvidenceItem,
  EvidenceSource,
  FactEvidence,
  InfrastructureCapability,
  InfrastructureImage,
  InfrastructureWorker,
  InjectionPayloadCategorySummary,
  InjectionPayloadListFilters,
  InjectionPayloadListResponse,
  InjectionPayloadRecord,
  AdminUser,
  AdvisoryLibrary,
  Hint,
  AdvisoryLibraryFile,
  AdvisoryLibraryFileContent,
  AdvisoryLibraryRevision,
  CreateAdvisoryLibraryRequest,
  CreateAdvisoryLibraryRevisionRequest,
  ResetAdminUserPasswordRequest,
  SurfaceManifestEntry,
  Target,
  TaskRunResultEventGroup,
  TenantMonitoringSummary,
  UpdateAdminUserStatusRequest,
  KnowledgeFile,
  KnowledgeFileContent,
  KnowledgeSet,
  ModelDiscoveryResult,
  ModelTestResult,
  DiscoverTenantModelRequest,
  TestTenantModelRequest,
  ProjectDetail,
  ProjectManagementOptions,
  ProjectResource,
  ProjectResourceBatch,
  ProjectResourceBatchMember,
  ProjectResourceConfig,
  ProjectResourceImportResponse,
  ProjectResourceKind,
  ProjectSummary,
  ProjectWorkerOption,
  ReportTemplate,
  ReportTemplateCatalogEntry,
  ReportInventoryFilters,
  ReportInventoryPage,
  PromptFile,
  PromptFileContent,
  PromptGroup,
  PromptGroupValidationResult,
  TenantModel,
  TenantWorker,
  CreateTenantModelRequest,
  CreateTenantWorkerRequest,
  UpdateTenantModelRequest,
  UpdateTenantWorkerRequest,
  UpdateProjectResourceBatchMemberRequest,
  UpdateProjectWorkersRequest,
  UpdateTargetRequest,
  UpdateWorkerTemplateRequest,
  WorkerTemplate,
} from './types'

export class ApiError<T = unknown> extends Error {
  readonly status: number
  readonly data: T

  constructor(message: string, status: number, data: T) {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.data = data
  }
}

export const isRevisionConflict = (error: unknown) =>
  error instanceof ApiError && error.status === 409 && error.message.includes('revision conflict')

const CSRF_STORAGE_KEY = 'cairn:csrf-token'
const SAFE_METHODS = new Set(['GET', 'HEAD', 'OPTIONS'])
const CSRF_EXEMPT_PATHS = new Set(['/auth/login', '/auth/csrf'])
let unauthorizedHandler: ((requestGeneration: number) => void) | null = null
let authGenerationProvider: (() => number) | null = null

const csrfStorageAvailable = () => typeof window !== 'undefined' && Boolean(window.localStorage)

// CSRF token 本身已对同源脚本可读；localStorage 仅用于同源标签页协调，不保存 session cookie 或 bearer token。
export const getCsrfToken = () =>
  csrfStorageAvailable() ? window.localStorage.getItem(CSRF_STORAGE_KEY) : null

export const setCsrfToken = (token: string | null) => {
  if (!csrfStorageAvailable()) return
  if (token) window.localStorage.setItem(CSRF_STORAGE_KEY, token)
  else window.localStorage.removeItem(CSRF_STORAGE_KEY)
}

export const setUnauthorizedHandler = (
  handler: ((requestGeneration: number) => void) | null,
  generationProvider: (() => number) | null = null,
) => {
  unauthorizedHandler = handler
  authGenerationProvider = generationProvider
}

const bootstrapCsrfToken = async (signal?: AbortSignal): Promise<string | null> => {
  const response = await fetch('/auth/csrf', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    credentials: 'same-origin',
    signal,
  })
  if (!response.ok) return null
  const data = (await response.json()) as { csrf_token?: unknown }
  if (typeof data.csrf_token !== 'string' || !data.csrf_token) return null
  setCsrfToken(data.csrf_token)
  return data.csrf_token
}

interface ApiRequestBody {
  body?: BodyInit
  contentType?: string
}

export async function api<T = unknown>(
  method: string,
  path: string,
  body?: unknown,
  signal?: AbortSignal,
): Promise<T> {
  const requestBody: ApiRequestBody = { contentType: 'application/json' }
  if (body !== undefined) requestBody.body = JSON.stringify(body)
  return authenticatedRequest<T>(method, path, requestBody, signal)
}

export async function apiRaw<T = unknown>(
  method: string,
  path: string,
  body: Blob,
  contentType = 'application/octet-stream',
  signal?: AbortSignal,
): Promise<T> {
  return authenticatedRequest<T>(method, path, { body, contentType }, signal)
}

async function authenticatedRequest<T>(
  method: string,
  path: string,
  requestBody: ApiRequestBody,
  signal?: AbortSignal,
): Promise<T> {
  const normalizedMethod = method.toUpperCase()
  const requestGeneration = authGenerationProvider?.() ?? 0
  return apiRequest<T>(normalizedMethod, path, requestBody, false, requestGeneration, signal)
}

async function apiRequest<T>(
  method: string,
  path: string,
  requestBody: ApiRequestBody,
  csrfRetried: boolean,
  requestGeneration: number,
  signal?: AbortSignal,
): Promise<T> {
  const headers: Record<string, string> = {}
  if (requestBody.contentType) headers['Content-Type'] = requestBody.contentType
  const csrfToken = getCsrfToken()
  if (!SAFE_METHODS.has(method) && !CSRF_EXEMPT_PATHS.has(path) && csrfToken) {
    headers['X-Cairn-CSRF-Token'] = csrfToken
  }
  const options: RequestInit = { method, headers, credentials: 'same-origin', signal }
  if (requestBody.body !== undefined) options.body = requestBody.body

  const response = await fetch(path, options)
  if (response.status === 204) return null as T

  const contentType = response.headers.get('content-type') || ''
  const data: unknown = contentType.includes('application/json')
    ? await response.json()
    : await response.text()
  const detail = data && typeof data === 'object' && 'detail' in data ? data.detail : undefined

  if (!response.ok) {
    const invalidCsrf = response.status === 403 && detail === 'Invalid CSRF token'
    if (
      invalidCsrf &&
      !csrfRetried &&
      !SAFE_METHODS.has(method) &&
      !CSRF_EXEMPT_PATHS.has(path) &&
      (await bootstrapCsrfToken(signal))
    ) {
      return apiRequest<T>(method, path, requestBody, true, requestGeneration, signal)
    }
    if (response.status === 401 && path !== '/auth/login' && path !== '/auth/session') {
      unauthorizedHandler?.(requestGeneration)
    }
    let message = `HTTP ${response.status}`
    if (typeof detail === 'string') {
      message = detail
    } else if (Array.isArray(detail)) {
      message = detail
        .map((item) => {
          if (item && typeof item === 'object' && 'msg' in item && typeof item.msg === 'string') {
            return item.msg
          }
          return String(item)
        })
        .join('; ')
    } else if (
      detail &&
      typeof detail === 'object' &&
      'message' in detail &&
      typeof detail.message === 'string'
    ) {
      message = detail.message
    } else if (typeof data === 'string' && data.trim()) {
      message = data
    }
    throw new ApiError(message, response.status, data)
  }

  return data as T
}

export async function fetchText(path: string, signal?: AbortSignal): Promise<string> {
  const requestGeneration = authGenerationProvider?.() ?? 0
  const response = await fetch(path, { credentials: 'same-origin', ...(signal ? { signal } : {}) })
  const text = await response.text()
  if (!response.ok) {
    if (response.status === 401) unauthorizedHandler?.(requestGeneration)
    let detail = text
    try {
      const data = JSON.parse(text)
      detail = data.detail || text
    } catch {
      // plain text response
    }
    throw new ApiError(detail || `HTTP ${response.status}`, response.status, text)
  }
  return text
}

const encodePathPart = (value: string) => encodeURIComponent(value)

export const tenantMonitoringQueryKey = ['tenant-monitoring-summary'] as const
export const getTenantMonitoringSummary = () =>
  api<TenantMonitoringSummary>('GET', '/monitoring/summary')

export const getProjectMonitoringSummary = (projectId: string) =>
  api<TenantMonitoringSummary>(
    'GET',
    `/projects/${encodeURIComponent(projectId)}/monitoring/summary`,
  )

const encodeKnowledgeFilePath = (path: string) => {
  return path.split('/').map(encodePathPart).join('/')
}

export const listKnowledgeSets = (signal?: AbortSignal) =>
  api<KnowledgeSet[]>('GET', '/knowledge', undefined, signal)

export const createKnowledgeSet = (key: string) => {
  return api<KnowledgeSet>('POST', '/knowledge', { key })
}

export const deleteKnowledgeSet = (key: string) => {
  return api<void>('DELETE', `/knowledge/${encodePathPart(key)}`)
}

export const listKnowledgeFiles = (key: string, signal?: AbortSignal) => {
  return api<KnowledgeFile[]>('GET', `/knowledge/${encodePathPart(key)}/files`, undefined, signal)
}

export const readKnowledgeFile = (key: string, path: string, signal?: AbortSignal) => {
  return api<KnowledgeFileContent>(
    'GET',
    `/knowledge/${encodePathPart(key)}/files/${encodeKnowledgeFilePath(path)}`,
    undefined,
    signal,
  )
}

export const writeKnowledgeFile = (key: string, path: string, content: string) => {
  return api<KnowledgeFileContent>(
    'PUT',
    `/knowledge/${encodePathPart(key)}/files/${encodeKnowledgeFilePath(path)}`,
    { content },
  )
}

export const deleteKnowledgeFile = (key: string, path: string) => {
  return api<void>(
    'DELETE',
    `/knowledge/${encodePathPart(key)}/files/${encodeKnowledgeFilePath(path)}`,
  )
}

export const listAdvisoryLibraries = () => api<AdvisoryLibrary[]>('GET', '/libraries')

export const listLibraryCatalog = () => api<AdvisoryLibrary[]>('GET', '/library-catalog')

export const createAdvisoryLibrary = (body: CreateAdvisoryLibraryRequest) => {
  return api<AdvisoryLibrary>('POST', '/libraries', body)
}

export const archiveAdvisoryLibrary = (key: string) => {
  return api<void>('DELETE', `/libraries/${encodePathPart(key)}`)
}

export const listAdvisoryLibraryRevisions = (key: string, signal?: AbortSignal) => {
  return api<AdvisoryLibraryRevision[]>(
    'GET',
    `/libraries/${encodePathPart(key)}/versions`,
    undefined,
    signal,
  )
}

export const createAdvisoryLibraryRevision = (
  key: string,
  body: CreateAdvisoryLibraryRevisionRequest,
) => api<AdvisoryLibraryRevision>('POST', `/libraries/${encodePathPart(key)}/versions`, body)

export const listAdvisoryLibraryFiles = (key: string, version: number) => {
  return api<AdvisoryLibraryFile[]>(
    'GET',
    `/libraries/${encodePathPart(key)}/versions/${version}/files`,
  )
}

export const readAdvisoryLibraryFile = (key: string, version: number, path: string) => {
  return api<AdvisoryLibraryFileContent>(
    'GET',
    `/libraries/${encodePathPart(key)}/versions/${version}/files/${encodeKnowledgeFilePath(path)}`,
  )
}

export const listPromptGroups = (signal?: AbortSignal) =>
  api<PromptGroup[]>('GET', '/prompt-groups', undefined, signal)

const adminUserPath = (userId?: string) =>
  `/admin/users${userId ? `/${encodePathPart(userId)}` : ''}`

export const listAdminUsers = (limit = 50, offset = 0) =>
  api<AdminUser[]>('GET', `${adminUserPath()}?limit=${limit}&offset=${offset}`)

// 初始密码由服务端固定分配，创建请求只传递登录名。
export const createAdminUser = (body: CreateAdminUserRequest) =>
  api<AdminUser>('POST', adminUserPath(), body)

export const updateAdminUserStatus = (userId: string, body: UpdateAdminUserStatusRequest) =>
  api<AdminUser>('PATCH', `${adminUserPath(userId)}/status`, body)

export const resetAdminUserPassword = (userId: string, body: ResetAdminUserPasswordRequest) =>
  api<AdminUser>('POST', `${adminUserPath(userId)}/password-reset`, body)

export const revokeAdminUserSessions = (userId: string) =>
  api<AdminUser>('POST', `${adminUserPath(userId)}/sessions/revoke`)

const tenantModelPath = (modelId?: string) =>
  `/models${modelId ? `/${encodePathPart(modelId)}` : ''}`

export const listTenantModels = () => api<TenantModel[]>('GET', tenantModelPath())

export const createTenantModel = (body: CreateTenantModelRequest) =>
  api<TenantModel>('POST', tenantModelPath(), body)

export const getTenantModel = (modelId: string) => api<TenantModel>('GET', tenantModelPath(modelId))

export const updateTenantModel = (modelId: string, body: UpdateTenantModelRequest) =>
  api<TenantModel>('PUT', tenantModelPath(modelId), body)

// API Key 仅随草稿请求 body 发送，不进入 URL、读取模型或 query cache。
export const discoverTenantModelDraft = (body: DiscoverTenantModelRequest) =>
  api<ModelDiscoveryResult>('POST', `${tenantModelPath()}/discover`, body)

export const testTenantModelDraft = (body: TestTenantModelRequest) =>
  api<ModelTestResult>('POST', `${tenantModelPath()}/test`, body)

export const deleteTenantModel = (modelId: string, expectedRevision: number) =>
  api<void>('DELETE', tenantModelPath(modelId), { expected_revision: expectedRevision })

export const deleteTenantModelCredential = (modelId: string, expectedRevision: number) =>
  api<TenantModel>('DELETE', `${tenantModelPath(modelId)}/credential`, {
    expected_revision: expectedRevision,
  })

const tenantWorkerPath = (workerId?: string) =>
  `/tenant-workers${workerId ? `/${encodePathPart(workerId)}` : ''}`

export const listTenantWorkers = () => api<TenantWorker[]>('GET', tenantWorkerPath())
export const listTenantWorkerOptions = () =>
  api<TenantWorker[]>('GET', `${tenantWorkerPath()}/options`)
export const createTenantWorker = (body: CreateTenantWorkerRequest) =>
  api<TenantWorker>('POST', tenantWorkerPath(), body)
export const updateTenantWorker = (workerId: string, body: UpdateTenantWorkerRequest) =>
  api<TenantWorker>('PUT', tenantWorkerPath(workerId), body)
export const deleteTenantWorker = (workerId: string, expectedRevision: number) =>
  api<void>('DELETE', tenantWorkerPath(workerId), { expected_revision: expectedRevision })

export const listInfrastructureWorkers = () => api<InfrastructureWorker[]>('GET', '/workers')
export const listProjectWorkerOptions = () =>
  api<ProjectWorkerOption[]>('GET', '/project-worker-options')

export const listInfrastructureImages = () => api<InfrastructureImage[]>('GET', '/images')

export const listInfrastructureCapabilities = () =>
  api<InfrastructureCapability[]>('GET', '/capabilities')

export const readDispatcherYaml = () => api<DispatcherYamlDocument>('GET', '/dispatcher/yaml')

export const updateDispatcherYaml = (content: string, revision?: string) =>
  api<DispatcherYamlDocument>('PUT', '/dispatcher/yaml', { content, revision })

export const readDispatcherConfig = () => api<DispatcherConfigDocument>('GET', '/dispatcher/config')

export const updateDispatcherConfig = (config: Record<string, unknown>, revision?: string) => {
  return api<DispatcherConfigDocument>('PUT', '/dispatcher/config', { config, revision })
}

const controlPath = (kind: 'providers' | 'models' | 'workers', id?: string) =>
  `/dispatcher/${kind}${id ? `/${encodePathPart(id)}` : ''}`

export const listDispatcherProviders = () =>
  api<DispatcherControlCollection<DispatcherProvider>>('GET', controlPath('providers'))
export const saveDispatcherProvider = (
  provider: DispatcherProviderUpdate,
  revision: string,
  currentId?: string,
) =>
  api<DispatcherConfigDocument>(currentId ? 'PUT' : 'POST', controlPath('providers', currentId), {
    revision,
    item: provider,
  })
export const deleteDispatcherProvider = (id: string, revision: string) =>
  api<DispatcherConfigDocument>('DELETE', controlPath('providers', id), { revision })
export const setDispatcherProviderCredential = (id: string, credential: string, revision: string) =>
  api<DispatcherCredentialStatus>('PUT', `${controlPath('providers', id)}/credential`, {
    revision,
    credential,
  })
export const deleteDispatcherProviderCredential = (id: string, revision: string) =>
  api<DispatcherCredentialStatus>('DELETE', `${controlPath('providers', id)}/credential`, {
    revision,
  })

export const listDispatcherModels = () =>
  api<DispatcherControlCollection<DispatcherModel>>('GET', controlPath('models'))
export const saveDispatcherModel = (model: DispatcherModel, revision: string, currentId?: string) =>
  api<DispatcherConfigDocument>(currentId ? 'PUT' : 'POST', controlPath('models', currentId), {
    revision,
    item: model,
  })
export const deleteDispatcherModel = (id: string, revision: string) =>
  api<DispatcherConfigDocument>('DELETE', controlPath('models', id), { revision })

export const listDispatcherWorkers = () =>
  api<DispatcherControlCollection<DispatcherWorkerConfig>>('GET', controlPath('workers'))
export const saveDispatcherWorker = (
  worker: DispatcherManagedWorkerUpdate,
  revision: string,
  currentId?: string,
) =>
  api<DispatcherConfigDocument>(currentId ? 'PUT' : 'POST', controlPath('workers', currentId), {
    revision,
    item: worker,
  })
export const deleteDispatcherWorker = (id: string, revision: string) =>
  api<DispatcherConfigDocument>('DELETE', controlPath('workers', id), { revision })

export const startConnectivityTest = (
  targetType: 'providers' | 'models' | 'workers',
  id: string,
  timeoutSeconds = 10,
) =>
  api<DispatcherConnectivityAccepted>('POST', `${controlPath(targetType, id)}/connectivity-tests`, {
    timeout_seconds: timeoutSeconds,
  })
export const getConnectivityTest = (testId: string) =>
  api<DispatcherConnectivityTest>('GET', `/dispatcher/connectivity-tests/${encodePathPart(testId)}`)
export const pollConnectivityTest = (pollUrl: string) => {
  if (!pollUrl.startsWith('/dispatcher/connectivity-tests/'))
    throw new Error('Invalid connectivity poll URL')
  return api<DispatcherConnectivityTest>('GET', pollUrl)
}

export const createPromptGroup = (key: string, cloneFrom?: string | null) => {
  return api<PromptGroup>('POST', '/prompt-groups', {
    key,
    clone_from: cloneFrom || null,
  })
}

export const deletePromptGroup = (key: string) => {
  return api<void>('DELETE', `/prompt-groups/${encodePathPart(key)}`)
}

export const listPromptFiles = (key: string, signal?: AbortSignal) => {
  return api<PromptFile[]>('GET', `/prompt-groups/${encodePathPart(key)}/files`, undefined, signal)
}

export const readPromptFile = (key: string, name: string, signal?: AbortSignal) => {
  return api<PromptFileContent>(
    'GET',
    `/prompt-groups/${encodePathPart(key)}/files/${encodePathPart(name)}`,
    undefined,
    signal,
  )
}

export const writePromptFile = (key: string, name: string, content: string) => {
  return api<PromptFileContent>(
    'PUT',
    `/prompt-groups/${encodePathPart(key)}/files/${encodePathPart(name)}`,
    { content },
  )
}

export const validatePromptGroup = (key: string, signal?: AbortSignal) => {
  return api<PromptGroupValidationResult>(
    'POST',
    `/prompt-groups/${encodePathPart(key)}/validate`,
    undefined,
    signal,
  )
}

export const repairPromptGroup = (key: string, cloneFrom?: string | null) => {
  return api<PromptGroup>('POST', `/prompt-groups/${encodePathPart(key)}/repair`, {
    clone_from: cloneFrom || null,
  })
}

export const restorePromptGroup = (key: string) => {
  return api<PromptGroup>('POST', `/prompt-groups/${encodePathPart(key)}/restore`)
}

export const updateProjectWorkers = (projectId: string, body: UpdateProjectWorkersRequest) =>
  api<ProjectSummary>('PUT', `/projects/${encodeURIComponent(projectId)}/workers`, body)

export const getProjectManagementOptions = (projectId: string) =>
  api<ProjectManagementOptions>(
    'GET',
    `/projects/${encodeURIComponent(projectId)}/management-options`,
  )

export const createProject = (body: CreateProjectRequest) =>
  api<ProjectDetail>('POST', '/projects', body)

export const getProjectDetail = (projectId: string) =>
  api<ProjectDetail>('GET', `/projects/${encodeURIComponent(projectId)}`)

export const createProjectHint = (projectId: string, body: CreateProjectHintRequest) =>
  api<Hint>('POST', `/projects/${encodeURIComponent(projectId)}/hints`, body)

export const listProjectTargets = (projectId: string) =>
  api<Target[]>('GET', `/projects/${encodeURIComponent(projectId)}/targets`)

export const createProjectTarget = (projectId: string, body: CreateTargetRequest) =>
  api<Target>('POST', `/projects/${encodeURIComponent(projectId)}/targets`, body)

export const updateProjectTarget = (
  projectId: string,
  targetId: string,
  body: UpdateTargetRequest,
) =>
  api<Target>(
    'PUT',
    `/projects/${encodeURIComponent(projectId)}/targets/${encodeURIComponent(targetId)}`,
    body,
  )

export const getProjectResourceConfig = (signal?: AbortSignal) =>
  api<ProjectResourceConfig>('GET', '/project-resource-config', undefined, signal)

export const createProjectResourceBatch = () =>
  api<ProjectResourceBatch>('POST', '/project-resource-batches', {})

export const getProjectResourceBatch = (batchId: string, signal?: AbortSignal) =>
  api<ProjectResourceBatch>(
    'GET',
    `/project-resource-batches/${encodeURIComponent(batchId)}`,
    undefined,
    signal,
  )

export const uploadProjectResourceBatchFile = (
  batchId: string,
  file: File,
  path: string,
  kind?: ProjectResourceKind,
  signal?: AbortSignal,
) => {
  const params = new URLSearchParams({ path })
  if (kind) params.set('kind', kind)
  return apiRaw<ProjectResourceBatchMember>(
    'POST',
    `/project-resource-batches/${encodeURIComponent(batchId)}/files?${params.toString()}`,
    file,
    'application/octet-stream',
    signal,
  )
}

export const importProjectResourceBatchZip = (batchId: string, file: File, signal?: AbortSignal) =>
  apiRaw<ProjectResourceBatch>(
    'POST',
    `/project-resource-batches/${encodeURIComponent(batchId)}/zip`,
    file,
    'application/zip',
    signal,
  )

export const updateProjectResourceBatchMember = (
  batchId: string,
  memberId: string,
  body: UpdateProjectResourceBatchMemberRequest,
) =>
  api<ProjectResourceBatchMember>(
    'PATCH',
    `/project-resource-batches/${encodeURIComponent(batchId)}/members/${encodeURIComponent(memberId)}`,
    body,
  )

export const deleteProjectResourceBatchMember = (batchId: string, memberId: string) =>
  api<void>(
    'DELETE',
    `/project-resource-batches/${encodeURIComponent(batchId)}/members/${encodeURIComponent(memberId)}`,
  )

export const cancelProjectResourceBatch = (batchId: string) =>
  api<void>('DELETE', `/project-resource-batches/${encodeURIComponent(batchId)}`)

export const commitProjectResourceBatch = (projectId: string, batchId: string) =>
  api<ProjectResourceImportResponse>(
    'POST',
    `/projects/${encodeURIComponent(projectId)}/resource-imports`,
    { resource_batch_id: batchId },
  )

export const listProjectResources = (projectId: string) =>
  api<ProjectResource[]>('GET', `/projects/${encodeURIComponent(projectId)}/resources`)

export const readProjectResource = (projectId: string, resourceId: string) =>
  fetchText(
    `/projects/${encodeURIComponent(projectId)}/resources/${encodeURIComponent(resourceId)}`,
  )

export const createProjectResource = (projectId: string, body: CreateProjectResourceRequest) =>
  api<ProjectResource>('POST', `/projects/${encodeURIComponent(projectId)}/resources`, body)

export const deactivateProjectResource = (projectId: string, resourceId: string) =>
  api<void>(
    'DELETE',
    `/projects/${encodeURIComponent(projectId)}/resources/${encodeURIComponent(resourceId)}`,
  )

export const listProjectSurfaceManifest = (projectId: string, signal?: AbortSignal) =>
  api<SurfaceManifestEntry[]>(
    'GET',
    `/projects/${encodeURIComponent(projectId)}/surface-manifest`,
    undefined,
    signal,
  )

export const listProjectCoverageCells = (projectId: string, signal?: AbortSignal) =>
  api<CoverageCell[]>(
    'GET',
    `/projects/${encodeURIComponent(projectId)}/coverage-slots`,
    undefined,
    signal,
  )

export const listReports = (filters: ReportInventoryFilters = {}, signal?: AbortSignal) => {
  const params = new URLSearchParams()
  if (filters.q) params.set('q', filters.q)
  if (filters.project_id) params.set('project_id', filters.project_id)
  if (filters.template_key) params.set('template_key', filters.template_key)
  if (filters.date_from) params.set('date_from', filters.date_from)
  if (filters.date_to) params.set('date_to', filters.date_to)
  params.set('page', String(filters.page ?? 1))
  params.set('page_size', String(filters.page_size ?? 20))
  return api<ReportInventoryPage>('GET', `/reports?${params.toString()}`, undefined, signal)
}

export const listReportTemplateCatalog = (signal?: AbortSignal) =>
  api<ReportTemplateCatalogEntry[]>('GET', '/report-template-catalog', undefined, signal)

export const listReportTemplates = (signal?: AbortSignal) =>
  api<ReportTemplate[]>('GET', '/report-templates', undefined, signal)

export const createReportTemplate = (body: CreateReportTemplateRequest) =>
  api<ReportTemplate>('POST', '/report-templates', body)

export const getReportTemplate = (key: string, signal?: AbortSignal) =>
  api<ReportTemplate>('GET', `/report-templates/${encodePathPart(key)}`, undefined, signal)

export const updateReportTemplate = (key: string, body: UpdateReportTemplateRequest) =>
  api<ReportTemplate>('PUT', `/report-templates/${encodePathPart(key)}`, body)

export const deleteReportTemplate = (key: string) =>
  api<void>('DELETE', `/report-templates/${encodePathPart(key)}`)

export const getAssessmentReportFinalization = async (projectId: string, signal?: AbortSignal) => {
  try {
    return await api<AssessmentReportFinalization>(
      'GET',
      `/projects/${encodeURIComponent(projectId)}/assessment-report-finalization`,
      undefined,
      signal,
    )
  } catch (error) {
    const detail =
      error instanceof ApiError &&
      typeof error.data === 'object' &&
      error.data !== null &&
      'detail' in error.data
        ? (error.data as { detail?: unknown }).detail
        : null
    if (
      error instanceof ApiError &&
      error.status === 404 &&
      detail === 'assessment_report_finalization_not_found'
    )
      return null
    throw error
  }
}

export const requestAssessmentReportFinalization = (
  projectId: string,
  body: AssessmentReportGenerationRequest,
) =>
  api<AssessmentReportFinalization>(
    'POST',
    `/projects/${encodeURIComponent(projectId)}/assessment-report-finalization/request`,
    body,
  )

// Worker 模板同样由认证会话隔离；只保存稳定 Tenant Worker ID，不保存 Model credential。
export const listWorkerTemplates = () => api<WorkerTemplate[]>('GET', '/worker-templates')

export const createWorkerTemplate = (body: CreateWorkerTemplateRequest) =>
  api<WorkerTemplate>('POST', '/worker-templates', body)

export const updateWorkerTemplate = (id: string, body: UpdateWorkerTemplateRequest) =>
  api<WorkerTemplate>('PUT', `/worker-templates/${encodeURIComponent(id)}`, body)

export const deleteWorkerTemplate = (id: string) =>
  api<void>('DELETE', `/worker-templates/${encodeURIComponent(id)}`)

// 漏洞管理（所有场景通用）：列表/详情/创建/状态流转/fact 关联查询。
export interface Vulnerability {
  id: string
  project_id: string
  title: string
  severity: 'critical' | 'high' | 'medium' | 'low' | 'info'
  vuln_type: string
  target: string
  confidence: number
  status: 'new' | 'confirmed' | 'dismissed' | 'remediated'
  source_fact_id: string | null
  remediation: string
  evidence_items: EvidenceItem[]
  repro_steps: string
  discovered_by_run_id: string | null
  discovered_by_worker: string | null
  slot_key: string | null
  fingerprint: string | null
  identity: Record<string, unknown>
  created_at: string
  updated_at: string
}

// 漏洞证据聚合：来源 fact 的 evidence、产出 run 的 stdout/diagnostic 日志
export interface VulnerabilityEvidenceArtifact {
  id: string
  kind: string
  filename: string
  pointer: string
  source: string // fact_evidence / discovered_run
}

export interface VulnerabilitySourceFact {
  id: string
  description: string
  task_run_id: string | null
  evidence: VulnerabilityEvidenceArtifact[]
}

export interface VulnerabilityObservation {
  id: string
  source_fact_id: string | null
  task_run_id: string | null
  worker: string | null
  slot_key: string | null
  identity: Record<string, unknown>
  severity: string
  confidence: number
  evidence_items: EvidenceItem[]
  observed_at: string
}

export interface VulnerabilityVerification {
  id: string
  vulnerability_id: string
  observation_id: string
  intent_id: string
  fact_id: string
  verifier_task_run_id: string
  verifier_worker: string
  discovery_task_run_id: string
  discovery_worker: string | null
  independence_level: 'different_worker' | 'same_worker_new_session'
  outcome: 'confirmed' | 'not_reproduced' | 'environment_changed' | 'insufficient_evidence'
  evidence: EvidenceRef[]
  notes: string
  created_at: string
}

// 详情：聚合来源 fact、发现记录、独立验证与产出 run 证据，供完整审阅。
export interface VulnerabilityDetail extends Vulnerability {
  observation_id: string | null
  source: EvidenceSource
  record_evidence_items: EvidenceItem[]
  source_fact: VulnerabilitySourceFact | null
  run_artifacts: VulnerabilityEvidenceArtifact[]
  observations: VulnerabilityObservation[]
  verifications: VulnerabilityVerification[]
}

export interface DuplicateVulnerabilityCandidateMember {
  vulnerability_id: string
  title: string
  status: string
  severity: string
  confidence: number
  observation_count: number
  fingerprint: string | null
  identity_source: string
}

export interface DuplicateVulnerabilityCandidate {
  id: string
  project_id: string
  requested_vulnerability_id: string
  rule_version: number
  rule_id: string
  status: 'advisory_candidate'
  advisory: true
  vulnerability_ids: string[]
  members: DuplicateVulnerabilityCandidateMember[]
  reasons: string[]
  shared_dimensions: Record<string, unknown>
  differing_dimensions: Record<string, Record<string, unknown>>
}

export interface DuplicateVulnerabilityCandidatesResponse {
  project_id: string
  requested_vulnerability_id: string
  rule_version: number
  advisory: true
  candidates: DuplicateVulnerabilityCandidate[]
}

// 跨项目成果列表项：附带项目标题
export interface VulnerabilityListItem extends Vulnerability {
  project_title: string
}

export interface CreateVulnerabilityRequest {
  title: string
  severity: string
  vuln_type?: string
  target?: string
  remediation?: string
  confidence?: number
  source_fact_id?: string | null
  slot_key?: string | null
  evidence_items?: EvidenceItem[]
}

export interface UpdateVulnerabilityRequest {
  status?: string
  severity?: string
  remediation?: string
  confidence?: number
  evidence_items?: EvidenceItem[]
  repro_steps?: string
}

export const listVulnerabilities = (
  projectId: string,
  severity?: string,
  status?: string,
  signal?: AbortSignal,
) => {
  const params = new URLSearchParams()
  if (severity) params.set('severity', severity)
  if (status) params.set('status', status)
  const qs = params.toString()
  return api<Vulnerability[]>(
    'GET',
    `/projects/${encodeURIComponent(projectId)}/vulnerabilities${qs ? '?' + qs : ''}`,
    undefined,
    signal,
  )
}

// 跨项目成果列表（主菜单"成果列表"用），附带项目标题
export const listAllVulnerabilities = (
  severity?: string,
  status?: string,
  signal?: AbortSignal,
  currentTenantOnly = false,
) => {
  const params = new URLSearchParams()
  if (severity) params.set('severity', severity)
  if (status) params.set('status', status)
  if (currentTenantOnly) params.set('current_tenant_only', 'true')
  const qs = params.toString()
  return api<VulnerabilityListItem[]>(
    'GET',
    `/vulnerabilities${qs ? '?' + qs : ''}`,
    undefined,
    signal,
  )
}

export const getVulnerability = (projectId: string, vulnId: string, signal?: AbortSignal, observationId?: string) =>
  api<VulnerabilityDetail>(
    'GET',
    `/projects/${encodeURIComponent(projectId)}/vulnerabilities/${encodeURIComponent(vulnId)}${observationId ? `?${new URLSearchParams({ observation_id: observationId })}` : ''}`,
    undefined,
    signal,
  )

export const getDuplicateVulnerabilityCandidates = (
  projectId: string,
  vulnId: string,
  signal?: AbortSignal,
) =>
  api<DuplicateVulnerabilityCandidatesResponse>(
    'GET',
    `/projects/${encodeURIComponent(projectId)}/vulnerabilities/${encodeURIComponent(vulnId)}/duplicate-candidates`,
    undefined,
    signal,
  )

export const createVulnerability = (projectId: string, body: CreateVulnerabilityRequest) =>
  api<Vulnerability>('POST', `/projects/${encodeURIComponent(projectId)}/vulnerabilities`, body)

export const updateVulnerability = (
  projectId: string,
  vulnId: string,
  body: UpdateVulnerabilityRequest,
) =>
  api<Vulnerability>(
    'PATCH',
    `/projects/${encodeURIComponent(projectId)}/vulnerabilities/${encodeURIComponent(vulnId)}`,
    body,
  )

export const getFactVulnerability = (projectId: string, factId: string) =>
  api<Vulnerability | null>(
    'GET',
    `/projects/${encodeURIComponent(projectId)}/facts/${encodeURIComponent(factId)}/vulnerability`,
  )

const projectInjectionPayloadPath = (projectId: string, suffix = '') =>
  `/projects/${encodePathPart(projectId)}/injection-payloads${suffix}`

export const listProjectInjectionPayloads = (
  projectId: string,
  filters: InjectionPayloadListFilters = {},
  signal?: AbortSignal,
) => {
  const params = new URLSearchParams()
  if (filters.injectionType) params.set('injection_type', filters.injectionType)
  if (filters.responseStatus) params.set('response_status', filters.responseStatus)
  if (filters.search) params.set('search', filters.search)
  if (filters.limit !== undefined) params.set('limit', String(filters.limit))
  if (filters.offset !== undefined) params.set('offset', String(filters.offset))
  const query = params.toString()
  return api<InjectionPayloadListResponse>(
    'GET',
    `${projectInjectionPayloadPath(projectId)}${query ? `?${query}` : ''}`,
    undefined,
    signal,
  )
}

export const listProjectInjectionPayloadCategories = (projectId: string, signal?: AbortSignal) =>
  api<InjectionPayloadCategorySummary[]>(
    'GET',
    projectInjectionPayloadPath(projectId, '/categories'),
    undefined,
    signal,
  )

export const getProjectInjectionPayload = (
  projectId: string,
  payloadId: string,
  signal?: AbortSignal,
) =>
  api<InjectionPayloadRecord>(
    'GET',
    projectInjectionPayloadPath(projectId, `/${encodePathPart(payloadId)}`),
    undefined,
    signal,
  )

export const projectInjectionPayloadExportUrl = (projectId: string) =>
  projectInjectionPayloadPath(projectId, '/export.jsonl')

export type ArtifactKind =
  'log' | 'file' | 'report' | 'screenshot' | 'patch' | 'pcap' | 'har' | 'raw_output' | 'hook_log'

export type ArtifactCategory = 'evidence' | 'deliverable'

export interface ProjectArtifact {
  id: string
  project_id: string
  task_run_id: string | null
  kind: ArtifactKind
  kind_deprecated: boolean
  deprecation_message: string | null
  category: ArtifactCategory
  uri: string
  sha256: string
  size: number
  created_at: string
  metadata: Record<string, unknown>
}

// artifact 内容预览：保留完整性元数据，避免把截断内容误认为完整证据。
export interface ArtifactPreview {
  artifact_id: string
  project_id: string
  task_run_id: string | null
  kind: ArtifactKind | 'packet'
  kind_deprecated: boolean
  deprecation_message: string | null
  content: string
  size: number
  bytes_read: number
  truncated: boolean
  sha256: string
}

const artifactPath = (projectId: string, artifactId?: string) =>
  `/projects/${encodeURIComponent(projectId)}/artifacts${artifactId ? `/${encodeURIComponent(artifactId)}` : ''}`

export const listProjectArtifacts = (projectId: string) =>
  api<ProjectArtifact[]>('GET', artifactPath(projectId))

const normalizeArtifactPreview = (
  data: ArtifactPreview | string,
  projectId: string,
  artifactId: string,
): ArtifactPreview => {
  if (typeof data !== 'string') return data
  const bytes = new TextEncoder().encode(data).byteLength
  return {
    artifact_id: artifactId,
    project_id: projectId,
    task_run_id: null,
    kind: 'file',
    kind_deprecated: false,
    deprecation_message: null,
    content: data,
    size: bytes,
    bytes_read: bytes,
    truncated: false,
    sha256: '',
  }
}

const fetchArtifactPreviewWindow = async (
  projectId: string,
  artifactId: string,
  window: 'preview' | 'preview-tail',
  signal?: AbortSignal,
): Promise<ArtifactPreview> => {
  const data = await api<ArtifactPreview | string>(
    'GET',
    `${artifactPath(projectId, artifactId)}/${window}`,
    undefined,
    signal,
  )
  return normalizeArtifactPreview(data, projectId, artifactId)
}

export const fetchArtifactPreview = (projectId: string, artifactId: string, signal?: AbortSignal) =>
  fetchArtifactPreviewWindow(projectId, artifactId, 'preview', signal)

export const fetchArtifactTailPreview = (
  projectId: string,
  artifactId: string,
  signal?: AbortSignal,
) => fetchArtifactPreviewWindow(projectId, artifactId, 'preview-tail', signal)

// 兼容旧调用名和字符串返回语义。
export const fetchVulnerabilityPacket = async (
  projectId: string,
  artifactId: string,
  signal?: AbortSignal,
): Promise<string> => (await fetchArtifactPreview(projectId, artifactId, signal)).content

export type TaskRunType = 'bootstrap' | 'reason' | 'explore' | 'verify' | 'conclude' | 'report'
export type TaskRunWorkerType = 'claudecode' | 'codex' | 'pi' | 'mock'
export type TaskRunLifecycleStatus =
  'queued' | 'running' | 'succeeded' | 'failed' | 'cancelled' | 'timed_out'

// 项目 task_run 列表与 Runs 端点保持同一响应字段。
export interface TaskRunSummary {
  id: string
  project_id: string
  intent_id: string | null
  task_type: TaskRunType
  worker_name: string
  worker_type: TaskRunWorkerType
  status: TaskRunLifecycleStatus
  session_id: string | null
  prompt_group: string | null
  prompt_version: string | null
  execution_profile: string | null
  started_at: string | null
  ended_at: string | null
  exit_code: number | null
  error_type: string | null
  error_message: string | null
  assessment_attempt_id: string | null
  runtime_context: Record<string, unknown>
}

export const listProjectTaskRuns = (projectId: string, signal?: AbortSignal) =>
  api<TaskRunSummary[]>(
    'GET',
    `/projects/${encodeURIComponent(projectId)}/task-runs`,
    undefined,
    signal,
  )

export const listProjectTaskRunResultEvents = (
  projectId: string,
  taskRunIds: readonly string[],
  signal?: AbortSignal,
) => {
  const params = new URLSearchParams()
  taskRunIds.forEach((taskRunId) => params.append('task_run_id', taskRunId))
  const query = params.toString()
  return api<TaskRunResultEventGroup[]>(
    'GET',
    `/projects/${encodeURIComponent(projectId)}/task-runs/result-events${query ? `?${query}` : ''}`,
    undefined,
    signal,
  )
}

export const listProjectFactEvidence = (projectId: string, factId: string, signal?: AbortSignal) =>
  api<FactEvidence[]>(
    'GET',
    `/projects/${encodeURIComponent(projectId)}/facts/${encodeURIComponent(factId)}/evidence`,
    undefined,
    signal,
  )
