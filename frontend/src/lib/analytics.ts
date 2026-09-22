import { AnalyticsConfig, AnalyticsEvent, PageView } from '@/types/analytics';

// Internal session tracking structure (different from exported UserSession)
interface InternalSession {
  id: string;
  userId?: string;
  startTime: number;
  endTime: number;
  duration: number;
  pageViews: number;
  events: number;
  bounce: boolean;
  entryPage: string;
  exitPage: string;
}

class AnalyticsService {
  private config: AnalyticsConfig;
  private sessionId: string;
  private eventQueue: AnalyticsEvent[] = [];
  private isInitialized = false;

  constructor(config: AnalyticsConfig) {
    this.config = config;
    this.sessionId = this.generateSessionId();
    this.initialize();
  }

  private generateSessionId(): string {
    return `session_${Date.now()}_${Math.random().toString(36).substring(2, 9)}`;
  }

  private async initialize() {
    if (this.config.enableDebug) {
      console.log('[Analytics] Initializing with config:', this.config);
    }

    // Initialize based on provider
    switch (this.config.provider) {
      case 'google-analytics':
        await this.initializeGoogleAnalytics();
        break;
      case 'plausible':
        await this.initializePlausible();
        break;
      case 'custom':
        await this.initializeCustom();
        break;
      case 'none':
        console.log('[Analytics] Analytics disabled');
        return;
    }

    // Track initial page view
    if (this.config.trackPageViews) {
      this.trackPageView(window.location.pathname, document.title);
    }

    this.isInitialized = true;
    this.setupEventListeners();
  }

  private async initializeGoogleAnalytics() {
    if (!this.config.measurementId) {
      console.warn('[Analytics] Google Analytics measurement ID not provided');
      return;
    }

    // Load gtag script
    const script = document.createElement('script');
    script.async = true;
    script.src = `https://www.googletagmanager.com/gtag/js?id=${this.config.measurementId}`;
    document.head.appendChild(script);

    // Initialize gtag
    window.dataLayer = window.dataLayer || [];
    window.gtag = function gtag() {
      // eslint-disable-next-line prefer-rest-params
      window.dataLayer?.push(arguments);
    };
    window.gtag('js', new Date());
    window.gtag('config', this.config.measurementId, {
      send_page_view: false, // We'll handle page views manually
    });
  }

  private async initializePlausible() {
    if (!this.config.measurementId) {
      console.warn('[Analytics] Plausible domain not provided');
      return;
    }

    // Load Plausible script
    const script = document.createElement('script');
    script.async = true;
    script.defer = true;
    script.dataset.domain = this.config.measurementId;
    script.src = 'https://plausible.io/js/script.js';
    document.head.appendChild(script);
  }

  private async initializeCustom() {
    if (!this.config.customEndpoint) {
      console.warn('[Analytics] Custom endpoint not provided');
      return;
    }
    // Custom analytics will be sent via fetch in trackEvent method
  }

  private setupEventListeners() {
    // Track page changes for SPAs
    if (this.config.trackPageViews) {
      let lastPath = window.location.pathname;

      const observer = new MutationObserver(() => {
        if (window.location.pathname !== lastPath) {
          lastPath = window.location.pathname;
          this.trackPageView(window.location.pathname, document.title);
        }
      });

      observer.observe(document, { subtree: true, childList: true });
    }

    // Track session end
    window.addEventListener('beforeunload', () => {
      this.endSession();
    });

    // Track visibility changes (for accurate session duration)
    document.addEventListener('visibilitychange', () => {
      if (document.hidden) {
        this.trackEvent('engagement', 'page_hidden', 'Session paused');
      } else {
        this.trackEvent('engagement', 'page_visible', 'Session resumed');
      }
    });
  }

  public trackPageView(path: string, title: string, properties?: Record<string, any>) {
    if (!this.isInitialized || !this.config.trackPageViews) return;

    const pageView: PageView = {
      path,
      title,
      timestamp: new Date().toISOString(),
      userId: this.getUserId(),
      sessionId: this.sessionId,
      referrer: document.referrer,
      userAgent: navigator.userAgent,
      properties,
    };

    // Send to provider
    switch (this.config.provider) {
      case 'google-analytics':
        if (window.gtag) {
          window.gtag('config', this.config.measurementId, {
            page_path: path,
            page_title: title,
          });
        }
        break;
      case 'plausible':
        if (window.plausible) {
          window.plausible('pageview', {
            props: { path, title, ...properties },
          });
        }
        break;
      case 'custom':
        this.sendToCustomEndpoint({
          event: 'pageview',
          page: pageView,
        });
        break;
    }

    // Store locally for dashboard
    this.storePageView(pageView);

    if (this.config.enableDebug) {
      console.log('[Analytics] Page view tracked:', pageView);
    }
  }

  public trackEvent(
    category: string,
    action: string,
    label?: string,
    value?: number,
    properties?: Record<string, any>
  ) {
    if (!this.isInitialized || !this.config.trackEvents) return;

    const event: AnalyticsEvent = {
      event: `${category}_${action}`,
      category,
      action,
      label,
      value,
      properties,
      timestamp: new Date().toISOString(),
      userId: this.getUserId(),
      sessionId: this.sessionId,
      page: window.location.pathname,
      userAgent: navigator.userAgent,
      referrer: document.referrer,
    };

    // Send to provider
    switch (this.config.provider) {
      case 'google-analytics':
        if (window.gtag) {
          window.gtag('event', action, {
            event_category: category,
            event_label: label,
            value: value,
            custom_map: properties,
          });
        }
        break;
      case 'plausible':
        if (window.plausible) {
          window.plausible(action, {
            props: {
              category,
              label,
              value,
              ...properties,
            },
          });
        }
        break;
      case 'custom':
        this.sendToCustomEndpoint({ event });
        break;
    }

    // Store locally
    this.storeEvent(event);

    if (this.config.enableDebug) {
      console.log('[Analytics] Event tracked:', event);
    }
  }

