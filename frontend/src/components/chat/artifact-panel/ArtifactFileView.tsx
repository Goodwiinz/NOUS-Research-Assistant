'use client';

import { useEffect, useRef, useState, type ReactElement } from 'react';
import { useQueryClient } from '@tanstack/react-query';
import { useArtifactScope } from '@/hooks/chat/useArtifactScope';
import {
  artifactVersionsKey,
  useArtifactCapabilities,
  useArtifactContent,
} from '@/hooks/chat/useThreadArtifacts';
import {
  artifactService,
  type ArtifactEdit,
  type ArtifactVersion,
} from '@/services/artifactService';
import { useArtifactPanelStore } from '@/store/artifactPanelStore';
import { APIErrorClass } from '@/types/api';
import {
  ArtifactPreview,
  blobToText,
  MAX_TEXT_PREVIEW_BYTES,
} from './ArtifactPreview';

export function isEditableArtifact(version: ArtifactVersion): boolean {
  const mime = version.mimeType.toLowerCase();
  return (
    version.byteSize <= MAX_TEXT_PREVIEW_BYTES &&
    (mime.startsWith('text/') ||
      [
        'application/json',
        'application/javascript',
        'application/xml',
        'application/yaml',
      ].includes(mime))
  );
}

export function ArtifactFileView({
  version,
}: {
  version: ArtifactVersion;
}): ReactElement {
  const scope = useArtifactScope();
  return (
    <ScopedFileView
      key={`${scope}:${version.versionId}`}
      version={version}
      scope={scope}
    />
  );
}

function ScopedFileView({
  version,
  scope,
}: {
  version: ArtifactVersion;
  scope: string | null;
}): ReactElement {
  const capabilities = useArtifactCapabilities();
  const [editing, setEditing] = useState(false);
  if (editing)
    return (
      <ArtifactEditor
        version={version}
        scope={scope}
        onCancel={() => setEditing(false)}
      />
    );
  return (
    <>
      {scope &&
        capabilities.data?.editingEnabled === true &&
        isEditableArtifact(version) && (
          <button
            type="button"
            className="m-3 rounded-md border px-3 py-2 text-sm"
            onClick={() => setEditing(true)}
          >
            Edit
          </button>
        )}
      <ArtifactPreview version={version} />
    </>
  );
}

