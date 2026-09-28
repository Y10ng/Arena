export const injectionPayloadKeys = {
  project: (projectId: string) => ['injection-payloads', 'project', projectId] as const,
  list: (
    projectId: string,
    injectionType: string,
    responseStatus: string,
    search: string,
    limit: number,
    offset: number,
    visitGeneration: number,
  ) =>
    [
      ...injectionPayloadKeys.project(projectId),
      'list',
      injectionType,
      responseStatus,
      search,
      limit,
      offset,
      visitGeneration,
    ] as const,
  categories: (projectId: string, visitGeneration: number) =>
    [...injectionPayloadKeys.project(projectId), 'categories', visitGeneration] as const,
  detail: (projectId: string, payloadId: string | null, visitGeneration: number) =>
    [...injectionPayloadKeys.project(projectId), 'detail', payloadId, visitGeneration] as const,
}
