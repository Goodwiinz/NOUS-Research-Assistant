/**
 * High-performance Next.js configuration optimized for real-time status streaming
 * Implements advanced code splitting, bundle optimization, and performance enhancements
 */

const withBundleAnalyzer = require('@next/bundle-analyzer')({
  enabled: process.env.ANALYZE === 'true',
});

const webpack = require('webpack');
const CompressionPlugin = require('compression-webpack-plugin');
const { resolveBackendUrl } = require('./config/resolveBackendUrl');

const performanceConfig = {
  // Enable React strict mode for development debugging
  reactStrictMode: process.env.NODE_ENV === 'development',

  // Experimental features for maximum performance
  experimental: {
    // Optimize CSS delivery
    optimizeCss: true,

    // Optimize package imports with tree shaking
    optimizePackageImports: [
      'lucide-react',
      '@radix-ui/react-icons',
      'lodash-es',
      'date-fns',
      'recharts',
    ],

    // Enable app directory for improved routing
    appDir: true,

    // Optimize server components
    serverComponentsExternalPackages: ['@prisma/client'],

    // Enable optimized font loading
    fontLoaders: [
      { loader: '@next/font/google', options: { subsets: ['latin'] } },
    ],

    // Enable scroll_restoration for better UX
    scrollRestoration: true,

    // Enable webVitalsAttribution for performance tracking
    webVitalsAttribution: ['CLS', 'LCP'],

    // Enable large page data bytes for better caching
    largePageDataBytes: 128 * 1000, // 128KB

    // Enable worker threads for CSS optimization
    workerThreads: false, // Disabled for compatibility
  },

  // Advanced image optimization
  images: {
    domains: [
      'localhost',
      process.env.NEXT_PUBLIC_API_URL?.replace('https://', '').replace('http://', ''),
      'cdn.example.com',
    ],
    formats: ['image/webp', 'image/avif'],
    deviceSizes: [640, 750, 828, 1080, 1200, 1920, 2048, 3840],
    imageSizes: [16, 32, 48, 64, 96, 128, 256, 384],
    minimumCacheTTL: 60 * 60 * 24 * 30, // 30 days
    dangerouslyAllowSVG: true,
    contentSecurityPolicy: "default-src 'self'; script-src 'none'; sandbox;",
  },

  // Optimized webpack configuration
  webpack: (config, { isServer, dev, webpack }) => {
    // Performance optimizations
    config.optimization = {
      ...config.optimization,
      splitChunks: {
        chunks: 'all',
        cacheGroups: {
          // Separate vendor chunks for better caching
          vendor: {
            test: /[\\/]node_modules[\\/]/,
            name: 'vendors',
            chunks: 'all',
            priority: 10,
          },

          // React-related libraries
          react: {
            test: /[\\/]node_modules[\\/](react|react-dom|react-router)[\\/]/,
            name: 'react',
            chunks: 'all',
            priority: 20,
          },

          // UI libraries
          ui: {
            test: /[\\/]node_modules[\\/](@radix-ui|@headlessui|@mui|@mui-lab)[\\/]/,
            name: 'ui',
            chunks: 'all',
            priority: 15,
          },

          // Chart libraries
          charts: {
            test: /[\\/]node_modules[\\/](recharts|chart|d3)[\\/]/,
            name: 'charts',
            chunks: 'all',
            priority: 15,
          },

          // Utility libraries
          utils: {
            test: /[\\/]node_modules[\\/](lodash|date-fns|moment)[\\/]/,
            name: 'utils',
            chunks: 'all',
            priority: 12,
          },

          // Common chunks for pages
          common: {
            name: 'common',
            minChunks: 2,
            chunks: 'all',
            priority: 5,
            reuseExistingChunk: true,
          },
        },
      },

      // Enable module concatenation for production
      concatenateModules: !dev,

      // Optimize for production builds
      minimize: !dev,

      // Additional optimization options
      usedExports: true,
      sideEffects: false,
    };

    // Configure file loaders for document processing
    config.module.rules.push({
      test: /\.(pdf|docx?|txt|jpe?g|png|mp3|mp4|mov|avi|wav)$/i,
      use: [
        {
          loader: 'file-loader',
          options: {
            publicPath: '/_next/static/files/',
            outputPath: 'static/files/',
            name: '[name].[hash].[ext]',
            esModule: false,
          },
        },
      ],
    });

    // WebAssembly support for performance-critical operations
    config.experiments = {
      ...config.experiments,
      syncWebAssembly: true,
      asyncWebAssembly: true,
      layers: true,
    };

    // Performance-related plugins
    if (!dev && !isServer) {
      config.plugins.push(
        // Gzip compression for static assets
        new CompressionPlugin({
          algorithm: 'gzip',
          test: /\.(js|css|html|svg)$/,
          threshold: 8192,
          minRatio: 0.8,
        }),

        // Brotli compression (better than gzip)
        new CompressionPlugin({
          filename: '[path][base].br',
          algorithm: 'brotliCompress',
          test: /\.(js|css|html|svg)$/,
          compressionOptions: {
            level: 11,
          },
          threshold: 8192,
          minRatio: 0.8,
        })
      );
    }

    // Environment-specific optimizations
    if (!dev) {
      // Production optimizations
      config.resolve.alias = {
        ...config.resolve.alias,
        // Replace heavy development libraries with production versions
        'react-dom': 'react-dom/profiling',
      };

      // Performance profiling
      config.plugins.push(
        new webpack.DefinePlugin({
          'process.env.NODE_ENV': JSON.stringify('production'),
          'process.env.NEXT_PUBLIC_PERFORMANCE_MODE': JSON.stringify('true'),
        })
      );
    }

    // Memory optimization for large builds
    config.cache = {
      type: 'filesystem',
      buildDependencies: {
        config: [__filename],
      },
      maxMemoryGenerations: 1,
    };

    return config;
  },

  // API rewrites with performance headers
  async rewrites() {
    const backendUrl = resolveBackendUrl();
    return [
      // API routes with caching hints
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

  // Advanced security and performance headers
  async headers() {
    const securityHeaders = [
      {
        key: 'X-DNS-Prefetch-Control',
        value: 'on',
      },
      {
        key: 'X-XSS-Protection',
        value: '1; mode=block',
      },
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
      {
        key: 'Permissions-Policy',
        value: 'camera=(), microphone=(), geolocation=()',
      },
    ];

    const performanceHeaders = [
      {
        key: 'X-DNS-Prefetch-Control',
        value: 'on',
      },
      {
        key: 'X-Vercel-Cache-Control',
        value: 'public, s-maxage=31536000, max-age=31536000',
      },
    ];

    return [
      {
        source: '/api/:path*',
        headers: [
          ...securityHeaders,
          {
            key: 'Cache-Control',
            value: 'no-store, no-cache, must-revalidate, proxy-revalidate',
          },
          {
            key: 'Pragma',
            value: 'no-cache',
          },
          {
            key: 'Expires',
            value: '0',
          },
        ],
      },
      {
        source: '/_next/static/(.*)',
        headers: [
          ...performanceHeaders,
          {
            key: 'Cache-Control',
            value: 'public, max-age=31536000, immutable',
          },
        ],
      },
      {
        source: '/(.*)',
        headers: [
          ...securityHeaders,
          ...performanceHeaders,
          {
            key: 'Strict-Transport-Security',
            value: 'max-age=63072000; includeSubDomains; preload',
          },
        ],
      },
    ];
  },

  // Generate redirects for common routes
  async redirects() {
    return [
      {
        source: '/home',
        destination: '/',
        permanent: true,
      },
      {
        source: '/dashboard',
        destination: '/documents',
        permanent: true,
      },
    ];
  },

  // Output configuration for optimization
  output: 'standalone',

  // Build optimizations
  compiler: {
    // Remove console.log in production
    removeConsole: process.env.NODE_ENV === 'production',

    // SWC minification
    styledComponents: true,

    // React optimizations
    reactRemoveProperties: process.env.NODE_ENV === 'production',
  },

  // Power by header for monitoring
  poweredByHeader: false,

  // Generate source maps for debugging (only in development)
  productionBrowserSourceMaps: false,

  // Enable cross-origin isolation for SharedArrayBuffer
  crossOrigin: 'anonymous',

  // Environment variables (exposed to client)
  env: {
    ...(process.env.NEXT_PUBLIC_API_URL
      ? { NEXT_PUBLIC_API_URL: process.env.NEXT_PUBLIC_API_URL }
      : {}),
    ...(process.env.NEXT_PUBLIC_WS_URL
      ? { NEXT_PUBLIC_WS_URL: process.env.NEXT_PUBLIC_WS_URL }
      : {}),
    NEXT_PUBLIC_PERFORMANCE_MODE: process.env.NODE_ENV === 'production' ? 'true' : 'false',
    NEXT_PUBLIC_ENABLE_BUNDLE_ANALYSIS: process.env.ANALYZE === 'true' ? 'true' : 'false',
  },

  // Modularize imports for better tree shaking
  modularizeImports: {
    'lucide-react': {
      transform: 'lucide-react/dist/esm/icons/{{kebabCase member}}',
      preventFullImport: true,
    },
    '@radix-ui/react-icons': {
      transform: '@radix-ui/react-icons/dist/{{kebabCase member}}',
      preventFullImport: true,
    },
    'date-fns': {
      transform: 'date-fns/{{member}}',
      preventFullImport: true,
    },
    'lodash-es': {
      transform: 'lodash-es/{{member}}',
      preventFullImport: true,
    },
  },

  // Custom page extensions for better performance
  pageExtensions: ['ts', 'tsx', 'js', 'jsx'],

  // Optimized trailing slash handling
  trailingSlash: false,

  // Domain configuration for CDN
  assetPrefix: process.env.ASSET_PREFIX,

  // Distinct directory for build output
  distDir: 'build',

  // Custom TypeScript configuration
  typescript: {
    ignoreBuildErrors: process.env.NODE_ENV === 'development',
  },

  // ESLint configuration
  eslint: {
    ignoreDuringBuilds: process.env.NODE_ENV === 'production',
  },
};

// Apply bundle analyzer conditionally
module.exports = withBundleAnalyzer(performanceConfig);