function ArtifactEditor({
  version,
  scope,
  onCancel,
}: {
  version: ArtifactVersion;
  scope: string | null;
  onCancel: () => void;
}): ReactElement {
  const content = useArtifactContent(version.versionId);
  const client = useQueryClient();
  const [initial, setInitial] = useState<string | null>(null);
  const [text, setText] = useState('');
  const [readFailed, setReadFailed] = useState(false);
  const [pending, setPending] = useState<ArtifactEdit | null>(null);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<'save' | 'conflict' | null>(null);
  const [conflictVersionId, setConflictVersionId] = useState<string | null>(
    null
  );
  const [copyFailed, setCopyFailed] = useState(false);
  const [latest, setLatest] = useState<ArtifactVersion | null>(null);
  const mounted = useRef(true);
  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);
  useEffect(() => {
    let cancelled = false;
    const blob = content.data;
    if (blob && initial === null) {
      if (blob.size > MAX_TEXT_PREVIEW_BYTES) return;
      void blobToText(blob)
        .then((value) => {
          if (!cancelled) {
            setInitial(value);
            setText(value);
          }
        })
        .catch(() => {
          if (!cancelled) setReadFailed(true);
        });
    }
    return () => {
      cancelled = true;
    };
  }, [content.data, initial]);
  const dirty = initial !== null && text !== initial;
  useEffect(() => {
    const guard = (): boolean =>
      !dirty ||
      window.confirm(
        'Discard your unsaved changes? Choose Cancel to keep editing or copy your changes first.'
      );
    useArtifactPanelStore.getState().setNavigationGuard(guard);
    const beforeUnload = (event: BeforeUnloadEvent): void => {
      if (dirty) {
        event.preventDefault();
        event.returnValue = '';
      }
    };
    window.addEventListener('beforeunload', beforeUnload);
    return () => {
      if (useArtifactPanelStore.getState().navigationGuard === guard)
        useArtifactPanelStore.getState().setNavigationGuard(null);
      window.removeEventListener('beforeunload', beforeUnload);
    };
  }, [dirty]);
  const current = (): boolean =>
    mounted.current &&
    useArtifactPanelStore.getState().scope === scope &&
    useArtifactPanelStore.getState().activeVersionId === version.versionId;
  const save = async (): Promise<void> => {
    if (saving || error === 'conflict' || !scope || initial === null) return;
    const request = pending ?? {
      expectedParentVersionId: version.versionId,
      publicationId: crypto.randomUUID(),
      text,
    };
    setPending(request);
    setSaving(true);
    setError(null);
    try {
      const published = await artifactService.editVersion(
        version.artifactId,
        request
      );
      void client.invalidateQueries({
        queryKey: artifactVersionsKey(version.artifactId),
      });
      void client.invalidateQueries({
        predicate: (query) => query.queryKey.includes('artifacts'),
      });
      if (!current()) return;
      useArtifactPanelStore.getState().setNavigationGuard(null);
      useArtifactPanelStore.getState().openArtifact(
        {
          kind: 'generated',
          artifactId: published.artifactId,
          versionId: published.versionId,
          title: published.title,
        },
        { scope, source: 'user' }
      );
      setPending(null);
      setInitial(request.text);
      onCancel();
    } catch (caught) {
      if (!current()) return;
      if (caught instanceof APIErrorClass && caught.error.status_code === 409) {
        setError('conflict');
        const currentVersionId = caught.error.details?.current_version_id;
        if (
          typeof currentVersionId === 'string' &&
          /^[a-f0-9-]{36}$/i.test(currentVersionId)
        )
          setConflictVersionId(currentVersionId);
        setPending(null);
        try {
          const versions = await client.fetchQuery({
            queryKey: [...artifactVersionsKey(version.artifactId), scope],
            queryFn: ({ signal }) =>
              artifactService.listVersions(version.artifactId, signal),
            staleTime: 0,
          });
          if (current()) setLatest(versions[versions.length - 1] ?? null);
        } catch {
          /* The user's buffer remains available even if latest cannot load. */
        }
      } else {
        setError('save');
      }
    } finally {
      if (mounted.current) setSaving(false);
    }
  };
  if (
    readFailed ||
    content.isError ||
    (content.data?.size ?? 0) > MAX_TEXT_PREVIEW_BYTES
  )
    return (
      <div role="alert" className="p-4">
        Could not load this file for editing.{' '}
        <button type="button" onClick={onCancel}>
          Back to source
        </button>
      </div>
    );
  if (initial === null)
    return (
      <p role="status" className="p-4">
        Loading editor…
      </p>
    );
  const tooLarge =
    new TextEncoder().encode(text).byteLength > MAX_TEXT_PREVIEW_BYTES;
  return (
    <div className="flex h-full min-h-0 flex-col gap-3 p-4">
      <label className="text-sm" htmlFor="artifact-editor">
        Edit file contents
      </label>
      <textarea
        id="artifact-editor"
        aria-label="Edit file contents"
        value={text}
        disabled={saving || pending !== null}
        onChange={(event) => setText(event.target.value)}
        className="min-h-64 flex-1 resize-y rounded-md border bg-background p-3 font-mono text-sm"
      />
      {tooLarge && <p role="alert">Changes exceed the 2 MB limit.</p>}
      {error === 'save' && (
        <p role="alert">
          Could not confirm this save. Retry to check the same publication. Your
          changes are kept.
        </p>
      )}
      {error === 'conflict' && (
        <div role="alert">
          A newer version is available. Your changes are kept; copy them before
          loading another version.
          {(latest || conflictVersionId) && (
            <p>Latest version: {latest?.versionId ?? conflictVersionId}</p>
          )}
        </div>
      )}
      {copyFailed && (
        <p role="alert">
          Copy failed. Select and copy your changes from the editor.
        </p>
      )}
      <div className="flex flex-wrap gap-2">
        <button
          type="button"
          onClick={() => void save()}
          disabled={
            saving || tooLarge || error === 'conflict' || (!dirty && !pending)
          }
          className="rounded-md border px-3 py-2 text-sm"
        >
          {saving ? 'Saving…' : pending ? 'Retry save' : 'Save new version'}
        </button>
        <button
          type="button"
          onClick={() => {
            void navigator.clipboard
              .writeText(text)
              .catch(() => setCopyFailed(true));
          }}
          className="rounded-md border px-3 py-2 text-sm"
        >
          Copy changes
        </button>
        <button
          type="button"
          onClick={() => {
            const guard = useArtifactPanelStore.getState().navigationGuard;
            if (!guard || guard()) onCancel();
          }}
          className="rounded-md border px-3 py-2 text-sm"
        >
          Cancel editing
        </button>
        {error === 'conflict' && latest && (
          <button
            type="button"
            className="rounded-md border px-3 py-2 text-sm"
            onClick={() =>
              useArtifactPanelStore.getState().openArtifact(
                {
                  kind: 'generated',
                  artifactId: latest.artifactId,
                  versionId: latest.versionId,
                  title: latest.title,
                },
                { scope, source: 'user' }
              )
            }
          >
            Load latest version
          </button>
        )}
      </div>
    </div>
  );
}
