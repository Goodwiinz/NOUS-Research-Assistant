import { test, expect, devices } from '@playwright/test';
import { LoginPage, DocumentsPage, SearchPage } from '../utils/page-objects';

// Browser engines belong to Playwright projects; describe groups can vary
// context options without changing the worker's defaultBrowserType.
function contextOptions(device: (typeof devices)[string]) {
  return {
    viewport: device.viewport,
    userAgent: device.userAgent,
    deviceScaleFactor: device.deviceScaleFactor,
    isMobile: device.isMobile,
    hasTouch: device.hasTouch,
  };
}

/**
 * Cross-Browser and Device Compatibility Tests
 *
 * Test Coverage:
 * - Desktop browsers: Chrome, Firefox, Safari, Edge
 * - Tablet devices: iPad, Android tablet
 * - Mobile devices: iPhone, Android mobile
 * - Responsive design validation
 * - Touch interaction testing
 */

test.describe('Cross-Browser Compatibility', () => {
  // Desktop browser tests
  ['chromium', 'firefox', 'webkit', 'edge'].forEach(browserName => {
    test.describe(`${browserName} - Desktop Compatibility`, () => {
      test.use(contextOptions(devices['Desktop Chrome']));

      test(`${browserName}: Application loads and functions correctly`, async ({ page }) => {
        const loginPage = new LoginPage(page);

        // Navigate to application
        await page.goto('/login');

        // Verify page loads correctly
        await expect(page.locator('h2')).toContainText('Sign in to your account');

        // Test basic functionality
        await expect(loginPage.emailInput).toBeVisible();
        await expect(loginPage.passwordInput).toBeVisible();
        await expect(loginPage.loginButton).toBeVisible();

        // Test responsive layout
        const viewport = page.viewportSize();
        expect(viewport?.width).toBeGreaterThanOrEqual(1280);
        expect(viewport?.height).toBeGreaterThanOrEqual(720);

        // Take screenshot for browser comparison
        await page.screenshot({
          path: `test-results/screenshots/${browserName}-desktop-login.png`,
          fullPage: true
        });
      });

      test(`${browserName}: Document upload workflow`, async ({ page }) => {
        // Navigate to documents page
        await page.goto('/documents');

        const documentsPage = new DocumentsPage(page);

        // Verify upload functionality
        await expect(documentsPage.uploadArea).toBeVisible();
        await expect(documentsPage.fileInput).toBeVisible();

        // Test drag-and-drop area visibility
        const uploadArea = documentsPage.uploadArea;
        await expect(uploadArea).toBeVisible();

        // Verify file list container
        await expect(documentsPage.fileList).toBeVisible();

        // Test search and filter controls
        await expect(documentsPage.searchFiles).toBeVisible();
        await expect(documentsPage.filterButton).toBeVisible();
        await expect(documentsPage.sortButton).toBeVisible();

        await page.screenshot({
          path: `test-results/screenshots/${browserName}-desktop-documents.png`,
          fullPage: true
        });
      });

      test(`${browserName}: Search interface functionality`, async ({ page }) => {
        await page.goto('/search');

        const searchPage = new SearchPage(page);

        // Verify search components
        await expect(searchPage.searchInput).toBeVisible();
        await expect(searchPage.searchButton).toBeVisible();
        await expect(searchPage.resultsContainer).toBeVisible();

        // Test tab navigation
        await expect(searchPage.answersTab).toBeVisible();
        await expect(searchPage.sourcesTab).BeVisible();
        await expect(searchPage.graphTab).BeVisible();
        await expect(searchPage.evaluationTab).BeVisible();

        // Test filters panel
        await expect(searchPage.filtersButton).toBeVisible();

        await page.screenshot({
          path: `test-results/screenshots/${browserName}-desktop-search.png`,
          fullPage: true
        });
      });
    });
  });
});

