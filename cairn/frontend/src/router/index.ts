import { createRouter, createWebHashHistory } from 'vue-router'
import Overview from '@/features/overview/Overview.vue'
import Projects from '@/features/projects/Projects.vue'
import ProjectWorkspace from '@/features/projects/ProjectWorkspace.vue'
import GraphPage from '@/features/graph/GraphPage.vue'
import RunsPage from '@/features/runs/RunsPage.vue'
import HintsPage from '@/features/hints/HintsPage.vue'
import ArtifactsPage from '@/features/artifacts/ArtifactsPage.vue'
import ProvenancePage from '@/features/provenance/ProvenancePage.vue'
import { useAuthStore } from '@/stores/auth'
import { resolveAuthNavigation } from './authNavigation'

// 系统管理模块：作为 FrontLayout 子路由在右侧主区域渲染，不再跳转独立 Console 布局
const ScenariosConsole = () => import('@/features/console/ScenariosConsole.vue')
const WorkersConsole = () => import('@/features/console/WorkersConsole.vue')
const PromptGroupsConsole = () => import('@/features/console/PromptGroupsConsole.vue')
const KnowledgeConsole = () => import('@/features/console/KnowledgeConsole.vue')
const ReportTemplatesConsole = () => import('@/features/console/ReportTemplatesConsole.vue')
const TimeoutConsole = () => import('@/features/console/TimeoutConsole.vue')

const routes = [
  {
    path: '/login',
    name: 'Login',
    component: () => import('@/features/auth/LoginPage.vue'),
    meta: { public: true },
  },
  {
    path: '/change-password',
    name: 'ChangePassword',
    component: () => import('@/features/auth/ChangePasswordPage.vue'),
  },
  {
    path: '/',
    component: () => import('@/layouts/FrontLayout.vue'),
    children: [
      { path: '', name: 'Overview', component: Overview },
      {
        path: 'findings',
        name: 'Findings',
        component: () => import('@/features/findings/FindingsPage.vue'),
      },
      { path: 'provenance', name: 'Provenance', component: ProvenancePage },
      { path: 'projects', name: 'Projects', component: Projects },
      {
        path: 'verifications',
        name: 'Verifications',
        component: () => import('@/features/verifications/Verifications.vue'),
      },
      {
        path: 'verifications/new',
        name: 'NewVerification',
        component: () => import('@/features/verifications/Verifications.vue'),
      },
      {
        path: 'verifications/:id',
        name: 'VerificationDetail',
        component: () => import('@/features/verifications/VerificationDetail.vue'),
      },
      {
        path: 'project-schedules/:id',
        name: 'ProjectSchedule',
        component: () => import('@/features/projects/ProjectScheduleDetail.vue'),
      },
      {
        path: 'projects/new',
        name: 'NewProject',
        redirect: { path: '/projects', query: { create: '1' } },
      },
      {
        path: 'projects/:id',
        component: ProjectWorkspace,
        children: [
          { path: '', name: 'WorkspaceGraph', component: GraphPage },
          { path: 'runs', name: 'WorkspaceRuns', component: RunsPage },
          { path: 'hints', name: 'WorkspaceHints', component: HintsPage },
          { path: 'artifacts', name: 'WorkspaceArtifacts', component: ArtifactsPage },
          {
            path: 'vulnerabilities',
            name: 'WorkspaceVulnerabilities',
            component: () => import('@/features/vulnerabilities/VulnerabilitiesPage.vue'),
          },
        ],
      },
      {
        path: 'monitoring',
        name: 'Monitoring',
        component: () => import('@/features/monitoring/Monitoring.vue'),
        meta: { superAdmin: true },
      },
      {
        path: 'console',
        children: [
          { path: '', redirect: '/console/models' },
          {
            path: 'models',
            name: 'ConsoleModels',
            component: () => import('@/features/models/TenantModelsPage.vue'),
          },
          {
            path: 'workers',
            name: 'ConsoleWorkers',
            component: () => import('@/features/workers/TenantWorkersPage.vue'),
          },
          {
            path: 'verification-integrations',
            name: 'VerificationIntegrations',
            redirect: { name: 'VerificationConfiguration' },
            meta: { superAdmin: true },
          },
          {
            path: 'verification-configuration',
            name: 'VerificationConfiguration',
            component: () => import('@/features/verifications/VerificationIntegrations.vue'),
            meta: { superAdmin: true },
          },
          {
            path: 'users',
            name: 'ConsoleUsers',
            component: () => import('@/features/settings/UsersSettingsPage.vue'),
            meta: { superAdmin: true },
          },
          {
            path: 'dispatcher',
            name: 'ConsoleDispatcher',
            component: WorkersConsole,
            meta: { superAdmin: true },
          },
          {
            path: 'scenarios',
            name: 'ConsoleScenarios',
            component: ScenariosConsole,
            meta: { superAdmin: true },
          },
          {
            path: 'prompts',
            name: 'ConsolePrompts',
            component: PromptGroupsConsole,
            meta: { superAdmin: true },
          },
          {
            path: 'knowledge',
            name: 'ConsoleKnowledge',
            component: KnowledgeConsole,
            meta: { superAdmin: true },
          },
          {
            path: 'report-templates',
            name: 'ConsoleReportTemplates',
            component: ReportTemplatesConsole,
            meta: { superAdmin: true },
          },
          {
            path: 'timeout',
            name: 'ConsoleTimeout',
            component: TimeoutConsole,
            meta: { superAdmin: true },
          },
        ],
      },
      {
        path: 'forbidden',
        name: 'Forbidden',
        component: () => import('@/features/status/ForbiddenPage.vue'),
      },
      {
        path: ':pathMatch(.*)*',
        name: 'NotFound',
        component: () => import('@/features/status/NotFoundPage.vue'),
      },
    ],
  },
]

const router = createRouter({
  history: createWebHashHistory(),
  routes,
})

const LAST_FRONT_ROUTE_KEY = 'cairn:last-front-route'

router.beforeEach(async (to) => {
  const authStore = useAuthStore()
  await authStore.initialize()
  return resolveAuthNavigation(
    {
      name: to.name,
      fullPath: to.fullPath,
      isPublic: to.matched.some((record) => record.meta.public === true),
      requiresSuperAdmin: to.matched.some((record) => record.meta.superAdmin === true),
    },
    {
      isAuthenticated: authStore.isAuthenticated,
      isSuperAdmin: authStore.isSuperAdmin,
      mustChangePassword: authStore.mustChangePassword,
    },
  )
})

router.afterEach((to) => {
  window.sessionStorage.setItem(LAST_FRONT_ROUTE_KEY, to.fullPath || '/')
})

export default router
