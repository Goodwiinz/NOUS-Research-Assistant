import * as React from 'react';
import { Slot } from '@radix-ui/react-slot';
import { cva, type VariantProps } from 'class-variance-authority';

import { cn } from 'src/lib/utils';
import { Spinner } from './spinner';

// NOUS migration (2026-04-19):
//   - Hover lift + shadow on CTA variants (default, destructive, erebus, accent).
//   - New `erebus` variant = handoff "Primary action" (Erebus dark).
//   - New `accent` variant = handoff "Accent action" (Sol gold + glow shadow).
//   - New `nous-ghost` variant = hover fills with Aurum wash (handoff nav-ghost).
const buttonVariants = cva(
  'inline-flex items-center justify-center gap-2 whitespace-nowrap rounded-md text-sm font-medium ring-offset-background transition-all duration-200 focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 disabled:pointer-events-none disabled:opacity-50 [&_svg]:pointer-events-none [&_svg]:size-4 [&_svg]:shrink-0',
  {
    variants: {
      variant: {
        default:
          'bg-primary text-primary-foreground hover:bg-primary/90 hover:-translate-y-px hover:shadow-[0_4px_12px_rgba(var(--nous-erebus-rgb),0.15)]',
        destructive:
          'bg-destructive text-destructive-foreground hover:bg-destructive/90 hover:-translate-y-px hover:shadow-[0_4px_12px_rgba(var(--nous-mars-rgb),0.25)]',
        outline:
          'border border-input bg-background hover:bg-accent hover:text-accent-foreground',
        secondary:
          'bg-secondary text-secondary-foreground hover:bg-secondary/80',
        ghost: 'hover:bg-accent hover:text-accent-foreground',
        link: 'text-primary underline-offset-4 hover:underline',
        erebus:
          'bg-(--nous-erebus) text-white hover:-translate-y-px hover:shadow-[0_4px_12px_rgba(var(--nous-erebus-rgb),0.2)]',
        accent:
          'bg-(--nous-sol) text-white hover:-translate-y-px hover:shadow-[0_8px_30px_rgba(var(--nous-sol-rgb),0.3)]',
        'nous-ghost':
          'bg-transparent text-(--nous-fg-2) hover:bg-(--nous-aurum) hover:text-(--nous-fg-1)',
      },
      size: {
        default: 'h-10 px-4 py-2',
        sm: 'h-9 rounded-md px-3',
        lg: 'h-11 rounded-md px-8',
        icon: 'h-10 w-10',
      },
    },
    defaultVariants: {
      variant: 'default',
      size: 'default',
    },
  }
);

export interface ButtonProps
  extends
    React.ButtonHTMLAttributes<HTMLButtonElement>,
    VariantProps<typeof buttonVariants> {
  asChild?: boolean;
  isLoading?: boolean;
  loadingText?: string;
}

const Button = React.forwardRef<HTMLButtonElement, ButtonProps>(
  (
    {
      className,
      variant,
      size,
      asChild = false,
      isLoading = false,
      loadingText,
      children,
      disabled,
      ...props
    },
    ref
  ) => {
    const Comp = asChild ? Slot : 'button';
    const isIcon = size === 'icon';

    if (asChild) {
      return (
        <Comp
          className={cn(buttonVariants({ variant, size, className }))}
          ref={ref}
          disabled={disabled || isLoading}
          {...props}
        >
          {children}
        </Comp>
      );
    }

    return (
      <Comp
        className={cn(buttonVariants({ variant, size, className }))}
        ref={ref}
        disabled={disabled || isLoading}
        {...props}
      >
        {isLoading && <Spinner size="sm" />}
        {!isLoading && children}
        {isLoading && !isIcon && (loadingText || children)}
      </Comp>
    );
  }
);
Button.displayName = 'Button';

export { Button, buttonVariants };
