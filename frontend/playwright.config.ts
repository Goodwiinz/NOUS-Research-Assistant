import { defineConfig, devices, expect } from '@playwright/test';
import path from 'path';
import { AxeBuilder } from '@axe-core/playwright';

/**
 * Comprehensive Playwright E2E Testing Configuration
 *
 * Features:
 * - Cross-browser testing (Chrome, Firefox, Safari, Edge)
 * - Mobile and tablet device emulation
 * - Accessibility testing with axe-core
 * - Visual regression testing
 * - Performance monitoring
 * - Parallel test execution
 * - Test data fixtures and management
 * - Error handling and retry logic
 */

// Global test configuration
const GLOBAL_TIMEOUT = 30000; // 30 seconds
const ACTION_TIMEOUT = 10000; // 10 seconds
const NAVIGATION_TIMEOUT = 15000; // 15 seconds

// Base URL for the application
const BASE_URL = process.env.BASE_URL || 'http://localhost:3000';

// Test credentials
const TEST_CREDENTIALS = {
  validUser: {
    email: process.env.TEST_USER_EMAIL || 'test@example.com',
    password: process.env.TEST_USER_PASSWORD || 'testpassword123'
  },
  adminUser: {
    email: process.env.TEST_ADMIN_EMAIL || 'admin@example.com',
    password: process.env.TEST_ADMIN_PASSWORD || 'adminpassword123'
  }
};

// File paths for test data
const TEST_FILES_PATH = path.join(__dirname, 'test-data', 'files');
const REPORTS_PATH = path.join(__dirname, 'test-results', 'reports');

