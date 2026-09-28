import { defineConfig, devices } from '@playwright/test'

const localBaseUrl = 'http://127.0.0.1:4173'
const externalBaseUrl = process.env.CAIRN_E2E_BASE_URL

export default defineConfig({
  testDir: './e2e',
  fullyParallel: true,
  forbidOnly: Boolean(process.env.CI),
  retries: process.env.CI ? 2 : 0,
  reporter: process.env.CI ? 'github' : 'list',
  use: {
    baseURL: externalBaseUrl || localBaseUrl,
    trace: 'on-first-retry',
    launchOptions: { executablePath: process.env.CAIRN_E2E_EXECUTABLE_PATH || undefined },
  },
  webServer: externalBaseUrl
    ? undefined
    : {
        command:
          'npm run build:verify && npm run preview -- --host 127.0.0.1 --port 4173 --outDir .build/verify',
        url: localBaseUrl,
        reuseExistingServer: !process.env.CI,
        timeout: 180_000,
      },
  projects: [
    { name: 'chromium', use: { ...devices['Desktop Chrome'] } },
    { name: 'mobile-chromium', use: { ...devices['Pixel 7'] } },
  ],
})
