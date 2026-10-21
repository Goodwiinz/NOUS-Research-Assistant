> **Dated design guide (2026-10-17 review): describes the sidebar enhancement as designed. Verify against `frontend/src/contexts/sidebar-context.tsx` and the dashboard layout before relying on it.**

# Enhanced Sidebar System Documentation

## Overview

The enhanced sidebar system provides a premium navigation experience with smooth animations, persistent state management, and responsive design. It supports three distinct states: Expanded, Mini (icon-only), and Collapsed.

## Features

### ✅ Core Features

1. **Three State System**
   - Expanded (280px): Full sidebar with text labels
   - Mini (64px): Icon-only mode for space efficiency
   - Collapsed (0px): Completely hidden

2. **Persistent State**
   - State automatically saved to localStorage
   - Restored on page refresh
   - Per-user preference preservation

3. **Smooth Animations**
   - Framer Motion powered transitions
   - 60fps performance optimized
   - Spring physics for natural movement

4. **Keyboard Shortcuts**
   - `Cmd/Ctrl + B`: Toggle sidebar
   - `Cmd/Ctrl + Shift + B`: Expand sidebar
   - `Cmd/Ctrl + Alt + B`: Collapse sidebar
   - `Escape`: Close mobile sidebar

5. **Responsive Design**
   - Mobile: Overlay with backdrop
   - Tablet: Auto mini mode
   - Desktop: Full functionality with resize

6. **Accessibility**
   - Full keyboard navigation
   - Screen reader support
   - ARIA labels and roles
   - Focus management

7. **Visual Enhancements**
   - Hover effects and micro-interactions
   - Active state indicators
   - Smooth icon animations
   - Gradient accents

## Installation

### Dependencies

```bash
pnpm --dir frontend add framer-motion zustand
```

### Required Components

```tsx
// Store for state management
import { useSidebarStore } from '@/store/sidebar-store';

// Enhanced components
import { EnhancedSidebarProvider } from '@/contexts/sidebar-context';
import { EnhancedSidebar, EnhancedSidebarMenuItem } from '@/components/ui/enhanced-sidebar';
import { EnhancedSidebarTrigger } from '@/components/ui/enhanced-sidebar-trigger';
```

## Usage

### Basic Setup

```tsx
import { EnhancedSidebarProvider } from '@/contexts/sidebar-context';
import { useSidebarKeyboardShortcuts } from '@/store/sidebar-store';

function App() {
  // Enable keyboard shortcuts globally
  useSidebarKeyboardShortcuts();

  return (
    <EnhancedSidebarProvider defaultOpen={true}>
      <YourAppContent />
    </EnhancedSidebarProvider>
  );
}
```

### Dashboard Layout Integration

```tsx
import { DashboardLayout } from '@/components/layout/dashboard/DashboardLayout';

function YourPage() {
  return (
    <DashboardLayout>
      <YourPageContent />
    </DashboardLayout>
  );
}
```

### Custom Sidebar Items

```tsx
import { EnhancedSidebarMenuItem } from '@/components/ui/enhanced-sidebar-menu-item';

function CustomSidebar() {
  return (
    <EnhancedSidebar>
      <EnhancedSidebarContent>
        <EnhancedSidebarMenu>
          <EnhancedSidebarMenuItem
            title="Dashboard"
            href="/dashboard"
            icon={LayoutDashboard}
            badge="New"
          />
          <EnhancedSidebarMenuItem
            title="Settings"
            href="/settings"
            icon={Settings}
          />
        </EnhancedSidebarMenu>
      </EnhancedSidebarContent>
    </EnhancedSidebar>
  );
}
```

## State Management

### Using the Store

```tsx
import { useSidebarStore } from '@/store/sidebar-store';

function SidebarControls() {
  const {
    state,
    isOpen,
    toggleSidebar,
    expandSidebar,
    collapseSidebar,
    setMiniMode,
  } = useSidebarStore();

  return (
    <div>
      <p>Current state: {state}</p>
      <button onClick={toggleSidebar}>Toggle</button>
      <button onClick={expandSidebar}>Expand</button>
      <button onClick={collapseSidebar}>Collapse</button>
      <button onClick={setMiniMode}>Mini Mode</button>
    </div>
  );
}
```

