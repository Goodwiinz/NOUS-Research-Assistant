import { test, expect } from '../../fixtures/enhanced-test-data.fixture';
import { DocumentProcessor } from '../../utils/document-processor';

/**
 * Comprehensive Performance Testing Under Load
 *
 * Test Coverage:
 * - High-volume concurrent file uploads
 * - WebSocket message throughput under stress
 * - Memory usage and garbage collection
 * - CPU usage and processing efficiency
 * - Network bandwidth utilization
 * - UI responsiveness under load
 * - Database query performance
 * - Resource cleanup and memory leaks
 */

test.describe('Performance Testing Under Load', () => {
  let documentProcessor: DocumentProcessor;

  test.beforeEach(async ({ page, webSocketUtils }) => {
    documentProcessor = new DocumentProcessor(page, webSocketUtils);
    await page.goto('/documents');
    await page.waitForLoadState('networkidle');
  });

  test.afterEach(async () => {
    await documentProcessor.cleanup();
  });

  test.describe('Load Testing Scenarios', () => {
    test('LOAD-1: High-volume concurrent uploads', async ({
      page,
      webSocketUtils,
      performanceMonitor,
      testDataManager
    }) => {
      test.slow();

      // Generate test files for load testing
      const fileCount = 20;
      const testFiles = await Promise.all(
        Array(fileCount).fill(null).map((_, i) =>
          testDataManager.createFile(`load-test-${i}.pdf`, 1024 * 1024) // 1MB each
        )
      );

      // Start comprehensive performance monitoring
      const performanceId = await performanceMonitor.startMeasurement('high-volume-uploads');

      // Monitor initial system state
      const initialMemory = await performanceMonitor.getMemoryUsage();
      const initialNetworkRequests = webSocketUtils.getMessageLog().length;

      // Upload files in batches to simulate realistic load
      const batchSize = 5;
      const uploadResults = [];
      const uploadStartTime = Date.now();

      for (let i = 0; i < testFiles.length; i += batchSize) {
        const batch = testFiles.slice(i, i + batchSize);
        const batchStartTime = Date.now();

        // Upload batch concurrently
        const batchPromises = batch.map(async (file, index) => {
          try {
            const documentId = await documentProcessor.uploadDocument(file, {
              waitForProcessing: false
            });

            return {
              fileIndex: i + index,
              documentId,
              success: true,
              uploadTime: Date.now() - batchStartTime
            };
          } catch (error) {
            return {
              fileIndex: i + index,
              documentId: null,
              success: false,
              error: error.message,
              uploadTime: Date.now() - batchStartTime
            };
          }
        });

        const batchResults = await Promise.all(batchPromises);
        uploadResults.push(...batchResults);

        // Monitor system state between batches
        const currentMemory = await performanceMonitor.getMemoryUsage();
        const memoryIncrease = currentMemory && initialMemory
          ? currentMemory.usedJSHeapSize - initialMemory.usedJSHeapSize
          : 0;

        console.log(`Batch ${Math.floor(i / batchSize) + 1}/${Math.ceil(fileCount / batchSize)} completed. Memory increase: ${memoryIncrease} bytes`);

        // Small delay between batches to simulate realistic usage
        await page.waitForTimeout(1000);
      }

      const totalUploadTime = Date.now() - uploadStartTime;

      // Stop performance monitoring
      const performanceMetrics = await performanceMonitor.stopMeasurement(performanceId);

      // Monitor final system state
      const finalMemory = await performanceMonitor.getMemoryUsage();
      const finalNetworkRequests = webSocketUtils.getMessageLog().length;

      // Calculate performance metrics
      const successfulUploads = uploadResults.filter(r => r.success).length;
      const failedUploads = uploadResults.filter(r => !r.success).length;
      const averageUploadTime = uploadResults
        .filter(r => r.success)
        .reduce((sum, r) => sum + r.uploadTime, 0) / successfulUploads;
      const uploadsPerSecond = (successfulUploads / totalUploadTime) * 1000;
      const memoryLeak = finalMemory && initialMemory
        ? finalMemory.usedJSHeapSize - initialMemory.usedJSHeapSize
        : 0;
      const networkMessagesProcessed = finalNetworkRequests - initialNetworkRequests;

      // Performance assertions
      expect(successfulUploads).toBeGreaterThan(fileCount * 0.9); // At least 90% success rate
      expect(averageUploadTime).toBeLessThan(30000); // Average upload < 30 seconds
      expect(uploadsPerSecond).toBeGreaterThan(0.1); // At least 0.1 uploads per second
      expect(memoryLeak).toBeLessThan(100 * 1024 * 1024); // Memory leak < 100MB
      expect(networkMessagesProcessed).toBeGreaterThan(successfulUploads * 2); // At least 2 messages per upload

      // WebSocket performance under load
      const wsMetrics = await webSocketUtils.getPerformanceMetrics();
      expect(wsMetrics.averageLatency).toBeLessThan(500); // Average latency < 500ms under load
      expect(wsMetrics.messagesReceived).toBeGreaterThan(networkMessagesProcessed * 0.8); // At least 80% of messages received

      // Log comprehensive performance results
      console.log('\n=== HIGH-VOLUME UPLOAD PERFORMANCE ===');
      console.log(`Total Files: ${fileCount}`);
      console.log(`Successful Uploads: ${successfulUploads} (${((successfulUploads / fileCount) * 100).toFixed(1)}%)`);
      console.log(`Failed Uploads: ${failedUploads}`);
      console.log(`Total Upload Time: ${totalUploadTime}ms`);
      console.log(`Average Upload Time: ${averageUploadTime.toFixed(0)}ms`);
      console.log(`Uploads per Second: ${uploadsPerSecond.toFixed(2)}`);
      console.log(`Memory Usage Increase: ${(memoryLeak / 1024 / 1024).toFixed(1)}MB`);
      console.log(`Network Messages: ${networkMessagesProcessed}`);
      console.log(`WebSocket Latency: ${wsMetrics.averageLatency.toFixed(0)}ms`);
      console.log(`WebSocket Messages: ${wsMetrics.messagesReceived}`);

      // Take screenshot of final state
      await page.screenshot({
        path: 'test-results/high-volume-load-test.png',
        fullPage: true
      });
    });

    test('LOAD-2: WebSocket message throughput stress test', async ({
      page,
      webSocketUtils,
      performanceMonitor
    }) => {
      test.slow();

      // Clear existing WebSocket messages
      webSocketUtils.clearMessageLog();

      // Start performance monitoring
      const performanceId = await performanceMonitor.startMeasurement('websocket-throughput');

      // Test parameters
      const messageCount = 1000;
      const concurrentConnections = 5;
      const messageTypes = ['document_status_update', 'processing_stage_update', 'error_message', 'heartbeat', 'progress_update'];

      // Monitor initial state
      const initialMemory = await performanceMonitor.getMemoryUsage();
      const startTime = Date.now();

      // Simulate high-frequency WebSocket messages
      const messagePromises = [];

      for (let connection = 0; connection < concurrentConnections; connection++) {
        for (let i = 0; i < messageCount / concurrentConnections; i++) {
          const messageType = messageTypes[i % messageTypes.length];
          const message = {
            type: messageType,
            payload: {
              documentId: `stress-test-${connection}-${i}`,
              status: 'processing',
              progress: Math.floor(Math.random() * 100),
              timestamp: Date.now()
            },
            timestamp: Date.now()
          };

          // Add delay to simulate realistic message timing
          const delay = i * 10; // 10ms between messages
          messagePromises.push(
            new Promise(resolve => {
              setTimeout(() => {
                webSocketUtils.mockWebSocketMessage(message);
                resolve(true);
              }, delay);
            })
          );
        }
      }

      // Wait for all messages to be processed
      await Promise.all(messagePromises);
      const messageSendTime = Date.now() - startTime;

      // Wait for message processing
      await page.waitForTimeout(2000);

      // Stop performance monitoring
      const performanceMetrics = await performanceMonitor.stopMeasurement(performanceId);

      // Monitor final state
      const finalMemory = await performanceMonitor.getMemoryUsage();
      const wsMetrics = await webSocketUtils.getPerformanceMetrics();
      const messageLog = webSocketUtils.getMessageLog();

      // Calculate throughput metrics
      const messagesPerSecond = (messageCount / messageSendTime) * 1000;
      const memoryIncrease = finalMemory && initialMemory
        ? finalMemory.usedJSHeapSize - initialMemory.usedJSHeapSize
        : 0;
      const processedMessages = messageLog.length;
      const processingRate = (processedMessages / messageSendTime) * 1000;

      // Performance assertions
      expect(messagesPerSecond).toBeGreaterThan(50); // At least 50 messages/second
      expect(processedMessages).toBeGreaterThan(messageCount * 0.9); // At least 90% messages processed
      expect(wsMetrics.averageLatency).toBeLessThan(100); // Average latency < 100ms
      expect(memoryIncrease).toBeLessThan(50 * 1024 * 1024); // Memory increase < 50MB

      // Verify message ordering and integrity
      const messagesByType = new Map();
      messageLog.forEach(msg => {
        const count = messagesByType.get(msg.type) || 0;
        messagesByType.set(msg.type, count + 1);
      });

      // Verify all message types were processed
      messageTypes.forEach(type => {
        expect(messagesByType.get(type)).toBeGreaterThan(0);
      });

      // Log throughput results
      console.log('\n=== WEBSOCKET THROUGHPUT PERFORMANCE ===');
      console.log(`Total Messages Sent: ${messageCount}`);
      console.log(`Messages Processed: ${processedMessages}`);
      console.log(`Processing Rate: ${processingRate.toFixed(2)} messages/second`);
      console.log(`Send Time: ${messageSendTime}ms`);
      console.log(`Average Latency: ${wsMetrics.averageLatency.toFixed(0)}ms`);
      console.log(`Memory Increase: ${(memoryIncrease / 1024 / 1024).toFixed(1)}MB`);

      messagesByType.forEach((count, type) => {
        console.log(`${type}: ${count} messages`);
      });
    });

    test('LOAD-3: UI responsiveness under heavy load', async ({
      page,
      performanceMonitor,
      testDataManager
    }) => {
      test.slow();

      // Start UI performance monitoring
      const performanceId = await performanceMonitor.startMeasurement('ui-responsiveness');

      // Upload files to create UI load
      const fileCount = 50;
      const testFiles = await Promise.all(
        Array(fileCount).fill(null).map((_, i) =>
          testDataManager.createFile(`ui-test-${i}.pdf`, 512 * 1024) // 512KB each
        )
      );

      // Monitor UI responsiveness metrics
      const uiMetrics = {
        renderTimes: [],
        clickResponseTimes: [],
        scrollPerformance: [],
        memorySnapshots: []
      };

      // Start file uploads rapidly
      const uploadPromises = testFiles.map(async (file, index) => {
        const startTime = Date.now();

        // Monitor render time for each file item
        page.waitForSelector(`[data-file-name="${file.name}"]`, { timeout: 30000 })
          .then(() => {
            const renderTime = Date.now() - startTime;
            uiMetrics.renderTimes.push(renderTime);
          });

        await documentProcessor.uploadDocument(file, { waitForProcessing: false });
      });

      // Simulate user interactions during uploads
      const interactionPromises = [];

      for (let i = 0; i < 10; i++) {
        const interactionDelay = i * 1000; // Interact every second

        interactionPromises.push(
          new Promise(resolve => {
            setTimeout(async () => {
              // Test click responsiveness
              const clickStartTime = Date.now();
              try {
                await page.click('[data-testid="upload-area"]');
                const clickTime = Date.now() - clickStartTime;
                uiMetrics.clickResponseTimes.push(clickTime);
              } catch (error) {
                // Element might not be clickable due to loading
                uiMetrics.clickResponseTimes.push(-1);
              }

              // Test scroll performance
              const scrollStartTime = Date.now();
              try {
                await page.evaluate(() => {
                  window.scrollBy(0, 100);
                });
                const scrollTime = Date.now() - scrollStartTime;
                uiMetrics.scrollPerformance.push(scrollTime);
              } catch (error) {
                uiMetrics.scrollPerformance.push(-1);
              }

              // Capture memory snapshot
              const memory = await performanceMonitor.getMemoryUsage();
              if (memory) {
                uiMetrics.memorySnapshots.push({
                  timestamp: Date.now(),
                  usedJSHeapSize: memory.usedJSHeapSize
                });
              }

              resolve(true);
            }, interactionDelay);
          })
        );
      }

      // Wait for all uploads and interactions
      await Promise.all([...uploadPromises, ...interactionPromises]);

      // Stop performance monitoring
      const performanceMetrics = await performanceMonitor.stopMeasurement(performanceId);

      // Calculate UI performance metrics
      const averageRenderTime = uiMetrics.renderTimes.length > 0
        ? uiMetrics.renderTimes.reduce((sum, time) => sum + time, 0) / uiMetrics.renderTimes.length
        : 0;

      const averageClickTime = uiMetrics.clickResponseTimes.filter(t => t > 0).length > 0
        ? uiMetrics.clickResponseTimes.filter(t => t > 0).reduce((sum, time) => sum + time, 0) /
          uiMetrics.clickResponseTimes.filter(t => t > 0).length
        : 0;

      const averageScrollTime = uiMetrics.scrollPerformance.filter(t => t > 0).length > 0
        ? uiMetrics.scrollPerformance.filter(t => t > 0).reduce((sum, time) => sum + time, 0) /
          uiMetrics.scrollPerformance.filter(t => t > 0).length
        : 0;

      const memoryGrowth = uiMetrics.memorySnapshots.length > 1
        ? uiMetrics.memorySnapshots[uiMetrics.memorySnapshots.length - 1].usedJSHeapSize -
          uiMetrics.memorySnapshots[0].usedJSHeapSize
        : 0;

      // UI performance assertions
      expect(averageRenderTime).toBeLessThan(2000); // Average render time < 2 seconds
      expect(averageClickTime).toBeLessThan(500); // Click response < 500ms
      expect(averageScrollTime).toBeLessThan(100); // Scroll performance < 100ms
      expect(memoryGrowth).toBeLessThan(100 * 1024 * 1024); // Memory growth < 100MB

      // Verify UI remains interactive
      await expect(page.locator('[data-testid="upload-area"]')).toBeVisible();
      await expect(page.locator('[data-testid="file-list"]')).toBeVisible();

      // Log UI performance results
      console.log('\n=== UI RESPONSIVENESS PERFORMANCE ===');
      console.log(`Files Uploaded: ${fileCount}`);
      console.log(`Average Render Time: ${averageRenderTime.toFixed(0)}ms`);
      console.log(`Average Click Response: ${averageClickTime.toFixed(0)}ms`);
      console.log(`Average Scroll Time: ${averageScrollTime.toFixed(0)}ms`);
      console.log(`Memory Growth: ${(memoryGrowth / 1024 / 1024).toFixed(1)}MB`);
      console.log(`UI Interactions Tested: ${uiMetrics.clickResponseTimes.length}`);
    });

    test('LOAD-4: Memory leak detection under extended load', async ({
      page,
      webSocketUtils,
      performanceMonitor,
      testDataManager
    }) => {
      test.slow();

      // Extended load test to detect memory leaks
      const cycles = 5;
      const filesPerCycle = 20;
      const memorySnapshots = [];

      for (let cycle = 0; cycle < cycles; cycle++) {
        console.log(`Memory leak test cycle ${cycle + 1}/${cycles}`);

        // Create test files for this cycle
        const cycleFiles = await Promise.all(
          Array(filesPerCycle).fill(null).map((_, i) =>
            testDataManager.createFile(`cycle-${cycle}-file-${i}.pdf`, 256 * 1024) // 256KB each
          )
        );

        // Start memory monitoring for this cycle
        const cycleMemoryStart = await performanceMonitor.getMemoryUsage();
        const cycleStartTime = Date.now();

        // Upload files
        const uploadPromises = cycleFiles.map(file =>
          documentProcessor.uploadDocument(file, { waitForProcessing: false })
        );

        await Promise.all(uploadPromises);

        // Simulate WebSocket messages
        for (let i = 0; i < 50; i++) {
          await webSocketUtils.mockWebSocketMessage({
            type: 'test_message',
            payload: { cycle, index: i, timestamp: Date.now() },
            timestamp: Date.now()
          });
        }

        // Wait for processing
        await page.waitForTimeout(5000);

        // Capture memory snapshot
        const cycleMemoryEnd = await performanceMonitor.getMemoryUsage();
        const cycleDuration = Date.now() - cycleStartTime;

        memorySnapshots.push({
          cycle: cycle + 1,
          memoryStart: cycleMemoryStart?.usedJSHeapSize || 0,
          memoryEnd: cycleMemoryEnd?.usedJSHeapSize || 0,
          memoryIncrease: cycleMemoryEnd && cycleMemoryStart
            ? cycleMemoryEnd.usedJSHeapSize - cycleMemoryStart.usedJSHeapSize
            : 0,
          duration: cycleDuration
        });

        // Force garbage collection if available
        await page.evaluate(() => {
          if ((window as any).gc) {
            (window as any).gc();
          }
        });

        await page.waitForTimeout(2000); // Allow GC to complete

        // Clear processed data for next cycle
        await page.evaluate(() => {
          // Clear any temporary data
          if ((window as any).tempData) {
            (window as any).tempData = [];
          }
        });
      }

      // Analyze memory growth patterns
      const memoryGrowthRates = [];
      for (let i = 1; i < memorySnapshots.length; i++) {
        const current = memorySnapshots[i];
        const previous = memorySnapshots[i - 1];
        const growthRate = current.memoryIncrease - previous.memoryIncrease;
        memoryGrowthRates.push(growthRate);
      }

      const averageMemoryGrowth = memoryGrowthRates.reduce((sum, rate) => sum + rate, 0) / memoryGrowthRates.length;
      const maxMemoryGrowth = Math.max(...memoryGrowthRates);
      const totalMemoryIncrease = memorySnapshots[memorySnapshots.length - 1].memoryIncrease;

      // Memory leak assertions
      expect(averageMemoryGrowth).toBeLessThan(10 * 1024 * 1024); // Average growth < 10MB per cycle
      expect(maxMemoryGrowth).toBeLessThan(50 * 1024 * 1024); // Max growth < 50MB per cycle
      expect(totalMemoryIncrease).toBeLessThan(100 * 1024 * 1024); // Total growth < 100MB

      // Verify system remains responsive after extended load
      await expect(page.locator('[data-testid="upload-area"]')).toBeVisible();
      await expect(page.locator('[data-testid="file-list"]')).toBeVisible();

      // Log memory analysis results
      console.log('\n=== MEMORY LEAK ANALYSIS ===');
      console.log(`Test Cycles: ${cycles}`);
      console.log(`Files Per Cycle: ${filesPerCycle}`);
      console.log(`Total Files Processed: ${cycles * filesPerCycle}`);
      console.log(`Average Memory Growth per Cycle: ${(averageMemoryGrowth / 1024 / 1024).toFixed(1)}MB`);
      console.log(`Maximum Memory Growth per Cycle: ${(maxMemoryGrowth / 1024 / 1024).toFixed(1)}MB`);
      console.log(`Total Memory Increase: ${(totalMemoryIncrease / 1024 / 1024).toFixed(1)}MB`);

      memorySnapshots.forEach(snapshot => {
        console.log(`Cycle ${snapshot.cycle}: ${(snapshot.memoryIncrease / 1024 / 1024).toFixed(1)}MB increase`);
      });
    });

    test('LOAD-5: Database query performance under load', async ({
      page,
      performanceMonitor,
      testDataManager
    }) => {
      test.slow();

      // This test simulates database performance by testing search and filtering operations
      const performanceId = await performanceMonitor.startMeasurement('database-performance');

      // Upload many documents to create database load
      const documentCount = 100;
      const testFiles = await Promise.all(
        Array(documentCount).fill(null).map((_, i) =>
          testDataManager.createFile(`db-test-${i}.pdf`, 128 * 1024) // 128KB each
        )
      );

      console.log('Uploading documents for database performance test...');
      const uploadPromises = testFiles.map(file =>
        documentProcessor.uploadDocument(file, { waitForProcessing: false })
      );

      await Promise.all(uploadPromises);
      console.log('Documents uploaded successfully');

      // Wait for documents to be indexed
      await page.waitForTimeout(10000);

      // Test search performance under various loads
      const searchQueries = [
        'test document',
        'performance',
        'database',
        'query',
        'search term',
        'sample content',
        'test file',
        'document analysis',
        'performance testing',
        'e2e testing'
      ];

      const searchMetrics = [];

      for (const query of searchQueries) {
        const searchStartTime = Date.now();

        // Perform search
        await page.fill('[data-testid="search-input"]', query);
        await page.click('[data-testid="search-button"]');

        // Wait for search results
        await page.waitForSelector('[data-testid="search-results"]', { timeout: 15000 });

        const searchTime = Date.now() - searchStartTime;
        const resultCount = await page.locator('[data-testid="search-result-item"]').count();

        searchMetrics.push({
          query,
          searchTime,
          resultCount,
          resultsPerMs: resultCount / searchTime
        });

        // Clear search for next query
        await page.fill('[data-testid="search-input"]', '');
        await page.waitForTimeout(500);
      }

      // Test filtering performance
      const filterMetrics = [];

      const filterTests = [
        { type: 'pdf', expectedCount: documentCount },
        { type: 'image', expectedCount: 0 },
        { type: 'text', expectedCount: 0 },
        { type: 'processed', expectedCount: documentCount },
        { type: 'failed', expectedCount: 0 }
      ];

      for (const filterTest of filterTests) {
        const filterStartTime = Date.now();

        await page.click('[data-testid="filter-button"]');
        await page.click(`[data-testid="filter-${filterTest.type}"]`);

        await page.waitForTimeout(2000); // Wait for filter to apply

        const filterTime = Date.now() - filterStartTime;
        const filteredCount = await page.locator('[data-testid="file-item"]').count();

        filterMetrics.push({
          filter: filterTest.type,
          filterTime,
          filteredCount,
          expectedCount: filterTest.expectedCount
        });

        // Clear filter
        await page.click('[data-testid="clear-filters"]');
        await page.waitForTimeout(500);
      }

      // Test sorting performance
      const sortMetrics = [];

      const sortOptions = ['name', 'date', 'size', 'type'];

      for (const sortOption of sortOptions) {
        const sortStartTime = Date.now();

        await page.click('[data-testid="sort-button"]');
        await page.click(`[data-testid="sort-by-${sortOption}"]`);

        await page.waitForTimeout(1000); // Wait for sort to apply

        const sortTime = Date.now() - sortStartTime;

        sortMetrics.push({
          sortBy: sortOption,
          sortTime
        });
      }

      // Stop performance monitoring
      const performanceMetrics = await performanceMonitor.stopMeasurement(performanceId);

      // Calculate database performance metrics
      const averageSearchTime = searchMetrics.reduce((sum, m) => sum + m.searchTime, 0) / searchMetrics.length;
      const averageFilterTime = filterMetrics.reduce((sum, m) => sum + m.filterTime, 0) / filterMetrics.length;
      const averageSortTime = sortMetrics.reduce((sum, m) => sum + m.sortTime, 0) / sortMetrics.length;

      const maxSearchTime = Math.max(...searchMetrics.map(m => m.searchTime));
      const maxFilterTime = Math.max(...filterMetrics.map(m => m.filterTime));
      const maxSortTime = Math.max(...sortMetrics.map(m => m.sortTime));

      // Performance assertions
      expect(averageSearchTime).toBeLessThan(2000); // Average search < 2 seconds
      expect(maxSearchTime).toBeLessThan(5000); // Max search < 5 seconds
      expect(averageFilterTime).toBeLessThan(1000); // Average filter < 1 second
      expect(maxFilterTime).toBeLessThan(3000); // Max filter < 3 seconds
      expect(averageSortTime).toBeLessThan(500); // Average sort < 500ms
      expect(maxSortTime).toBeLessThan(2000); // Max sort < 2 seconds

      // Log database performance results
      console.log('\n=== DATABASE PERFORMANCE RESULTS ===');
      console.log(`Documents Processed: ${documentCount}`);
      console.log(`Search Queries Tested: ${searchQueries.length}`);
      console.log(`Average Search Time: ${averageSearchTime.toFixed(0)}ms`);
      console.log(`Max Search Time: ${maxSearchTime.toFixed(0)}ms`);
      console.log(`Average Filter Time: ${averageFilterTime.toFixed(0)}ms`);
      console.log(`Max Filter Time: ${maxFilterTime.toFixed(0)}ms`);
      console.log(`Average Sort Time: ${averageSortTime.toFixed(0)}ms`);
      console.log(`Max Sort Time: ${maxSortTime.toFixed(0)}ms`);

      searchMetrics.forEach(metric => {
        console.log(`Search "${metric.query}": ${metric.searchTime}ms, ${metric.resultCount} results`);
      });
    });
  });

  test.describe('Resource Management', () => {
    test('RES-1: Resource cleanup and garbage collection', async ({
      page,
      performanceMonitor,
      testDataManager
    }) => {
      test.slow();

      // Test resource cleanup by uploading and then removing files
      const fileCount = 30;
      const testFiles = await Promise.all(
        Array(fileCount).fill(null).map((_, i) =>
          testDataManager.createFile(`cleanup-test-${i}.pdf`, 512 * 1024) // 512KB each
        )
      );

      const resourceMetrics = [];

      // Phase 1: Upload files and monitor resource usage
      console.log('Phase 1: Uploading files...');
      const uploadStartTime = Date.now();
      const initialMemory = await performanceMonitor.getMemoryUsage();

      const documentIds = [];
      for (const file of testFiles) {
        try {
          const documentId = await documentProcessor.uploadDocument(file, {
            waitForProcessing: false
          });
          documentIds.push(documentId);
        } catch (error) {
          console.warn(`Failed to upload ${file.name}:`, error.message);
        }
      }

      const uploadEndTime = Date.now();
      const afterUploadMemory = await performanceMonitor.getMemoryUsage();

      resourceMetrics.push({
        phase: 'upload',
        startTime: uploadStartTime,
        endTime: uploadEndTime,
        memoryStart: initialMemory?.usedJSHeapSize || 0,
        memoryEnd: afterUploadMemory?.usedJSHeapSize || 0,
        memoryIncrease: afterUploadMemory && initialMemory
          ? afterUploadMemory.usedJSHeapSize - initialMemory.usedJSHeapSize
          : 0
      });

      // Phase 2: Wait for processing and monitor memory
      console.log('Phase 2: Waiting for processing...');
      await page.waitForTimeout(10000);
      const afterProcessingMemory = await performanceMonitor.getMemoryUsage();

      resourceMetrics.push({
        phase: 'processing',
        startTime: uploadEndTime,
        endTime: Date.now(),
        memoryStart: afterUploadMemory?.usedJSHeapSize || 0,
        memoryEnd: afterProcessingMemory?.usedJSHeapSize || 0,
        memoryIncrease: afterProcessingMemory && afterUploadMemory
          ? afterProcessingMemory.usedJSHeapSize - afterUploadMemory.usedJSHeapSize
          : 0
      });

      // Phase 3: Remove files and monitor cleanup
      console.log('Phase 3: Removing files...');
      const cleanupStartTime = Date.now();

      // Remove files through UI
      for (let i = 0; i < Math.min(documentIds.length, 10); i++) {
        try {
          const documentId = documentIds[i];
          const fileItem = page.locator(`[data-document-id="${documentId}"]`);

          if (await fileItem.isVisible()) {
            await fileItem.click();
            await page.click('[data-testid="delete-document"]');
            await page.click('[data-testid="confirm-delete"]');
            await page.waitForTimeout(1000);
          }
        } catch (error) {
          console.warn(`Failed to remove file ${i}:`, error.message);
        }
      }

      const cleanupEndTime = Date.now();
      const afterCleanupMemory = await performanceMonitor.getMemoryUsage();

      resourceMetrics.push({
        phase: 'cleanup',
        startTime: cleanupStartTime,
        endTime: cleanupEndTime,
        memoryStart: afterProcessingMemory?.usedJSHeapSize || 0,
        memoryEnd: afterCleanupMemory?.usedJSHeapSize || 0,
        memoryIncrease: afterCleanupMemory && afterProcessingMemory
          ? afterCleanupMemory.usedJSHeapSize - afterProcessingMemory.usedJSHeapSize
          : 0
      });

      // Phase 4: Force garbage collection
      console.log('Phase 4: Forcing garbage collection...');
      await page.evaluate(() => {
        if ((window as any).gc) {
          (window as any).gc();
        }
      });

      await page.waitForTimeout(3000);
      const afterGCMemory = await performanceMonitor.getMemoryUsage();

      resourceMetrics.push({
        phase: 'garbage_collection',
        startTime: cleanupEndTime,
        endTime: Date.now(),
        memoryStart: afterCleanupMemory?.usedJSHeapSize || 0,
        memoryEnd: afterGCMemory?.usedJSHeapSize || 0,
        memoryIncrease: afterGCMemory && afterCleanupMemory
          ? afterGCMemory.usedJSHeapSize - afterCleanupMemory.usedJSHeapSize
          : 0
      });

      // Analyze resource cleanup efficiency
      const uploadMemoryIncrease = resourceMetrics[0].memoryIncrease;
      const cleanupMemoryDecrease = resourceMetrics[2].memoryIncrease; // This should be negative
      const gcMemoryDecrease = resourceMetrics[3].memoryIncrease; // This should be negative
      const totalMemoryRecovery = Math.abs(cleanupMemoryDecrease) + Math.abs(gcMemoryDecrease);
      const cleanupEfficiency = uploadMemoryIncrease > 0 ? (totalMemoryRecovery / uploadMemoryIncrease) * 100 : 0;

      // Resource cleanup assertions
      expect(cleanupEfficiency).toBeGreaterThan(50); // At least 50% of memory should be recovered
      expect(Math.abs(gcMemoryDecrease)).toBeGreaterThan(0); // GC should free some memory
      expect(afterGCMemory?.usedJSHeapSize).toBeLessThan(afterUploadMemory?.usedJSHeapSize || Infinity * 1.5);

      // Log resource cleanup results
      console.log('\n=== RESOURCE CLEANUP ANALYSIS ===');
      console.log(`Files Processed: ${fileCount}`);
      console.log(`Files Removed: ${Math.min(documentIds.length, 10)}`);
      console.log(`Upload Memory Increase: ${(uploadMemoryIncrease / 1024 / 1024).toFixed(1)}MB`);
      console.log(`Cleanup Memory Recovery: ${Math.abs(cleanupMemoryDecrease / 1024 / 1024).toFixed(1)}MB`);
      console.log(`GC Memory Recovery: ${Math.abs(gcMemoryDecrease / 1024 / 1024).toFixed(1)}MB`);
      console.log(`Cleanup Efficiency: ${cleanupEfficiency.toFixed(1)}%`);

      resourceMetrics.forEach(metric => {
        console.log(`${metric.phase}: ${(metric.memoryIncrease / 1024 / 1024).toFixed(1)}MB change`);
      });
    });
  });
});