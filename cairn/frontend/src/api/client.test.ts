import { afterEach, describe, expect, it, vi } from 'vitest'
import { changePassword } from './auth'
import {
  ApiError,
  api,
  apiRaw,
  archiveReportTemplate,
  cancelProjectResourceBatch,
  commitProjectResourceBatch,
  createAdminUser,
  createProject,
  createProjectHint,
  createProjectResource,
  createProjectResourceBatch,
  deleteProjectResourceBatchMember,
  createProjectTarget,
  createReportTemplate,
  createReportTemplateVersion,
  createTenantModel,
  createVulnerability,
  createTenantWorker,
  createWorkerTemplate,
  deactivateProjectResource,
  deleteTenantModel,
  deleteTenantWorker,
  discoverTenantModelDraft,
  fetchArtifactPreview,
  fetchArtifactTailPreview,
  fetchVulnerabilityPacket,
  getAssessmentReportFinalization,
  getDuplicateVulnerabilityCandidates,
  getProjectDetail,
  getProjectResourceBatch,
  getProjectResourceConfig,
  getVulnerability,
  getProjectManagementOptions,
  getProjectMonitoringSummary,
  getProjectInjectionPayload,
  getReportTemplateVersion,
  getTenantMonitoringSummary,
  listAdminUsers,
  listAdvisoryLibraryRevisions,
  listAllVulnerabilities,
  listKnowledgeFiles,
  listKnowledgeSets,
  listProjectArtifacts,
  listProjectCoverageCells,
  listProjectFactEvidence,
  listProjectInjectionPayloadCategories,
  listProjectInjectionPayloads,
  listProjectResources,
  listProjectSurfaceManifest,
  listProjectTargets,
  listProjectTaskRunResultEvents,
  listReports,
  listReportTemplateCatalog,
  listReportTemplates,
  listReportTemplateVersions,
  listPromptFiles,
  listPromptGroups,
  listTenantWorkerOptions,
  listVulnerabilities,
  importProjectResourceBatchZip,
  readKnowledgeFile,
  readProjectResource,
  readPromptFile,
  projectInjectionPayloadExportUrl,
  requestAssessmentReportFinalization,
  resetAdminUserPassword,
  revokeAdminUserSessions,
  saveDispatcherProvider,
  setCsrfToken,
  setDispatcherProviderCredential,
  setUnauthorizedHandler,
  startConnectivityTest,
  tenantMonitoringQueryKey,
  testTenantModelDraft,
  updateAdminUserStatus,
  updateProjectResourceBatchMember,
  updateProjectTarget,
  updateProjectWorkers,
  uploadProjectResourceBatchFile,
  updateVulnerability,
  updateTenantModel,
  updateTenantWorker,
  validatePromptGroup,
} from './client'

const jsonResponse = (data: unknown) =>
  ({
    status: 200,
    ok: true,
    headers: new Headers({ 'content-type': 'application/json' }),
    json: async () => data,
    text: async () => JSON.stringify(data),
  }) as Response