### Store API

```tsx
interface SidebarStore {
  // State
  state: 'expanded' | 'collapsed' | 'mini';
  isMobile: boolean;
  mobileOpen: boolean;
  isAnimating: boolean;

  // Actions
  toggleSidebar: () => void;
  expandSidebar: () => void;
  collapseSidebar: () => void;
  setMiniMode: () => void;
  setMobileOpen: (open: boolean) => void;

  // Computed
  isOpen: () => boolean;
  shouldShowTooltips: () => boolean;
  getWidth: () => string;
}
```

## Customization

### Theme Integration

```tsx
// Override CSS variables in your theme
:root {
  --sidebar-width: 280px;
  --sidebar-width-icon: 64px;
  --sidebar-background: hsl(var(--background));
  --sidebar-foreground: hsl(var(--foreground));
  --sidebar-primary: hsl(var(--primary));
  --sidebar-accent: hsl(var(--accent));
  --sidebar-border: hsl(var(--border));
}
```

### Custom Animations

```tsx
import { motion } from 'framer-motion';

const customVariants = {
  expanded: { width: 320, transition: { duration: 0.4 } },
  mini: { width: 80, transition: { duration: 0.4 } },
  collapsed: { width: 0, transition: { duration: 0.4 } },
};

<EnhancedSidebar customVariants={customVariants}>
  {/* Content */}
</EnhancedSidebar>
```

### Custom Breakpoints

```tsx
// Update in use-mobile hook
const MOBILE_BREAKPOINT = 768;
const TABLET_BREAKPOINT = 1024;
```

## Performance Considerations

1. **Animation Performance**
   - Use `transform` and `opacity` for animations
   - Avoid layout thrashing
   - Will-change optimization

2. **Memory Management**
   - Cleanup event listeners
   - Debounce resize handlers
   - Unsubscribe from store

3. **Bundle Size**
   - Tree-shake unused features
   - Lazy load heavy components
   - Code split animations

## Troubleshooting

### Common Issues

1. **Sidebar not collapsing on mobile**
   ```tsx
   // Ensure mobile detection is working
   const isMobile = useIsMobile();
   console.log('Is mobile:', isMobile);
   ```

2. **State not persisting**
   ```tsx
   // Check localStorage availability
   if (typeof window !== 'undefined') {
     console.log('localStorage available');
   }
   ```

3. **Animations not smooth**
   ```css
   /* Add to global CSS */
   * {
     box-sizing: border-box;
   }

   .sidebar-animate {
     will-change: transform;
   }
   ```

## Migration Guide

### From Basic Sidebar

1. Replace `SidebarProvider` with `EnhancedSidebarProvider`
2. Update imports to use enhanced components
3. Add state management integration
4. Enable keyboard shortcuts
5. Test responsive behavior

### Example Migration

```tsx
// Before
import { SidebarProvider, Sidebar } from '@/components/ui/sidebar';

<SidebarProvider>
  <Sidebar>{/* content */}</Sidebar>
</SidebarProvider>

// After
import { EnhancedSidebarProvider } from '@/contexts/sidebar-context';
import { EnhancedSidebar } from '@/components/ui/enhanced-sidebar';

<EnhancedSidebarProvider>
  <EnhancedSidebar>{/* content */}</EnhancedSidebar>
</EnhancedSidebarProvider>
```

## Browser Support

- Chrome 90+
- Firefox 88+
- Safari 14+
- Edge 90+

## TypeScript Support

Full TypeScript support included:

```tsx
import type { SidebarState } from '@/store/sidebar-store';

const [state, setState] = useState<SidebarState>('expanded');
```

## Contributing

When contributing to the sidebar system:

1. Follow the existing component patterns
2. Add TypeScript definitions
3. Test on all device sizes
4. Ensure accessibility compliance
5. Update documentation

## License

MIT License - see LICENSE file for details.