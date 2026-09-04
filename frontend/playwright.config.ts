import { defineConfig, devices } from '@playwright/test'

const externalBaseUrl = process.env.ZIZU_E2E_BASE_URL
const localBaseUrl = 'http://127.0.0.1:4173'

export default defineConfig({
  testDir: './e2e',
  fullyParallel: false,
  workers: 1,
  retries: 0,
  timeout: 60_000,
  expect: { timeout: 40_000 },
  globalTimeout: 900_000,
  outputDir: 'test-results/playwright-artifacts',
  reporter: [
    ['line'],
    ['./e2e/support/nodeManagementReporter.mjs', { outputFile: 'test-results/node-management-summary.json' }],
  ],
  use: {
    baseURL: externalBaseUrl || localBaseUrl,
    headless: true,
    screenshot: 'only-on-failure',
    trace: 'retain-on-failure',
    video: 'off',
  },
  webServer: externalBaseUrl ? undefined : {
    command: 'npm run dev -- --host 127.0.0.1 --port 4173',
    url: localBaseUrl,
    reuseExistingServer: true,
    timeout: 120_000,
  },
  projects: [
    {
      name: 'chromium',
      use: { ...devices['Desktop Chrome'], channel: 'chromium' },
    },
  ],
})
