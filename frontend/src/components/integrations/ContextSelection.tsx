'use client';

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useState, type ReactElement } from 'react';

import { useAuth } from '@/hooks/useAuth';
import { integrationContextService } from '@/services/integrationContextService';
import type { ApiContextOptions } from '@/types/api/integration-context-contract';

export const MAX_SHARED_MEMORIES = 25;
export const MAX_SHARED_SKILLS = 32;
type ScopedSave = SelectionSave & { ownerId: string; requestId: string };
type SelectionSave = {
  memoryIds: string[];
  skillVersionIds?: string[];
  refreshSkills: boolean;
};

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
    mutationFn: ({
      memoryIds,
      skillVersionIds,
      refreshSkills,
      requestId: savedRequestId,
    }: ScopedSave) =>
      skillVersionIds === undefined
        ? integrationContextService.save(savedRequestId, memoryIds)
        : integrationContextService.save(
            savedRequestId,
            memoryIds,
            skillVersionIds,
            refreshSkills
          ),
    onSuccess: (saved, initiating) =>
      queryClient.setQueryData(
        ['integration-context', initiating.ownerId, initiating.requestId],
        saved
      ),
  });

  const currentSave =
    save.variables?.ownerId === user?.id &&
    save.variables?.requestId === requestId;

  return (
    <section className="mx-auto max-w-2xl space-y-6 p-6">
      <h1 className="text-2xl font-semibold">Share project memories</h1>
      <p>
        Choose which of your memories the connected device may read. Nothing is
        shared until you select it and save. You can also choose approved
        project skill versions below.
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
          key={`${user?.id}:${options.data.request_id}:${options.data.selected_memory_ids.join(',')}:${options.data.selected_skill_version_ids?.join(',')}:${options.data.skill_snapshot_status}:${options.data.skill_snapshot_expires_at}`}
          data={options.data}
          saving={currentSave && save.isPending}
          onSave={(selection) => {
            if (user?.id)
              save.mutate({ ...selection, ownerId: user.id, requestId });
          }}
        />
      )}
      {currentSave && save.isError && (
        <p role="alert">
          The selection was not saved. It may exceed the sharing limit or be out
          of date; reload and try again.
        </p>
      )}
      {currentSave && save.isSuccess && options.data && (
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
  onSave: (selection: SelectionSave) => void;
}): ReactElement {
  const [checked, setChecked] = useState<Set<string>>(
    () => new Set(data.selected_memory_ids)
  );
  const [checkedSkills, setCheckedSkills] = useState<Set<string>>(
    () => new Set(data.selected_skill_version_ids ?? [])
  );
  const full = checked.size >= MAX_SHARED_MEMORIES;
  const submit = (refreshSkills = false): void =>
    onSave({
      memoryIds: data.memories.map((m) => m.id).filter((id) => checked.has(id)),
      ...(data.skills !== undefined
        ? { skillVersionIds: [...checkedSkills] }
        : {}),
      refreshSkills,
    });
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
        submit();
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
      {data.skills !== undefined && (
        <fieldset className="space-y-2">
          <legend className="font-semibold">
            Project skills ({checkedSkills.size} of at most {MAX_SHARED_SKILLS}{' '}
            selected)
          </legend>
          <p>
            Choose exact versions to share. The device can load up to three
            skills per saved selection. Skill instructions stay fixed when the
            project changes.
          </p>
          {data.skills.length === 0 && (
            <p>No approved project skills are available.</p>
          )}
          {data.skills.map((skill) => (
            <label key={skill.version_id} className="flex items-start gap-2">
              <input
                type="checkbox"
                checked={checkedSkills.has(skill.version_id)}
                disabled={
                  checkedSkills.size >= MAX_SHARED_SKILLS &&
                  !checkedSkills.has(skill.version_id)
                }
                onChange={() =>
                  setCheckedSkills((previous) => {
                    const next = new Set(previous);
                    if (next.has(skill.version_id))
                      next.delete(skill.version_id);
                    else {
                      for (const option of data.skills ?? []) {
                        if (option.name === skill.name)
                          next.delete(option.version_id);
                      }
                      next.add(skill.version_id);
                    }
                    return next;
                  })
                }
              />
              <span>
                {skill.name} (v{skill.version}) — {skill.description}
              </span>
            </label>
          ))}
          {data.skill_snapshot_status === 'unavailable' && (
            <div className="space-y-2">
              <p>
                Frozen skills are unavailable. Review the selected versions,
                then refresh to share them again and start a new three-skill
                allowance. Your memories stay selected.
              </p>
              <button
                type="button"
                disabled={saving}
                className="rounded border px-4 py-2"
                onClick={() => submit(true)}
              >
                Refresh selected skills
              </button>
            </div>
          )}
          {data.skill_snapshot_status === 'ready' &&
            data.skill_snapshot_expires_at && (
              <p>
                Selected skills expire on{' '}
                {new Date(data.skill_snapshot_expires_at).toLocaleDateString()}.
              </p>
            )}
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