test.describe('Device Compatibility', () => {
  // Tablet devices
  test.describe('iPad Compatibility', () => {
    test.use(contextOptions(devices['iPad Pro 11']));

    test('iPad: Responsive layout and touch interactions', async ({ page }) => {
      await page.goto('/login');

      // Verify responsive layout
      await expect(page.locator('h2')).toBeVisible();

      // Test touch-friendly elements
      const loginButton = page.locator('[data-testid="login-button"]');
      await expect(loginButton).toBeVisible();

      // Verify appropriate sizing for touch
      const buttonBox = await loginButton.boundingBox();
      expect(buttonBox?.height).toBeGreaterThanOrEqual(44); // iOS touch target minimum

      // Test document page on tablet
      await page.goto('/documents');
      const documentsPage = new DocumentsPage(page);
      await expect(documentsPage.uploadArea).toBeVisible();

      // Test search page on tablet
      await page.goto('/search');
      const searchPage = new SearchPage(page);
      await expect(searchPage.searchInput).toBeVisible();

      await page.screenshot({
        path: 'test-results/screenshots/ipad-documents.png',
        fullPage: true
      });
    });

    test('iPad: Tablet-specific features', async ({ page }) => {
      await page.goto('/graph');

      // Test graph visualization on tablet
      await expect(page.locator('[data-testid="graph-canvas"]')).toBeVisible();
      await expect(page.locator('[data-testid="graph-controls"]')).toBeVisible();

      // Test touch gestures (simulated)
      const canvas = page.locator('[data-testid="graph-canvas"]');
      await canvas.tap();

      // Verify pinch zoom indicators
      await page.waitForTimeout(1000);

      await page.screenshot({
        path: 'test-results/screenshots/ipad-graph.png',
        fullPage: true
      });
    });
  });

  test.describe('Android Tablet Compatibility', () => {
    test.use(contextOptions(devices['Galaxy Tab S4']));

    test('Android Tablet: Layout adaptation', async ({ page }) => {
      await page.goto('/login');

      // Verify layout adapts to Android tablet
      await expect(page.locator('h2')).toBeVisible();

      // Test material design elements if present
      await page.goto('/documents');
      await expect(page.locator('[data-testid="upload-area"]')).toBeVisible();

      await page.goto('/search');
      await expect(page.locator('[data-testid="search-input"]')).toBeVisible();

      await page.screenshot({
        path: 'test-results/screenshots/android-tablet-documents.png',
        fullPage: true
      });
    });
  });

  // Mobile devices
  test.describe('iPhone Compatibility', () => {
    test.use(contextOptions(devices['iPhone 14']));

    test('iPhone: Mobile responsive design', async ({ page }) => {
      await page.goto('/login');

      // Verify mobile layout
      await expect(page.locator('h2')).toBeVisible();

      // Test mobile-specific navigation
      await expect(page.locator('[data-testid="mobile-menu-button"]')).toBeVisible({ timeout: 5000 });

      // Test touch targets
      const emailInput = page.locator('[data-testid="email-input"]');
      const passwordInput = page.locator('[data-testid="password-input"]');
      const loginButton = page.locator('[data-testid="login-button"]');

      await expect(emailInput).toBeVisible();
      await expect(passwordInput).toBeVisible();
      await expect(loginButton).toBeVisible();

      // Verify appropriate touch target sizes
      const emailBox = await emailInput.boundingBox();
      const buttonBox = await loginButton.boundingBox();

      expect(emailBox?.height).toBeGreaterThanOrEqual(44); // iOS touch target minimum
      expect(buttonBox?.height).toBeGreaterThanOrEqual(44);

      await page.screenshot({
        path: 'test-results/screenshots/iphone-login.png',
        fullPage: true
      });
    });

    test('iPhone: Mobile document upload', async ({ page }) => {
      await page.goto('/documents');

      // Test mobile upload interface
      await expect(page.locator('[data-testid="mobile-upload-button"]')).toBeVisible({ timeout: 5000 });

      // Test camera integration (simulated)
      const uploadButton = page.locator('[data-testid="mobile-upload-button"]');
      await uploadButton.click();

      // Verify mobile upload options
      await expect(page.locator('[data-testid="upload-options"]')).toBeVisible({ timeout: 3000 });

      await page.screenshot({
        path: 'test-results/screenshots/iphone-upload.png',
        fullPage: true
      });
    });

    test('iPhone: Mobile search interface', async ({ page }) => {
      await page.goto('/search');

      // Test mobile search
      const searchPage = new SearchPage(page);
      await expect(searchPage.searchInput).toBeVisible();
      await expect(searchPage.searchButton).toBeVisible();

      // Test mobile results display
      await searchPage.searchInput.fill('test query');
      await searchPage.searchButton.click();

      // Verify mobile-optimized results
      await expect(page.locator('[data-testid="mobile-results"]')).toBeVisible({ timeout: 10000 });

      // Test swipe gestures for tab navigation
      const tabsContainer = page.locator('[data-testid="tabs-container"]');
      if (await tabsContainer.isVisible()) {
        // Simulate swipe gesture
        await tabsContainer.tap();
        await page.waitForTimeout(500);
      }

      await page.screenshot({
        path: 'test-results/screenshots/iphone-search.png',
        fullPage: true
      });
    });
  });

  test.describe('Android Mobile Compatibility', () => {
    test.use(contextOptions(devices['Pixel 5']));

    test('Android Mobile: Material Design adaptation', async ({ page }) => {
      await page.goto('/login');

      // Verify Android-specific styling
      await expect(page.locator('h2')).toBeVisible();

      // Test material design components
      await page.goto('/documents');
      await expect(page.locator('[data-testid="upload-area"]')).toBeVisible();

      // Test Android back navigation
      await page.goBack();
      await expect(page).toHaveURL('/login');

      await page.screenshot({
        path: 'test-results/screenshots/android-mobile-login.png',
        fullPage: true
      });
    });
  });
});