describe('authenticated API client', () => {
  afterEach(() => {
    setUnauthorizedHandler(null)
    vi.unstubAllGlobals()
  })

  it('adds same-origin credentials and CSRF to unsafe requests', async () => {
    const storage = new Map<string, string>()
    vi.stubGlobal('window', {
      localStorage: {
        getItem: (key: string) => storage.get(key) || null,
        setItem: (key: string, value: string) => storage.set(key, value),
        removeItem: (key: string) => storage.delete(key),
      },
    })
    setCsrfToken('csrf-1')
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse({ ok: true }))
    vi.stubGlobal('fetch', fetchMock)

    await api('POST', '/projects', { title: 'one' })

    expect(fetchMock).toHaveBeenCalledWith(
      '/projects',
      expect.objectContaining({
        credentials: 'same-origin',
        headers: {
          'Content-Type': 'application/json',
          'X-Cairn-CSRF-Token': 'csrf-1',
        },
      }),
    )
  })

  it('exempts only login and csrf from CSRF header injection', async () => {
    const storage = new Map<string, string>()
    vi.stubGlobal('window', {
      localStorage: {
        getItem: (key: string) => storage.get(key) || null,
        setItem: (key: string, value: string) => storage.set(key, value),
        removeItem: (key: string) => storage.delete(key),
      },
    })
    setCsrfToken('csrf-1')
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse({ ok: true }))
    vi.stubGlobal('fetch', fetchMock)

    await api('POST', '/auth/login', {})
    await api('POST', '/auth/csrf')
    await api('POST', '/auth/logout')
    await changePassword({ current_password: 'old-secret', new_password: 'new-secret' })

    expect(fetchMock.mock.calls[0]?.[1]?.headers).toEqual({ 'Content-Type': 'application/json' })
    expect(fetchMock.mock.calls[1]?.[1]?.headers).toEqual({ 'Content-Type': 'application/json' })
    expect(fetchMock.mock.calls[2]?.[1]?.headers).toEqual({
      'Content-Type': 'application/json',
      'X-Cairn-CSRF-Token': 'csrf-1',
    })
    expect(fetchMock.mock.calls[3]?.[0]).toBe('/auth/password/change')
    expect(fetchMock.mock.calls[3]?.[1]).toEqual(
      expect.objectContaining({
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'X-Cairn-CSRF-Token': 'csrf-1',
        },
        body: JSON.stringify({ current_password: 'old-secret', new_password: 'new-secret' }),
      }),
    )
  })

  it('reuses a CSRF token written by another same-origin tab', async () => {
    const sharedStorage = new Map<string, string>([['cairn:csrf-token', 'peer-token']])
    vi.stubGlobal('window', {
      localStorage: {
        getItem: (key: string) => sharedStorage.get(key) || null,
        setItem: (key: string, value: string) => sharedStorage.set(key, value),
        removeItem: (key: string) => sharedStorage.delete(key),
      },
    })
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse({ ok: true }))
    vi.stubGlobal('fetch', fetchMock)

    await api('POST', '/projects', { title: 'peer' })

    expect(fetchMock).toHaveBeenCalledWith(
      '/projects',
      expect.objectContaining({
        headers: {
          'Content-Type': 'application/json',
          'X-Cairn-CSRF-Token': 'peer-token',
        },
      }),
    )
  })

  it('re-bootstraps stale CSRF, publishes the rotation, and retries exactly once', async () => {
    const storage = new Map<string, string>()
    vi.stubGlobal('window', {
      localStorage: {
        getItem: (key: string) => storage.get(key) || null,
        setItem: (key: string, value: string) => storage.set(key, value),
        removeItem: (key: string) => storage.delete(key),
      },
    })
    setCsrfToken('stale-token')
    const invalidCsrf = {
      status: 403,
      ok: false,
      headers: new Headers({ 'content-type': 'application/json' }),
      json: async () => ({ detail: 'Invalid CSRF token' }),
      text: async () => '',
    } as Response
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(invalidCsrf)
      .mockResolvedValueOnce(jsonResponse({ csrf_token: 'fresh-token' }))
      .mockResolvedValueOnce(jsonResponse({ id: 'project-1' }))
    vi.stubGlobal('fetch', fetchMock)
    const controller = new AbortController()

    await expect(api('POST', '/projects', { title: 'one' }, controller.signal)).resolves.toEqual({
      id: 'project-1',
    })

    expect(fetchMock).toHaveBeenCalledTimes(3)
    expect(fetchMock.mock.calls[0]?.[0]).toBe('/projects')
    expect(fetchMock.mock.calls[1]?.[0]).toBe('/auth/csrf')
    expect(fetchMock.mock.calls[1]?.[1]?.headers).toEqual({ 'Content-Type': 'application/json' })
    expect(fetchMock.mock.calls[1]?.[1]?.signal).toBe(controller.signal)
    expect(fetchMock.mock.calls[2]?.[0]).toBe('/projects')
    expect(fetchMock.mock.calls[2]?.[1]?.headers).toEqual({
      'Content-Type': 'application/json',
      'X-Cairn-CSRF-Token': 'fresh-token',
    })
    expect(fetchMock.mock.calls[0]?.[1]?.signal).toBe(controller.signal)
    expect(fetchMock.mock.calls[2]?.[1]?.signal).toBe(controller.signal)
    expect(storage.get('cairn:csrf-token')).toBe('fresh-token')
  })

  it('reuses CSRF refresh and auth generation for raw Blob requests', async () => {
    const storage = new Map<string, string>()
    vi.stubGlobal('window', {
      localStorage: {
        getItem: (key: string) => storage.get(key) || null,
        setItem: (key: string, value: string) => storage.set(key, value),
        removeItem: (key: string) => storage.delete(key),
      },
    })
    setCsrfToken('stale-token')
    const file = new File([new Uint8Array([0xff, 0x00])], 'binary.bin')
    const invalidCsrf = {
      status: 403,
      ok: false,
      headers: new Headers({ 'content-type': 'application/json' }),
      json: async () => ({ detail: 'Invalid CSRF token' }),
      text: async () => '',
    } as Response
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(invalidCsrf)
      .mockResolvedValueOnce(jsonResponse({ csrf_token: 'fresh-token' }))
      .mockResolvedValueOnce(jsonResponse({ id: 'member-1' }))
    vi.stubGlobal('fetch', fetchMock)

    await expect(apiRaw('POST', '/binary', file, 'application/octet-stream')).resolves.toEqual({
      id: 'member-1',
    })

    expect(fetchMock).toHaveBeenCalledTimes(3)
    expect(fetchMock.mock.calls[0]?.[1]).toEqual(
      expect.objectContaining({
        body: file,
        credentials: 'same-origin',
        headers: {
          'Content-Type': 'application/octet-stream',
          'X-Cairn-CSRF-Token': 'stale-token',
        },
      }),
    )
    expect(fetchMock.mock.calls[2]?.[1]).toEqual(
      expect.objectContaining({
        body: file,
        headers: {
          'Content-Type': 'application/octet-stream',
          'X-Cairn-CSRF-Token': 'fresh-token',
        },
      }),
    )
  })

  it('keeps structured raw upload errors in ApiError data', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue({
        status: 422,
        ok: false,
        headers: new Headers({ 'content-type': 'application/json' }),
        json: async () => ({
          detail: {
            code: 'zip_member_reserved_name',
            message: 'ZIP 成员使用了保留名称',
            path: 'docs/manifest.json',
          },
        }),
        text: async () => '',
      } as Response),
    )

    await expect(
      apiRaw('POST', '/project-resource-batches/b1/zip', new Blob(['zip']), 'application/zip'),
    ).rejects.toMatchObject({
      status: 422,
      message: 'ZIP 成员使用了保留名称',
      data: {
        detail: {
          code: 'zip_member_reserved_name',
          path: 'docs/manifest.json',
        },
      },
    })
  })

  it('aborts CSRF bootstrap with the original request signal', async () => {
    const invalidCsrf = {
      status: 403,
      ok: false,
      headers: new Headers({ 'content-type': 'application/json' }),
      json: async () => ({ detail: 'Invalid CSRF token' }),
      text: async () => '',
    } as Response
    const abortError = new DOMException('Aborted', 'AbortError')
    const fetchMock = vi.fn().mockResolvedValueOnce(invalidCsrf).mockRejectedValueOnce(abortError)
    vi.stubGlobal('fetch', fetchMock)
    const controller = new AbortController()

    const request = api('POST', '/projects', { title: 'one' }, controller.signal)
    controller.abort()

    await expect(request).rejects.toBe(abortError)
    expect(fetchMock.mock.calls[1]?.[0]).toBe('/auth/csrf')
    expect(fetchMock.mock.calls[1]?.[1]?.signal).toBe(controller.signal)
    expect(fetchMock).toHaveBeenCalledTimes(2)
  })

  it('does not loop when the CSRF retry is rejected again', async () => {
    setCsrfToken('stale-token')
    const invalidCsrfResponse = () =>
      ({
        status: 403,
        ok: false,
        headers: new Headers({ 'content-type': 'application/json' }),
        json: async () => ({ detail: 'Invalid CSRF token' }),
        text: async () => '',
      }) as Response
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(invalidCsrfResponse())
      .mockResolvedValueOnce(jsonResponse({ csrf_token: 'fresh-token' }))
      .mockResolvedValueOnce(invalidCsrfResponse())
    vi.stubGlobal('fetch', fetchMock)

    await expect(api('POST', '/projects', {})).rejects.toMatchObject({ status: 403 })
    expect(fetchMock).toHaveBeenCalledTimes(3)
    expect(fetchMock.mock.calls.filter(([path]) => path === '/auth/csrf')).toHaveLength(1)
  })

  it('does not retry unrelated 403 responses', async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      status: 403,
      ok: false,
      headers: new Headers({ 'content-type': 'application/json' }),
      json: async () => ({ detail: 'Untrusted request origin' }),
      text: async () => '',
    } as Response)
    vi.stubGlobal('fetch', fetchMock)

    await expect(api('POST', '/projects', {})).rejects.toMatchObject({ status: 403 })
    expect(fetchMock).toHaveBeenCalledTimes(1)
  })

  it('notifies the auth layer once on protected 401 responses', async () => {
    const unauthorized = vi.fn()
    setUnauthorizedHandler(unauthorized, () => 3)
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue({
        status: 401,
        ok: false,
        headers: new Headers({ 'content-type': 'application/json' }),
        json: async () => ({ detail: 'expired' }),
        text: async () => '',
      } as Response),
    )

    await expect(api('GET', '/projects')).rejects.toMatchObject({ status: 401 })
    expect(unauthorized).toHaveBeenCalledOnce()
    expect(unauthorized).toHaveBeenCalledWith(3)
  })

  it('keeps a newer login when an older protected request returns 401 late', async () => {
    let resolveRequest!: (response: Response) => void
    const deferredResponse = new Promise<Response>((resolve) => {
      resolveRequest = resolve
    })
    const fetchMock = vi.fn().mockReturnValue(deferredResponse)
    vi.stubGlobal('fetch', fetchMock)

    let activeGeneration = 1
    let activeSession = 'new-session'
    const unauthorized = vi.fn((requestGeneration: number) => {
      if (requestGeneration === activeGeneration) activeSession = ''
    })
    setUnauthorizedHandler(unauthorized, () => activeGeneration)

    const oldRequest = api('GET', '/projects')
    activeGeneration = 2
    resolveRequest({
      status: 401,
      ok: false,
      headers: new Headers({ 'content-type': 'application/json' }),
      json: async () => ({ detail: 'expired' }),
      text: async () => '',
    } as Response)

    await expect(oldRequest).rejects.toMatchObject({ status: 401 })
    expect(unauthorized).toHaveBeenCalledWith(1)
    expect(activeSession).toBe('new-session')
  })
})

