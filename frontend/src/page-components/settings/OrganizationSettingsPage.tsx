'use client';

import {
  useCallback,
  useEffect,
  useState,
  type FormEvent,
  type ReactElement,
} from 'react';
import Link from 'next/link';
import {
  ArrowRight,
  BriefcaseBusiness,
  Loader2,
  Trash2,
  UserPlus,
} from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Badge } from '@/components/ui/badge';
import { Card, CardContent } from '@/components/ui/card';
import { Input } from '@/components/ui/input';
import { useAuthStore } from '@/stores/authStore';
import { workspaceService } from '@/services/workspaceService';
import type { WorkspaceDetail, WorkspaceMember } from '@/types/workspace';

type OrganizationArea = {
  title: string;
  description: string;
  /**
   * A real, reachable management surface. When null, the area is not built
   * yet and we show an honest "not available" note instead of a dead link.
   */
  href: string | null;
  /** Honest copy when no data source or sub-route exists yet. */
  emptyNote: string;
};

const ORGANIZATION_AREAS: OrganizationArea[] = [
  {
    title: 'Usage and billing',
    description:
      'See document capacity, credit consumption, and the next plan reset.',
    href: null,
    emptyNote: 'No usage data yet. Billing is not connected to this workspace.',
  },
  {
    title: 'Developer access',
    description:
      'Manage API tokens and provider access for models and integrations.',
    href: '/settings/api-keys',
    emptyNote: '',
  },
];

const MEMBER_ROLES = ['viewer', 'editor', 'admin'] as const;
type MemberRole = (typeof MEMBER_ROLES)[number];

function errorMessage(error: unknown, fallback: string): string {
  return error instanceof Error ? error.message : fallback;
}