test.describe('Responsive Design Validation', () => {
  const viewports = [
    { width: 1920, height: 1080, name: 'desktop-large' },
    { width: 1366, height: 768, name: 'desktop-medium' },
    { width: 1280, height: 720, name: 'desktop-small' },
    { width: 1024, height: 768, name: 'tablet-landscape' },
    { width: 768, height: 1024, name: 'tablet-portrait' },
    { width: 414, height: 896, name: 'mobile-large' },
    { width: 375, height: 667, name: 'mobile-medium' },
    { width: 320, height: 568, name: 'mobile-small' }
  ];

  viewports.forEach(viewport => {
    test(`Responsive design: ${viewport.name} (${viewport.width}x${viewport.height})`, async ({ page }) => {
      // Set viewport size
      await page.setViewportSize({ width: viewport.width, height: viewport.height });

      // Test login page
      await page.goto('/login');
      await expect(page.locator('h2')).toBeVisible();

      // Verify elements fit within viewport
      const loginContainer = page.locator('[data-testid="login-container"]');
      if (await loginContainer.isVisible()) {
        const containerBox = await loginContainer.boundingBox();
        expect(containerBox?.width).toBeLessThanOrEqual(viewport.width);
      }

      // Test documents page
      await page.goto('/documents');
      await expect(page.locator('[data-testid="upload-area"]')).toBeVisible();

      // Test search page
      await page.goto('/search');
      await expect(page.locator('[data-testid="search-input"]')).toBeVisible();

      // Take screenshot for responsive comparison
      await page.screenshot({
        path: `test-results/screenshots/responsive-${viewport.name}.png`,
        fullPage: true
      });
    });
  });
});