describe('Tenant monitoring client', () => {
  afterEach(() => vi.unstubAllGlobals())

  it('reads the tenant-scoped monitoring summary', async () => {
    const summary = {
      generated_at: '2026-08-18T00:00:00Z',
      long_running_threshold_seconds: 300,
      project_count: 1,
      active_project_count: 1,
      dispatcher: { status: 'running', stale: false },
      queued_tasks: [],
      running_tasks: [],
      long_running_tasks: [],
      recent_failures: [],
    }
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse(summary))
    vi.stubGlobal('fetch', fetchMock)

    expect(tenantMonitoringQueryKey).toEqual(['tenant-monitoring-summary'])
    await expect(getTenantMonitoringSummary()).resolves.toEqual(summary)
    expect(fetchMock).toHaveBeenCalledWith(
      '/monitoring/summary',
      expect.objectContaining({
        method: 'GET',
        credentials: 'same-origin',
      }),
    )
  })

  it('reads monitoring through the encoded target project', async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse({ running_tasks: [] }))
    vi.stubGlobal('fetch', fetchMock)

    await getProjectMonitoringSummary('foreign/项目')

    expect(fetchMock).toHaveBeenCalledWith(
      '/projects/foreign%2F%E9%A1%B9%E7%9B%AE/monitoring/summary',
      expect.objectContaining({ method: 'GET' }),
    )
  })
})

describe('Admin Users client', () => {
  afterEach(() => vi.unstubAllGlobals())

  it('uses paginated list and write-only create bodies', async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse([]))
    vi.stubGlobal('fetch', fetchMock)

    await listAdminUsers(25, 50)
    await createAdminUser({ login: 'user@example.test' })

    expect(fetchMock.mock.calls[0]?.[0]).toBe('/admin/users?limit=25&offset=50')
    expect(fetchMock.mock.calls[0]?.[1]?.method).toBe('GET')
    expect(fetchMock.mock.calls[1]?.[0]).toBe('/admin/users')
    expect(fetchMock.mock.calls[1]?.[1]?.body).toBe(
      JSON.stringify({
        login: 'user@example.test',
      }),
    )
  })

  it('uses encoded lifecycle action endpoints', async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValue(jsonResponse({ id: 'u/1', active_session_count: 0 }))
    vi.stubGlobal('fetch', fetchMock)

    await updateAdminUserStatus('u/1', {
      disabled: true,
      expected_updated_at: '2026-08-13T00:00:00Z',
    })
    await resetAdminUserPassword('u/1', {
      password: 'new-secret',
      expected_updated_at: '2026-08-13T00:00:01Z',
    })
    await revokeAdminUserSessions('u/1')

    expect(fetchMock.mock.calls[0]?.[0]).toBe('/admin/users/u%2F1/status')
    expect(fetchMock.mock.calls[0]?.[1]?.method).toBe('PATCH')
    expect(fetchMock.mock.calls[0]?.[1]?.body).toBe(
      JSON.stringify({
        disabled: true,
        expected_updated_at: '2026-08-13T00:00:00Z',
      }),
    )
    expect(fetchMock.mock.calls[1]?.[0]).toBe('/admin/users/u%2F1/password-reset')
    expect(fetchMock.mock.calls[1]?.[1]?.body).toBe(
      JSON.stringify({
        password: 'new-secret',
        expected_updated_at: '2026-08-13T00:00:01Z',
      }),
    )
    expect(fetchMock.mock.calls[2]?.[0]).toBe('/admin/users/u%2F1/sessions/revoke')
    expect(fetchMock.mock.calls[2]?.[1]?.method).toBe('POST')
  })
})

describe('Tenant Models client', () => {
  afterEach(() => vi.unstubAllGlobals())

  const config = {
    display_name: 'Primary GPT',
    protocol: 'openai-responses' as const,
    base_url: 'https://api.example.test/v1',
    auth_header: 'authorization-bearer' as const,
    custom_headers: [{ name: 'X-Gateway', value: 'café-route' }],
    models_endpoint: null,
    inference_endpoint: '/invoke',
    model_name: 'gpt-test',
    enabled: true,
  }

  it('sends revision-controlled CRUD bodies and optional Key only in request bodies', async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse({ id: 'm/1', revision: 2 }))
    vi.stubGlobal('fetch', fetchMock)

    await createTenantModel({ ...config, api_key: 'create-key' })
    await updateTenantModel('m/1', { ...config, expected_revision: 2, api_key: 'rotate-key' })
    await deleteTenantModel('m/1', 3)

    expect(fetchMock.mock.calls[0]?.[0]).toBe('/models')
    expect(fetchMock.mock.calls[0]?.[1]?.body).toBe(
      JSON.stringify({ ...config, api_key: 'create-key' }),
    )
    expect(String(fetchMock.mock.calls[0]?.[0])).not.toContain('create-key')
    expect(fetchMock.mock.calls[1]?.[0]).toBe('/models/m%2F1')
    expect(fetchMock.mock.calls[1]?.[1]?.body).toBe(
      JSON.stringify({
        ...config,
        expected_revision: 2,
        api_key: 'rotate-key',
      }),
    )
    expect(String(fetchMock.mock.calls[1]?.[0])).not.toContain('rotate-key')
    expect(fetchMock.mock.calls[2]?.[1]?.body).toBe(JSON.stringify({ expected_revision: 3 }))
  })

  it('uses independent synchronous draft discovery and test endpoints', async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse({ http_status: 200 }))
    vi.stubGlobal('fetch', fetchMock)

    await discoverTenantModelDraft({
      ...config,
      model_id: 'm/1',
      api_key: 'draft-key',
      timeout_seconds: 12,
    })
    await testTenantModelDraft({
      ...config,
      model_id: 'm/1',
      api_key: 'draft-key',
      timeout_seconds: 20,
      prompt: 'hello 原文',
    })

    expect(fetchMock.mock.calls[0]?.[0]).toBe('/models/discover')
    expect(fetchMock.mock.calls[0]?.[1]?.body).toBe(
      JSON.stringify({
        ...config,
        model_id: 'm/1',
        api_key: 'draft-key',
        timeout_seconds: 12,
      }),
    )
    expect(fetchMock.mock.calls[1]?.[0]).toBe('/models/test')
    expect(fetchMock.mock.calls[1]?.[1]?.body).toBe(
      JSON.stringify({
        ...config,
        model_id: 'm/1',
        api_key: 'draft-key',
        timeout_seconds: 20,
        prompt: 'hello 原文',
      }),
    )
    expect(fetchMock.mock.calls.every(([path]) => !String(path).includes('draft-key'))).toBe(true)
  })
})

