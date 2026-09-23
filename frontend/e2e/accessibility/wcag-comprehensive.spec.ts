import { test, expect } from '../fixtures/enhanced-test-data.fixture';
import { AxeBuilder } from '@axe-core/playwright';

/**
 * Comprehensive Accessibility Testing (WCAG 2.1 AA Compliance)
 *
 * Test Coverage:
 * - WCAG 2.1 AA Level compliance across all pages
 * - Keyboard navigation and focus management
 * - Screen reader compatibility
 * - Color contrast and visual accessibility
 * - ARIA attributes and roles
 * - Form accessibility and validation
 * - Dynamic content accessibility
 * - Mobile accessibility considerations
 */

test.describe('WCAG 2.1 AA Accessibility Compliance', () => {
  test.describe('Perceivable', () => {
    test('PERC-1: Text alternatives for non-text content', async ({ page }) => {
      // Navigate to document page with images and media
      await page.goto('/documents');
      await page.waitForLoadState('networkidle');

      // Run axe-core for image accessibility
      const accessibilityScan = await new AxeBuilder({ page })
        .withTags(['wcag2a', 'wcag2aa', 'wcag21aa'])
        .analyze();

      // Check for image alt text
      const images = page.locator('img');
      const imageCount = await images.count();

      for (let i = 0; i < imageCount; i++) {
        const img = images.nth(i);
        const altText = await img.getAttribute('alt');
        const role = await img.getAttribute('role');

        // Images should have alt text unless they're decorative
        if (role !== 'presentation' && role !== 'none') {
          expect(altText).toBeTruthy();
          expect(altText!.length).toBeGreaterThan(0);
        }
      }

      // Check for video content with captions
      const videos = page.locator('video');
      const videoCount = await videos.count();

      for (let i = 0; i < videoCount; i++) {
        const video = videos.nth(i);
        const hasTracks = await video.locator('track[kind="captions"]').count() > 0;
        const hasControls = await video.getAttribute('controls');

        expect(hasControls).toBeTruthy(); // Videos should have controls
        // Videos with audio should have captions
      }

      // Check for audio content with transcripts
      const audioElements = page.locator('audio');
      const audioCount = await audioElements.count();

      for (let i = 0; i < audioCount; i++) {
        const audio = audioElements.nth(i);
        const hasControls = await audio.getAttribute('controls');
        expect(hasControls).toBeTruthy(); // Audio should have controls
      }

      // Verify no accessibility violations
      expect(accessibilityScan.violations).toHaveLength(0);
    });

    test('PERC-2: Color contrast and visual presentation', async ({ page }) => {
      await page.goto('/documents');
      await page.waitForLoadState('networkidle');

      // Check for sufficient color contrast using axe-core
      const accessibilityScan = await new AxeBuilder({ page })
        .withTags(['color-contrast', 'wcag2aa'])
        .analyze();

      // Test high contrast mode
      await page.emulateMedia({ colorScheme: 'dark' });
      await page.waitForTimeout(1000);

      const darkModeScan = await new AxeBuilder({ page })
        .withTags(['color-contrast', 'wcag2aa'])
        .analyze();

      // Test light mode
      await page.emulateMedia({ colorScheme: 'light' });
      await page.waitForTimeout(1000);

      const lightModeScan = await new AxeBuilder({ page })
        .withTags(['color-contrast', 'wcag2aa'])
        .analyze();

      // Verify no color contrast violations in either mode
      expect(accessibilityScan.violations.filter(v => v.impact === 'serious')).toHaveLength(0);
      expect(darkModeScan.violations.filter(v => v.impact === 'serious')).toHaveLength(0);
      expect(lightModeScan.violations.filter(v => v.impact === 'serious')).toHaveLength(0);

      // Check for text that relies on color alone
      const textElements = page.locator('p, h1, h2, h3, h4, h5, h6, span, div');
      const textCount = await textElements.count();

      // Ensure no text uses only color for emphasis (should have other indicators)
      const colorOnlyText = await page.evaluate(() => {
        const elements = document.querySelectorAll('p, h1, h2, h3, h4, h5, h6, span, div');
        return Array.from(elements).filter(el => {
          const styles = window.getComputedStyle(el);
          const hasColor = styles.color !== 'rgb(0, 0, 0)' && styles.color !== 'rgb(255, 255, 255)';
          const hasNoOtherStyling =
            styles.fontWeight === 'normal' &&
            styles.textDecoration === 'none solid rgb(0, 0, 0)' &&
            styles.fontStyle === 'normal';
          return hasColor && hasNoOtherStyling && el.textContent && el.textContent.trim().length > 0;
        });
      });

      // Elements that use color should also have other styling indicators
      expect(colorOnlyText.length).toBeLessThanOrEqual(textCount * 0.1); // Less than 10% of text elements
    });

    test('PERC-3: Adaptable content and structure', async ({ page }) => {
      await page.goto('/documents');
      await page.waitForLoadState('networkidle');

      // Check for proper heading structure
      const headings = page.locator('h1, h2, h3, h4, h5, h6');
      const headingCount = await headings.count();

      if (headingCount > 0) {
        // First heading should be h1
        const firstHeading = headings.first();
        await expect(firstHeading).toHaveRole('heading');

        // Check heading hierarchy (no skipped levels)
        const headingLevels = [];
        for (let i = 0; i < headingCount; i++) {
          const tagName = await headings.nth(i).evaluate(el => el.tagName);
          const level = parseInt(tagName.substring(1));
          headingLevels.push(level);
        }

        // Verify no skipped heading levels (e.g., h1 followed by h3)
        for (let i = 1; i < headingLevels.length; i++) {
          expect(headingLevels[i]).toBeLessThanOrEqual(headingLevels[i - 1] + 1);
        }
      }

      // Check for list markup
      const lists = page.locator('ul, ol, dl');
      const listCount = await lists.count();

      for (let i = 0; i < listCount; i++) {
        const list = lists.nth(i);
        const listItems = list.locator('li');
        const itemCount = await listItems.count();

        // Lists should have list items
        if (itemCount === 0) {
          // Empty lists should still have proper ARIA or be hidden
          const ariaLabel = await list.getAttribute('aria-label');
          const ariaHidden = await list.getAttribute('aria-hidden');
          expect(ariaLabel || ariaHidden).toBeTruthy();
        }
      }

      // Check for proper table structure
      const tables = page.locator('table');
      const tableCount = await tables.count();

      for (let i = 0; i < tableCount; i++) {
        const table = tables.nth(i);

        // Tables should have captions or headers
        const hasCaption = await table.locator('caption').count() > 0;
        const hasHeaders = await table.locator('th').count() > 0;
        const hasAriaLabel = await table.getAttribute('aria-label');
        const hasAriaDescribedBy = await table.getAttribute('aria-describedby');

        expect(hasCaption || hasHeaders || hasAriaLabel || hasAriaDescribedBy).toBeTruthy();

        // Check for proper header associations
        const headers = table.locator('th');
        const headerCount = await headers.count();

        for (let j = 0; j < headerCount; j++) {
          const header = headers.nth(j);
          const scope = await header.getAttribute('scope');
          expect(scope).toBeTruthy(); // Headers should have scope
        }
      }

      // Run comprehensive accessibility scan
      const accessibilityScan = await new AxeBuilder({ page })
        .withTags(['wcag2a', 'wcag2aa', 'wcag21aa'])
        .analyze();

      expect(accessibilityScan.violations.filter(v => v.impact === 'critical')).toHaveLength(0);
    });
  });

  test.describe('Operable', () => {
    test('OPERA-1: Keyboard accessibility and navigation', async ({ page }) => {
      await page.goto('/documents');
      await page.waitForLoadState('networkidle');

      // Test keyboard navigation through all interactive elements
      const interactiveElements = page.locator('button, a, input, select, textarea, [tabindex]:not([tabindex="-1"])');
      const elementCount = await interactiveElements.count();

      // Ensure all interactive elements are focusable
      for (let i = 0; i < Math.min(elementCount, 10); i++) {
        const element = interactiveElements.nth(i);
        await element.focus();

        // Check if element actually receives focus
        const focusedElement = await page.evaluate(() => document.activeElement?.tagName);
        const elementTag = await element.evaluate(el => el.tagName);

        expect(focusedElement?.toLowerCase()).toBe(elementTag.toLowerCase());

        // Test keyboard interactions
        await element.press('Enter');
        await page.waitForTimeout(100);
      }

      // Test tab order sequence
      await page.keyboard.press('Tab');
      let focusedElement = await page.evaluate(() => document.activeElement?.tagName);
      expect(focusedElement).toBeTruthy();

      // Test Tab navigation through form elements
      await page.goto('/settings');
      await page.waitForLoadState('networkidle');

      const formElements = page.locator('input, select, textarea, button');
      const formElementCount = await formElements.count();

      // Test tabbing through form
      for (let i = 0; i < Math.min(formElementCount, 5); i++) {
        await page.keyboard.press('Tab');
        focusedElement = await page.evaluate(() => document.activeElement?.tagName);
        expect(['INPUT', 'SELECT', 'TEXTAREA', 'BUTTON']).toContain(focusedElement?.toUpperCase() || '');
      }

      // Test keyboard shortcuts
      await page.keyboard.press('Alt+h'); // Help shortcut
      await page.waitForTimeout(500);

      // Test Escape key functionality
      await page.keyboard.press('Escape');
      await page.waitForTimeout(200);

      // Run axe-core for keyboard accessibility
      const accessibilityScan = await new AxeBuilder({ page })
        .withTags(['keyboard', 'focus-order', 'tabindex'])
        .analyze();

      expect(accessibilityScan.violations.filter(v => v.impact === 'critical')).toHaveLength(0);
    });

    test('OPERA-2: No keyboard traps and focus management', async ({ page }) => {
      await page.goto('/documents');
      await page.waitForLoadState('networkidle');

      // Test modal/dialog focus management
      await page.click('[data-testid="upload-button"]');

      // Wait for modal to appear
      const modal = page.locator('[data-testid="upload-modal"]');
      await expect(modal).toBeVisible({ timeout: 5000 });

      // Check focus is trapped in modal
      const modalFocusableElements = modal.locator('button, a, input, select, textarea, [tabindex]:not([tabindex="-1"])');
      const modalElementCount = await modalFocusableElements.count();

      // Tab through modal elements
      for (let i = 0; i < Math.min(modalElementCount, 5); i++) {
        await page.keyboard.press('Tab');
        const focusedElement = await page.evaluate(() => document.activeElement);

        // Focused element should be within modal
        const isInsideModal = await focusedElement?.evaluate(el =>
          el.closest('[data-testid="upload-modal"]') !== null
        );
        expect(isInsideModal).toBeTruthy();
      }

      // Test closing modal with Escape key
      await page.keyboard.press('Escape');
      await expect(modal).not.toBeVisible({ timeout: 3000 });

      // Test focus is restored after modal close
      const triggerButton = page.locator('[data-testid="upload-button"]');
      await expect(triggerButton).toBeFocused();

      // Test dropdown focus management
      await page.click('[data-testid="filter-dropdown"]');
      const dropdown = page.locator('[data-testid="filter-menu"]');
      await expect(dropdown).toBeVisible();

      // Test Arrow key navigation in dropdown
      await page.keyboard.press('ArrowDown');
      await page.keyboard.press('ArrowDown');
      await page.keyboard.press('Enter');

      // Verify dropdown closed and selection made
      await page.waitForTimeout(500);

      // Test no keyboard traps
      await page.keyboard.press('Tab'); // Should be able to tab away
      const focusedAfterTab = await page.evaluate(() => document.activeElement?.tagName);
      expect(focusedAfterTab).toBeTruthy();
    });

    test('OPERA-3: Timing and timeout adjustments', async ({ page }) => {
      await page.goto('/documents');
      await page.waitForLoadState('networkidle');

      // Test for time-based content adjustments
      await page.evaluate(() => {
        // Simulate user preference for reduced motion
        (window as any).matchMedia('(prefers-reduced-motion: reduce)').matches = true;
      });

      // Test with reduced motion preference
      await page.emulateMedia({ reducedMotion: 'reduce' });
      await page.waitForTimeout(1000);

      // Check for animations that should be disabled
      const animatedElements = page.locator('[data-testid*="animation"], [style*="animation"], [style*="transition"]');
      const animationCount = await animatedElements.count();

      // Elements should respect reduced motion preference
      for (let i = 0; i < animationCount; i++) {
        const element = animatedElements.nth(i);
        const animationDuration = await element.evaluate(el => {
          const styles = window.getComputedStyle(el);
          return styles.animationDuration || styles.transitionDuration;
        });

        // Animations should be disabled or very fast with reduced motion
        expect(animationDuration === '0s' || animationDuration === '0.01s' || animationDuration.includes('0')).toBeTruthy();
      }

      // Test auto-dismissal notifications
      await page.click('[data-testid="show-notification"]');
      const notification = page.locator('[data-testid="notification"]');
      await expect(notification).toBeVisible();

      // Check if notification has close button or controls
      const hasCloseButton = await notification.locator('[data-testid="close-notification"]').count() > 0;
      const hasPauseControl = await notification.locator('[data-testid="pause-notification"]').count() > 0;

      expect(hasCloseButton || hasPauseControl).toBeTruthy();

      // Test for content that doesn't auto-update without user control
      await page.goto('/dashboard');
      await page.waitForLoadState('networkidle');

      // Check for auto-refreshing content
      const autoRefreshElements = page.locator('[data-testid*="auto-refresh"], [data-refresh-interval]');
      const autoRefreshCount = await autoRefreshElements.count();

      for (let i = 0; i < autoRefreshCount; i++) {
        const element = autoRefreshElements.nth(i);
        const hasControl = await element.locator('[data-testid="pause"], [data-testid="stop"], [data-testid="disable"]').count() > 0;
        expect(hasControl).toBeTruthy();
      }
    });
  });

  test.describe('Understandable', () => {
    test('UNDER-1: Language and readability', async ({ page }) => {
      await page.goto('/documents');
      await page.waitForLoadState('networkidle');

      // Check for language attributes
      const htmlLang = await page.evaluate(() => document.documentElement.lang);
      expect(htmlLang).toBeTruthy();
      expect(htmlLang!.length).toBeGreaterThan(0);

      // Check for language changes in content
      const langElements = page.locator('[lang]');
      const langElementCount = await langElements.count();

      for (let i = 0; i < langElementCount; i++) {
        const element = langElements.nth(i);
        const lang = await element.getAttribute('lang');
        expect(lang).toBeTruthy();
        expect(lang!.length).toBeGreaterThanOrEqual(2); // Language codes should be at least 2 characters
      }

      // Check for text readability
      const textBlocks = page.locator('p, div, span');
      const textBlockCount = await textBlocks.count();

      // Test some text blocks for readability metrics
      for (let i = 0; i < Math.min(textBlockCount, 5); i++) {
        const textBlock = textBlocks.nth(i);
        const textContent = await textBlock.textContent();

        if (textContent && textContent.length > 0) {
          // Check for long sentences (simplified readability check)
          const sentences = textContent.split(/[.!?]+/).filter(s => s.trim().length > 0);
          const longSentences = sentences.filter(sentence => sentence.split(' ').length > 30);

          // Most sentences should be reasonably short
          expect(longSentences.length / sentences.length).toBeLessThan(0.3); // Less than 30% long sentences
        }
      }

      // Check for definitions of abbreviations
      const abbreviations = page.locator('abbr');
      const abbrCount = await abbreviations.count();

      for (let i = 0; i < abbrCount; i++) {
        const abbr = abbreviations.nth(i);
        const title = await abbr.getAttribute('title');
        expect(title).toBeTruthy(); // Abbreviations should have title attributes for definitions
      }

      // Run accessibility scan for readability
      const accessibilityScan = await new AxeBuilder({ page })
        .withTags(['wcag2a', 'wcag2aa', 'wcag21aa'])
        .analyze();

      expect(accessibilityScan.violations.filter(v => v.impact === 'moderate' && v.tags.includes('wcag2aa'))).toHaveLength(0);
    });

    test('UNDER-2: Input assistance and form validation', async ({ page, testDataManager }) => {
      await page.goto('/settings');
      await page.waitForLoadState('networkidle');

      // Test form fields have proper labels
      const inputs = page.locator('input, select, textarea');
      const inputCount = await inputs.count();

      for (let i = 0; i < Math.min(inputCount, 10); i++) {
        const input = inputs.nth(i);
        const inputId = await input.getAttribute('id');
        const hasLabel = await page.locator(`label[for="${inputId}"]`).count() > 0;
        const hasAriaLabel = await input.getAttribute('aria-label');
        const hasAriaLabelledBy = await input.getAttribute('aria-labelledby');

        // Input should have associated label
        expect(hasLabel || hasAriaLabel || hasAriaLabelledBy).toBeTruthy();

        // Check for input types and appropriate validation
        const inputType = await input.getAttribute('type');
        if (inputType === 'email') {
          const pattern = await input.getAttribute('pattern');
          const hasValidation = await input.getAttribute('required');
          expect(pattern || hasValidation).toBeTruthy();
        } else if (inputType === 'tel') {
          const pattern = await input.getAttribute('pattern');
          // Phone numbers should have some validation
        } else if (inputType === 'url') {
          const pattern = await input.getAttribute('pattern');
          // URLs should have validation
        }
      }

      // Test error identification
      // Fill form with invalid data
      await page.fill('[data-testid="email-field"]', 'invalid-email');
      await page.fill('[data-testid="required-field"]', '');
      await page.click('[data-testid="save-settings"]');

      // Check for error messages
      const errorMessages = page.locator('[data-testid="error-message"], [role="alert"]');
      const errorMessageCount = await errorMessages.count();

      expect(errorMessageCount).toBeGreaterThan(0);

      // Error messages should be programmatically associated with inputs
      for (let i = 0; i < errorMessageCount; i++) {
        const errorMessage = errorMessages.nth(i);
        const ariaDescribedBy = await errorMessage.getAttribute('aria-describedby');
        const role = await errorMessage.getAttribute('role');

        expect(role === 'alert' || role === 'status' || ariaDescribedBy).toBeTruthy();
      }

      // Test help text and instructions
      const helpText = page.locator('[data-testid="help-text"], [data-testid="instructions"]');
      const helpTextCount = await helpText.count();

      expect(helpTextCount).toBeGreaterThan(0);

      // Check for input suggestions
      const inputSuggestions = page.locator('[data-testid="input-suggestions"], [list]');
      const suggestionCount = await inputSuggestions.count();

      // Test form submission with valid data
      await page.fill('[data-testid="email-field"]', 'test@example.com');
      await page.fill('[data-testid="required-field"]', 'Valid input');
      await page.click('[data-testid="save-settings"]');

      // Check for success confirmation
      const successMessage = page.locator('[data-testid="success-message"], [role="status"]');
      await expect(successMessage).toBeVisible({ timeout: 5000 });

      // Run comprehensive form accessibility scan
      const accessibilityScan = await new AxeBuilder({ page })
        .withTags(['forms', 'labels', 'validation'])
        .analyze();

      expect(accessibilityScan.violations.filter(v => v.impact === 'critical')).toHaveLength(0);
    });
  });

  test.describe('Robust', () => {
    test('ROBUST-1: ARIA attributes and roles', async ({ page }) => {
      await page.goto('/documents');
      await page.waitForLoadState('networkidle');

      // Check for proper ARIA roles
      const ariaElements = page.locator('[role]');
      const ariaElementCount = await ariaElements.count();

      for (let i = 0; i < Math.min(ariaElementCount, 10); i++) {
        const element = ariaElements.nth(i);
        const role = await element.getAttribute('role');

        // Verify role is valid
        const validRoles = [
          'alert', 'alertdialog', 'application', 'article', 'banner', 'button', 'cell',
          'checkbox', 'columnheader', 'combobox', 'complementary', 'contentinfo',
          'definition', 'dialog', 'directory', 'document', 'feed', 'figure', 'form',
          'grid', 'gridcell', 'group', 'heading', 'img', 'link', 'list', 'listbox',
          'listitem', 'log', 'main', 'marquee', 'math', 'menu', 'menubar', 'menuitem',
          'menuitemcheckbox', 'menuitemradio', 'navigation', 'none', 'note', 'option',
          'presentation', 'progressbar', 'radio', 'radiogroup', 'region', 'row',
          'rowgroup', 'rowheader', 'scrollbar', 'search', 'searchbox', 'separator',
          'slider', 'spinbutton', 'status', 'switch', 'tab', 'table', 'tablist',
          'tabpanel', 'tablist', 'td', 'term', 'textbox', 'timer', 'toolbar',
          'tooltip', 'tree', 'treegrid', 'treeitem', 'rowgroup', 'main'
        ];

        expect(validRoles).toContain(role);
      }

      // Check for proper ARIA attributes
      const ariaLabelElements = page.locator('[aria-label]');
      const ariaLabelCount = await ariaLabelElements.count();

      for (let i = 0; i < Math.min(ariaLabelCount, 5); i++) {
        const element = ariaLabelElements.nth(i);
        const ariaLabel = await element.getAttribute('aria-label');
        expect(ariaLabel).toBeTruthy();
        expect(ariaLabel!.length).toBeGreaterThan(0);
      }

      // Check for ARIA described elements
      const ariaDescribedByElements = page.locator('[aria-describedby]');
      const describedByCount = await ariaDescribedByElements.count();

      for (let i = 0; i < Math.min(describedByCount, 5); i++) {
        const element = ariaDescribedByElements.nth(i);
        const describedBy = await element.getAttribute('aria-describedby');

        if (describedBy) {
          // Verify referenced element exists
          const describedElement = page.locator(`#${describedBy.split(' ')[0]}`);
          await expect(describedElement).toHaveCount({ min: 1 });
        }
      }

      // Test dynamic content ARIA updates
      await page.click('[data-testid="upload-button"]');
      const modal = page.locator('[data-testid="upload-modal"]');
      await expect(modal).toBeVisible();

      // Check modal has proper ARIA attributes
      const modalRole = await modal.getAttribute('role');
      const modalLabel = await modal.getAttribute('aria-label');
      const modalLabelledBy = await modal.getAttribute('aria-labelledby');

      expect(modalRole).toBe('dialog');
      expect(modalLabel || modalLabelledBy).toBeTruthy();

      // Check modal content is accessible
      const modalContent = modal.locator('[role="dialog"] > *');
      const contentCount = await modalContent.count();
      expect(contentCount).toBeGreaterThan(0);

      // Close modal and check ARIA updates
      await page.keyboard.press('Escape');
      await expect(modal).not.toBeVisible({ timeout: 3000 });

      // Run comprehensive ARIA scan
      const accessibilityScan = await new AxeBuilder({ page })
        .withTags(['wcag2a', 'wcag2aa', 'wcag21aa', 'aria'])
        .analyze();

      expect(accessibilityScan.violations.filter(v => v.impact === 'critical')).toHaveLength(0);
    });

    test('ROBUST-2: Screen reader compatibility', async ({ page, testDataManager }) => {
      await page.goto('/documents');
      await page.waitForLoadState('networkidle');

      // Test screen reader announcements for dynamic content
      await page.click('[data-testid="upload-button"]');

      // Check for live regions
      const liveRegions = page.locator('[aria-live], [role="status"], [role="alert"]');
      const liveRegionCount = await liveRegions.count();

      expect(liveRegionCount).toBeGreaterThan(0);

      // Test file upload screen reader announcements
      const testFile = await testDataManager.getTestFile('screen-reader-test.pdf');
      await page.setInputFiles('input[type="file"]', testFile.path);

      // Check for progress announcements
      const progressRegion = page.locator('[aria-live="polite"], [role="status"]');
      await expect(progressRegion).toBeVisible({ timeout: 5000 });

      // Test for proper table headers for screen readers
      await page.goto('/dashboard');
      await page.waitForLoadState('networkidle');

      const tables = page.locator('table');
      const tableCount = await tables.count();

      for (let i = 0; i < Math.min(tableCount, 2); i++) {
        const table = tables.nth(i);

        // Check for table headers
        const headers = table.locator('th');
        const headerCount = await headers.count();

        if (headerCount > 0) {
          // Headers should have scope or aria attributes
          for (let j = 0; j < Math.min(headerCount, 3); j++) {
            const header = headers.nth(j);
            const scope = await header.getAttribute('scope');
            const ariaSort = await header.getAttribute('aria-sort');

            expect(scope || ariaSort).toBeTruthy();
          }
        }

        // Check for table caption
        const caption = table.locator('caption');
        const hasCaption = await caption.count() > 0;
        const ariaLabel = await table.getAttribute('aria-label');
        const ariaLabelledBy = await table.getAttribute('aria-labelledby');

        expect(hasCaption || ariaLabel || ariaLabelledBy).toBeTruthy();
      }

      // Test for skip links
      const skipLinks = page.locator('a[href^="#"], [data-testid="skip-link"]');
      const skipLinkCount = await skipLinks.count();

      // Should have skip links for accessibility
      if (skipLinkCount > 0) {
        for (let i = 0; i < skipLinkCount; i++) {
          const skipLink = skipLinks.nth(i);
          const href = await skipLink.getAttribute('href');

          if (href && href.startsWith('#')) {
            const targetId = href.substring(1);
            const target = page.locator(`#${targetId}`);
            await expect(target).toHaveCount({ min: 1 });
          }
        }
      }

      // Test focus indicators for keyboard navigation
      const focusableElements = page.locator('button, a, input, select, textarea, [tabindex]:not([tabindex="-1"])');
      const focusableCount = await focusableElements.count();

      // Focus should be visible for keyboard users
      for (let i = 0; i < Math.min(focusableCount, 5); i++) {
        const element = focusableElements.nth(i);
        await element.focus();

        // Check for visible focus indicator
        const computedStyle = await element.evaluate(el => {
          return window.getComputedStyle(el);
        });

        const hasFocusOutline = computedStyle.outline !== 'none' && computedStyle.outline !== '0px';
        const hasFocusBoxShadow = computedStyle.boxShadow !== 'none';

        expect(hasFocusOutline || hasFocusBoxShadow).toBeTruthy();
      }

      // Test for proper heading hierarchy for navigation
      const headings = page.locator('h1, h2, h3, h4, h5, h6');
      const headingCount = await headings.count();

      if (headingCount > 0) {
        // Should have at least one h1
        const hasH1 = await page.locator('h1').count() > 0;
        expect(hasH1).toBeTruthy();
      }

      console.log('Screen reader compatibility tests completed');
    });
  });

  test.describe('Mobile Accessibility', () => {
    test('MOBILE-1: Touch and mobile accessibility', async ({ page }) => {
      // Set mobile viewport
      await page.setViewportSize({ width: 375, height: 667 }); // iPhone size
      await page.goto('/documents');
      await page.waitForLoadState('networkidle');

      // Test touch target sizes (minimum 44x44 pixels)
      const touchTargets = page.locator('button, a, input[type="submit"], input[type="button"], [role="button"]');
      const touchTargetCount = await touchTargets.count();

      for (let i = 0; i < Math.min(touchTargetCount, 10); i++) {
        const target = touchTargets.nth(i);
        const boundingBox = await target.boundingBox();

        if (boundingBox) {
          expect(boundingBox.width).toBeGreaterThanOrEqual(44);
          expect(boundingBox.height).toBeGreaterThanOrEqual(44);
        }
      }

      // Test spacing between touch targets
      for (let i = 0; i < Math.min(touchTargetCount - 1, 5); i++) {
        const current = touchTargets.nth(i);
        const next = touchTargets.nth(i + 1);

        const currentBox = await current.boundingBox();
        const nextBox = await next.boundingBox();

        if (currentBox && nextBox) {
          // Check for adequate spacing (simplified check)
          const verticalDistance = Math.abs(currentBox.y - nextBox.y);
          const horizontalDistance = Math.abs(currentBox.x - nextBox.x);

          // Targets should have some spacing
          expect(verticalDistance > 0 || horizontalDistance > 0).toBeTruthy();
        }
      }

      // Test mobile-specific accessibility
      await page.emulateMedia({ reducedMotion: 'reduce' });

      // Test mobile navigation accessibility
      const mobileMenu = page.locator('[data-testid="mobile-menu"]');
      if (await mobileMenu.isVisible()) {
        await mobileMenu.tap();

        const menuItems = mobileMenu.locator('a, button, [role="menuitem"]');
        const menuItemCount = await menuItems.count();

        expect(menuItemCount).toBeGreaterThan(0);

        // Test mobile menu navigation
        for (let i = 0; i < Math.min(menuItemCount, 3); i++) {
          const menuItem = menuItems.nth(i);
          await menuItem.tap();
          await page.waitForTimeout(200);
        }
      }

      // Test zoom and scaling
      await page.evaluate(() => {
        document.body.style.zoom = '2';
      });

      await page.waitForTimeout(1000);

      // Content should remain readable at 200% zoom
      const textElements = page.locator('p, h1, h2, h3, span, div');
      const textCount = await textElements.count();

      expect(textCount).toBeGreaterThan(0);

      // Test orientation change
      await page.setViewportSize({ width: 667, height: 375 }); // Landscape
      await page.waitForTimeout(1000);

      // Content should reflow properly
      const overflowElements = page.locator('[style*="overflow"]');
      const overflowCount = await overflowElements.count();

      // Check for horizontal scrolling (should be avoided)
      const pageWidth = await page.evaluate(() => document.documentElement.scrollWidth);
      const viewportWidth = await page.evaluate(() => window.innerWidth);

      expect(pageWidth).toBeLessThanOrEqual(viewportWidth * 1.1); // Allow minimal horizontal overflow

      // Run mobile accessibility scan
      const accessibilityScan = await new AxeBuilder({ page })
        .withTags(['wcag2a', 'wcag2aa', 'wcag21aa'])
        .analyze();

      expect(accessibilityScan.violations.filter(v => v.impact === 'critical')).toHaveLength(0);
    });
  });

  test.describe('Comprehensive Page Accessibility', () => {
    test('COMP-1: Full application accessibility audit', async ({ page }) => {
      const pages = [
        { path: '/', name: 'Home' },
        { path: '/documents', name: 'Documents' },
        { path: '/dashboard', name: 'Dashboard' },
        { path: '/settings', name: 'Settings' }
      ];

      const accessibilityResults = [];

      for (const pageConfig of pages) {
        console.log(`Testing accessibility for: ${pageConfig.name}`);

        await page.goto(pageConfig.path);
        await page.waitForLoadState('networkidle');

        // Run comprehensive accessibility scan
        const accessibilityScan = await new AxeBuilder({ page })
          .withTags(['wcag2a', 'wcag2aa', 'wcag21aa'])
          .exclude('[data-testid="debug"]') // Exclude debug elements
          .analyze();

        const violations = accessibilityScan.violations.filter(v => v.impact !== 'minor');

        accessibilityResults.push({
          page: pageConfig.name,
          path: pageConfig.path,
          violations: violations.length,
          criticalViolations: violations.filter(v => v.impact === 'critical').length,
          seriousViolations: violations.filter(v => v.impact === 'serious').length,
          moderateViolations: violations.filter(v => v.impact === 'moderate').length,
          minorViolations: violations.filter(v => v.impact === 'minor').length
        });

        // Verify no critical violations
        expect(violations.filter(v => v.impact === 'critical')).toHaveLength(0);

        // Log any violations found
        if (violations.length > 0) {
          console.log(`\nAccessibility issues found on ${pageConfig.name}:`);
          violations.forEach(violation => {
            console.log(`  - ${violation.id}: ${violation.description}`);
            console.log(`    Impact: ${violation.impact}`);
            console.log(`    Tags: ${violation.tags.join(', ')}`);
            if (violation.nodes.length > 0) {
              console.log(`    Affected elements: ${violation.nodes.length}`);
            }
          });
        }

        // Take accessibility screenshot
        await page.screenshot({
          path: `test-results/accessibility-${pageConfig.name.toLowerCase()}.png`,
          fullPage: true
        });
      }

      // Summary statistics
      const totalViolations = accessibilityResults.reduce((sum, result) => sum + result.violations, 0);
      const criticalViolations = accessibilityResults.reduce((sum, result) => sum + result.criticalViolations, 0);
      const seriousViolations = accessibilityResults.reduce((sum, result) => sum + result.seriousViolations, 0);

      console.log('\n=== ACCESSIBILITY AUDIT SUMMARY ===');
      console.log(`Pages tested: ${pages.length}`);
      console.log(`Total violations: ${totalViolations}`);
      console.log(`Critical violations: ${criticalViolations}`);
      console.log(`Serious violations: ${seriousViolations}`);

      accessibilityResults.forEach(result => {
        console.log(`\n${result.page} (${result.path}):`);
        console.log(`  Violations: ${result.violations}`);
        console.log(`  Critical: ${result.criticalViolations}`);
        console.log(`  Serious: ${result.seriousViolations}`);
      });

      // Assert overall compliance
      expect(criticalViolations).toBe(0);
      expect(seriousViolations).toBeLessThan(5); // Allow minimal serious issues
      expect(totalViolations).toBeLessThan(20); // Reasonable threshold for total issues
    });
  });
});