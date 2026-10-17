> **Archived 2026-10-17: early E2E framework write-up; commands and layout are outdated. The live E2E suite is tests/e2e.**
> Historical record, not a live contract. Originally at `frontend/E2E_TESTING_DOCUMENTATION.md`; relative links and paths below were not updated.

# E2E Testing Documentation

## Overview

This document provides comprehensive documentation for the End-to-End (E2E) testing framework built with Playwright for the Multimodal Enterprise RAG System. The framework ensures all critical user journeys are fully functional across different browsers and devices.

## Table of Contents

1. [Architecture](#architecture)
2. [Test Structure](#test-structure)
3. [Test Coverage](#test-coverage)
4. [Running Tests](#running-tests)
5. [Configuration](#configuration)
6. [Page Objects](#page-objects)
7. [Test Data Management](#test-data-management)
8. [CI/CD Integration](#cicd-integration)
9. [Performance Testing](#performance-testing)
10. [Accessibility Testing](#accessibility-testing)
11. [Visual Regression Testing](#visual-regression-testing)
12. [Troubleshooting](#troubleshooting)

## Architecture

### Framework Components

- **Playwright**: Modern E2E testing framework supporting multiple browsers
- **Page Object Model**: Maintainable test automation pattern
- **Fixtures and Data Management**: Reusable test data and setup
- **Cross-browser Testing**: Chrome, Firefox, Safari, Edge compatibility
- **Mobile Testing**: iOS and Android device emulation
- **Accessibility Testing**: WCAG 2.1 AA compliance with axe-core
- **Performance Monitoring**: Core Web Vitals and custom metrics
- **Visual Regression**: Screenshot comparison testing

### Directory Structure

```
frontend/
├── e2e/
│   ├── accessibility/           # Accessibility test suite
│   ├── cross-browser/          # Cross-browser compatibility tests
│   ├── error-handling/         # Error scenario tests
│   ├── fixtures/               # Test fixtures and data
│   ├── performance/            # Performance test suite
│   ├── user-workflows/         # Core user journey tests
│   ├── utils/                  # Page objects and utilities
│   ├── visual/                 # Visual regression tests
│   ├── global-setup.ts         # Global test setup
│   ├── global-teardown.ts      # Global test cleanup
│   └── playwright.config.ts    # Playwright configuration
├── scripts/                    # CI/CD helper scripts
└── test-data/                  # Test data and files
```

## Test Coverage

### User Story Coverage

#### US1: Document Ingestion Workflow
- User registration and login
- Document upload (drag-and-drop) for all file types
- Upload progress tracking and error handling
- Processing status monitoring (real-time updates)
- Document library management and deletion

#### US2: Natural Language Query Workflow
- Query input with natural language
- Search execution with loading states
- Results display with source citations
- Tab navigation (Answers, Sources, Graph, Eval)
- Search history management

#### US3: Knowledge Graph Exploration
- Graph visualization rendering
- Entity node interaction and navigation
- Relationship exploration and filtering
- Graph controls (zoom, pan, layout selection)
- Entity details panel display

#### US4: Query Performance Evaluation
- RAG Triad metrics display (Answer Relevancy, Faithfulness, Contextual Relevancy)
- Performance charts and trends
- Quality indicators and recommendations
- Evaluation test suite management

### Cross-Browser & Device Testing

#### Desktop Browsers
- **Chrome**: Latest stable version
- **Firefox**: Latest stable version
- **Safari**: Latest stable version
- **Edge**: Latest stable version

#### Mobile Devices
- **iPhone**: iPhone 14 (390x844)
- **Android**: Pixel 5 (393x851)

#### Tablet Devices
- **iPad**: iPad Pro (1024x1366)

### Accessibility Testing

- WCAG 2.1 AA compliance validation
- Screen reader compatibility testing
- Keyboard navigation testing
- Color contrast validation
- ARIA labels and semantic HTML verification
- Focus management testing

### Performance Testing

- Page load time optimization (<3 seconds)
- Search query response time (<2 seconds)
- File upload progress tracking (<100ms latency)
- Graph rendering performance (<1 second for 500 nodes)
- Core Web Vitals compliance
- Memory usage monitoring

## Running Tests

### Prerequisites

1. Install dependencies:
```bash
npm install --legacy-peer-deps
```

2. Install Playwright browsers:
```bash
npx playwright install
```

3. Ensure Docker services are running:
```bash
docker-compose up -d
```

### Running Tests Locally

#### Run All Tests
```bash
npx playwright test
```

#### Run Specific Test Suites

**User Workflows:**
```bash
npx playwright test e2e/user-workflows/
```

**Accessibility Tests:**
```bash
npx playwright test --project=accessibility
```

**Performance Tests:**
```bash
npx playwright test --project=performance
```

**Visual Regression Tests:**
```bash
npx playwright test --project=visual
```

#### Run Tests on Specific Browser
```bash
npx playwright test --project=chromium
npx playwright test --project=firefox
npx playwright test --project=webkit
```

#### Run Tests with Debug Mode
```bash
npx playwright test --debug
```

#### Run Tests with UI Mode
```bash
npx playwright test --ui
```

#### Generate Test Report
```bash
npx playwright test --reporter=html
```

### Test Filtering

#### Filter by Test Name
```bash
npx playwright test --grep "Document Upload"
```

#### Filter by Test File
```bash
npx playwright test e2e/user-workflows/us01-document-ingestion.spec.ts
```

#### Filter by Tag
```bash
npx playwright test --grep "@smoke"
npx playwright test --grep "@regression"
```

## Configuration

### Playwright Configuration

The main configuration is in `playwright.config.ts`:

#### Key Settings

- **Timeouts**: Global timeout (30s), action timeout (10s), navigation timeout (15s)
- **Retry Logic**: 2 retries in CI, 0 locally
- **Reporting**: HTML, JSON, JUnit reports
- **Parallel Execution**: Sharding support for faster execution
- **Screenshot Capture**: On failure only
- **Video Recording**: On failure only
- **Trace Collection**: On failure only

#### Environment Variables

```bash
# Application URL
BASE_URL=http://localhost:3000

# Test Credentials
TEST_USER_EMAIL=test@example.com
TEST_USER_PASSWORD=testpassword123
TEST_ADMIN_EMAIL=admin@example.com
TEST_ADMIN_PASSWORD=adminpassword123

# CI Environment
CI=true

# Performance Monitoring
E2E_PERFORMANCE_MONITORING=true

# Visual Testing
E2E_UPDATE_SNAPSHOTS=true
```

### Browser Configuration

#### Desktop Browsers
```typescript
{
  name: 'chromium',
  use: {
    ...devices['Desktop Chrome'],
    viewport: { width: 1280, height: 720 }
  }
}
```

#### Mobile Devices
```typescript
{
  name: 'iPhone',
  use: {
    ...devices['iPhone 14'],
    viewport: { width: 390, height: 844 }
  }
}
```

## Page Objects

### Architecture

Page Objects provide a maintainable layer between tests and the application UI:

#### BasePage
Common functionality across all pages:
- Navigation
- Page loading
- Error handling
- Screenshot capture

#### Specific Page Objects

**LoginPage**: Authentication functionality
- Form interactions
- Validation
- Error handling

**DocumentsPage**: Document management
- File upload
- Search and filtering
- Status monitoring

**SearchPage**: Search functionality
- Query execution
- Results handling
- Tab navigation

**KnowledgeGraphPage**: Graph exploration
- Node interaction
- Layout controls
- Entity details

**EvaluationPage**: Performance evaluation
- Metrics display
- Test management
- Data export

### Usage Example

```typescript
import { LoginPage } from '../utils/page-objects';

const loginPage = new LoginPage(page);
await loginPage.login('user@example.com', 'password123');
await loginPage.verifyLoginSuccessful();
```

## Test Data Management

### Fixtures

Test fixtures provide reusable test data and setup:

#### Test Data Fixture
```typescript
{
  users: {
    valid: { email: 'test@example.com', password: 'test123' },
    admin: { email: 'admin@example.com', password: 'admin123' }
  },
  files: {
    pdf: { path: 'test-data/files/sample.pdf', name: 'sample.pdf' },
    text: { path: 'test-data/files/sample.txt', name: 'sample.txt' }
  },
  queries: {
    simple: ['What is machine learning?'],
    complex: ['Compare different approaches to...']
  }
}
```

#### Authenticated Page Fixture
Automatically logs in before each test:
```typescript
test.use({
  authenticatedPage: async ({ page }, use) => {
    await loginAsUser(page, testUsers.valid);
    await use(page);
  }
});
```

### Test Files

#### Supported File Types
- **PDF**: Sample documents for upload testing
- **Text**: Plain text files for search testing
- **Images**: JPG/PNG for multimodal testing
- **Audio**: MP3 for transcription testing
- **Video**: MP4 for content analysis testing

#### File Locations
```
test-data/
├── files/           # Test files
├── uploads/         # Temporary upload storage
├── fixtures/        # Test data fixtures
└── temp/           # Temporary test data
```

## CI/CD Integration

### GitHub Actions Workflow

#### Triggers
- Push to main/develop branches
- Pull requests to main/develop
- Daily scheduled runs (2 AM UTC)
- Manual dispatch with parameters

#### Test Matrix
- **Smoke Tests**: Quick validation on PRs
- **Regression Tests**: Full suite on main branch
- **Accessibility Tests**: WCAG compliance validation
- **Performance Tests**: Core Web Vitals monitoring
- **Visual Tests**: UI consistency validation

#### Parallel Execution
- Sharding across multiple machines
- Browser-specific test runs
- Test type segregation (smoke, regression, etc.)

#### Reporting
- HTML reports with detailed results
- JSON artifacts for programmatic access
- JUnit XML for CI integration
- Summary reports with trends

### Environment Setup

#### Test Services
- PostgreSQL: Test database
- Redis: Caching layer
- Neo4j: Knowledge graph
- Qdrant: Vector store

#### Service Health Checks
```bash
# Wait for services to be ready
docker-compose up -d
sleep 20

# Verify service health
curl -f http://localhost:7474  # Neo4j
curl -f http://localhost:6333  # Qdrant
```

### Test Execution

#### Local Development
```bash
# Start services
docker-compose up -d

# Run tests
npm run test:e2e

# Generate reports
npm run test:e2e:report
```

#### CI/CD Pipeline
```bash
# Install dependencies
npm ci --legacy-peer-deps

# Run linting and type checking
npm run lint
npm run type-check

# Run E2E tests
npx playwright test

# Upload results
npm run test:upload-results
```

## Performance Testing

### Metrics Monitored

#### Core Web Vitals
- **LCP (Largest Contentful Paint)**: <2.5s
- **FID (First Input Delay)**: <100ms
- **CLS (Cumulative Layout Shift)**: <0.1

#### Custom Metrics
- **Page Load Time**: <3s
- **Search Response Time**: <2s
- **File Upload Latency**: <100ms
- **Graph Rendering Time**: <1s

#### Resource Metrics
- **Total Request Count**: <50
- **Total Transfer Size**: <5MB
- **Image Optimization**: WebP support
- **Script Optimization**: Minification, compression

### Performance Test Structure

```typescript
test('Page load performance', async ({ page }) => {
  const startTime = Date.now();
  await page.goto('/login');
  const loadTime = Date.now() - startTime;

  expect(loadTime).toBeLessThan(3000);

  const vitals = await page.evaluate(() => {
    // Core Web Vitals collection
  });
});
```

### Regression Detection

#### Baseline Comparison
- Automatic baseline updates on main branch
- Regression threshold: 20% degradation
- Critical regression: 50% degradation

#### Performance Budgets
- JavaScript bundle size limits
- Image compression requirements
- API response time limits

## Accessibility Testing

### WCAG 2.1 AA Compliance

#### axe-core Integration
```typescript
const accessibilityScanResults = await new AxeBuilder({ page })
  .withTags(['wcag2a', 'wcag2aa', 'wcag21aa'])
  .analyze();

expect(accessibilityScanResults.violations).toEqual([]);
```

#### Test Coverage Areas
- **Keyboard Navigation**: Tab order, focus management
- **Screen Reader Support**: ARIA labels, semantic HTML
- **Color Contrast**: WCAG AA compliance
- **Touch Targets**: Minimum 44x44px
- **Visual Indicators**: Focus states, error messages

### Accessibility Test Structure

```typescript
test('Screen reader compatibility', async ({ page }) => {
  // ARIA landmarks
  await expect(page.locator('main, [role="main"]')).toBeVisible();

  // Form labels
  await expect(page.locator('[data-testid="search-input"]'))
    .toHaveAttribute('aria-label');

  // Dynamic content announcements
  await page.evaluate(() => {
    const statusRegion = document.createElement('div');
    statusRegion.setAttribute('aria-live', 'polite');
    document.body.appendChild(statusRegion);
  });
});
```

### Automated Checks

#### Color Contrast
- Automated color contrast ratio validation
- Minimum 4.5:1 for normal text
- Minimum 3:1 for large text

#### Focus Management
- Visible focus indicators
- Logical tab order
- Focus trapping in modals

#### Screen Reader Support
- Proper ARIA labels
- Semantic HTML structure
- Heading hierarchy validation

## Visual Regression Testing

### Screenshot Testing

#### Full Page Screenshots
```typescript
await expect(page).toHaveScreenshot('login-page-full.png', {
  fullPage: true,
  animations: 'disabled'
});
```

#### Component Screenshots
```typescript
await expect(page.locator('[data-testid="upload-area"]'))
  .toHaveScreenshot('upload-area-default.png', {
    animations: 'disabled'
  });
```

### Cross-Browser Visual Testing

#### Browser Comparison
- Chrome, Firefox, Safari, Edge
- Consistent UI rendering
- Responsive design validation

#### Device Testing
- Mobile (iPhone, Android)
- Tablet (iPad, Android tablet)
- Desktop (various resolutions)

### State Testing

#### Interactive States
- Hover, focus, active states
- Loading and error states
- Disabled/enabled states

#### Dynamic Content
- Form validation states
- Modal overlays
- Loading indicators

## Troubleshooting

### Common Issues

#### Test Flakiness
1. **Wait Strategies**: Use explicit waits over implicit waits
2. **Locators**: Prefer stable locators (data-testid, text)
3. **Timeouts**: Increase timeouts for slow operations
4. **Retry Logic**: Implement retry for network-dependent tests

#### Browser Issues
1. **Browser Installation**: `npx playwright install`
2. **Browser Updates**: Regular browser updates
3. **Headless Mode**: Debug with headed mode
4. **Permissions**: Ensure proper browser permissions

#### Performance Issues
1. **Test Isolation**: Avoid test dependencies
2. **Parallel Execution**: Use sharding for large suites
3. **Resource Cleanup**: Proper test teardown
4. **Memory Leaks**: Monitor browser memory usage

### Debugging Techniques

#### Debug Mode
```bash
npx playwright test --debug
```

#### Headed Mode
```bash
npx playwright test --headed
```

#### Trace Viewing
```bash
npx playwright show-trace trace.zip
```

#### Code Generation
```bash
npx playwright codegen http://localhost:3000
```

### Environment Issues

#### Docker Services
```bash
# Check service status
docker-compose ps

# View service logs
docker-compose logs neo4j
docker-compose logs qdrant

# Restart services
docker-compose restart
```

#### Network Issues
```bash
# Test connectivity
curl -f http://localhost:3000
curl -f http://localhost:8000  # Backend API

# Check port conflicts
lsof -i :3000
lsof -i :8000
```

### Test Data Issues

#### File Upload Problems
1. Verify file paths exist
2. Check file permissions
3. Validate file formats
4. Monitor file size limits

#### Authentication Issues
1. Verify test credentials
2. Check token expiration
3. Validate session management
4. Test with different user roles

## Best Practices

### Test Design
1. **Independent Tests**: Each test should be self-contained
2. **Descriptive Names**: Clear test titles and descriptions
3. **Page Objects**: Use POM for maintainability
4. **Data Separation**: Separate test data from test logic

### Performance
1. **Parallel Execution**: Run tests in parallel
2. **Test Isolation**: Avoid test dependencies
3. **Resource Cleanup**: Proper teardown procedures
4. **Selective Execution**: Run only relevant tests

### Maintenance
1. **Regular Updates**: Keep dependencies current
2. **Code Reviews**: Review test changes
3. **Documentation**: Keep docs up to date
4. **Monitoring**: Track test success rates

### CI/CD
1. **Fast Feedback**: Quick smoke tests on PRs
2. **Comprehensive Testing**: Full suite on main branch
3. **Failure Notifications**: Alert on test failures
4. **Trend Analysis**: Monitor test performance

## Getting Help

### Resources
- [Playwright Documentation](https://playwright.dev/)
- [axe-core Documentation](https://www.deque.com/axe/)
- [WCAG Guidelines](https://www.w3.org/WAI/WCAG21/quickref/)
- [Core Web Vitals](https://web.dev/vitals/)

### Support Channels
- GitHub Issues: Report bugs and feature requests
- Team Chat: Real-time discussions
- Documentation: This guide and code comments
- Code Reviews: Peer review process

### Contributing
1. Fork the repository
2. Create feature branch
3. Add tests for new functionality
4. Ensure all tests pass
5. Submit pull request

---

This documentation provides comprehensive guidance for working with the E2E testing framework. For specific questions or issues, refer to the code comments or reach out to the development team.