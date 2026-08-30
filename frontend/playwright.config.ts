import { defineConfig, devices } from '@playwright/test'

export default defineConfig({
  testDir: './e2e',
  fullyParallel: false,
  workers: 1,
  retries: 0,
  timeout: 60_000,
  globalTimeout: 300_000,
  outputDir: 'test-results/node-management-artifacts',
  reporter: [
    ['line'],
    ['./e2e/support/nodeManagementReporter.mjs', { outputFile: 'test-results/node-management-summary.json' }],
  ],
  use: {
    baseURL: process.env.ZIZU_E2E_BASE_URL,
    headless: true,
    screenshot: 'only-on-failure',
    trace: 'retain-on-failure',
    video: 'off',
  },
  projects: [
    {
      name: 'chromium',
      use: { ...devices['Desktop Chrome'], channel: 'chromium' },
    },
  ],
})
