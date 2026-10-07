'use client';

import Link from 'next/link';
import { useState, type ComponentType, type ReactElement } from 'react';
import {
  ArrowRight,
  Bell,
  BriefcaseBusiness,
  Clock3,
  KeyRound,
  HardDrive,
  Laptop,
  UserCog,
  UserCircle2,
} from 'lucide-react';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Label } from '@/components/ui/label';
import { Separator } from '@/components/ui/separator';
import { Switch } from '@/components/ui/switch';
import { useAuth } from '@/hooks/useAuth';

type PreferenceKey =
  'compactMode' | 'emailNotifications' | 'desktopNotifications';

type SettingsStatusItem = {
  label: string;
  value: string;
  hint: string;
  connected: boolean;
  icon: ComponentType<{ className?: string }>;
};

type SettingsCardItem = {
  title: string;
  description: string;
  href: string;
  cta: string;
  icon: ComponentType<{ className?: string }>;
};

const PLAN_LABELS: Record<string, string> = {
  free: 'Free',
  pro: 'Pro',
  enterprise: 'Enterprise',
};

const ROLE_HINTS: Record<string, string> = {
  admin: 'Full governance access',
  user: 'Standard workspace access',
  viewer: 'Read-only access',
};

function formatLastSignIn(value: string | null | undefined): string {
  if (!value) return '';
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return '';
  return parsed.toLocaleString(undefined, {
    dateStyle: 'medium',
    timeStyle: 'short',
  });
}

function formatBytes(value: number | null | undefined): string {
  if (typeof value !== 'number' || !Number.isFinite(value) || value < 0) {
    return 'Not available';
  }
  if (value === 0) return '0 B';
  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  const exponent = Math.min(
    Math.floor(Math.log(value) / Math.log(1024)),
    units.length - 1
  );
  return `${(value / 1024 ** exponent).toFixed(exponent === 0 ? 0 : 1)} ${units[exponent]}`;
}

const PREFERENCE_ITEMS = [
  {
    key: 'compactMode',
    label: 'Compact mode',
    description: 'Reduce whitespace in dense research and admin workflows.',
  },
  {
    key: 'emailNotifications',
    label: 'Email notifications',
    description: 'Receive workspace alerts, summaries, and access updates.',
  },
  {
    key: 'desktopNotifications',
    label: 'Desktop notifications',
    description:
      'Show local prompts for live runs, incidents, and completed jobs.',
  },
] as const;

