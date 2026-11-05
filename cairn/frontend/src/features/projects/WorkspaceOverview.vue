<template>
  <section v-if="detail" class="project-overview" aria-label="项目总览">
    <div class="project-overview-originals">
      <section v-for="id in ['origin', 'goal']" :key="id" :aria-label="id === 'origin' ? 'Origin' : 'Goal'">
        <h2>{{ id === 'origin' ? 'Origin' : 'Goal' }}</h2>
        <p class="project-original">{{ detail.facts.find((fact) => fact.id === id)?.description ?? '未记录' }}</p>
      </section>
    </div>
    <aside class="project-overview-materials">
      <section class="project-overview-consumption" aria-label="累计 Token">
        <h2>累计 Token</h2><ProjectConsumption :consumption="detail.consumption" />
      </section>
      <section aria-label="辅助资料">
        <header ref="materialHeader"><h2>辅助资料</h2><Button size="sm" variant="secondary" @click="editorOpen = true; editorStarted = true">{{ hasDraft ? '继续添加' : '添加资料' }}</Button></header>
        <p v-if="!canMutateResources" class="material-note">{{ resourceMutationMessage }}</p>
        <Alert v-if="error" tone="danger">{{ error }}</Alert>
        <h3>上传文件</h3>
        <ul class="material-list">
          <li v-for="resource in detail.resources" :key="resource.id">
            <div><a :href="projectResourceUrl(projectId, resource.id)" download @click.prevent="download(projectResourceUrl(projectId, resource.id))">{{ resource.path }}</a>
              <small v-if="downloadErrors[projectResourceUrl(projectId, resource.id)]" role="alert">{{ downloadErrors[projectResourceUrl(projectId, resource.id)] }}</small>
              <small>{{ projectResourceKindLabel(resource.kind) }} · {{ formatProjectResourceSize(resource.size_bytes) }}</small></div>
            <Button size="sm" variant="ghost" :disabled="!canMutateResources || busy || !!mutationUnknown" @click="deactivate(resource.id)">停用</Button>
          </li>
        </ul>
        <p v-if="!detail.resources?.length" class="material-note">没有上传文件</p>
        <Button v-if="mutationUnknown" size="sm" variant="secondary" @click="checkDeactivation">核对停用结果</Button>
        <h3>资料库文件</h3>
        <div v-for="binding in detail.library_bindings" :key="`${binding.library_key}:${binding.version}`" class="bound-library">
          <details><summary>{{ binding.library_name }} · v{{ binding.version }}</summary><small class="material-digest">{{ binding.digest_sha256 }}</small></details>
          <p v-if="libraryErrors[binding.library_key]" role="alert">{{ libraryErrors[binding.library_key] }} <Button size="sm" variant="ghost" @click="loadLibraries">重试</Button></p>
          <ul class="material-list"><li v-for="file in libraryFiles[binding.library_key] || []" :key="file.path"><div>
            <a :href="libraryFileUrl(binding.library_key, binding.version, file.path)" download @click.prevent="download(libraryFileUrl(binding.library_key, binding.version, file.path))">{{ file.path }}</a>
            <small v-if="downloadErrors[libraryFileUrl(binding.library_key, binding.version, file.path)]" role="alert">{{ downloadErrors[libraryFileUrl(binding.library_key, binding.version, file.path)] }}</small>
            <small>{{ formatProjectResourceSize(file.size_bytes) }}</small>
          </div></li></ul>
        </div>
        <p v-if="!detail.library_bindings?.length" class="material-note">没有绑定资料库文件</p>
      </section>
    </aside>
    <Dialog v-if="editorStarted" :open="editorOpen" title="添加资料" size="lg" :unmount-on-hide="false" :dismissible="!busy" :close-on-outside="false" @update:open="editorOpen = $event">
      <p v-if="!canMutateResources" class="material-note">{{ resourceMutationMessage }}</p>
      <Alert v-if="commitUnknown" tone="warning" title="导入结果未确认">草稿已保留，请先核对批次，不要重复提交。
        <template #actions><Button size="sm" variant="secondary" :disabled="busy" @click="checkCommit">核对导入结果</Button></template>
      </Alert>
      <Alert v-if="error" tone="danger">{{ error }}</Alert>
      <ProjectResourceBatchUploader ref="uploader" v-model="batch" :disabled="!canMutateResources || saving || commitUnknown"
        :existing-count="detail.resources.length" :existing-bytes="resourceBytes" :existing-paths="detail.resources.map((item) => item.path)"
        @busy="uploadBusy = $event" @pending="uploadPending = $event" />
      <template #footer>
        <Button variant="ghost" :disabled="busy || commitUnknown" @click="discard">放弃暂存</Button>
        <Button variant="secondary" :disabled="busy" @click="editorOpen = false">关闭</Button>
        <Button :loading="saving" :disabled="!canMutateResources || busy || uploadPending || commitUnknown || !batch?.members.length" @click="save">提交增补</Button>
      </template>
    </Dialog>
  </section>
</template>