export default defineConfig({
  // Test directory
  testDir: './e2e',
  // Live model evaluation has its own explicit opt-in configuration.
  testIgnore: '**/agent-qa/**',

  // Global settings
  timeout: GLOBAL_TIMEOUT,
  expect: {
    timeout: ACTION_TIMEOUT,
    toHaveScreenshot: {
      threshold: 0.2, // Allow for small pixel differences
      maxDiffPixels: 1000,
      animationHandling: 'allow'
    },
    toMatchSnapshot: {
      threshold: 0.2
    }
  },

  // Global setup and teardown
  globalSetup: require.resolve('./e2e/global-setup.ts'),
  globalTeardown: require.resolve('./e2e/global-teardown.ts'),

  // Output configuration
  outputDir: './test-results',

  // Retry configuration
  retries: process.env.CI ? 2 : 0,

  // Reporter configuration
  reporter: [
    ['html', {
      outputFolder: path.join(__dirname, 'playwright-report'),
      open: process.env.CI ? 'never' : 'on-failure'
    }],
    ['json', {
      outputFile: path.join(REPORTS_PATH, 'results.json')
    }],
    ['junit', {
      outputFile: path.join(REPORTS_PATH, 'junit.xml'),
      stripANSIControlSequences: true
    }],
    ['list'],
  ],

  // Web server configuration
  webServer: {
    command: 'corepack pnpm@10.18.2 run dev',
    url: BASE_URL,
    reuseExistingServer: !process.env.CI,
    timeout: 120000, // 2 minutes
    stdout: 'pipe',
    stderr: 'pipe'
  },

  // Projects for different browsers and devices
  projects: [
    // Desktop browsers
    {
      name: 'chromium',
      use: {
        ...devices['Desktop Chrome'],
        viewport: { width: 1280, height: 720 },
        contextOptions: {
          permissions: ['clipboard-read', 'clipboard-write']
        }
      },
      dependencies: ['setup'],
    },
    {
      name: 'firefox',
      use: {
        ...devices['Desktop Firefox'],
        viewport: { width: 1280, height: 720 }
      },
      dependencies: ['setup'],
    },
    {
      name: 'webkit',
      use: {
        ...devices['Desktop Safari'],
        viewport: { width: 1280, height: 720 }
      },
      dependencies: ['setup'],
    },
    {
      name: 'edge',
      use: {
        ...devices['Desktop Edge'],
        viewport: { width: 1280, height: 720 },
        channel: 'msedge'
      },
      dependencies: ['setup'],
    },

    // Mobile devices
    {
      name: 'iPhone',
      use: {
        ...devices['iPhone 14'],
        viewport: { width: 390, height: 844 }
      },
      dependencies: ['setup'],
    },
    {
      name: 'Android',
      use: {
        ...devices['Pixel 5'],
        viewport: { width: 393, height: 851 }
      },
      dependencies: ['setup'],
    },

    // Tablet devices
    {
      name: 'iPad',
      use: {
        ...devices['iPad Pro 11'],
        viewport: { width: 1024, height: 1366 }
      },
      dependencies: ['setup'],
    },

    // Accessibility testing project
    {
      name: 'accessibility',
      use: {
        ...devices['Desktop Chrome'],
        viewport: { width: 1280, height: 720 }
      },
      dependencies: ['setup'],
      testMatch: '**/accessibility/**/*.spec.ts',
    },

    // Performance testing project
    {
      name: 'performance',
      use: {
        ...devices['Desktop Chrome'],
        viewport: { width: 1280, height: 720 },
        // Launch Chrome with performance features
        launchOptions: {
          args: [
            '--enable-precise-memory-info',
            '--disable-web-security',
            '--disable-features=IsolateOrigins,site-per-process'
          ]
        }
      },
      dependencies: ['setup'],
      testMatch: '**/performance/**/*.spec.ts',
    },

    // Visual regression testing project
    {
      name: 'visual',
      use: {
        ...devices['Desktop Chrome'],
        viewport: { width: 1280, height: 720 },
        // Ensure consistent visual testing
        deviceScaleFactor: 1,
        hasTouch: false,
        isMobile: false
      },
      dependencies: ['setup'],
      testMatch: '**/visual/**/*.spec.ts',
    },

    // Setup project (runs first)
    {
      name: 'setup',
      testMatch: '**/setup/**/*.spec.ts',
    },
  ],

  // Custom global fixtures
  use: {
    // Base URL
    baseURL: BASE_URL,

    // Timeouts
    actionTimeout: ACTION_TIMEOUT,
    navigationTimeout: NAVIGATION_TIMEOUT,

    // Screenshots and videos
    screenshot: 'only-on-failure',
    video: 'retain-on-failure',
    trace: 'retain-on-failure',

    // Custom fixtures
    testData: async ({}, use: any) => {
      await use({
        credentials: TEST_CREDENTIALS,
        filesPath: TEST_FILES_PATH,
        baseUrl: BASE_URL
      });
    },

    // Accessibility fixture
    axeBuilder: async ({ page }: any, use: any) => {
      const axeBuilder = new AxeBuilder({ page });
      await use(axeBuilder);
    },

    // Performance monitoring fixture
    performanceMetrics: async ({ page }: any, use: any) => {
      const metrics = {
        measurePerformance: async (name: string) => {
          await page.evaluate(() => performance.mark(`${name}-start`));
          return {
            end: async () => {
              await page.evaluate((n: any) => performance.mark(`${n}-end`), name);
              return page.evaluate((n: any) => {
                performance.measure(n, `${n}-start`, `${n}-end`);
                const entries = performance.getEntriesByName(n, 'measure');
                return entries[entries.length - 1];
              }, name);
            }
          };
        },
        getMemoryUsage: async () => {
          return page.evaluate(() => {
            if ('memory' in performance) {
              return {
                usedJSHeapSize: (performance as any).memory.usedJSHeapSize,
                totalJSHeapSize: (performance as any).memory.totalJSHeapSize,
                jsHeapSizeLimit: (performance as any).memory.jsHeapSizeLimit
              };
            }
            return null;
          });
        }
      };
      await use(metrics);
    }
  },

  // Metadata for test organization
  metadata: {
    'Test Suite': 'Multimodal Enterprise RAG E2E Tests',
    'Application': 'Multimodal Enterprise RAG System',
    'Version': '1.0.0',
    'Test Environment': process.env.NODE_ENV || 'test'
  }
});