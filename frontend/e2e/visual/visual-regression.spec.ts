import { test, expect } from '../fixtures/test-data.fixture';
import { LoginPage, DocumentsPage, SearchPage, KnowledgeGraphPage } from '../utils/page-objects';

/**
 * Visual Regression Testing Suite
 *
 * Test Coverage:
 * - UI component consistency across browsers
 * - Layout verification on different screen sizes
 * - Theme consistency (light/dark mode)
 * - Component state variations
 * - Responsive design validation
 * - Cross-browser visual consistency
 */

test.describe('Visual Regression Tests', () => {
  let loginPage: LoginPage;
  let documentsPage: DocumentsPage;
  let searchPage: SearchPage;
  let graphPage: KnowledgeGraphPage;

  test.use({ viewport: { width: 1280, height: 720 } });

  test.beforeEach(async ({ page }) => {
    loginPage = new LoginPage(page);
    documentsPage = new DocumentsPage(page);
    searchPage = new SearchPage(page);
    graphPage = new KnowledgeGraphPage(page);
  });

  test('Login page visual layout', async ({ page }) => {
    await page.goto('/login');

    // Take full page screenshot
    await expect(page).toHaveScreenshot('login-page-full.png', {
      fullPage: true,
      animations: 'disabled'
    });

    // Test form field states
    await loginPage.emailInput.fill('test@example.com');
    await expect(page.locator('form')).toHaveScreenshot('login-page-with-email.png', {
      animations: 'disabled'
    });

    await loginPage.passwordInput.fill('password123');
    await expect(page.locator('form')).toHaveScreenshot('login-page-with-credentials.png', {
      animations: 'disabled'
    });

    // Test hover states
    await loginPage.loginButton.hover();
    await expect(loginPage.loginButton).toHaveScreenshot('login-button-hover.png', {
      animations: 'disabled'
    });

    // Test focus states
    await loginPage.emailInput.focus();
    await expect(loginPage.emailInput).toHaveScreenshot('email-input-focus.png', {
      animations: 'disabled'
    });
  });

  test('Documents page visual layout', async ({ page }) => {
    await page.goto('/documents');

    // Full page layout
    await expect(page).toHaveScreenshot('documents-page-full.png', {
      fullPage: true,
      animations: 'disabled'
    });

    // Upload area states
    await expect(documentsPage.uploadArea).toHaveScreenshot('upload-area-default.png', {
      animations: 'disabled'
    });

    // Drag over state simulation
    await documentsPage.uploadArea.hover();
    await page.mouse.down();
    await expect(documentsPage.uploadArea).toHaveScreenshot('upload-area-drag-over.png', {
      animations: 'disabled'
    });
    await page.mouse.up();

    // Test empty state
    await expect(documentsPage.fileList).toHaveScreenshot('empty-file-list.png', {
      animations: 'disabled'
    });
  });

  test('Search page visual layout', async ({ page }) => {
    await page.goto('/search');

    // Full page layout
    await expect(page).toHaveScreenshot('search-page-full.png', {
      fullPage: true,
      animations: 'disabled'
    });

    // Search input states
    await searchPage.searchInput.fill('test query');
    await expect(searchPage.searchInput).toHaveScreenshot('search-input-with-text.png', {
      animations: 'disabled'
    });

    // Test search suggestions
    await page.waitForTimeout(500);
    const suggestions = page.locator('[data-testid="search-suggestions"]');
    if (await suggestions.isVisible()) {
      await expect(suggestions).toHaveScreenshot('search-suggestions.png', {
        animations: 'disabled'
      });
    }

    // Test tabs visual state
    await expect(page.locator('[data-testid="tabs-container"]')).toHaveScreenshot('search-tabs-default.png', {
      animations: 'disabled'
    });

    // Test tab active state
    await searchPage.sourcesTab.click();
    await expect(page.locator('[data-testid="tabs-container"]')).toHaveScreenshot('search-tabs-sources-active.png', {
      animations: 'disabled'
    });
  });

  test('Knowledge graph visual layout', async ({ page }) => {
    await page.goto('/graph');

    // Wait for graph to load
    await page.waitForSelector('[data-testid="graph-canvas"]', { timeout: 15000 });

    // Full page layout
    await expect(page).toHaveScreenshot('graph-page-full.png', {
      fullPage: true,
      animations: 'disabled'
    });

    // Graph controls
    await expect(page.locator('[data-testid="graph-controls"]')).toHaveScreenshot('graph-controls.png', {
      animations: 'disabled'
    });

    // Test minimap if present
    const minimap = page.locator('[data-testid="graph-minimap"]');
    if (await minimap.isVisible()) {
      await expect(minimap).toHaveScreenshot('graph-minimap.png', {
        animations: 'disabled'
      });
    }

    // Test layout selector
    await graphPage.layoutSelector.click();
    await expect(page.locator('[data-testid="layout-dropdown"]')).toHaveScreenshot('layout-selector-open.png', {
      animations: 'disabled'
    });
  });

  test.describe('Responsive Design Screenshots', () => {
    const viewports = [
      { width: 1920, height: 1080, name: 'desktop-large' },
      { width: 1366, height: 768, name: 'desktop-medium' },
      { width: 1024, height: 768, name: 'tablet-landscape' },
      { width: 768, height: 1024, name: 'tablet-portrait' },
      { width: 414, height: 896, name: 'mobile-large' },
      { width: 375, height: 667, name: 'mobile-medium' }
    ];

    viewports.forEach(viewport => {
      test(`Responsive layout - ${viewport.name}`, async ({ page }) => {
        await page.setViewportSize({ width: viewport.width, height: viewport.height });

        // Test login page
        await page.goto('/login');
        await expect(page).toHaveScreenshot(`login-${viewport.name}.png`, {
          fullPage: true,
          animations: 'disabled'
        });

        // Test documents page
        await page.goto('/documents');
        await expect(page).toHaveScreenshot(`documents-${viewport.name}.png`, {
          fullPage: true,
          animations: 'disabled'
        });

        // Test search page
        await page.goto('/search');
        await expect(page).toHaveScreenshot(`search-${viewport.name}.png`, {
          fullPage: true,
          animations: 'disabled'
        });
      });
    });
  });

  test.describe('Component State Variations', () => {
    test('Button states and variations', async ({ page }) => {
      await page.goto('/login');

      // Test different button states
      const loginButton = loginPage.loginButton;

      // Default state
      await expect(loginButton).toHaveScreenshot('button-default.png', {
        animations: 'disabled'
      });

      // Hover state
      await loginButton.hover();
      await expect(loginButton).toHaveScreenshot('button-hover.png', {
        animations: 'disabled'
      });

      // Focus state
      await loginButton.focus();
      await expect(loginButton).toHaveScreenshot('button-focus.png', {
        animations: 'disabled'
      });

      // Active/pressed state
      await page.mouse.down();
      await expect(loginButton).toHaveScreenshot('button-active.png', {
        animations: 'disabled'
      });
      await page.mouse.up();

      // Disabled state
      await loginPage.loginButton.fill('test@example.com');
      await loginPage.passwordInput.fill('');
      await expect(loginButton).toHaveAttribute('disabled');
      await expect(loginButton).toHaveScreenshot('button-disabled.png', {
        animations: 'disabled'
      });
    });

    test('Form field states and validation', async ({ page }) => {
      await page.goto('/login');

      // Test email input states
      const emailInput = loginPage.emailInput;

      // Default state
      await expect(emailInput).toHaveScreenshot('input-default.png', {
        animations: 'disabled'
      });

      // Focus state
      await emailInput.focus();
      await expect(emailInput).toHaveScreenshot('input-focus.png', {
        animations: 'disabled'
      });

      // With content
      await emailInput.fill('test@example.com');
      await expect(emailInput).toHaveScreenshot('input-with-content.png', {
        animations: 'disabled'
      });

      // Error state
      await emailInput.fill('invalid-email');
      await emailInput.blur();
      await page.waitForTimeout(1000);
      await expect(emailInput).toHaveScreenshot('input-error.png', {
        animations: 'disabled'
      });
    });

    test('Loading and progress states', async ({ page }) => {
      await page.goto('/search');

      // Simulate loading state
      await searchPage.searchInput.fill('test query');
      await searchPage.searchButton.click();

      // Loading state
      await expect(page.locator('[data-testid="loading-results"]')).toHaveScreenshot('loading-state.png', {
        animations: 'disabled'
      });

      // Wait for results
      await page.waitForSelector('[data-testid="search-results"]', { timeout: 15000 });

      // Results state
      await expect(page.locator('[data-testid="search-results"]')).toHaveScreenshot('results-loaded.png', {
        animations: 'disabled'
      });
    });

    test('Modal and overlay states', async ({ page }) => {
      await page.goto('/documents');

      // Look for any modal triggers
      const modalTrigger = page.locator('[data-testid="modal-trigger"], [data-testid="help-button"], [data-testid="settings-button"]');
      if (await modalTrigger.count() > 0) {
        await modalTrigger.first().click();
        await page.waitForTimeout(500);

        // Modal overlay
        await expect(page.locator('[data-testid="modal-overlay"]')).toHaveScreenshot('modal-overlay.png', {
          animations: 'disabled'
        });

        // Modal content
        await expect(page.locator('[data-testid="modal-content"]')).toHaveScreenshot('modal-content.png', {
          animations: 'disabled'
        });

        // Close modal
        await page.keyboard.press('Escape');
        await page.waitForTimeout(500);
      }
    });
  });

  test.describe('Theme Consistency', () => {
    test('Light theme consistency', async ({ page }) => {
      // Ensure light theme
      await page.emulateMedia({ colorScheme: 'light' });
      await page.goto('/');

      // Take screenshots of key components in light theme
      await expect(page.locator('header')).toHaveScreenshot('header-light.png', {
        animations: 'disabled'
      });

      await expect(page.locator('nav')).toHaveScreenshot('navigation-light.png', {
        animations: 'disabled'
      });

      await expect(page.locator('main')).toHaveScreenshot('main-content-light.png', {
        animations: 'disabled'
      });
    });

    test('Dark theme consistency', async ({ page }) => {
      // Enable dark theme
      await page.emulateMedia({ colorScheme: 'dark' });
      await page.goto('/');

      // Take screenshots of key components in dark theme
      await expect(page.locator('header')).toHaveScreenshot('header-dark.png', {
        animations: 'disabled'
      });

      await expect(page.locator('nav')).toHaveScreenshot('navigation-dark.png', {
        animations: 'disabled'
      });

      await expect(page.locator('main')).toHaveScreenshot('main-content-dark.png', {
        animations: 'disabled'
      });
    });

    test('High contrast mode', async ({ page }) => {
      // Enable high contrast mode
      await page.emulateMedia({ forcedColors: 'active', colorScheme: 'light' });
      await page.goto('/login');

      // Verify high contrast compatibility
      await expect(page).toHaveScreenshot('high-contrast-login.png', {
        fullPage: true,
        animations: 'disabled'
      });
    });
  });

  test.describe('Cross-browser Visual Consistency', () => {
    ['chromium', 'firefox', 'webkit'].forEach(browserName => {
      test.describe(`${browserName} visual consistency`, () => {
        // Browser engine comes from the project; keep only a shared viewport.
        test.use({ viewport: { width: 1280, height: 720 } });

        test(`${browserName}: Login page layout`, async ({ page }) => {
          await page.goto('/login');

          await expect(page).toHaveScreenshot(`${browserName}-login-page.png`, {
            fullPage: true,
            animations: 'disabled'
          });

          // Test form element consistency
          await expect(page.locator('form')).toHaveScreenshot(`${browserName}-login-form.png`, {
            animations: 'disabled'
          });
        });

        test(`${browserName}: Documents page layout`, async ({ page }) => {
          await page.goto('/documents');

          await expect(page).toHaveScreenshot(`${browserName}-documents-page.png`, {
            fullPage: true,
            animations: 'disabled'
          });

          // Test upload area consistency
          await expect(page.locator('[data-testid="upload-area"]')).toHaveScreenshot(`${browserName}-upload-area.png`, {
            animations: 'disabled'
          });
        });

        test(`${browserName}: Search page layout`, async ({ page }) => {
          await page.goto('/search');

          await expect(page).toHaveScreenshot(`${browserName}-search-page.png`, {
            fullPage: true,
            animations: 'disabled'
          });

          // Test search input consistency
          await expect(page.locator('[data-testid="search-input"]')).toHaveScreenshot(`${browserName}-search-input.png`, {
            animations: 'disabled'
          });
        });
      });
    });
  });

  test.describe('Dynamic Content Visual Testing', () => {
    test('File upload progress visualization', async ({ page, testData }) => {
      await page.goto('/documents');

      // Start file upload for screenshot
      const fileInput = page.locator('[data-testid="file-input"]');
      await fileInput.setInputFiles(testData.files.text.path);

      // Wait for upload to start
      await page.waitForSelector('[data-testid^="file-"]', { state: 'visible', timeout: 10000 });

      // Screenshot upload in progress
      await expect(page.locator('[data-testid="file-list"]')).toHaveScreenshot('upload-in-progress.png', {
        animations: 'disabled'
      });

      // Wait for upload completion
      await page.waitForSelector('[data-testid^="file-"][data-status="uploaded"]', { timeout: 30000 });

      // Screenshot completed upload
      await expect(page.locator('[data-testid="file-list"]')).toHaveScreenshot('upload-completed.png', {
        animations: 'disabled'
      });
    });

    test('Search results visualization', async ({ page, testData }) => {
      await page.goto('/search');

      // Perform search to generate results
      await searchPage.searchInput.fill(testData.queries.simple[0]);
      await searchPage.searchButton.click();

      // Wait for results
      await page.waitForSelector('[data-testid="search-results"]', { timeout: 15000 });

      // Screenshot search results
      await expect(page.locator('[data-testid="search-results"]')).toHaveScreenshot('search-results.png', {
        animations: 'disabled'
      });

      // Test results with expanded details
      const firstResult = page.locator('[data-testid="result-item"]').first();
      if (await firstResult.isVisible()) {
        await firstResult.click();
        await page.waitForTimeout(500);

        await expect(page.locator('[data-testid="search-results"]')).toHaveScreenshot('search-results-expanded.png', {
          animations: 'disabled'
        });
      }
    });

    test('Graph interaction states', async ({ page }) => {
      await page.goto('/graph');

      // Wait for graph to load
      await page.waitForSelector('[data-testid="graph-canvas"]', { timeout: 15000 });

      // Default graph state
      await expect(page.locator('[data-testid="graph-canvas"]')).toHaveScreenshot('graph-default.png', {
        animations: 'disabled'
      });

      // Test zoom states
      await page.click('[data-testid="zoom-in"]');
      await page.waitForTimeout(500);
      await expect(page.locator('[data-testid="graph-canvas"]')).toHaveScreenshot('graph-zoomed-in.png', {
        animations: 'disabled'
      });

      await page.click('[data-testid="zoom-out"]');
      await page.waitForTimeout(500);
      await expect(page.locator('[data-testid="graph-canvas"]')).toHaveScreenshot('graph-zoomed-out.png', {
        animations: 'disabled'
      });

      // Test node selection if available
      const nodes = page.locator('[data-testid="graph-node"]');
      if (await nodes.count() > 0) {
        await nodes.first().click();
        await page.waitForTimeout(500);

        await expect(page.locator('[data-testid="graph-canvas"]')).toHaveScreenshot('graph-node-selected.png', {
          animations: 'disabled'
        });
      }
    });
  });

  test.describe('Error and Edge Case Visual Testing', () => {
    test('Error state visualization', async ({ page }) => {
      // Navigate to invalid URL to trigger error
      await page.goto('/invalid-page-that-does-not-exist');

      // Screenshot error page
      await expect(page).toHaveScreenshot('error-page-404.png', {
        fullPage: true,
        animations: 'disabled'
      });
    });

    test('Empty state visualization', async ({ page }) => {
      await page.goto('/documents');

      // Ensure empty state
      await expect(page.locator('[data-testid="file-list"]')).toHaveScreenshot('empty-state-documents.png', {
        animations: 'disabled'
      });

      await page.goto('/search');
      await searchPage.searchInput.fill('query-that-will-return-no-results');
      await searchPage.searchButton.click();

      // Wait for no results
      await page.waitForSelector('[data-testid="no-results"]', { timeout: 10000 });

      await expect(page.locator('[data-testid="search-results"]')).toHaveScreenshot('empty-state-search.png', {
        animations: 'disabled'
      });
    });

    test('Loading state visualization', async ({ page }) => {
      // Test loading spinner
      await page.goto('/search');
      await searchPage.searchInput.fill('test query');
      await searchPage.searchButton.click();

      // Screenshot loading state
      await expect(page.locator('[data-testid="loading-results"]')).toHaveScreenshot('loading-spinner.png', {
        animations: 'disabled'
      });
    });
  });
});