<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { onBeforeRouteLeave, onBeforeRouteUpdate } from 'vue-router'
import { Alert, Button, Dialog } from '@/components/ui'
import { useWorkspaceStore } from '@/stores/workspace'
import { ApiError, api, commitProjectResourceBatch, deactivateProjectResource, getProjectResourceBatch, projectResourceUrl } from '@/api/client'
import type { ProjectResourceBatch } from '@/api/types'
import type { AdvisoryLibraryFile } from '@/api/types/libraries'
import { formatProjectResourceSize, projectResourceKindLabel } from './projectResources'
import ProjectResourceBatchUploader from './ProjectResourceBatchUploader.vue'
import ProjectConsumption from './ProjectConsumption.vue'

defineProps<{ canMutateResources: boolean; resourceMutationMessage: string }>()
const store = useWorkspaceStore()
const detail = computed(() => store.projectDetail)
const projectId = computed(() => detail.value?.project.id || '')
const resourceBytes = computed(() => (detail.value?.resources || []).reduce((sum, item) => sum + item.size_bytes, 0))
const libraryFiles = ref<Record<string, AdvisoryLibraryFile[]>>({})
const libraryErrors = ref<Record<string, string>>({})
const downloadErrors = ref<Record<string, string>>({})
const downloading = new Set<string>()
async function download(url: string) {
  if (downloading.has(url)) return
  const generation = libraryGeneration
  downloading.add(url)
  delete downloadErrors.value[url]
  try {
    // 只预检状态与原件完整性；正文交给浏览器下载，不进入 JS 堆。
    const response = await fetch(url, { method: 'HEAD', credentials: 'same-origin', cache: 'no-store' })
    if (!response.ok) throw new Error(`材料读取失败（${response.status}），请重试`)
    if (generation !== libraryGeneration) return
    const link = document.createElement('a')
    link.href = url
    link.download = ''
    link.click()
  } catch (err) {
    if (generation === libraryGeneration) downloadErrors.value[url] = err instanceof Error ? err.message : '材料读取失败，请重试'
  } finally { downloading.delete(url) }
}
let libraryGeneration = 0
const librarySignature = computed(() => JSON.stringify([projectId.value, detail.value?.library_bindings]))
const libraryBase = (key: string, version: number) => `/projects/${encodeURIComponent(projectId.value)}/advisory-libraries/${encodeURIComponent(key)}/versions/${version}/files`
const libraryFileUrl = (key: string, version: number, path: string) => `${libraryBase(key, version)}/${path.split('/').map(encodeURIComponent).join('/')}`
async function loadLibraries() {
  const generation = ++libraryGeneration
  libraryFiles.value = {}
  libraryErrors.value = {}
  downloadErrors.value = {}
  await Promise.all((detail.value?.library_bindings || []).map(async (binding) => {
    try {
      const files = await api<AdvisoryLibraryFile[]>('GET', libraryBase(binding.library_key, binding.version))
      if (generation === libraryGeneration) libraryFiles.value[binding.library_key] = files
    } catch {
      if (generation === libraryGeneration) libraryErrors.value[binding.library_key] = '资料列表读取失败'
    }
  }))
}
watch(librarySignature, loadLibraries, { immediate: true })
const materialHeader = ref<HTMLElement | null>(null)
const editorOpen = ref(false)
watch(editorOpen, async (open) => {
  if (open) return
  // 保留草稿的 Dialog 不卸载，关闭时显式恢复入口焦点。
  await nextTick()
  materialHeader.value?.querySelector('button')?.focus()
})
const editorStarted = ref(false)
const batch = ref<ProjectResourceBatch | null>(null)
const uploader = ref<{ cancel: () => Promise<void> } | null>(null)
const uploadBusy = ref(false)
const uploadPending = ref(false)
const saving = ref(false)
const commitUnknown = ref(false)
const mutationUnknown = ref('')
const error = ref('')
const busy = computed(() => saving.value || uploadBusy.value)
const hasDraft = computed(() => busy.value || uploadPending.value || Boolean(batch.value?.members.length) || commitUnknown.value || !!mutationUnknown.value)
const rejected = (err: unknown) => err instanceof ApiError && err.status >= 400 && err.status < 500 && err.status !== 408
// 切项目或卸载后，旧请求不得更新新页面；也覆盖离开后又回到同一项目的情况。
let mutationGeneration = 0
async function finishCommit(id: string, generation: number) {
  if (generation !== mutationGeneration || projectId.value !== id) return
  batch.value = null
  commitUnknown.value = false
  editorOpen.value = false
  error.value = ''
  await store.loadProject(id)
}
async function save() {
  if (!batch.value || busy.value || commitUnknown.value) return
  const id = projectId.value
  const generation = mutationGeneration
  saving.value = true
  error.value = ''
  try { await commitProjectResourceBatch(id, batch.value.id); await finishCommit(id, generation) }
  catch (err) {
    if (generation !== mutationGeneration) return
    commitUnknown.value = !rejected(err)
    error.value = err instanceof Error ? err.message : '资料导入失败'
  } finally { if (generation === mutationGeneration) saving.value = false }
}
async function checkCommit() {
  if (!batch.value || busy.value) return
  const id = projectId.value
  const generation = mutationGeneration
  saving.value = true
  try {
    const current = await getProjectResourceBatch(batch.value.id)
    if (generation !== mutationGeneration) return
    if (current.state === 'consumed' && current.consumed_project_id === id) await finishCommit(id, generation)
    else error.value = '尚未确认本次导入完成，草稿已保留。'
  } catch { if (generation === mutationGeneration) error.value = '核对失败，草稿已保留。' }
  finally { if (generation === mutationGeneration) saving.value = false }
}
async function discard() {
  const generation = mutationGeneration
  try {
    await uploader.value?.cancel()
    if (generation !== mutationGeneration) return false
    batch.value = null; error.value = ''; return true
  } catch (err) {
    if (generation === mutationGeneration) error.value = err instanceof Error ? err.message : '取消暂存失败'
    return false
  }
}
async function deactivate(resourceId: string) {
  if (busy.value || mutationUnknown.value) return
  const id = projectId.value
  const generation = mutationGeneration
  saving.value = true
  error.value = ''
  try { await deactivateProjectResource(id, resourceId); if (generation === mutationGeneration) await store.loadProject(id) }
  catch (err) {
    if (generation !== mutationGeneration) return
    error.value = err instanceof Error ? err.message : '停用失败'
    if (!rejected(err)) mutationUnknown.value = resourceId
  } finally { if (generation === mutationGeneration) saving.value = false }
}
async function checkDeactivation() {
  if (busy.value) return
  const id = projectId.value
  const generation = mutationGeneration
  saving.value = true
  try {
    await store.loadProject(id)
    if (generation === mutationGeneration && detail.value && !store.error && !detail.value.resources.some((item) => item.id === mutationUnknown.value)) {
      mutationUnknown.value = ''; error.value = ''
    }
  } finally { if (generation === mutationGeneration) saving.value = false }
}
async function mayLeave() {
  if (!hasDraft.value) return true
  if (busy.value || commitUnknown.value || mutationUnknown.value) { error.value = '资料操作结果尚未确认，请留在当前项目核对。'; return false }
  if (!window.confirm('离开将放弃尚未提交的资料，确定离开吗？')) return false
  return discard()
}
onBeforeRouteLeave(mayLeave)
onBeforeRouteUpdate(async (to, from) => to.params.id === from.params.id || await mayLeave())
function beforeUnload(event: BeforeUnloadEvent) { if (hasDraft.value) { event.preventDefault(); event.returnValue = '' } }
onMounted(() => window.addEventListener('beforeunload', beforeUnload))
onBeforeUnmount(() => { libraryGeneration++; mutationGeneration++; window.removeEventListener('beforeunload', beforeUnload) })
watch(projectId, () => {
  mutationGeneration++
  editorOpen.value = false; editorStarted.value = false; batch.value = null; error.value = ''
  saving.value = false; uploadBusy.value = false; uploadPending.value = false
  commitUnknown.value = false; mutationUnknown.value = ''
}, { flush: 'sync' })
</script>