describe('Tenant Workers client', () => {
  afterEach(() => vi.unstubAllGlobals())

  it('uses tenant-scoped CRUD and option endpoints with revisions', async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse({ id: 'w/1', revision: 2 }))
    vi.stubGlobal('fetch', fetchMock)
    const body = {
      display_name: 'Worker One',
      model_id: 'm1',
      task_types: ['reason', 'explore'] as const,
      max_running: 2,
      priority: 50,
      enabled: true,
    }

    await listTenantWorkerOptions()
    await getProjectManagementOptions('foreign/项目')
    await createTenantWorker({ ...body, task_types: [...body.task_types] })
    await updateTenantWorker('w/1', {
      ...body,
      task_types: [...body.task_types],
      expected_revision: 2,
    })
    await deleteTenantWorker('w/1', 3)

    expect(fetchMock.mock.calls[0]?.[0]).toBe('/tenant-workers/options')
    expect(fetchMock.mock.calls[0]?.[1]?.method).toBe('GET')
    expect(fetchMock.mock.calls[1]?.[0]).toBe(
      '/projects/foreign%2F%E9%A1%B9%E7%9B%AE/management-options',
    )
    expect(fetchMock.mock.calls[2]?.[0]).toBe('/tenant-workers')
    expect(fetchMock.mock.calls[3]?.[0]).toBe('/tenant-workers/w%2F1')
    expect(fetchMock.mock.calls[3]?.[1]?.body).toBe(
      JSON.stringify({
        ...body,
        task_types: [...body.task_types],
        expected_revision: 2,
      }),
    )
    expect(fetchMock.mock.calls[4]?.[1]?.body).toBe(JSON.stringify({ expected_revision: 3 }))
  })

  it('updates project bindings by stable Tenant Worker ID', async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse({ id: 'p1' }))
    vi.stubGlobal('fetch', fetchMock)

    await updateProjectWorkers('p/1', { tenant_worker_ids: ['w1', 'w2'] })

    expect(fetchMock).toHaveBeenCalledWith(
      '/projects/p%2F1/workers',
      expect.objectContaining({
        method: 'PUT',
        body: JSON.stringify({ tenant_worker_ids: ['w1', 'w2'] }),
      }),
    )
  })

  it('sends template bindings by stable Tenant Worker ID', async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse({ id: 'tpl1' }))
    vi.stubGlobal('fetch', fetchMock)

    await createWorkerTemplate({ name: 'Default', tenant_worker_ids: ['w1', 'w2'] })

    expect(fetchMock).toHaveBeenCalledWith(
      '/worker-templates',
      expect.objectContaining({
        method: 'POST',
        body: JSON.stringify({ name: 'Default', tenant_worker_ids: ['w1', 'w2'] }),
      }),
    )
  })
})

describe('Report template client', () => {
  afterEach(() => vi.unstubAllGlobals())

  it('uses catalog, admin version, archive, and manual request endpoints', async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse([]))
    vi.stubGlobal('fetch', fetchMock)
    const controller = new AbortController()

    await listReportTemplateCatalog(controller.signal)
    await listReportTemplates(controller.signal)
    await createReportTemplate({ key: 'security-report', name: 'Security Report' })
    await listReportTemplateVersions('security/report', controller.signal)
    await createReportTemplateVersion('security/report', {
      markdown_content: '# Report',
      message: 'Initial',
    })
    await getReportTemplateVersion('security/report', 3, controller.signal)
    await archiveReportTemplate('security/report')
    await requestAssessmentReportFinalization('project / one', {
      template_key: 'security-report',
      template_version: 3,
    })

    expect(fetchMock.mock.calls.map(([path]) => path)).toEqual([
      '/report-template-catalog',
      '/report-templates',
      '/report-templates',
      '/report-templates/security%2Freport/versions',
      '/report-templates/security%2Freport/versions',
      '/report-templates/security%2Freport/versions/3',
      '/report-templates/security%2Freport',
      '/projects/project%20%2F%20one/assessment-report-finalization/request',
    ])
    expect(fetchMock.mock.calls[2]?.[1]).toEqual(
      expect.objectContaining({
        method: 'POST',
        body: JSON.stringify({ key: 'security-report', name: 'Security Report' }),
      }),
    )
    expect(fetchMock.mock.calls[4]?.[1]?.body).toBe(
      JSON.stringify({ markdown_content: '# Report', message: 'Initial' }),
    )
    expect(fetchMock.mock.calls[6]?.[1]?.method).toBe('DELETE')
    expect(fetchMock.mock.calls[7]?.[1]?.body).toBe(
      JSON.stringify({ template_key: 'security-report', template_version: 3 }),
    )
  })
})

describe('Project hints client', () => {
  afterEach(() => vi.unstubAllGlobals())

  it('uses an encoded project path and preserves the exact hint payload', async () => {
    const created = {
      id: 'h001',
      content: 'Authorization: Bearer allowed-secret\npassword=credential',
      creator: 'usr-1',
      fact_id: null,
      intent_id: 'intent / one',
      created_at: '2026-08-21T00:00:00Z',
    }
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse(created))
    vi.stubGlobal('fetch', fetchMock)
    const body = {
      content: created.content,
      intent_id: 'intent / one',
    }

    await expect(createProjectHint('project / one', body)).resolves.toEqual(created)

    expect(fetchMock).toHaveBeenCalledWith(
      '/projects/project%20%2F%20one/hints',
      expect.objectContaining({
        method: 'POST',
        credentials: 'same-origin',
        body: JSON.stringify(body),
      }),
    )
    expect(JSON.parse(fetchMock.mock.calls[0]?.[1]?.body as string)).toEqual(body)
  })
})

