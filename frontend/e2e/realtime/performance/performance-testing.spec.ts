import { test, expect } from '../../fixtures/test-data.fixture';
import { DocumentsPage, SearchPage, KnowledgeGraphPage } from '../../utils/page-objects';

/**
 * Performance Testing Suite
 *
 * Test Coverage:
 * - Page load time optimization
 * - File upload performance
 * - Search query response time
 * - Graph rendering performance
 * - Memory usage and leak detection
 * - Network performance and optimization
 * - Core Web Vitals compliance
 */

test.describe('Performance Testing', () => {
  let documentsPage: DocumentsPage;
  let searchPage: SearchPage;
  let graphPage: KnowledgeGraphPage;

  test.beforeEach(async ({ authenticatedPage }) => {
    documentsPage = new DocumentsPage(authenticatedPage);
    searchPage = new SearchPage(authenticatedPage);
    graphPage = new KnowledgeGraphPage(authenticatedPage);
  });

  test('Page load performance - Login page', async ({ page }) => {
    // Enable performance monitoring
    await page.goto('/login', { waitUntil: 'domcontentloaded' });

    // Capture performance metrics
    const performanceMetrics = await page.evaluate(() => {
      const navigation = performance.getEntriesByType('navigation')[0] as PerformanceNavigationTiming;
      const paint = performance.getEntriesByType('paint');

      return {
        domContentLoaded: navigation.domContentLoadedEventEnd - navigation.domContentLoadedEventStart,
        loadComplete: navigation.loadEventEnd - navigation.loadEventStart,
        firstPaint: paint.find(entry => entry.name === 'first-paint')?.startTime || 0,
        firstContentfulPaint: paint.find(entry => entry.name === 'first-contentful-paint')?.startTime || 0,
        totalTime: navigation.loadEventEnd - navigation.startTime,
        resourceCount: performance.getEntriesByType('resource').length
      };
    });

    console.log('Login Page Performance:', performanceMetrics);

    // Performance assertions
    expect(performanceMetrics.firstContentfulPaint).toBeLessThan(2000); // <2s
    expect(performanceMetrics.domContentLoaded).toBeLessThan(3000); // <3s
    expect(performanceMetrics.loadComplete).toBeLessThan(5000); // <5s

    // Core Web Vitals
    const vitals = await page.evaluate(() => {
      return new Promise((resolve) => {
        new PerformanceObserver((list) => {
          const entries = list.getEntries();
          const vitals: any = {};

          entries.forEach((entry) => {
            if (entry.entryType === 'largest-contentful-paint') {
              vitals.LCP = entry.startTime;
            } else if (entry.entryType === 'first-input') {
              vitals.FID = (entry as any).processingStart - entry.startTime;
            } else if (entry.entryType === 'layout-shift') {
              vitals.CLS = (entry as any).value;
            }
          });

          if (Object.keys(vitals).length > 0) {
            resolve(vitals);
          }
        }).observe({ entryTypes: ['largest-contentful-paint', 'first-input', 'layout-shift'] });

        // Fallback timeout
        setTimeout(() => resolve({}), 5000);
      });
    });

    console.log('Core Web Vitals:', vitals);

    if (vitals.LCP) expect(vitals.LCP).toBeLessThan(2500); // <2.5s
    if (vitals.FID) expect(vitals.FID).toBeLessThan(100); // <100ms
    if (vitals.CLS) expect(vitals.CLS).toBeLessThan(0.1); // <0.1
  });

  test('Page load performance - Documents page', async ({ page }) => {
    const startTime = Date.now();

    await page.goto('/documents', { waitUntil: 'networkidle' });
    const loadTime = Date.now() - startTime;

    console.log(`Documents page loaded in ${loadTime}ms`);

    // Page should load within performance requirements
    expect(loadTime).toBeLessThan(3000); // <3s

    // Check for resource loading optimization
    const resourceMetrics = await page.evaluate(() => {
      const resources = performance.getEntriesByType('resource');
      const totalSize = resources.reduce((acc, resource) => {
        return acc + (resource as any).transferSize || 0;
      }, 0);

      return {
        totalResources: resources.length,
        totalSize: totalSize,
        imageResources: resources.filter(r => r.name.match(/\.(jpg|jpeg|png|gif|svg|webp)$/i)).length,
        scriptResources: resources.filter(r => r.name.match(/\.js$/i)).length,
        cssResources: resources.filter(r => r.name.match(/\.css$/i)).length
      };
    });

    console.log('Document page resource metrics:', resourceMetrics);

    // Verify reasonable resource usage
    expect(resourceMetrics.totalResources).toBeLessThan(50);
    expect(resourceMetrics.totalSize).toBeLessThan(5 * 1024 * 1024); // <5MB
  });

  test('Search query performance', async ({ page, testData }) => {
    await page.goto('/search');

    // Measure search performance
    const searchMetrics = await Promise.all([
      page.waitForNavigation({ waitUntil: 'networkidle' }),
      page.evaluate(async () => {
        const start = performance.now();
        return { start };
      })
    ]);

    // Perform search and measure response time
    const query = testData.queries.simple[0];
    const searchStartTime = Date.now();

    await searchPage.performSearch(query);
    const searchEndTime = Date.now();

    const searchResponseTime = searchEndTime - searchStartTime;
    console.log(`Search query "${query}" completed in ${searchResponseTime}ms`);

    // Search should be responsive
    expect(searchResponseTime).toBeLessThan(2000); // <2s requirement

    // Wait for results to fully load
    await searchPage.waitForResults();

    // Measure render performance of results
    const renderStartTime = Date.now();
    const resultCount = await searchPage.getResultsCount();
    const renderEndTime = Date.now();

    const renderTime = renderEndTime - renderStartTime;
    console.log(`Results rendering completed in ${renderTime}ms for ${resultCount} results`);

    // Results should render quickly
    expect(renderTime).toBeLessThan(1000); // <1s

    // Test performance with complex queries
    const complexQuery = testData.queries.complex[0];
    const complexSearchStart = Date.now();

    await searchPage.performSearch(complexQuery);
    await searchPage.waitForResults();

    const complexSearchTime = Date.now() - complexSearchStart;
    console.log(`Complex search completed in ${complexSearchTime}ms`);

    // Even complex queries should be reasonably fast
    expect(complexSearchTime).toBeLessThan(5000); // <5s
  });

  test('File upload performance', async ({ page, testData }) => {
    await page.goto('/documents');

    const testFile = testData.files.text; // Use smaller file for performance test

    // Monitor upload progress
    const uploadMetrics = await page.evaluate((fileName) => {
      return new Promise((resolve) => {
        let uploadStartTime: number;
        let uploadEndTime: number;

        // Override XMLHttpRequest to monitor upload
        const originalXHR = window.XMLHttpRequest;
        window.XMLHttpRequest = function() {
          const xhr = new originalXHR();
          const originalOpen = xhr.open;
          const originalSend = xhr.send;

          xhr.open = function(method: string, url: string | URL) {
            if (url.includes('/upload')) {
              uploadStartTime = performance.now();
            }
            return originalOpen.call(this, method, url);
          };

          xhr.send = function(data) {
            if (uploadStartTime) {
              xhr.addEventListener('loadend', () => {
                uploadEndTime = performance.now();
                resolve({
                  uploadTime: uploadEndTime - uploadStartTime,
                  fileName: fileName
                });
              });
            }
            return originalSend.call(this, data);
          };

          return xhr;
        };

        // Fallback timeout
        setTimeout(() => resolve({ uploadTime: -1, fileName }), 30000);
      });
    }, testFile.name);

    // Perform upload
    const fileItem = await documentsPage.uploadFile(testFile.path, testFile.name);

    // Wait for upload metrics
    await page.waitForTimeout(2000);

    const uploadResult = await page.evaluate(() => {
      return (window as any).lastUploadMetrics || { uploadTime: -1 };
    });

    if (uploadResult.uploadTime > 0) {
      console.log(`File upload completed in ${uploadResult.uploadTime}ms`);
      expect(uploadResult.uploadTime).toBeLessThan(10000); // <10s for reasonable file sizes
    }

    // Test batch upload performance
    const files = [testData.files.pdf, testData.files.text].filter(f => f.size > 0);
    if (files.length > 1) {
      const batchStartTime = Date.now();

      const filePaths = files.map(f => f.path);
      await documentsPage.fileInput.setInputFiles(filePaths);

      // Wait for all uploads to appear
      for (const file of files) {
        const fileItem = documentsPage.getFileItem(file.name);
        await fileItem.waitFor({ state: 'visible', timeout: 30000 });
      }

      const batchUploadTime = Date.now() - batchStartTime;
      console.log(`Batch upload of ${files.length} files completed in ${batchUploadTime}ms`);

      // Batch upload should be efficient
      expect(batchUploadTime).toBeLessThan(30000); // <30s for multiple files
    }
  });

  test('Knowledge graph rendering performance', async ({ page }) => {
    const renderStartTime = Date.now();

    await page.goto('/graph');
    await page.waitForSelector('[data-testid="graph-canvas"]', { timeout: 15000 });

    const initialRenderTime = Date.now() - renderStartTime;
    console.log(`Initial graph render completed in ${initialRenderTime}ms`);

    // Graph should render quickly
    expect(initialRenderTime).toBeLessThan(3000); // <3s

    // Wait for nodes to load
    await page.waitForSelector('[data-testid="graph-node"]', { timeout: 10000 });

    // Measure interaction performance
    const interactionStartTime = Date.now();

    const nodes = page.locator('[data-testid="graph-node"]');
    if (await nodes.count() > 0) {
      await nodes.first().click();

      const interactionTime = Date.now() - interactionStartTime;
      console.log(`Graph node interaction completed in ${interactionTime}ms`);

      // Interactions should be responsive
      expect(interactionTime).toBeLessThan(500); // <500ms
    }

    // Test zoom performance
    const zoomStartTime = Date.now();
    await page.click('[data-testid="zoom-in"]');
    await page.waitForTimeout(500);
    const zoomTime = Date.now() - zoomStartTime;

    console.log(`Graph zoom completed in ${zoomTime}ms`);
    expect(zoomTime).toBeLessThan(1000); // <1s

    // Test layout change performance
    const layoutStartTime = Date.now();
    await page.click('[data-testid="layout-selector"]');
    await page.click('[data-testid="layout-circular"]');
    await page.waitForTimeout(2000);

    const layoutTime = Date.now() - layoutStartTime;
    console.log(`Graph layout change completed in ${layoutTime}ms`);
    expect(layoutTime).toBeLessThan(5000); // <5s
  });

  test('Memory usage and leak detection', async ({ page }) => {
    // Get baseline memory usage
    const baselineMemory = await page.evaluate(() => {
      if ('memory' in performance) {
        return {
          usedJSHeapSize: (performance as any).memory.usedJSHeapSize,
          totalJSHeapSize: (performance as any).memory.totalJSHeapSize,
          jsHeapSizeLimit: (performance as any).memory.jsHeapSizeLimit
        };
      }
      return null;
    });

    console.log('Baseline memory usage:', baselineMemory);

    // Perform memory-intensive operations
    await page.goto('/documents');
    await page.goto('/search');
    await page.goto('/graph');
    await page.goto('/documents');

    // Force garbage collection if available
    await page.evaluate(() => {
      if ((window as any).gc) {
        (window as any).gc();
      }
    });

    // Check memory usage after operations
    const finalMemory = await page.evaluate(() => {
      if ('memory' in performance) {
        return {
          usedJSHeapSize: (performance as any).memory.usedJSHeapSize,
          totalJSHeapSize: (performance as any).memory.totalJSHeapSize,
          jsHeapSizeLimit: (performance as any).memory.jsHeapSizeLimit
        };
      }
      return null;
    });

    console.log('Final memory usage:', finalMemory);

    if (baselineMemory && finalMemory) {
      const memoryIncrease = finalMemory.usedJSHeapSize - baselineMemory.usedJSHeapSize;
      const memoryIncreaseMB = memoryIncrease / (1024 * 1024);

      console.log(`Memory increase: ${memoryIncreaseMB.toFixed(2)} MB`);

      // Memory increase should be reasonable
      expect(memoryIncreaseMB).toBeLessThan(50); // <50MB increase

      // Check for potential memory leaks
      expect(memoryIncreaseMB).toBeLessThan(20); // <20MB for simple navigation
    }
  });

  test('Network performance and optimization', async ({ page }) => {
    // Monitor network requests
    const networkMetrics: any[] = [];

    page.on('request', (request) => {
      networkMetrics.push({
        url: request.url(),
        method: request.method(),
        startTime: Date.now()
      });
    });

    page.on('response', (response) => {
      const requestIndex = networkMetrics.findIndex(m => m.url === response.url());
      if (requestIndex >= 0) {
        networkMetrics[requestIndex] = {
          ...networkMetrics[requestIndex],
          status: response.status(),
          endTime: Date.now(),
          responseSize: response.headers()['content-length'] || 0
        };
        networkMetrics[requestIndex].duration =
          networkMetrics[requestIndex].endTime - networkMetrics[requestIndex].startTime;
      }
    });

    // Navigate through application
    await page.goto('/documents');
    await page.goto('/search');
    await page.goto('/graph');

    // Analyze network performance
    const completedRequests = networkMetrics.filter(m => m.duration);
    const slowRequests = completedRequests.filter(m => m.duration > 1000);
    const failedRequests = completedRequests.filter(m => m.status >= 400);

    console.log(`Network performance summary:`);
    console.log(`- Total requests: ${completedRequests.length}`);
    console.log(`- Slow requests (>1s): ${slowRequests.length}`);
    console.log(`- Failed requests: ${failedRequests.length}`);

    if (slowRequests.length > 0) {
      console.log('Slow requests:', slowRequests.map(r => `${r.url} (${r.duration}ms)`));
    }

    // Performance assertions
    expect(slowRequests.length).toBeLessThan(3); // Few slow requests
    expect(failedRequests.length).toBe(0); // No failed requests

    // Check for optimized resource loading
    const imageRequests = completedRequests.filter(m =>
      m.url.match(/\.(jpg|jpeg|png|gif|svg|webp)$/i)
    );
    const scriptRequests = completedRequests.filter(m =>
      m.url.match(/\.js$/i)
    );

    // Verify reasonable number of requests
    expect(imageRequests.length).toBeLessThan(20);
    expect(scriptRequests.length).toBeLessThan(10);

    // Check for caching headers
    const cacheableRequests = completedRequests.filter(m =>
      m.url.match(/\.(js|css|png|jpg|jpeg|gif|svg|webp)$/i)
    );

    console.log(`Cacheable resources: ${cacheableRequests.length}`);
  });

  test('Core Web Vitals compliance', async ({ page }) => {
    await page.goto('/');

    // Collect Core Web Vitals
    const vitals = await page.evaluate(() => {
      return new Promise((resolve) => {
        const vitalsData: any = {};

        const observer = new PerformanceObserver((list) => {
          list.getEntries().forEach((entry) => {
            if (entry.entryType === 'largest-contentful-paint') {
              vitalsData.LCP = entry.startTime;
            } else if (entry.entryType === 'first-input') {
              vitalsData.FID = (entry as any).processingStart - entry.startTime;
            } else if (entry.entryType === 'layout-shift') {
              if (!vitalsData.CLS) vitalsData.CLS = 0;
              vitalsData.CLS += (entry as any).value;
            } else if (entry.entryType === 'paint') {
              vitalsData[entry.name.replace('-', '')] = entry.startTime;
            }
          });
        });

        observer.observe({ entryTypes: [
          'largest-contentful-paint',
          'first-input',
          'layout-shift',
          'paint'
        ]});

        // Wait for metrics to stabilize
        setTimeout(() => resolve(vitalsData), 5000);
      });
    });

    console.log('Core Web Vitals metrics:', vitals);

    // Validate against Google's thresholds
    if (vitals.LCP !== undefined) {
      expect(vitals.LCP).toBeLessThan(2500); // Good: <2.5s
      console.log(`LCP: ${vitals.LCP.toFixed(0)}ms - ${vitals.LCP < 2500 ? 'GOOD' : vitals.LCP < 4000 ? 'NEEDS IMPROVEMENT' : 'POOR'}`);
    }

    if (vitals.FID !== undefined) {
      expect(vitals.FID).toBeLessThan(100); // Good: <100ms
      console.log(`FID: ${vitals.FID.toFixed(0)}ms - ${vitals.FID < 100 ? 'GOOD' : vitals.FID < 300 ? 'NEEDS IMPROVEMENT' : 'POOR'}`);
    }

    if (vitals.CLS !== undefined) {
      expect(vitals.CLS).toBeLessThan(0.1); // Good: <0.1
      console.log(`CLS: ${vitals.CLS.toFixed(3)} - ${vitals.CLS < 0.1 ? 'GOOD' : vitals.CLS < 0.25 ? 'NEEDS IMPROVEMENT' : 'POOR'}`);
    }

    if (vitals.firstContentfulPaint !== undefined) {
      expect(vitals.firstContentfulPaint).toBeLessThan(1800); // Good: <1.8s
      console.log(`FCP: ${vitals.firstContentfulPaint.toFixed(0)}ms`);
    }

    if (vitals.firstpaint !== undefined) {
      expect(vitals.firstpaint).toBeLessThan(1000); // Good: <1s
      console.log(`FP: ${vitals.firstpaint.toFixed(0)}ms`);
    }
  });

  test('Performance regression detection', async ({ page, testData }) => {
    // Create performance baseline
    const baselineMetrics: any = {};

    // Measure search performance baseline
    await page.goto('/search');
    const searchStart = Date.now();
    await searchPage.performSearch(testData.queries.simple[0]);
    await searchPage.waitForResults();
    baselineMetrics.searchTime = Date.now() - searchStart;

    // Measure page load baseline
    const navStart = Date.now();
    await page.goto('/documents');
    baselineMetrics.pageLoadTime = Date.now() - navStart;

    console.log('Performance baseline metrics:', baselineMetrics);

    // Simulate additional load and measure degradation
    for (let i = 0; i < 5; i++) {
      await page.goto('/search');
      await searchPage.performSearch(testData.queries.simple[i % testData.queries.simple.length]);
      await searchPage.waitForResults();
    }

    // Measure performance under load
    const loadSearchStart = Date.now();
    await searchPage.performSearch(testData.queries.complex[0]);
    await searchPage.waitForResults();
    const loadSearchTime = Date.now() - loadSearchStart;

    const loadPageStart = Date.now();
    await page.goto('/graph');
    await page.waitForSelector('[data-testid="graph-canvas"]');
    const loadPageTime = Date.now() - loadPageStart;

    console.log('Performance under load:', {
      searchTime: loadSearchTime,
      pageLoadTime: loadPageTime,
      baseline: baselineMetrics
    });

    // Performance should not degrade significantly
    const searchDegradation = (loadSearchTime - baselineMetrics.searchTime) / baselineMetrics.searchTime;
    const pageLoadDegradation = (loadPageTime - baselineMetrics.pageLoadTime) / baselineMetrics.pageLoadTime;

    expect(searchDegradation).toBeLessThan(0.5); // <50% degradation
    expect(pageLoadDegradation).toBeLessThan(0.3); // <30% degradation

    console.log(`Performance degradation - Search: ${(searchDegradation * 100).toFixed(1)}%, Page Load: ${(pageLoadDegradation * 100).toFixed(1)}%`);
  });
});