<style scoped>
.project-overview { display: grid; grid-template-columns: minmax(0, 1.7fr) minmax(320px, 1fr); align-content: start; gap: var(--space-8); padding: var(--layout-content-padding); }
.project-overview h2 { font-size: var(--text-section); font-weight: var(--font-semibold); margin: 0 0 var(--space-4); }
.project-overview h3 { font-size: var(--text-md); font-weight: var(--font-medium); margin: var(--space-5) 0 var(--space-2); color: var(--text-muted); }
.project-overview-originals { min-width: 0; display: grid; align-content: start; gap: var(--space-7); }
.project-original { white-space: pre-wrap; overflow-wrap: anywhere; line-height: var(--leading-relaxed); margin: 0; }
.project-overview-materials { min-width: 0; border-left: 1px solid var(--border-subtle); padding-left: var(--space-6); }
.project-overview-consumption { margin-bottom: var(--space-7); }
.project-overview-consumption :deep(.project-consumption) { font-size: var(--text-object); }
.project-overview header { display: flex; align-items: baseline; justify-content: space-between; gap: var(--space-3); }
.material-list { list-style: none; padding: 0; margin: 0; }
.material-list li { display: flex; align-items: center; justify-content: space-between; gap: var(--space-2); padding: var(--space-3) 0; border-bottom: 1px solid var(--border-subtle); }
.material-list li > div { display: grid; gap: var(--space-1); min-width: 0; }
.material-list a { color: var(--accent); overflow-wrap: anywhere; text-decoration: none; }
.material-list a:hover { text-decoration: underline; }
.material-list small, .material-note, .material-digest { color: var(--text-muted); font-size: var(--text-sm); overflow-wrap: anywhere; }
.bound-library { margin-block: var(--space-3); }
@container workspace (max-width: 900px) { .project-overview { grid-template-columns: minmax(0, 1fr); } .project-overview-materials { border-left: 0; padding-left: 0; border-top: 1px solid var(--border-subtle); padding-top: var(--space-5); } }
</style>