describe('Project audit client', () => {
  afterEach(() => vi.unstubAllGlobals())

  it('encodes audit identifiers and preserves repeated run query order', async () => {
    const groups = [
      {
        task_run_id: 'run / one',
        events: [
          {
            id: 'event-1',
            task_run_id: 'run / one',
            project_id: 'project / one',
            ts: '2026-08-21T00:00:00Z',
            kind: 'network_flow',
            payload: {
              command: 'curl -H "Authorization: Bearer allowed-secret" https://example.test',
              cookie: 'session=credential',
            },
          },
        ],
      },
    ]
    const evidence = [
      {
        project_id: 'project / one',
        fact_id: 'fact / one',
        artifact_id: 'artifact-1',
        pointer: 'Authorization: Bearer allowed-secret\npassword=credential',
        verifier_run_id: 'run / one',
        artifact_exists: true,
        verifier_run_exists: true,
        evidence_valid: true,
        created_at: '2026-08-21T00:00:00Z',
      },
    ]
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(jsonResponse(groups))
      .mockResolvedValueOnce(jsonResponse(evidence))
    vi.stubGlobal('fetch', fetchMock)

    await expect(
      listProjectTaskRunResultEvents('project / one', ['run / one', 'run&two']),
    ).resolves.toEqual(groups)
    await expect(listProjectFactEvidence('project / one', 'fact / one')).resolves.toEqual(evidence)

    expect(fetchMock.mock.calls.map(([path]) => path)).toEqual([
      '/projects/project%20%2F%20one/task-runs/result-events?task_run_id=run+%2F+one&task_run_id=run%26two',
      '/projects/project%20%2F%20one/facts/fact%20%2F%20one/evidence',
    ])
    expect(fetchMock.mock.calls[0]?.[1]).toEqual(
      expect.objectContaining({ method: 'GET', credentials: 'same-origin' }),
    )
    expect(fetchMock.mock.calls[1]?.[1]).toEqual(
      expect.objectContaining({ method: 'GET', credentials: 'same-origin' }),
    )
  })
})

describe('Project targets client', () => {
  afterEach(() => vi.unstubAllGlobals())

  it('uses encoded target endpoints and exact mutation payloads', async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(jsonResponse([]))
      .mockResolvedValueOnce(jsonResponse({ id: 'target / one', status: 'discovered' }))
      .mockResolvedValueOnce(jsonResponse({ id: 'target / one', status: 'approved' }))
    vi.stubGlobal('fetch', fetchMock)
    const createBody = {
      value: 'https://example.test/path',
      type: 'url' as const,
      status: 'discovered' as const,
      source_fact_id: 'fact-1',
      discovered_by_run_id: 'run-1',
    }
    const updateBody = { status: 'approved' as const }

    await listProjectTargets('project / one')
    await createProjectTarget('project / one', createBody)
    await updateProjectTarget('project / one', 'target / one', updateBody)

    expect(fetchMock.mock.calls.map(([path]) => path)).toEqual([
      '/projects/project%20%2F%20one/targets',
      '/projects/project%20%2F%20one/targets',
      '/projects/project%20%2F%20one/targets/target%20%2F%20one',
    ])
    expect(fetchMock.mock.calls[0]?.[1]).toEqual(expect.objectContaining({ method: 'GET' }))
    expect(fetchMock.mock.calls[1]?.[1]).toEqual(
      expect.objectContaining({
        method: 'POST',
        body: JSON.stringify(createBody),
      }),
    )
    expect(fetchMock.mock.calls[2]?.[1]).toEqual(
      expect.objectContaining({
        method: 'PUT',
        body: JSON.stringify(updateBody),
      }),
    )
  })
})

describe('console request cancellation', () => {
  afterEach(() => vi.unstubAllGlobals())

  it('passes AbortSignal through knowledge and prompt read helpers', async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse([]))
    vi.stubGlobal('fetch', fetchMock)
    const controller = new AbortController()

    await listKnowledgeSets(controller.signal)
    await listKnowledgeFiles('knowledge / one', controller.signal)
    await readKnowledgeFile('knowledge / one', 'nested/file.md', controller.signal)
    await listAdvisoryLibraryRevisions('knowledge / one', controller.signal)
    await listPromptGroups(controller.signal)
    await listPromptFiles('prompt / one', controller.signal)
    await readPromptFile('prompt / one', 'reason.md', controller.signal)
    await validatePromptGroup('prompt / one', controller.signal)

    expect(fetchMock.mock.calls.map(([path]) => path)).toEqual([
      '/knowledge',
      '/knowledge/knowledge%20%2F%20one/files',
      '/knowledge/knowledge%20%2F%20one/files/nested/file.md',
      '/libraries/knowledge%20%2F%20one/versions',
      '/prompt-groups',
      '/prompt-groups/prompt%20%2F%20one/files',
      '/prompt-groups/prompt%20%2F%20one/files/reason.md',
      '/prompt-groups/prompt%20%2F%20one/validate',
    ])
    for (const call of fetchMock.mock.calls) {
      expect(call[1]).toEqual(expect.objectContaining({ signal: controller.signal }))
    }
  })

  it('keeps AbortError as cancellation instead of converting it to an API error', async () => {
    const abortError = new DOMException('Aborted', 'AbortError')
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(abortError))
    const controller = new AbortController()

    await expect(readKnowledgeFile('knowledge-a', 'README.md', controller.signal)).rejects.toBe(
      abortError,
    )
    await expect(readPromptFile('prompt-a', 'reason.md', controller.signal)).rejects.toBe(
      abortError,
    )
  })
})

