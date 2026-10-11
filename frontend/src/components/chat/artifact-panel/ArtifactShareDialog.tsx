'use client';

import {
  useEffect,
  useId,
  useRef,
  useState,
  type ReactElement,
  type RefObject,
} from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import {
  Dialog,
  DialogContent,
  DialogTitle,
  DialogDescription,
} from '@/components/ui/dialog';
import {
  artifactSharesKey,
  artifactShareScope,
  useArtifactShareScope,
} from '@/hooks/chat/useArtifactSharing';
import {
  artifactService,
  type ArtifactShare,
  type ArtifactVersion,
} from '@/services/artifactService';

const MAX_EXPIRY_MS = 7 * 86400000;
const USER_ID =
  /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
type Props = {
  version: ArtifactVersion;
  onClose: () => void;
  triggerRef?: RefObject<HTMLButtonElement | null>;
};

export function ArtifactShareDialog(props: Props): ReactElement | null {
  const scope = useArtifactShareScope();
  return scope ? (
    <ScopedShareDialog
      key={`${scope}:${props.version.versionId}`}
      {...props}
      scope={scope}
    />
  ) : null;
}

function localDateTime(timestamp: number): string {
  const value = new Date(timestamp);
  return new Date(timestamp - value.getTimezoneOffset() * 60000)
    .toISOString()
    .slice(0, 16);
}

