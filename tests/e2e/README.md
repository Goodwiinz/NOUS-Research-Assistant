# E2E Test Suite for Knowledge Graph Analytics Dashboard

This comprehensive E2E testing framework validates complete user workflows across the Knowledge Graph Analytics Dashboard system.

## 🎯 Test Coverage

### User Journey Tests
- **Analytics Dashboard Access**: Login → Dashboard View → Widget Interaction → Logout
- **Graph Analytics Exploration**: Navigate → Apply Filters → Explore Entities → Generate Reports
- **Custom Dashboard Creation**: Create → Add Widgets → Configure → Save → Share
- **Real-time Monitoring**: Live Metrics → Watch Updates → Receive Alerts → Respond
- **Multi-tenant Workflow**: Organization Switching → Data Isolation → Cross-org Validation
- **Document Analytics Flow**: Upload → Processing → Analytics → Insights
- **Admin Management**: User Management → Role Assignment → Configuration → Health

### Testing Types
- **Visual Regression**: Screenshot comparison for UI consistency
- **Accessibility**: WCAG 2.1 AA compliance validation
- **Cross-browser**: Chrome, Firefox, Safari compatibility
- **Mobile Responsive**: Tablet and mobile device testing
- **Performance**: Page load times and interaction responsiveness
- **Data Flow**: End-to-end data integrity validation
- **Error Handling**: Graceful failure and recovery testing

## 🚀 Quick Start

### Prerequisites
- Node.js 18+
- Docker and Docker Compose
- Playwright browsers

### Installation

```bash
# Install dependencies
cd tests/e2e
pnpm install --frozen-lockfile

# Install Playwright browsers
pnpm exec playwright install

# Install additional browsers (if needed)
pnpm exec playwright install firefox webkit
```

### Environment Setup

```bash
# Copy environment configuration
cp .env.test.example .env.test

# Edit environment variables
nano .env.test
```

### Running Tests

```bash
# Run all tests
pnpm run test:e2e

# Run tests with UI
pnpm run test:e2e:ui

# Run tests with debugging
pnpm run test:e2e:debug

# Run specific test suites
pnpm run test:e2e:visual
pnpm run test:e2e:accessibility
pnpm run test:e2e:mobile
pnpm run test:e2e:performance
```

## 📊 Test Reports

After running tests, reports are generated in `test-results/`:

- **HTML Report**: Interactive test results with screenshots and videos
- **JSON Report**: Machine-readable test metrics
- **Summary Report**: Consolidated test results across all categories

View reports:
```bash
pnpm run test:e2e:report
```

## 🧪 Test Configuration

### Base Configuration (`playwright.config.ts`)
- Browser support: Chromium, Firefox, WebKit
- Parallel execution for faster test runs
- Automatic retries and timeouts
- Screenshot/video capture on failures
- Trace collection for debugging

### Specialized Configurations
- `playwright-visual.config.ts`: Visual regression testing
- `playwright-accessibility.config.ts`: Accessibility testing with axe-core
- `playwright-mobile.config.ts`: Mobile device testing
- `playwright-performance.config.ts`: Performance testing with metrics

### Environment Variables

Key variables in `.env.test`:

```bash
# Application URLs
BASE_URL=http://localhost:3000
API_BASE_URL=http://localhost:8000

# Test Credentials
TEST_ADMIN_EMAIL=admin@test.com
TEST_ADMIN_PASSWORD=admin123456

# Database Connections
TEST_DATABASE_URL=postgresql://test_user:test_password@localhost:5432/rag_test_db
NEO4J_TEST_URI=bolt://localhost:7687

# Feature Flags
ENABLE_ANALYTICS_TESTS=true
ENABLE_GRAPH_TESTS=true
ENABLE_DOCUMENT_UPLOAD_TESTS=true
```

## 🎨 Visual Testing

### Taking Screenshots
```typescript
// Automatic screenshots on failures (configured)
await page.screenshot({ path: 'test-results/screenshots/test.png', fullPage: true });
```

### Visual Regression
```bash
# Run visual tests
pnpm run test:e2e:visual

# Update visual snapshots
UPDATE_SNAPSHOTS=true pnpm run test:e2e:visual
```

### Cross-browser Visual Testing
Tests automatically run on:
- Chrome/Chromium (Desktop & Mobile)
- Firefox (Desktop)
- Safari/WebKit (Desktop & Mobile)

## ♿ Accessibility Testing

### WCAG 2.1 AA Compliance
```typescript
// Automatic accessibility checks
await checkA11y(page, null, {
  detailedReport: true,
  rules: {
    'color-contrast': { enabled: true },
    'keyboard-navigation': { enabled: true },
    'aria-labels': { enabled: true },
  },
});
```

### Running Accessibility Tests
```bash
pnpm run test:e2e:accessibility
```

### Accessibility Report Features
- Violation categorization by impact level
- Interactive accessibility reports
- PR comments with accessibility findings
- Historical tracking of accessibility improvements

## 📱 Mobile Testing

### Device Coverage
- **Mobile**: iPhone 12, Pixel 5, Galaxy S20
- **Tablet**: iPad Pro, Surface Pro
- **Responsive**: Custom viewport testing

### Mobile-Specific Tests
- Touch gesture support
- Virtual keyboard handling
- Device orientation changes
- Mobile performance optimization

### Running Mobile Tests
```bash
pnpm run test:e2e:mobile
```

## ⚡ Performance Testing

### Performance Metrics
- **Page Load Time**: Target < 3 seconds
- **First Contentful Paint**: Target < 1.5 seconds
- **Time to Interactive**: Target < 5 seconds
- **Cumulative Layout Shift**: Target < 0.1
- **First Input Delay**: Target < 100ms