describe('artifact client', () => {
  afterEach(() => vi.unstubAllGlobals())

  it('lists project artifacts with complete metadata through an encoded path', async () => {
    const artifact = {
      id: 'artifact / one',
      project_id: 'project / one',
      task_run_id: 'run-1',
      kind: 'report',
      kind_deprecated: false,
      deprecation_message: null,
      category: 'deliverable',
      uri: 'reports/final.md',
      sha256: 'abc',
      size: 100,
      created_at: '2026-08-20T00:00:00Z',
      metadata: { source: 'worker' },
    }
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse([artifact]))
    vi.stubGlobal('fetch', fetchMock)

    await expect(listProjectArtifacts('project / one')).resolves.toEqual([artifact])
    expect(fetchMock).toHaveBeenCalledWith(
      '/projects/project%20%2F%20one/artifacts',
      expect.objectContaining({ method: 'GET', credentials: 'same-origin' }),
    )
  })

  it('keeps artifact completeness metadata and uses encoded head and tail paths', async () => {
    const response = {
      artifact_id: 'artifact / one',
      project_id: 'project / one',
      task_run_id: 'tr001',
      kind: 'raw_output',
      kind_deprecated: false,
      deprecation_message: null,
      content: 'preview',
      size: 100,
      bytes_read: 7,
      truncated: true,
      sha256: 'abc',
    }
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse(response))
    vi.stubGlobal('fetch', fetchMock)
    const controller = new AbortController()

    const head = await fetchArtifactPreview('project / one', 'artifact / one', controller.signal)
    const tail = await fetchArtifactTailPreview(
      'project / one',
      'artifact / one',
      controller.signal,
    )

    expect(head).toEqual(response)
    expect(tail).toEqual(response)
    expect(fetchMock.mock.calls.map(([path]) => path)).toEqual([
      '/projects/project%20%2F%20one/artifacts/artifact%20%2F%20one/preview',
      '/projects/project%20%2F%20one/artifacts/artifact%20%2F%20one/preview-tail',
    ])
    for (const call of fetchMock.mock.calls) {
      expect(call[1]).toEqual(expect.objectContaining({ signal: controller.signal }))
    }
  })

  it('normalizes legacy string responses for both preview windows', async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(jsonResponse('头部'))
      .mockResolvedValueOnce(jsonResponse('tail'))
    vi.stubGlobal('fetch', fetchMock)

    await expect(fetchArtifactPreview('p1', 'a001')).resolves.toMatchObject({
      artifact_id: 'a001',
      project_id: 'p1',
      content: '头部',
      bytes_read: 6,
      size: 6,
      truncated: false,
    })
    await expect(fetchArtifactTailPreview('p1', 'a001')).resolves.toMatchObject({
      content: 'tail',
      bytes_read: 4,
      size: 4,
    })
  })

  it('keeps the legacy packet helper string response and cancellation', async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse('legacy packet'))
    vi.stubGlobal('fetch', fetchMock)
    const controller = new AbortController()

    expect(await fetchVulnerabilityPacket('p1', 'a001', controller.signal)).toBe('legacy packet')
    expect(fetchMock).toHaveBeenCalledWith(
      '/projects/p1/artifacts/a001/preview',
      expect.objectContaining({ signal: controller.signal }),
    )
  })
})