export default function SettingsPage(): ReactElement {
  const { user, organization } = useAuth();
  const [preferences, setPreferences] = useState({
    compactMode: false,
    emailNotifications: true,
    desktopNotifications: true,
  });

  const realName = user?.name?.trim();
  const primaryEmail = user?.email ?? '';
  const accountName = realName || primaryEmail || 'Your account';
  // Avoid repeating the email when it is also standing in as the display name.
  const showEmailLine = Boolean(primaryEmail) && accountName !== primaryEmail;
  const accountInitial = accountName.charAt(0).toUpperCase() || '?';

  const role = user?.role;
  const roleLabel = role ? role.charAt(0).toUpperCase() + role.slice(1) : '';

  const workspaceName = organization?.name?.trim() ?? '';
  const planLabel = organization?.plan ? PLAN_LABELS[organization.plan] : '';
  const lastSignIn = formatLastSignIn(user?.last_login);
  const storageUsed = user?.storage_quota_used;
  const storageLimit = user?.storage_quota_limit;
  const storagePercent =
    typeof storageUsed === 'number' &&
    typeof storageLimit === 'number' &&
    storageLimit > 0
      ? Math.min(100, (storageUsed / storageLimit) * 100)
      : null;

  const statusItems: SettingsStatusItem[] = [
    {
      label: 'Workspace',
      value: workspaceName || 'No workspace connected',
      hint: workspaceName
        ? 'Active workspace'
        : 'Connect a workspace to manage members',
      connected: Boolean(workspaceName),
      icon: BriefcaseBusiness,
    },
    {
      label: 'Role',
      value: roleLabel || 'Unknown',
      hint: role ? (ROLE_HINTS[role] ?? 'Workspace member') : 'Sign in to view',
      connected: Boolean(role),
      icon: UserCog,
    },
    {
      label: 'Plan',
      value: planLabel || 'Not connected',
      hint: planLabel
        ? 'Current workspace plan'
        : 'No billing source linked yet',
      connected: Boolean(planLabel),
      icon: BriefcaseBusiness,
    },
    {
      label: 'API access',
      value: 'No tokens yet',
      hint: 'Create a token in developer access',
      connected: false,
      icon: KeyRound,
    },
    {
      label: 'Storage',
      value: `${formatBytes(storageUsed)} / ${formatBytes(storageLimit)}`,
      hint:
        storagePercent === null
          ? 'Quota details unavailable'
          : `${storagePercent.toFixed(0)}% of workspace quota used`,
      connected: storagePercent !== null,
      icon: HardDrive,
    },
  ];

  const settingsCards: SettingsCardItem[] = [
    {
      title: 'Connected devices',
      description: 'Review coding devices and revoke their project access.',
      href: '/integrations/devices',
      cta: 'Manage connected devices',
      icon: Laptop,
    },
    {
      title: 'Workspace and organization',
      description:
        'Workspace identity, members, roles, billing, and security posture.',
      href: '/settings/organization',
      cta: 'Open workspace settings',
      icon: BriefcaseBusiness,
    },
    {
      title: 'Developer access',
      description:
        'API tokens, provider connections, and credential management.',
      href: '/settings/api-keys',
      cta: 'Open developer access',
      icon: KeyRound,
    },
  ];

  const togglePreference = (key: PreferenceKey, checked: boolean): void => {
    setPreferences((current) => ({
      ...current,
      [key]: checked,
    }));
  };

  return (
    <div className="space-y-8 px-6 pb-20 pt-6 md:space-y-10 md:px-10 md:pb-24 md:pt-8 lg:px-12">
      <header className="space-y-5">
        <div className="space-y-3">
          <h1 className="text-3xl font-semibold text-(--nous-fg-1)">
            Settings
          </h1>
          <p className="max-w-3xl text-sm text-(--nous-fg-3)">
            Manage your account, workspace governance, model access, and
            platform controls.
          </p>
        </div>

        <Card className="overflow-hidden rounded-2xl border-(--nous-border-1) bg-(--nous-bg-2) shadow-none">
          <CardContent className="grid gap-5 p-5 sm:p-6 xl:grid-cols-[minmax(0,1.5fr)_minmax(0,1fr)]">
            <div className="flex items-start gap-3">
              <div
                className="flex h-14 w-14 shrink-0 items-center justify-center rounded-2xl border border-(--nous-border-1) bg-(--nous-sol-glow) text-lg font-semibold text-(--nous-fg-1)"
                aria-hidden="true"
              >
                {accountInitial}
              </div>
              <div className="space-y-1.5">
                <div className="flex flex-wrap items-center gap-2">
                  <p className="text-lg font-semibold text-(--nous-fg-1)">
                    {accountName}
                  </p>
                  {roleLabel ? (
                    <span className="inline-flex items-center rounded-full border border-(--nous-border-1) bg-(--nous-bg-1) px-2.5 py-1 text-xs font-medium text-(--nous-fg-1)">
                      {roleLabel}
                    </span>
                  ) : null}
                </div>
                {showEmailLine ? (
                  <p className="text-sm text-(--nous-fg-3)">{primaryEmail}</p>
                ) : !primaryEmail ? (
                  <p className="text-sm text-(--nous-fg-3)">No email on file</p>
                ) : null}
              </div>
            </div>

            <div className="grid gap-2 rounded-2xl border border-(--nous-border-1) bg-(--nous-bg-1) p-3.5 sm:p-4">
              <p className="text-xs font-medium text-(--nous-fg-3)">
                Last sign-in
              </p>
              <div className="flex items-center gap-2 text-sm text-(--nous-fg-1)">
                <Clock3
                  className="h-4 w-4 text-(--nous-sol)"
                  aria-hidden="true"
                />
                {lastSignIn || 'Not recorded yet'}
              </div>
            </div>
          </CardContent>
        </Card>
      </header>

      <section className="space-y-4" aria-labelledby="settings-status-title">
        <h2
          id="settings-status-title"
          className="text-base font-semibold text-(--nous-fg-1)"
        >
          Account overview
        </h2>
        <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
          {statusItems.map((item) => {
            const Icon = item.icon;

            return (
              <Card
                key={item.label}
                className="rounded-2xl border-(--nous-border-1) bg-(--nous-bg-2) shadow-none"
              >
                <CardContent className="space-y-2 p-4">
                  <div className="flex items-center gap-2">
                    <div
                      className="flex h-8 w-8 items-center justify-center rounded-lg bg-(--nous-sol-glow) text-(--nous-sol)"
                      aria-hidden="true"
                    >
                      <Icon className="h-4 w-4" />
                    </div>
                    <p className="text-sm font-medium text-(--nous-fg-2)">
                      {item.label}
                    </p>
                  </div>
                  <p
                    className={`text-lg font-semibold leading-tight ${
                      item.connected
                        ? 'text-(--nous-fg-1)'
                        : 'text-(--nous-fg-3)'
                    }`}
                  >
                    {item.value}
                  </p>
                  <p className="text-sm text-(--nous-fg-3)">{item.hint}</p>
                </CardContent>
              </Card>
            );
          })}
        </div>
      </section>

      <section className="space-y-4" aria-labelledby="settings-areas-title">
        <h2
          id="settings-areas-title"
          className="text-base font-semibold text-(--nous-fg-1)"
        >
          Settings areas
        </h2>
        <div className="grid gap-3 lg:grid-cols-2">
          <Card className="rounded-2xl border-(--nous-border-1) bg-(--nous-bg-2) shadow-none lg:col-span-2">
            <CardContent className="flex flex-col gap-4 p-5 sm:flex-row sm:items-center sm:justify-between">
              <div className="flex items-start gap-3">
                <div
                  className="flex h-10 w-10 shrink-0 items-center justify-center rounded-xl bg-(--nous-sol-glow) text-(--nous-sol)"
                  aria-hidden="true"
                >
                  <UserCircle2 className="h-5 w-5" />
                </div>
                <div className="space-y-1">
                  <p className="text-lg font-semibold text-(--nous-fg-1)">
                    Profile and preferences
                  </p>
                  <p className="text-sm leading-6 text-(--nous-fg-3)">
                    Identity, display defaults, and notification preferences for
                    your account.
                  </p>
                </div>
              </div>
              <a
                href="#personal-preferences"
                className="inline-flex shrink-0 items-center gap-2 text-sm font-medium text-(--nous-fg-1) transition-colors hover:text-(--nous-sol) focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-(--nous-sol)/40 rounded-md"
              >
                Jump to personal controls
                <ArrowRight className="h-4 w-4" aria-hidden="true" />
              </a>
            </CardContent>
          </Card>

          {settingsCards.map((item) => {
            const Icon = item.icon;

            return (
              <Card
                key={item.title}
                className="rounded-2xl border-(--nous-border-1) bg-(--nous-bg-2) shadow-none"
              >
                <CardHeader className="space-y-3 p-5 pb-2">
                  <div
                    className="flex h-10 w-10 items-center justify-center rounded-xl bg-(--nous-sol-glow) text-(--nous-sol)"
                    aria-hidden="true"
                  >
                    <Icon className="h-5 w-5" />
                  </div>
                  <div className="space-y-2">
                    <CardTitle className="text-xl text-(--nous-fg-1)">
                      {item.title}
                    </CardTitle>
                    <p className="text-sm leading-6 text-(--nous-fg-3)">
                      {item.description}
                    </p>
                  </div>
                </CardHeader>
                <CardContent className="p-5 pt-0">
                  <div className="border-t border-(--nous-border-1) pt-3">
                    <Link
                      href={item.href}
                      className="inline-flex items-center gap-2 text-sm font-medium text-(--nous-fg-1) transition-colors hover:text-(--nous-sol) focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-(--nous-sol)/40 rounded-md"
                    >
                      {item.cta}
                      <ArrowRight className="h-4 w-4" aria-hidden="true" />
                    </Link>
                  </div>
                </CardContent>
              </Card>
            );
          })}
        </div>
      </section>

      <section
        id="personal-preferences"
        className="scroll-mt-24"
        aria-labelledby="personal-preferences-title"
      >
        <Card className="rounded-2xl border-(--nous-border-1) bg-(--nous-bg-2) shadow-none">
          <CardHeader className="space-y-2 p-5 pb-2">
            <div className="flex items-center gap-2">
              <Bell className="h-4 w-4 text-(--nous-sol)" aria-hidden="true" />
              <CardTitle
                id="personal-preferences-title"
                className="text-2xl text-(--nous-fg-1)"
              >
                Personal preferences
              </CardTitle>
            </div>
            <p className="max-w-3xl text-sm text-(--nous-fg-3)">
              Quick personal defaults for this overview. These controls stay
              local in this pass and do not write to backend settings yet.
            </p>
          </CardHeader>
          <CardContent className="space-y-4 p-5 pt-0">
            {PREFERENCE_ITEMS.map((item, index) => (
              <div key={item.key} className="space-y-4">
                {index > 0 && <Separator className="bg-(--nous-border-1)" />}
                <div className="flex flex-col gap-4 sm:flex-row sm:items-center sm:justify-between">
                  <div className="space-y-1">
                    <Label
                      htmlFor={item.key}
                      className="text-sm font-medium text-(--nous-fg-1)"
                    >
                      {item.label}
                    </Label>
                    <p className="text-sm text-(--nous-fg-3)">
                      {item.description}
                    </p>
                  </div>
                  <Switch
                    id={item.key}
                    checked={preferences[item.key]}
                    onCheckedChange={(checked) =>
                      togglePreference(item.key, checked)
                    }
                  />
                </div>
              </div>
            ))}
          </CardContent>
        </Card>
      </section>
    </div>
  );
}