  // Convenience methods for common events
  public trackDocumentUpload(fileType: string, fileSize: number, success: boolean) {
    this.trackEvent(
      'document',
      'upload',
      fileType,
      fileSize,
      { success, fileType, fileSize }
    );
  }

  public trackSearch(query: string, resultsCount: number, searchType: string) {
    this.trackEvent(
      'search',
      'performed',
      searchType,
      resultsCount,
      { queryLength: query.length, resultsCount, searchType }
    );
  }

  public trackChatMessage(messageType: 'user' | 'assistant', chatType: 'llm', tokens?: number) {
    this.trackEvent(
      'chat',
      'message',
      `${messageType}_${chatType}`,
      tokens,
      { messageType, chatType, tokens }
    );
  }

  public trackFeatureUsage(feature: string, action: string, properties?: Record<string, any>) {
    this.trackEvent('feature', feature, action, undefined, properties);
  }

  public trackError(error: Error, context?: string) {
    this.trackEvent(
      'error',
      error.name || 'unknown',
      context || 'unknown',
      undefined,
      {
        message: error.message,
        stack: error.stack,
        context,
      }
    );
  }

  public trackUserInteraction(element: string, action: string, properties?: Record<string, any>) {
    this.trackEvent('interaction', element, action, undefined, properties);
  }

  private async sendToCustomEndpoint(data: any) {
    if (!this.config.customEndpoint) return;

    try {
      await fetch(this.config.customEndpoint, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
        },
        body: JSON.stringify(data),
      });
    } catch (error) {
      console.error('[Analytics] Failed to send to custom endpoint:', error);
      // Queue for retry
      this.eventQueue.push(data);
    }
  }

  private storeEvent(event: AnalyticsEvent) {
    // Store in localStorage for dashboard
    try {
      const events = JSON.parse(localStorage.getItem('analytics_events') || '[]');
      events.push(event);

      // Keep only last 1000 events
      if (events.length > 1000) {
        events.splice(0, events.length - 1000);
      }

      localStorage.setItem('analytics_events', JSON.stringify(events));
    } catch (error) {
      console.error('[Analytics] Failed to store event:', error);
    }
  }

  private storePageView(pageView: PageView) {
    // Store in localStorage for dashboard
    try {
      const views = JSON.parse(localStorage.getItem('analytics_pageviews') || '[]');
      views.push(pageView);

      // Keep only last 1000 page views
      if (views.length > 1000) {
        views.splice(0, views.length - 1000);
      }

      localStorage.setItem('analytics_pageviews', JSON.stringify(views));
    } catch (error) {
      console.error('[Analytics] Failed to store page view:', error);
    }
  }

  private getUserId(): string | undefined {
    // Get user ID from auth context or localStorage
    try {
      const user = JSON.parse(localStorage.getItem('user') || '{}');
      return user.id || user.email;
    } catch {
      return undefined;
    }
  }

  private endSession() {
    const session: InternalSession = {
      id: this.sessionId,
      userId: this.getUserId(),
      startTime: parseInt(sessionStorage.getItem('session_start') || Date.now().toString()),
      endTime: Date.now(),
      duration: Date.now() - parseInt(sessionStorage.getItem('session_start') || Date.now().toString()),
      pageViews: parseInt(sessionStorage.getItem('session_pageviews') || '0'),
      events: parseInt(sessionStorage.getItem('session_events') || '0'),
      bounce: parseInt(sessionStorage.getItem('session_pageviews') || '0') <= 1,
      entryPage: sessionStorage.getItem('session_entry') || '/',
      exitPage: window.location.pathname,
    };

    // Store session data
    try {
      const sessions = JSON.parse(localStorage.getItem('analytics_sessions') || '[]');
      sessions.push(session);

      // Keep only last 100 sessions
      if (sessions.length > 100) {
        sessions.splice(0, sessions.length - 100);
      }

      localStorage.setItem('analytics_sessions', JSON.stringify(sessions));
    } catch (error) {
      console.error('[Analytics] Failed to store session:', error);
    }

    // Clear session storage
    sessionStorage.removeItem('session_start');
    sessionStorage.removeItem('session_pageviews');
    sessionStorage.removeItem('session_events');
    sessionStorage.removeItem('session_entry');
  }
}

// Create singleton instance
let analyticsInstance: AnalyticsService | null = null;

export function initializeAnalytics(config: AnalyticsConfig) {
  analyticsInstance = new AnalyticsService(config);
  return analyticsInstance;
}

export function getAnalytics() {
  if (!analyticsInstance) {
    throw new Error('Analytics not initialized. Call initializeAnalytics first.');
  }
  return analyticsInstance;
}

// Type declarations for global variables
declare global {
  interface Window {
    gtag?: (...args: any[]) => void;
    plausible?: (event: string, options?: any) => void;
    dataLayer?: any[];
  }
}

export default AnalyticsService;