describe('dispatcher control client', () => {
  afterEach(() => vi.unstubAllGlobals())

  it('sends revision with provider mutations without exposing credentials', async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValue(jsonResponse({ revision: 'r2', config: {}, content: '', path: '' }))
    vi.stubGlobal('fetch', fetchMock)
    await saveDispatcherProvider(
      {
        id: 'anthropic-main',
        base_url: 'https://api.anthropic.com',
        provider_api: 'anthropic-messages',
      },
      'r1',
    )
    expect(fetchMock).toHaveBeenCalledWith(
      '/dispatcher/providers',
      expect.objectContaining({
        method: 'POST',
        body: JSON.stringify({
          revision: 'r1',
          item: {
            id: 'anthropic-main',
            base_url: 'https://api.anthropic.com',
            provider_api: 'anthropic-messages',
          },
        }),
      }),
    )
  })

  it('uses the write-only credential endpoint for key rotation', async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValue(jsonResponse({ provider_id: 'p1', configured: true, revision: 'r2' }))
    vi.stubGlobal('fetch', fetchMock)
    await setDispatcherProviderCredential('p1', 'new-secret', 'r1')
    expect(fetchMock).toHaveBeenCalledWith(
      '/dispatcher/providers/p1/credential',
      expect.objectContaining({
        method: 'PUT',
        body: JSON.stringify({ revision: 'r1', credential: 'new-secret' }),
      }),
    )
  })

  it('starts an async connectivity diagnostic', async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      jsonResponse({
        test_id: 'ct_1',
        status: 'queued',
        poll_url: '/dispatcher/connectivity-tests/ct_1',
      }),
    )
    vi.stubGlobal('fetch', fetchMock)
    await startConnectivityTest('workers', 'w1', 12)
    expect(fetchMock).toHaveBeenCalledWith(
      '/dispatcher/workers/w1/connectivity-tests',
      expect.objectContaining({
        method: 'POST',
        body: JSON.stringify({ timeout_seconds: 12 }),
      }),
    )
  })

  it('reads project coverage resources using encoded project ids', async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(jsonResponse({ project: { id: 'project / one' } }))
      .mockResolvedValueOnce(jsonResponse([{ surface_id: 'surface-1' }]))
      .mockResolvedValueOnce(jsonResponse([{ slot_key: 'cell-1' }]))
      .mockResolvedValueOnce(jsonResponse({ project_id: 'project / one', status: 'pending' }))
    vi.stubGlobal('fetch', fetchMock)
    const controller = new AbortController()

    await expect(getProjectDetail('project / one')).resolves.toMatchObject({
      project: { id: 'project / one' },
    })
    await expect(
      listProjectSurfaceManifest('project / one', controller.signal),
    ).resolves.toHaveLength(1)
    await expect(
      listProjectCoverageCells('project / one', controller.signal),
    ).resolves.toHaveLength(1)
    await expect(
      getAssessmentReportFinalization('project / one', controller.signal),
    ).resolves.toMatchObject({
      status: 'pending',
    })

    expect(fetchMock.mock.calls.map(([path]) => path)).toEqual([
      '/projects/project%20%2F%20one',
      '/projects/project%20%2F%20one/surface-manifest',
      '/projects/project%20%2F%20one/coverage-slots',
      '/projects/project%20%2F%20one/assessment-report-finalization',
    ])
    for (const call of fetchMock.mock.calls.slice(1)) {
      expect(call[1]).toEqual(expect.objectContaining({ signal: controller.signal }))
    }
  })

  it('preserves vulnerability filters, cancellation, ids, and mutation bodies', async () => {
    setCsrfToken('csrf-token')
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(jsonResponse([]))
      .mockResolvedValueOnce(jsonResponse([]))
      .mockResolvedValueOnce(jsonResponse({ id: 'vulnerability / one' }))
      .mockResolvedValueOnce(
        jsonResponse({
          project_id: 'project / one',
          requested_vulnerability_id: 'vulnerability / one',
          rule_version: 1,
          advisory: true,
          candidates: [],
        }),
      )
      .mockResolvedValueOnce(jsonResponse({ id: 'vulnerability / one' }))
      .mockResolvedValueOnce(jsonResponse({ id: 'vulnerability / one', status: 'confirmed' }))
    vi.stubGlobal('fetch', fetchMock)
    const controller = new AbortController()
    const createBody = {
      title: 'Authorization: Bearer full-token <script>alert(1)</script>',
      severity: 'high',
      vuln_type: 'IDOR',
      target: 'GET /users/2?password=clear-text',
      remediation: '校验资源所有者。',
      confidence: 0.9,
      repro_request: 'Cookie: session=credential-secret',
      repro_response: 'HTTP/1.1 200 OK\n\n{"password":"visible"}',
    }

    await listVulnerabilities('project / one', 'high', 'new', controller.signal)
    await listAllVulnerabilities('critical', 'confirmed', controller.signal)
    await getVulnerability('project / one', 'vulnerability / one', controller.signal)
    await getDuplicateVulnerabilityCandidates(
      'project / one',
      'vulnerability / one',
      controller.signal,
    )
    await createVulnerability('project / one', createBody)
    await updateVulnerability('project / one', 'vulnerability / one', {
      status: 'confirmed',
    })

    expect(fetchMock.mock.calls.map(([path]) => path)).toEqual([
      '/projects/project%20%2F%20one/vulnerabilities?severity=high&status=new',
      '/vulnerabilities?severity=critical&status=confirmed',
      '/projects/project%20%2F%20one/vulnerabilities/vulnerability%20%2F%20one',
      '/projects/project%20%2F%20one/vulnerabilities/vulnerability%20%2F%20one/duplicate-candidates',
      '/projects/project%20%2F%20one/vulnerabilities',
      '/projects/project%20%2F%20one/vulnerabilities/vulnerability%20%2F%20one',
    ])
    for (const call of fetchMock.mock.calls.slice(0, 4)) {
      expect(call[1]).toEqual(expect.objectContaining({ signal: controller.signal }))
    }
    expect(fetchMock.mock.calls[4]?.[1]).toEqual(
      expect.objectContaining({ method: 'POST', body: JSON.stringify(createBody) }),
    )
    expect(fetchMock.mock.calls[5]?.[1]).toEqual(
      expect.objectContaining({
        method: 'PATCH',
        body: JSON.stringify({ status: 'confirmed' }),
      }),
    )
  })

  it('omits absent vulnerability filters', async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse([]))
    vi.stubGlobal('fetch', fetchMock)

    await listVulnerabilities('project-a')
    await listAllVulnerabilities()

    expect(fetchMock.mock.calls.map(([path]) => path)).toEqual([
      '/projects/project-a/vulnerabilities',
      '/vulnerabilities',
    ])
  })

  it('encodes Payload list filters, detail ids, categories, and export paths', async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(jsonResponse({ items: [], total: 0, limit: 50, offset: 50 }))
      .mockResolvedValueOnce(jsonResponse([]))
      .mockResolvedValueOnce(jsonResponse({ id: 'p / 1', project_id: 'project / one' }))
    vi.stubGlobal('fetch', fetchMock)
    const controller = new AbortController()

    await listProjectInjectionPayloads(
      'project / one',
      {
        injectionType: 'RAG 注入 / 文档',
        responseStatus: 'returned',
        search: '<script>alert(1)</script>',
        limit: 50,
        offset: 50,
      },
      controller.signal,
    )
    await listProjectInjectionPayloadCategories('project / one', controller.signal)
    await getProjectInjectionPayload('project / one', 'p / 1', controller.signal)

    expect(fetchMock.mock.calls.map(([path]) => path)).toEqual([
      '/projects/project%20%2F%20one/injection-payloads?injection_type=RAG+%E6%B3%A8%E5%85%A5+%2F+%E6%96%87%E6%A1%A3&response_status=returned&search=%3Cscript%3Ealert%281%29%3C%2Fscript%3E&limit=50&offset=50',
      '/projects/project%20%2F%20one/injection-payloads/categories',
      '/projects/project%20%2F%20one/injection-payloads/p%20%2F%201',
    ])
    for (const call of fetchMock.mock.calls) {
      expect(call[1]).toEqual(expect.objectContaining({ signal: controller.signal }))
    }
    expect(projectInjectionPayloadExportUrl('project / one')).toBe(
      '/projects/project%20%2F%20one/injection-payloads/export.jsonl',
    )
  })

  it('uses the resource config, raw batch, member, ZIP, cancel, and commit contracts', async () => {
    const batch = {
      id: 'batch / one',
      state: 'open',
      created_at: '2026-09-01T00:00:00Z',
      expires_at: '2026-09-02T00:00:00Z',
      resource_count: 0,
      total_bytes: 0,
      members: [],
    }
    const member = {
      id: 'member / one',
      kind: 'file',
      path: 'docs/diagram one.png',
      size_bytes: 2,
      sha256: 'a'.repeat(64),
      source: 'file',
    }
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(jsonResponse({ kinds: ['note', 'script', 'file'] }))
      .mockResolvedValueOnce(jsonResponse(batch))
      .mockResolvedValueOnce(jsonResponse(batch))
      .mockResolvedValueOnce(jsonResponse(member))
      .mockResolvedValueOnce(jsonResponse({ ...batch, members: [member] }))
      .mockResolvedValueOnce(jsonResponse(member))
      .mockResolvedValueOnce({ status: 204, ok: true, headers: new Headers() } as Response)
      .mockResolvedValueOnce({ status: 204, ok: true, headers: new Headers() } as Response)
      .mockResolvedValueOnce(jsonResponse({ resources: [] }))
    vi.stubGlobal('fetch', fetchMock)
    const file = new File([new Uint8Array([0xff, 0x00])], 'diagram.png', { type: 'image/png' })
    const zip = new File(['zip'], 'bundle.zip', { type: 'application/zip' })

    await getProjectResourceConfig()
    await createProjectResourceBatch()
    await getProjectResourceBatch('batch / one')
    await uploadProjectResourceBatchFile('batch / one', file, 'docs/diagram one.png')
    await importProjectResourceBatchZip('batch / one', zip)
    await updateProjectResourceBatchMember('batch / one', 'member / one', {
      path: 'docs/renamed.png',
      kind: 'file',
    })
    await deleteProjectResourceBatchMember('batch / one', 'member / one')
    await cancelProjectResourceBatch('batch / one')
    await commitProjectResourceBatch('project / one', 'batch / one')

    expect(fetchMock.mock.calls.map(([path]) => path)).toEqual([
      '/project-resource-config',
      '/project-resource-batches',
      '/project-resource-batches/batch%20%2F%20one',
      '/project-resource-batches/batch%20%2F%20one/files?path=docs%2Fdiagram+one.png',
      '/project-resource-batches/batch%20%2F%20one/zip',
      '/project-resource-batches/batch%20%2F%20one/members/member%20%2F%20one',
      '/project-resource-batches/batch%20%2F%20one/members/member%20%2F%20one',
      '/project-resource-batches/batch%20%2F%20one',
      '/projects/project%20%2F%20one/resource-imports',
    ])
    expect(fetchMock.mock.calls[3]?.[1]).toEqual(
      expect.objectContaining({
        method: 'POST',
        body: file,
        headers: { 'Content-Type': 'application/octet-stream' },
      }),
    )
    expect(fetchMock.mock.calls[4]?.[1]).toEqual(
      expect.objectContaining({ body: zip, headers: { 'Content-Type': 'application/zip' } }),
    )
    expect(fetchMock.mock.calls[8]?.[1]?.body).toBe(
      JSON.stringify({ resource_batch_id: 'batch / one' }),
    )
  })

  it('creates projects and manages encoded project resource endpoints', async () => {
    setCsrfToken('csrf-token')
    const content = 'username=tester\npassword=allowed-secret\n'
    const resource = {
      id: 'resource / one',
      project_id: 'project / one',
      kind: 'script',
      path: 'auth/login.py',
      size_bytes: content.length,
      sha256: 'a'.repeat(64),
      active: true,
    }
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(
        jsonResponse({ project: { id: 'project / one' }, resources: [resource] }),
      )
      .mockResolvedValueOnce(jsonResponse([resource]))
      .mockResolvedValueOnce({
        status: 200,
        ok: true,
        headers: new Headers({ 'content-type': 'text/plain; charset=utf-8' }),
        text: async () => content,
      } as Response)
      .mockResolvedValueOnce(jsonResponse(resource))
      .mockResolvedValueOnce({ status: 204, ok: true, headers: new Headers() } as Response)
    vi.stubGlobal('fetch', fetchMock)

    const request = {
      title: 'Project',
      origin: 'origin',
      goal: 'goal',
      scenario: 'default-copy-v2',
      tenant_worker_ids: ['w1'],
      group_id: 'group-a',
      explore_budget: 40,
      authorization_expires_at: null,
      resources: [{ kind: 'script' as const, path: 'auth/login.py', content }],
    }
    await expect(createProject(request)).resolves.toMatchObject({
      project: { id: 'project / one' },
    })
    await expect(listProjectResources('project / one')).resolves.toHaveLength(1)
    await expect(readProjectResource('project / one', 'resource / one')).resolves.toBe(content)
    await expect(
      createProjectResource('project / one', request.resources[0]),
    ).resolves.toMatchObject({ id: 'resource / one' })
    await expect(deactivateProjectResource('project / one', 'resource / one')).resolves.toBeNull()

    expect(fetchMock.mock.calls.map(([path]) => path)).toEqual([
      '/projects',
      '/projects/project%20%2F%20one/resources',
      '/projects/project%20%2F%20one/resources/resource%20%2F%20one',
      '/projects/project%20%2F%20one/resources',
      '/projects/project%20%2F%20one/resources/resource%20%2F%20one',
    ])
    expect(fetchMock.mock.calls[0][1]).toEqual(
      expect.objectContaining({
        method: 'POST',
        body: JSON.stringify(request),
      }),
    )
    expect(fetchMock.mock.calls[2][1]).toEqual({ credentials: 'same-origin' })
    expect(fetchMock.mock.calls[3][1]).toEqual(
      expect.objectContaining({
        method: 'POST',
        body: JSON.stringify(request.resources[0]),
      }),
    )
    expect(fetchMock.mock.calls[4][1]).toEqual(expect.objectContaining({ method: 'DELETE' }))
  })

  it('lists tenant reports with encoded filters, pagination, and AbortSignal support', async () => {
    const page = { items: [], total: 0, page: 3, page_size: 50 }
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse(page))
    vi.stubGlobal('fetch', fetchMock)
    const controller = new AbortController()

    await expect(
      listReports(
        {
          q: 'final report',
          project_id: 'project / one',
          template_key: 'durable/report',
          date_from: '2026-08-01',
          date_to: '2026-08-29',
          page: 3,
          page_size: 50,
        },
        controller.signal,
      ),
    ).resolves.toEqual(page)
    expect(fetchMock).toHaveBeenCalledWith(
      '/reports?q=final+report&project_id=project+%2F+one&template_key=durable%2Freport&date_from=2026-08-01&date_to=2026-08-29&page=3&page_size=50',
      expect.objectContaining({
        method: 'GET',
        credentials: 'same-origin',
        signal: controller.signal,
      }),
    )
  })

  it('treats a missing assessment report finalization as not started', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue({
        status: 404,
        ok: false,
        headers: new Headers({ 'content-type': 'application/json' }),
        json: async () => ({ detail: 'assessment_report_finalization_not_found' }),
        text: async () => '',
      } as Response),
    )

    await expect(getAssessmentReportFinalization('legacy-project')).resolves.toBeNull()
  })

  it('does not hide unrelated finalization 404 responses', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue({
        status: 404,
        ok: false,
        headers: new Headers({ 'content-type': 'application/json' }),
        json: async () => ({ detail: 'Project not found' }),
        text: async () => '',
      } as Response),
    )

    await expect(getAssessmentReportFinalization('missing-project')).rejects.toMatchObject({
      status: 404,
      data: { detail: 'Project not found' },
    })
  })

  it('does not convert an aborted finalization request into not-started', async () => {
    const controller = new AbortController()
    const abortError = new DOMException('Aborted', 'AbortError')
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(abortError))

    await expect(getAssessmentReportFinalization('project-a', controller.signal)).rejects.toBe(
      abortError,
    )
  })

  it('does not inject or prompt for a legacy administrator token on 401', async () => {
    const prompt = vi.fn()
    const localStorage = { getItem: vi.fn(() => null), setItem: vi.fn(), removeItem: vi.fn() }
    vi.stubGlobal('window', { prompt, localStorage })
    const fetchMock = vi.fn().mockResolvedValue({
      status: 401,
      ok: false,
      headers: new Headers({ 'content-type': 'application/json' }),
      json: async () => ({ detail: 'unauthorized' }),
      text: async () => '',
    } as Response)
    vi.stubGlobal('fetch', fetchMock)

    await expect(startConnectivityTest('workers', 'w1')).rejects.toMatchObject({
      status: 401,
    } satisfies Partial<ApiError>)
    expect(fetchMock).toHaveBeenCalledTimes(1)
    expect(fetchMock.mock.calls[0]?.[1]?.headers).toEqual({ 'Content-Type': 'application/json' })
    expect(prompt).not.toHaveBeenCalled()
    expect(localStorage.getItem).toHaveBeenCalledWith('cairn:csrf-token')
    expect(localStorage.setItem).not.toHaveBeenCalled()
  })
})