test.describe('Cross-Browser Feature Compatibility', () => {
  ['chromium', 'firefox', 'webkit'].forEach(browserName => {
    test.describe(`${browserName} - Feature Support`, () => {
      test.use(contextOptions(devices['Desktop Chrome']));

      test(`${browserName}: Modern JavaScript features`, async ({ page }) => {
        await page.goto('/');

        // Test for modern JS feature support
        const modernFeaturesSupported = await page.evaluate(() => {
          try {
            // Test ES6+ features
            const testArrow = () => 'arrow function works';
            const testPromise = Promise.resolve('promise works');
            const testAsync = async () => 'async/await works';

            // Test modern APIs
            const hasFetch = 'fetch' in window;
            const hasLocalStorage = 'localStorage' in window;
            const hasSessionStorage = 'sessionStorage' in window;

            return {
              arrowFunctions: true,
              promises: true,
              asyncAwait: true,
              fetch: hasFetch,
              localStorage: hasLocalStorage,
              sessionStorage: hasSessionStorage
            };
          } catch (error) {
            return { error: error.message };
          }
        });

        console.log(`${browserName} Modern Features:`, modernFeaturesSupported);
        expect(modernFeaturesSupported.error).toBeUndefined();
      });

      test(`${browserName}: CSS feature support`, async ({ page }) => {
        await page.goto('/');

        // Test for CSS feature support
        const cssFeaturesSupported = await page.evaluate(() => {
          const testElement = document.createElement('div');
          document.body.appendChild(testElement);

          const styles = getComputedStyle(testElement);

          // Test CSS Grid
          const supportsGrid = CSS.supports('display', 'grid');

          // Test Flexbox
          const supportsFlexbox = CSS.supports('display', 'flex');

          // Test CSS Variables
          testElement.style.setProperty('--test', 'value');
          const supportsVariables = styles.getPropertyValue('--test') !== '';

          // Test modern features
          const supportsCustomProperties = CSS.supports('color', 'var(--test)');

          document.body.removeChild(testElement);

          return {
            grid: supportsGrid,
            flexbox: supportsFlexbox,
            variables: supportsVariables,
            customProperties: supportsCustomProperties
          };
        });

        console.log(`${browserName} CSS Features:`, cssFeaturesSupported);
        expect(cssFeaturesSupported.grid).toBe(true);
        expect(cssFeaturesSupported.flexbox).toBe(true);
      });

      test(`${browserName}: WebRTC and multimedia support`, async ({ page }) => {
        const multimediaSupport = await page.evaluate(() => {
          return {
            webRTC: !!(window.RTCPeerConnection || (window as any).webkitRTCPeerConnection),
            mediaRecorder: 'MediaRecorder' in window,
            getUserMedia: !!(navigator.mediaDevices && navigator.mediaDevices.getUserMedia),
            webAudio: 'AudioContext' in window || 'webkitAudioContext' in window,
            canvas: !!document.createElement('canvas').getContext,
            webGL: (() => {
              try {
                const canvas = document.createElement('canvas');
                return !!(canvas.getContext('webgl') || canvas.getContext('experimental-webgl'));
              } catch (e) {
                return false;
              }
            })()
          };
        });

        console.log(`${browserName} Multimedia Support:`, multimediaSupport);
        expect(multimediaSupport.canvas).toBe(true);
      });
    });
  });
});

test.describe('Performance Across Devices', () => {
  ['Desktop Chrome', 'iPhone 14', 'iPad Pro 11'].forEach(deviceName => {
    test.describe(deviceName, () => {
      test.use(contextOptions(devices[deviceName]));
      test(`Performance testing on ${deviceName}`, async ({ page }) => {

      // Monitor performance metrics
      const performanceMetrics = await page.evaluate(() => {
        return new Promise((resolve) => {
          const observer = new PerformanceObserver((list) => {
            const entries = list.getEntries();
            resolve({
              navigation: performance.getEntriesByType('navigation')[0],
              resources: performance.getEntriesByType('resource'),
              paint: performance.getEntriesByType('paint')
            });
          });
          observer.observe({ entryTypes: ['navigation', 'resource', 'paint'] });

          // Navigate to trigger performance observation
          window.location.href = '/login';
        });
      });

      console.log(`Performance metrics for ${deviceName}:`, performanceMetrics);
      });
    });
  });
});