function ScopedShareDialog({
  version,
  onClose,
  triggerRef,
  scope,
}: Props & { scope: string }): ReactElement {
  const client = useQueryClient();
  const key = artifactSharesKey(scope, version.artifactId);
  const shares = useQuery({
    queryKey: key,
    queryFn: ({ signal }) =>
      artifactService.listArtifactShares(version.artifactId, signal),
    staleTime: 0,
    gcTime: 0,
    retry: false,
  });
  const [audience, setAudience] = useState('');
  const [openedAt] = useState(Date.now);
  const [expiry, setExpiry] = useState(() =>
    localDateTime(Date.now() + 86400000)
  );
  const [error, setError] = useState<string | null>(null);
  const [status, setStatus] = useState<string | null>(null);
  const [pending, setPending] = useState<'create' | string | null>(null);
  const request = useRef<AbortController | null>(null);
  const closed = useRef(false);
  const input = useRef<HTMLTextAreaElement>(null);
  const audienceId = useId();
  const expiryId = useId();
  useEffect(() => {
    closed.current = false;
    return () => {
      closed.current = true;
      request.current?.abort();
    };
  }, []);
  const current = (controller: AbortController): boolean =>
    !closed.current &&
    !controller.signal.aborted &&
    artifactShareScope() === scope;
  const close = (): void => {
    closed.current = true;
    request.current?.abort();
    onClose();
  };
  const create = async (): Promise<void> => {
    if (request.current && !request.current.signal.aborted) return;
    const recipients = [
      ...new Set(
        audience
          .split(/[\s,;]+/)
          .filter(Boolean)
          .map((id) => id.toLowerCase())
      ),
    ];
    const expiresAt = new Date(expiry).getTime();
    setStatus(null);
    if (
      recipients.length < 1 ||
      recipients.length > 100 ||
      recipients.some((id) => !USER_ID.test(id))
    ) {
      setError('Enter 1–100 valid user IDs for the audience.');
      input.current?.focus();
      return;
    }
    if (
      !Number.isFinite(expiresAt) ||
      expiresAt <= Date.now() ||
      expiresAt > Date.now() + MAX_EXPIRY_MS
    ) {
      setError('Choose a future expiry within seven days.');
      return;
    }
    const controller = new AbortController();
    request.current = controller;
    setPending('create');
    setError(null);
    try {
      const share = await artifactService.createArtifactShare(
        version.versionId,
        recipients,
        new Date(expiresAt).toISOString(),
        controller.signal
      );
      if (!current(controller)) return;
      await client.cancelQueries({ queryKey: key });
      if (!current(controller)) return;
      client.setQueryData<ArtifactShare[]>(key, (previous) => [
        ...(previous ?? []).filter((item) => item.id !== share.id),
        share,
      ]);
      setStatus('Share link created for this exact version.');
    } catch {
      if (current(controller))
        setError(
          'Could not create the share link. Check the audience and try again.'
        );
    } finally {
      if (current(controller)) {
        request.current = null;
        setPending(null);
      }
    }
  };
  const revoke = async (shareId: string): Promise<void> => {
    if (request.current && !request.current.signal.aborted) return;
    const controller = new AbortController();
    request.current = controller;
    setPending(shareId);
    setError(null);
    setStatus(null);
    try {
      await artifactService.revokeArtifactShare(shareId, controller.signal);
      if (!current(controller)) return;
      await client.cancelQueries({ queryKey: key });
      if (!current(controller)) return;
      client.setQueryData<ArtifactShare[]>(key, (previous) =>
        (previous ?? []).filter((share) => share.id !== shareId)
      );
      await client.cancelQueries({
        queryKey: ['shared-artifact'],
        predicate: (query) => query.queryKey[2] === shareId,
      });
      client.setQueriesData(
        {
          queryKey: ['shared-artifact'],
          predicate: (query) => query.queryKey[2] === shareId,
        },
        { status: 'unavailable' }
      );
      if (current(controller)) setStatus('Share link revoked.');
    } catch {
      if (current(controller))
        setError('Could not revoke the share link. Try again.');
    } finally {
      if (current(controller)) {
        request.current = null;
        setPending(null);
      }
    }
  };
  const active = (shares.data ?? []).filter(
    (share) =>
      share.versionId === version.versionId &&
      !share.revokedAt &&
      new Date(share.expiresAt).getTime() > openedAt
  );
  const copy = async (shareId: string): Promise<void> => {
    setError(null);
    setStatus(null);
    try {
      await navigator.clipboard.writeText(
        `${window.location.origin}/artifacts/shared/${encodeURIComponent(shareId)}`
      );
      if (!closed.current && artifactShareScope() === scope)
        setStatus('Share link copied.');
    } catch {
      if (!closed.current && artifactShareScope() === scope)
        setError(
          'Could not copy the share link. Use the link above to copy its address.'
        );
    }
  };
  return (
    <Dialog
      open
      onOpenChange={(open) => {
        if (!open) close();
      }}
    >
      <DialogContent
        onOpenAutoFocus={(event) => {
          event.preventDefault();
          input.current?.focus();
        }}
        onCloseAutoFocus={(event) => {
          if (triggerRef?.current) {
            event.preventDefault();
            triggerRef.current.focus();
          }
        }}
      >
        <DialogTitle>Share this version</DialogTitle>
        <DialogDescription>
          {version.title} · This link stays on the selected version. Only the
          listed signed-in users in your organization can open it, for up to
          seven days.
        </DialogDescription>
        <form
          onSubmit={(event) => {
            event.preventDefault();
            void create();
          }}
          className="grid gap-3"
        >
          <label htmlFor={audienceId}>Audience user IDs</label>
          <textarea
            ref={input}
            id={audienceId}
            value={audience}
            onChange={(event) => setAudience(event.target.value)}
            disabled={pending !== null}
            aria-describedby={`${audienceId}-help`}
            className="rounded-md border bg-background p-2 text-sm"
          />
          <p
            id={`${audienceId}-help`}
            className="text-xs text-muted-foreground"
          >
            Enter 1–100 existing user UUIDs, separated by commas or spaces.
          </p>
          <label htmlFor={expiryId}>Expires at</label>
          <input
            id={expiryId}
            type="datetime-local"
            value={expiry}
            min={localDateTime(openedAt)}
            max={localDateTime(openedAt + MAX_EXPIRY_MS)}
            onChange={(event) => setExpiry(event.target.value)}
            disabled={pending !== null}
            aria-describedby={`${expiryId}-help`}
            className="rounded-md border bg-background p-2 text-sm"
          />
          <p id={`${expiryId}-help`} className="text-xs text-muted-foreground">
            Your local time. Latest expiry:{' '}
            {new Date(openedAt + MAX_EXPIRY_MS).toLocaleString()}.
          </p>
          {error && (
            <p role="alert" className="text-sm">
              {error}
            </p>
          )}
          {status && (
            <p role="status" className="text-sm">
              {status}
            </p>
          )}
          <div className="flex gap-2">
            <button
              type="submit"
              disabled={pending !== null}
              className="rounded-md border px-3 py-2 text-sm"
            >
              {pending === 'create' ? 'Creating…' : 'Create share link'}
            </button>
            <button
              type="button"
              onClick={close}
              className="rounded-md border px-3 py-2 text-sm"
            >
              Cancel
            </button>
          </div>
        </form>
        {shares.isError && (
          <p role="alert" className="text-sm">
            Could not load existing share links.{' '}
            <button type="button" onClick={() => void shares.refetch()}>
              Retry links
            </button>
          </p>
        )}
        {active.length > 0 && (
          <ul aria-label="Active share links" className="grid gap-3">
            {active.map((share) => (
              <li
                key={share.id}
                className="flex flex-wrap items-center gap-2 text-sm"
              >
                <a
                  href={`/artifacts/shared/${encodeURIComponent(share.id)}`}
                  className="underline"
                >
                  Open shared version
                </a>
                <span>
                  Expires {new Date(share.expiresAt).toLocaleString()}
                </span>
                <button
                  type="button"
                  disabled={pending !== null}
                  onClick={() => void copy(share.id)}
                  className="rounded-md border px-2 py-1"
                >
                  Copy share link
                </button>
                <button
                  type="button"
                  disabled={pending !== null}
                  onClick={() => void revoke(share.id)}
                  className="rounded-md border px-2 py-1"
                >
                  {pending === share.id ? 'Revoking…' : 'Revoke share link'}
                </button>
              </li>
            ))}
          </ul>
        )}
      </DialogContent>
    </Dialog>
  );
}