export const OrganizationSettingsPage = (): ReactElement => {
  const user = useAuthStore((state) => state.user);
  const [workspace, setWorkspace] = useState<WorkspaceDetail | null>(null);
  const [members, setMembers] = useState<WorkspaceMember[]>([]);
  const [memberLoading, setMemberLoading] = useState(false);
  const [memberSaving, setMemberSaving] = useState(false);
  const [memberError, setMemberError] = useState<string | null>(null);
  const [newUserId, setNewUserId] = useState('');
  const [newRole, setNewRole] = useState<MemberRole>('viewer');

  const loadWorkspace = useCallback(async () => {
    if (!user) return;
    setMemberLoading(true);
    setMemberError(null);
    try {
      const defaultWorkspace =
        await workspaceService.getOrCreateDefaultWorkspace(user.id);
      const details = await workspaceService.getWorkspace(defaultWorkspace.id);
      setWorkspace(details);
      setMembers(details.members ?? []);
    } catch (error) {
      setMemberError(errorMessage(error, 'Failed to load workspace members.'));
    } finally {
      setMemberLoading(false);
    }
  }, [user]);

  useEffect(() => {
    // The async loader owns the request state updates.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    void loadWorkspace();
  }, [loadWorkspace]);

  const canManageMembers = Boolean(
    user &&
    workspace &&
    (workspace.owner_id === user.id || user.role === 'admin')
  );

  const handleAddMember = async (
    event: FormEvent<HTMLFormElement>
  ): Promise<void> => {
    event.preventDefault();
    if (!workspace || !newUserId.trim() || !canManageMembers) return;

    setMemberSaving(true);
    setMemberError(null);
    try {
      const member = await workspaceService.addWorkspaceMember(workspace.id, {
        user_id: newUserId.trim(),
        role: newRole,
      });
      setMembers((current) => [...current, member]);
      setNewUserId('');
    } catch (error) {
      setMemberError(errorMessage(error, 'Failed to add workspace member.'));
    } finally {
      setMemberSaving(false);
    }
  };

  const handleRoleChange = async (
    member: WorkspaceMember,
    role: MemberRole
  ): Promise<void> => {
    if (!workspace || !canManageMembers || member.role === 'owner') return;

    setMemberError(null);
    try {
      const updated = await workspaceService.updateWorkspaceMember(
        workspace.id,
        member.user_id,
        { role }
      );
      setMembers((current) =>
        current.map((item) =>
          item.user_id === updated.user_id ? updated : item
        )
      );
    } catch (error) {
      setMemberError(errorMessage(error, 'Failed to update member role.'));
    }
  };

  const handleRemoveMember = async (member: WorkspaceMember): Promise<void> => {
    if (
      !workspace ||
      !canManageMembers ||
      member.role === 'owner' ||
      !window.confirm('Remove this member from the workspace?')
    ) {
      return;
    }

    setMemberError(null);
    try {
      await workspaceService.removeWorkspaceMember(
        workspace.id,
        member.user_id
      );
      setMembers((current) =>
        current.filter((item) => item.user_id !== member.user_id)
      );
    } catch (error) {
      setMemberError(errorMessage(error, 'Failed to remove workspace member.'));
    }
  };

  return (
    <div className="space-y-8 px-6 pb-20 pt-6 md:space-y-10 md:px-10 md:pb-24 md:pt-8 lg:px-12">
      <header className="space-y-6">
        <div className="space-y-3">
          <h1 className="text-3xl font-semibold text-(--nous-fg-1)">
            Organization Settings
          </h1>
          <p className="max-w-3xl text-sm text-(--nous-fg-2)">
            Manage workspace identity, access, and developer settings for NOUS.
          </p>
        </div>

        <Button
          asChild
          variant="outline"
          className="border-(--nous-border-1) bg-transparent text-(--nous-fg-1) hover:bg-(--nous-bg-1) hover:text-(--nous-fg-1) focus-visible:ring-2 focus-visible:ring-(--nous-sol)/40"
        >
          <Link href="/settings">
            <ArrowRight className="h-4 w-4 rotate-180" aria-hidden="true" />
            Back to settings
          </Link>
        </Button>
      </header>

      <Card className="rounded-2xl border-(--nous-border-1) bg-(--nous-bg-2) shadow-none">
        <CardContent className="flex flex-col gap-4 p-6 md:flex-row md:items-center md:justify-between">
          <div className="flex items-start gap-3">
            <div className="flex h-10 w-10 shrink-0 items-center justify-center rounded-xl border border-(--nous-border-1) bg-(--nous-bg-1) text-(--nous-sol)">
              <BriefcaseBusiness className="h-5 w-5" aria-hidden="true" />
            </div>
            <div className="space-y-1">
              <p className="text-base font-medium text-(--nous-fg-1)">
                Default research workspace
              </p>
              <p className="text-sm text-(--nous-fg-2)">
                The active workspace for research, ingestion, and access
                control.
              </p>
            </div>
          </div>
          <Badge
            variant="outline"
            className="self-start border-(--nous-border-1) bg-(--nous-bg-1) text-(--nous-fg-2) md:self-center"
          >
            Active workspace
          </Badge>
        </CardContent>
      </Card>

      <section
        aria-labelledby="workspace-members-heading"
        className="space-y-4"
      >
        <div className="space-y-1">
          <h2
            id="workspace-members-heading"
            className="text-lg font-semibold text-(--nous-fg-1)"
          >
            Members and roles
          </h2>
          <p className="text-sm text-(--nous-fg-2)">
            Manage who can access the active research workspace.
          </p>
        </div>
        <Card className="rounded-2xl border-(--nous-border-1) bg-(--nous-bg-2) shadow-none">
          <CardContent className="space-y-5 p-6">
            {memberError && (
              <p
                role="alert"
                className="rounded-lg border border-(--nous-mars)/30 bg-(--nous-mars)/10 px-3 py-2 text-sm text-(--nous-mars)"
              >
                {memberError}
              </p>
            )}

            {canManageMembers && (
              <form
                onSubmit={handleAddMember}
                className="grid gap-3 rounded-xl border border-(--nous-border-1) bg-(--nous-bg-1) p-4 md:grid-cols-[minmax(0,1fr)_9rem_auto] md:items-end"
              >
                <div className="space-y-1.5">
                  <label
                    htmlFor="workspace-member-user-id"
                    className="text-sm font-medium text-(--nous-fg-1)"
                  >
                    Add member by user ID
                  </label>
                  <Input
                    id="workspace-member-user-id"
                    value={newUserId}
                    onChange={(event) => setNewUserId(event.target.value)}
                    placeholder="UUID of an existing account"
                    autoComplete="off"
                    required
                  />
                </div>
                <label className="space-y-1.5">
                  <span className="text-sm font-medium text-(--nous-fg-1)">
                    Role
                  </span>
                  <select
                    value={newRole}
                    onChange={(event) =>
                      setNewRole(event.target.value as MemberRole)
                    }
                    className="flex h-10 w-full rounded-md border border-input bg-background px-3 py-2 text-sm text-foreground focus:outline-hidden focus:ring-2 focus:ring-(--nous-sol)/30"
                  >
                    {MEMBER_ROLES.map((role) => (
                      <option key={role} value={role}>
                        {role.charAt(0).toUpperCase() + role.slice(1)}
                      </option>
                    ))}
                  </select>
                </label>
                <Button type="submit" disabled={memberSaving || !workspace}>
                  {memberSaving ? (
                    <Loader2
                      className="mr-2 h-4 w-4 animate-spin"
                      aria-hidden="true"
                    />
                  ) : (
                    <UserPlus className="mr-2 h-4 w-4" aria-hidden="true" />
                  )}
                  Add member
                </Button>
              </form>
            )}

            {memberLoading ? (
              <p role="status" className="text-sm text-(--nous-fg-2)">
                Loading members…
              </p>
            ) : members.length > 0 ? (
              <ul
                className="divide-y divide-(--nous-border-1)"
                aria-label="Workspace members"
              >
                {members.map((member) => {
                  const label =
                    member.user_name?.trim() ||
                    member.user_email?.trim() ||
                    member.user_id;
                  const canEditMember =
                    canManageMembers && member.role !== 'owner';

                  return (
                    <li
                      key={member.user_id}
                      className="flex flex-col gap-3 py-3 sm:flex-row sm:items-center sm:justify-between"
                    >
                      <div className="min-w-0">
                        <p className="truncate text-sm font-medium text-(--nous-fg-1)">
                          {label}
                        </p>
                        <p className="truncate text-xs text-(--nous-fg-3)">
                          {member.user_email || member.user_id}
                        </p>
                      </div>
                      <div className="flex items-center gap-2">
                        <select
                          aria-label={`Role for ${label}`}
                          value={member.role}
                          disabled={!canEditMember}
                          onChange={(event) =>
                            void handleRoleChange(
                              member,
                              event.target.value as MemberRole
                            )
                          }
                          className="h-9 rounded-md border border-input bg-background px-2 text-sm text-foreground disabled:opacity-60"
                        >
                          <option value="owner">Owner</option>
                          {MEMBER_ROLES.map((role) => (
                            <option key={role} value={role}>
                              {role.charAt(0).toUpperCase() + role.slice(1)}
                            </option>
                          ))}
                        </select>
                        {canEditMember && (
                          <Button
                            type="button"
                            variant="ghost"
                            size="icon"
                            aria-label={`Remove ${label}`}
                            onClick={() => void handleRemoveMember(member)}
                          >
                            <Trash2 className="h-4 w-4" aria-hidden="true" />
                          </Button>
                        )}
                      </div>
                    </li>
                  );
                })}
              </ul>
            ) : (
              <p className="text-sm text-(--nous-fg-3)">
                {user
                  ? 'No workspace members found.'
                  : 'Sign in to manage workspace members.'}
              </p>
            )}
          </CardContent>
        </Card>
      </section>

      <section
        aria-labelledby="organization-areas-heading"
        className="space-y-4"
      >
        <h2
          id="organization-areas-heading"
          className="text-lg font-semibold text-(--nous-fg-1)"
        >
          Manage
        </h2>
        <ul className="space-y-3">
          {ORGANIZATION_AREAS.map((area) => {
            const isAvailable = area.href !== null;

            const body = (
              <div className="flex flex-col gap-3 p-5 sm:flex-row sm:items-center sm:justify-between">
                <div className="space-y-1.5">
                  <p className="text-base font-medium text-(--nous-fg-1)">
                    {area.title}
                  </p>
                  <p className="max-w-2xl text-sm text-(--nous-fg-2)">
                    {area.description}
                  </p>
                  {!isAvailable && (
                    <p className="text-sm text-(--nous-fg-3)">
                      {area.emptyNote}
                    </p>
                  )}
                </div>
                {isAvailable ? (
                  <span className="inline-flex items-center gap-1.5 text-sm font-medium text-(--nous-sol)">
                    Open
                    <ArrowRight className="h-4 w-4" aria-hidden="true" />
                  </span>
                ) : (
                  <Badge
                    variant="outline"
                    className="self-start border-(--nous-border-1) bg-(--nous-bg-1) text-(--nous-fg-3)"
                  >
                    Not available yet
                  </Badge>
                )}
              </div>
            );

            return (
              <li key={area.title}>
                {isAvailable && area.href ? (
                  <Card
                    interactive
                    className="rounded-2xl border-(--nous-border-1) bg-(--nous-bg-2) shadow-none"
                  >
                    <Link
                      href={area.href}
                      aria-label={`Open ${area.title.toLowerCase()}`}
                      className="block rounded-2xl focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-(--nous-sol)/40"
                    >
                      {body}
                    </Link>
                  </Card>
                ) : (
                  <Card className="rounded-2xl border-(--nous-border-1) bg-(--nous-bg-2) shadow-none">
                    {body}
                  </Card>
                )}
              </li>
            );
          })}
        </ul>
      </section>
    </div>
  );
};

export default OrganizationSettingsPage;
