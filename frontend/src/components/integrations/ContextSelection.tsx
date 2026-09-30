'use client';

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useState, type ReactElement } from 'react';

import { useAuth } from '@/hooks/useAuth';
import { integrationContextService } from '@/services/integrationContextService';
import type { ApiContextOptions } from '@/types/api/integration-context-contract';

export const MAX_SHARED_MEMORIES = 25;

/**
 * Lets the owner choose which of their project memories one connected device
 * may read. Nothing is shared until a memory is checked and saved.
 */
export function ContextSelection({
  requestId,
}: {
  requestId: string;
}): ReactElement {
  const { user } = useAuth();
  const queryClient = useQueryClient();
  const queryKey = ['integration-context', user?.id, requestId];
  const options = useQuery({
    queryKey,
    enabled: Boolean(user?.id && requestId),
    queryFn: () => integrationContextService.options(requestId),
    retry: false,
    staleTime: 0,
  });
  // Owned here, not by the form, so the result survives the form remounting
  // on the new saved selection.
  const save = useMutation({
    mutationFn: (memoryIds: string[]) =>
      integrationContextService.save(requestId, memoryIds),
    onSuccess: (saved) => queryClient.setQueryData(queryKey, saved),
  });

  return (
    <section className="mx-auto max-w-2xl space-y-6 p-6">
      <h1 className="text-2xl font-semibold">Share project memories</h1>
      <p>
        Choose which of your memories the connected device may read. Nothing is
        shared until you check a memory and save.
      </p>
      {options.isPending && <p role="status">Loading memories…</p>}
      {options.isError && (
        <p role="alert">
          These options could not be loaded. The connection may have been
          revoked or may belong to another account.
        </p>
      )}
      {options.data && (
        <SelectionForm
          // A new saved selection (here or in another tab) resets the checks.
          key={`${options.data.request_id}:${options.data.selected_memory_ids.join(',')}`}
          data={options.data}
          saving={save.isPending}
          onSave={(ids) => save.mutate(ids)}
        />
      )}
      {save.isError && (
        <p role="alert">
          The selection was not saved. It may exceed the sharing limit or be out
          of date; reload and try again.
        </p>
      )}
      {save.isSuccess && options.data && (
        <p role="status">
          Saved. The connected device can read{' '}
          {options.data.selected_memory_ids.length} selected memories.
        </p>
      )}
    </section>
  );
}

function SelectionForm({
  data,
  saving,
  onSave,
}: {
  data: ApiContextOptions;
  saving: boolean;
  onSave: (memoryIds: string[]) => void;
}): ReactElement {
  const [checked, setChecked] = useState<Set<string>>(
    () => new Set(data.selected_memory_ids)
  );
  const full = checked.size >= MAX_SHARED_MEMORIES;
  const toggle = (id: string): void =>
    setChecked((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });

  return (
    <form
      className="space-y-4"
      onSubmit={(event) => {
        event.preventDefault();
        // Selected memories are always listed, so list order is safe here.
        onSave(data.memories.map((m) => m.id).filter((id) => checked.has(id)));
      }}
    >
      <p>
        Project: {data.project_label} ({data.project_id})
      </p>
      {data.memories.length === 0 ? (
        <p>You have no memories in this project yet.</p>
      ) : (
        <fieldset className="space-y-2">
          <legend className="font-semibold">
            Memories ({checked.size} of at most {MAX_SHARED_MEMORIES} selected)
          </legend>
          {data.memories.map((memory) => (
            <label key={memory.id} className="flex items-start gap-2">
              <input
                type="checkbox"
                checked={checked.has(memory.id)}
                disabled={full && !checked.has(memory.id)}
                onChange={() => toggle(memory.id)}
              />
              <span className="whitespace-pre-wrap">{memory.content}</span>
            </label>
          ))}
        </fieldset>
      )}
      <button
        type="submit"
        disabled={saving}
        className="rounded bg-primary px-4 py-2 text-primary-foreground disabled:opacity-50"
      >
        Save selection
      </button>
    </form>
  );
}
