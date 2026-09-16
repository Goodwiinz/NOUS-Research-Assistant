const path = require('path');
const { withSentryConfig } = require('@sentry/nextjs');
const { resolveBackendUrl } = require('./config/resolveBackendUrl');
const {
  resolveSentryEnvironment,
} = require('./config/resolveSentryEnvironment');

const sentryOrg = process.env.SENTRY_ORG;
const sentryProject = process.env.SENTRY_PROJECT;
const shouldUploadSentrySourceMaps = Boolean(
  process.env.SENTRY_AUTH_TOKEN && sentryOrg && sentryProject
);
const sentryEnvironment = resolveSentryEnvironment();

/** @type {import('next').NextConfig} */
const nextConfig = {
  // Enable React strict mode
  reactStrictMode: true,
  transpilePackages: ['@nous/chat-runtime'],

  // Turbopack configuration (Next.js 16 default)
  turbopack: {},

  // Standalone output for Docker deployments
  output: 'standalone',

  // Output file tracing root for monorepo (must be relative for Docker compatibility)
  outputFileTracingRoot: path.join(__dirname, '..'),

  // Ignore TypeScript build errors temporarily
  typescript: {
    ignoreBuildErrors: process.env.NODE_ENV !== 'production',
  },

  // Experimental features for better performance
  experimental: {
    // Optimize CSS
    optimizeCss: true,
    // Optimize package imports
    optimizePackageImports: [
      'lucide-react',
      '@radix-ui/react-icons',
      'lodash-es',
      'date-fns',
      'recharts',
    ],
  },

  // Image optimization
  // WARNING: Add your production domains here. External images from unlisted
  // domains will not be optimized by Next.js and may break in production.
  images: {
    remotePatterns: [
      {
        protocol: 'http',
        hostname: 'localhost',
      },
      ...(process.env.NEXT_PUBLIC_APP_URL
        ? [
            {
              protocol: 'https',
              hostname: new URL(process.env.NEXT_PUBLIC_APP_URL).hostname,
            },
          ]
        : []),
    ],
    formats: ['image/webp', 'image/avif'],
    minimumCacheTTL: 60 * 60 * 24 * 7, // 7 days
  },

  // Webpack configuration for file uploads
  webpack: (config, { isServer }) => {
    config.resolve.alias = {
      ...config.resolve.alias,
      react: path.dirname(require.resolve('react/package.json')),
      'react/jsx-runtime': require.resolve('react/jsx-runtime'),
      'react/jsx-dev-runtime': require.resolve('react/jsx-dev-runtime'),
      'react-dom': path.dirname(require.resolve('react-dom/package.json')),
      'react-dom/client': require.resolve('react-dom/client'),
    };

    if (!isServer) {
      config.optimization = {
        ...config.optimization,
        splitChunks: {
          ...config.optimization?.splitChunks,
          cacheGroups: {
            ...config.optimization?.splitChunks?.cacheGroups,
            vendor: {
              test: /[\\/]node_modules[\\/]/,
              name: 'vendors',
              chunks: 'all',
              priority: 10,
            },
            react: {
              test: /[\\/]node_modules[\\/](react|react-dom)[\\/]/,
              name: 'react',
              chunks: 'all',
              priority: 20,
            },
            ui: {
              test: /[\\/]node_modules[\\/](@radix-ui)[\\/]/,
              name: 'ui',
              chunks: 'all',
              priority: 15,
            },
          },
        },
      };
    }

    // Handle file uploads for documents
    config.module.rules.push({
      test: /\.(pdf|docx?|txt|jpe?g|png|mp3|mp4|mov|avi|wav)$/i,
      use: [
        {
          loader: 'file-loader',
          options: {
            publicPath: '/_next/static/files/',
            outputPath: 'static/files/',
            name: '[name].[hash].[ext]',
          },
        },
      ],
    });

    // Handle source maps in development
    if (!isServer) {
      config.resolve.alias = {
        ...config.resolve.alias,
        fs: false,
      };
    }

    return config;
  },

  // API configuration
  async rewrites() {
    // Vercel/standalone rewrites are server-side; never silently point remote
    // deployments at localhost when the backend endpoint is missing.
    const backendUrl = resolveBackendUrl();
    return [
      // API rewrites for backend integration
      {
        source: '/api/v1/:path*',
        destination: `${backendUrl}/api/v1/:path*`,
      },
      {
        source: '/api/v2/:path*',
        destination: `${backendUrl}/api/v2/:path*`,
      },
    ];
  },

  // Environment variables
  // Do NOT set localhost fallbacks here — they get baked into the production
  // JS bundle and cause CORS/mixed-content errors in K8s deployments.
  // The frontend uses Next.js rewrites (/api/v1/* → backend) when these are unset.
  env: {
    // Vercel preview builds also use NODE_ENV=production. Bake the actual
    // deployment target into the browser bundle so preview events stay out of
    // Sentry's production environment.
    NEXT_PUBLIC_SENTRY_ENVIRONMENT: sentryEnvironment,
    ...(process.env.NEXT_PUBLIC_API_URL
      ? { NEXT_PUBLIC_API_URL: process.env.NEXT_PUBLIC_API_URL }
      : {}),
    ...(process.env.NEXT_PUBLIC_WS_URL
      ? { NEXT_PUBLIC_WS_URL: process.env.NEXT_PUBLIC_WS_URL }
      : {}),
  },

  // Combined CORS and Security headers
  async headers() {
    return [
      {
        source: '/api/:path*',
        headers: [
          {
            key: 'Access-Control-Allow-Origin',
            value:
              process.env.NEXT_PUBLIC_FRONTEND_URL || 'http://localhost:3000',
          },
          {
            key: 'Access-Control-Allow-Methods',
            value: 'GET, POST, PUT, DELETE, OPTIONS',
          },
          {
            key: 'Access-Control-Allow-Headers',
            value: 'Content-Type, Authorization',
          },
        ],
      },
      {
        source: '/(.*)',
        headers: [
          {
            key: 'X-Frame-Options',
            value: 'DENY',
          },
          {
            key: 'X-Content-Type-Options',
            value: 'nosniff',
          },
          {
            key: 'Referrer-Policy',
            value: 'origin-when-cross-origin',
          },
          // NOTE: the Content-Security-Policy is set per-request in
          // frontend/proxy.ts with a script NONCE (no unsafe-inline) — see
          // buildCsp() there. It must NOT also be set statically here: two CSP
          // headers enforce their INTERSECTION, so a static unsafe-inline copy
          // and the nonce policy would break each other. The proxy matcher
          // covers every scripted route; the excluded paths are static assets.
        ],
      },
    ];
  },

  // Redirects for common routes
  async redirects() {
    return [
      {
        source: '/home',
        destination: '/',
        permanent: true,
      },
    ];
  },
};

module.exports = withSentryConfig(nextConfig, {
  org: sentryOrg,
  project: sentryProject,
  authToken: process.env.SENTRY_AUTH_TOKEN,

  // Quiet local builds; CI surfaces logs.
  silent: !process.env.CI,

  // Only upload source maps when Sentry release settings are explicitly set.
  sourcemaps: {
    disable: !shouldUploadSentrySourceMaps,
  },
  errorHandler: (error) => {
    console.warn('Sentry source map upload skipped:', error.message);
  },

  // Upload a larger set of source maps so client errors symbolicate cleanly.
  widenClientFileUpload: true,

  // Route Sentry events through /monitoring to bypass adblockers.
  tunnelRoute: '/monitoring',

  webpack: {
    treeshake: {
      removeDebugLogging: true,
    },
    automaticVercelMonitors: false,
  },
});