### Performance Test Types
```typescript
// Page load performance
const metrics = await page.evaluate(() => {
  const navigation = performance.getEntriesByType('navigation')[0];
  return {
    domContentLoaded: navigation.domContentLoadedEventEnd - navigation.domContentLoadedEventStart,
    loadComplete: navigation.loadEventEnd - navigation.loadEventStart,
    firstContentfulPaint: performance.getEntriesByType('paint')[0]?.startTime,
  };
});
```

### Running Performance Tests
```bash
pnpm run test:e2e:performance
```

## 🔧 Test Utilities

### Helper Functions
```typescript
import { createTestHelpers, TEST_DATA } from '../utils/test-helpers';

const helpers = createTestHelpers(page, context, testInfo);

// Common operations
await helpers.login(TEST_DATA.USERS.ADMIN);
await helpers.waitForPageLoad();
await helpers.takeScreenshot('test-step');

// Assertions
await helpers.expectElementVisible('[data-testid="dashboard"]');
```

### Test Data Management
```typescript
// Generate test data
const testData = helpers.generateTestData('email');
const testUser = helpers.generateTestData('name');

// File uploads
await helpers.uploadFile('[data-testid="file-input"]', './test-file.pdf');
```

## 🐛 Debugging

### Debug Mode
```bash
# Run tests with debugging
pnpm run test:e2e:debug

# Generate code for test recording
pnpm run test:e2e:codegen
```

### Debug Features
- **Browser DevTools**: Inspect elements during test execution
- **Test Traces**: Detailed execution history
- **Network Monitoring**: API request/response inspection
- **Console Logs**: Browser console output capture

### Common Debugging Commands
```typescript
// Pause execution
await page.pause();

// Take screenshot
await page.screenshot({ path: 'debug.png' });

// Log page state
console.log(await page.content());

// Check element visibility
const isVisible = await page.locator('[data-testid="element"]').isVisible();
```

## 📈 CI/CD Integration

### GitHub Actions Workflow
The E2E tests are integrated with GitHub Actions and run on:

- **Pull Requests**: Full test suite
- **Main Branch**: Daily scheduled runs
- **Manual Triggers**: On-demand test execution

### CI Features
- **Parallel Execution**: Tests run across multiple machines
- **Artifact Storage**: Test results, screenshots, and videos
- **PR Comments**: Automated test result summaries
- **Slack Notifications**: Build status updates
- **GitHub Pages**: Published test reports

### Running Tests Locally for CI
```bash
# Install dependencies
pnpm install --frozen-lockfile

# Start services
docker-compose -f docker-compose.test.yml up -d

# Run tests
pnpm run test:e2e:ci

# Cleanup
docker-compose -f docker-compose.test.yml down
```

## 📋 Best Practices

### Test Organization
- **Describe Blocks**: Logical test grouping
- **Test Naming**: Clear, descriptive test names
- **Setup/Teardown**: Proper test isolation
- **Data Management**: Consistent test data handling

### Writing Reliable Tests
- **Explicit Waits**: Use proper waiting strategies
- **Test Data**: Use deterministic test data
- **Assertions**: Clear failure messages
- **Error Handling**: Graceful test failures

### Performance Considerations
- **Parallel Execution**: Optimize test run time
- **Resource Cleanup**: Proper test teardown
- **Browser Reuse**: Efficient browser management
- **Network Optimization**: Mock external dependencies

## 🔍 Troubleshooting

### Common Issues

#### Tests Fail Due to Timing
```typescript
// Use explicit waits
await page.waitForSelector('[data-testid="element"]', { state: 'visible' });
await page.waitForLoadState('networkidle');
```

#### Flaky Tests
- Increase timeout values
- Use more specific selectors
- Add explicit waits
- Review test isolation

#### Browser Installation Issues
```bash
# Reinstall browsers
pnpm exec playwright install --force

# Clear browser cache
pnpm exec playwright install-deps
```

#### Docker Service Issues
```bash
# Check service status
docker-compose -f docker-compose.test.yml ps

# View logs
docker-compose -f docker-compose.test.yml logs <service>

# Restart services
docker-compose -f docker-compose.test.yml down && docker-compose -f docker-compose.test.yml up -d
```

### Getting Help

1. **Check Logs**: Review test execution logs
2. **Screenshots**: Examine failure screenshots
3. **Network Tab**: Inspect API calls
4. **Console Logs**: Check for JavaScript errors
5. **Test Data**: Verify test data setup

## 🤝 Contributing

### Adding New Tests
1. Follow existing test patterns
2. Use proper test data management
3. Include accessibility and performance checks
4. Add appropriate test tags and descriptions
5. Update documentation

### Test Review Checklist
- [ ] Test has clear purpose and description
- [ ] Uses proper test data and fixtures
- [ ] Includes accessibility considerations
- [ ] Handles error scenarios
- [ ] Has appropriate assertions
- [ ] Follows naming conventions
- [ ] Documentation is updated

## 📚 Additional Resources

- [Playwright Documentation](https://playwright.dev/)
- [Accessibility Guidelines](https://www.w3.org/WAI/WCAG21/quickref/)
- [Performance Testing Best Practices](https://web.dev/performance/)
- [Visual Regression Testing](https://playwright.dev/docs/test-snapshots)
- [Mobile Testing Guide](https://playwright.dev/docs/emulation)

---

**Last Updated**: 2024-01-01
**Version**: 1.0.0
**Maintainers**: E2E Test Automation Team