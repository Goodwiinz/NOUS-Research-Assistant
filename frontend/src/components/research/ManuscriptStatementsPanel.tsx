'use client';

/**
 * GOO-316 manuscript statements: authorship order and identity, CRediT roles
 * per author, and the disclosure statements, saved as versioned statement
 * sets. An empty field is saved as an explicit `null` and shown as Missing
 * (never filled in). ORCID shows Authenticated only with a retained OAuth
 * receipt; "Verify with ORCID" is offered only on the viewer's own author
 * row. Authorship grants no project permission.
 */

import React from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { BadgeCheck, Plus, Save } from 'lucide-react';
import { useAuth } from '@/hooks/useAuth';
import { projectService } from '@/services/projectService';
import {
  CREDIT_LABELS,
  type ApiAuthorIdentity,
  type ApiAuthorIn,
  type ApiStatementBody,
  type ApiStatementSet,
} from '@/types/api/statements-contract';

const BUTTON =
  'flex items-center gap-1.5 px-2 py-1 bg-muted border border-border rounded text-xs text-muted-foreground hover:border-primary hover:text-primary transition-colors disabled:opacity-50 disabled:cursor-not-allowed focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-ring';
const INPUT =
  'w-full px-1.5 py-0.5 bg-background border border-border rounded text-xs text-foreground focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-ring';

const STATEMENTS: { key: keyof ApiStatementBody; label: string }[] = [
  { key: 'conflicts', label: 'Conflicts of interest' },
  { key: 'ethics', label: 'Ethics' },
  { key: 'limitations', label: 'Limitations' },
  { key: 'data_availability', label: 'Data availability' },
  { key: 'code_availability', label: 'Code availability' },
];

export const statementsQueryKey = (projectId: string) =>
  ['project', projectId, 'statements'] as const;

interface AuthorForm {
  author_key: string;
  display_name: string;
  affiliations: string;
  email: string;
  corresponding: boolean;
  user_id: string | null;
  orcid: string;
  credit_roles: string[];
}

interface Form {
  authors: AuthorForm[];
  text: Record<string, string>;
  funding: string;
  license: string;
}

const blank = (value: string): string | null => value.trim() || null;

function formFrom(tip: ApiStatementSet | null | undefined): Form {
  const body = (tip?.body ?? {}) as ApiStatementBody;
  return {
    authors: (body.authors ?? []).map((a) => ({
      author_key: a.author_key,
      display_name: a.display_name ?? '',
      affiliations: (a.affiliations ?? []).join('; '),
      email: a.email ?? '',
      corresponding: Boolean(a.corresponding),
      user_id: a.user_id ?? null,
      orcid: a.orcid ?? '',
      credit_roles: a.credit_roles ?? [],
    })),
    text: Object.fromEntries(
      STATEMENTS.map(({ key }) => [key, (body[key] as string | null) ?? ''])
    ),
    funding: body.funding?.text ?? '',
    license: body.licenses?.text ?? '',
  };
}

function bodyFrom(form: Form, previous: ApiStatementBody): ApiStatementBody {
  const authors: ApiAuthorIn[] = form.authors.map((a, index) => ({
    author_key: a.author_key,
    order: index + 1,
    display_name: blank(a.display_name),
    affiliations: a.affiliations.trim()
      ? a.affiliations
          .split(';')
          .map((x) => x.trim())
          .filter(Boolean)
      : null,
    email: blank(a.email),
    corresponding: a.corresponding,
    user_id: a.user_id,
    orcid: blank(a.orcid),
    credit_roles: a.credit_roles.length ? a.credit_roles : null,
  }));
  return {
    authors,
    funding: {
      text: blank(form.funding),
      grants: previous.funding?.grants ?? null,
    },
    conflicts: blank(form.text.conflicts ?? ''),
    ethics: blank(form.text.ethics ?? ''),
    limitations: blank(form.text.limitations ?? ''),
    data_availability: blank(form.text.data_availability ?? ''),
    code_availability: blank(form.text.code_availability ?? ''),
    licenses: {
      text: blank(form.license),
      data: previous.licenses?.data ?? null,
      code: previous.licenses?.code ?? null,
    },
  };
}

/** Authenticated only with a receipt: the badge never trusts the status alone. */
export const OrcidBadge: React.FC<{ author: ApiAuthorIdentity }> = ({
  author,
}) => {
  const receipt = author.orcid_receipt;
  if (author.orcid_status === 'authenticated' && receipt) {
    const date = new Date(receipt.token_received_at).toLocaleDateString();
    return (
      <span
        aria-label="ORCID: Authenticated"
        className="px-1 rounded bg-primary/10 text-primary"
      >
        Authenticated ({receipt.environment}, {date})
      </span>
    );
  }
  const label = author.orcid ? 'Unauthenticated' : 'Unknown';
  return (
    <span
      aria-label={`ORCID: ${label}`}
      className="px-1 rounded bg-muted text-muted-foreground"
    >
      {label}
    </span>
  );
};

const ApprovalCell: React.FC<{
  projectId: string;
  tip: ApiStatementSet;
  author: ApiAuthorIdentity;
  own: boolean;
}> = ({ projectId, tip, author, own }) => {
  const queryClient = useQueryClient();
  const [note, setNote] = React.useState('');
  const approve = useMutation({
    mutationFn: (method: 'in_app_self' | 'recorded_attestation') =>
      projectService.approveStatementSet(projectId, tip.id, {
        author_key: author.author_key,
        set_hash: tip.set_hash,
        method,
        attestation_note: method === 'recorded_attestation' ? note : null,
        idempotency_key: crypto.randomUUID(),
      }),
    onSettled: () =>
      queryClient.invalidateQueries({
        queryKey: statementsQueryKey(projectId),
      }),
  });
  if (author.approval) {
    return (
      <span aria-label={`Approval: ${author.author_key}`}>
        {author.approval.method === 'in_app_self'
          ? 'Approved by the author'
          : 'Attested approval recorded'}
      </span>
    );
  }
  return (
    <div className="space-y-1">
      <span className="text-muted-foreground">Not approved</span>
      {own && (
        <button
          type="button"
          onClick={() => approve.mutate('in_app_self')}
          disabled={approve.isPending}
          className={BUTTON}
        >
          Approve as me
        </button>
      )}
      <input
        aria-label={`Attestation note for ${author.display_name ?? author.author_key}`}
        value={note}
        onChange={(e) => setNote(e.target.value)}
        placeholder="How they approved"
        className={INPUT}
      />
      <button
        type="button"
        onClick={() => approve.mutate('recorded_attestation')}
        disabled={!note.trim() || approve.isPending}
        className={BUTTON}
      >
        Record attestation
      </button>
      {approve.isError && (
        <p role="alert" className="text-destructive">
          {approve.error instanceof Error
            ? approve.error.message
            : 'Approval failed'}
        </p>
      )}
    </div>
  );
};

/** Remounted per tip (``key``), so the form starts from the saved version. */
const StatementsEditor: React.FC<{
  projectId: string;
  tip: ApiStatementSet | null;
  roles: string[];
  vocabulary: string;
}> = ({ projectId, tip, roles, vocabulary }) => {
  const userId = useAuth().user?.id;
  const queryClient = useQueryClient();
  const [form, setForm] = React.useState<Form>(() => formFrom(tip));
  const save = useMutation({
    mutationFn: () =>
      projectService.createStatementSet(projectId, {
        body: bodyFrom(form, (tip?.body ?? {}) as ApiStatementBody),
        supersedes_set_id: tip?.id ?? null,
        idempotency_key: crypto.randomUUID(),
      }),
    onSettled: () =>
      queryClient.invalidateQueries({
        queryKey: statementsQueryKey(projectId),
      }),
  });
  const verify = useMutation({
    mutationFn: () => projectService.orcidStart(),
    onSuccess: ({ authorize_url }) => window.location.assign(authorize_url),
  });
  const missing = new Set(tip?.missing_fields ?? []);
  const identities = new Map(
    (tip?.authors ?? []).map((a) => [a.author_key, a])
  );
  const setAuthor = (index: number, patch: Partial<AuthorForm>): void =>
    setForm((f) => ({
      ...f,
      authors: f.authors.map((a, i) => (i === index ? { ...a, ...patch } : a)),
    }));

  return (
    <section aria-label="Manuscript statements" className="space-y-2 text-xs">
      <div className="flex items-center gap-2">
        <h3 className="font-medium text-foreground">Manuscript statements</h3>
        <span className="text-muted-foreground">
          CRediT {vocabulary} · grants no project permission
        </span>
      </div>
      {tip && tip.missing_items.length > 0 && (
        <ul aria-label="Missing fields" className="space-y-0.5">
          {tip.missing_items.map((item) => (
            <li key={item.field} className="text-muted-foreground">
              <span className="font-medium text-destructive">Missing</span>{' '}
              {item.field}: {item.fix}
            </li>
          ))}
        </ul>
      )}
      <table aria-label="Authors" className="w-full">
        <thead className="text-left text-muted-foreground">
          <tr>
            <th>#</th>
            <th>Name</th>
            <th>Affiliations</th>
            <th>Corresponding</th>
            <th>CRediT roles</th>
            <th>ORCID</th>
            <th>Approval</th>
          </tr>
        </thead>
        <tbody>
          {form.authors.map((author, index) => {
            const identity = identities.get(author.author_key);
            const own = Boolean(userId) && author.user_id === userId;
            const name = author.display_name || author.author_key;
            return (
              <tr
                key={author.author_key}
                aria-label={`Author ${index + 1}`}
                className="align-top"
              >
                <td>{index + 1}</td>
                <td>
                  <input
                    aria-label={`Name of author ${index + 1}`}
                    value={author.display_name}
                    onChange={(e) =>
                      setAuthor(index, { display_name: e.target.value })
                    }
                    className={INPUT}
                  />
                </td>
                <td>
                  <input
                    aria-label={`Affiliations of author ${index + 1}`}
                    value={author.affiliations}
                    onChange={(e) =>
                      setAuthor(index, { affiliations: e.target.value })
                    }
                    className={INPUT}
                  />
                </td>
                <td>
                  <input
                    type="checkbox"
                    aria-label={`Author ${index + 1} is corresponding`}
                    checked={author.corresponding}
                    onChange={(e) =>
                      setAuthor(index, { corresponding: e.target.checked })
                    }
                  />
                  {author.corresponding && (
                    <input
                      aria-label={`Email of author ${index + 1}`}
                      value={author.email}
                      onChange={(e) =>
                        setAuthor(index, { email: e.target.value })
                      }
                      className={INPUT}
                    />
                  )}
                </td>
                <td>
                  <select
                    multiple
                    aria-label={`CRediT roles of author ${index + 1}`}
                    value={author.credit_roles}
                    onChange={(e) =>
                      setAuthor(index, {
                        credit_roles: Array.from(
                          e.target.selectedOptions,
                          (o) => o.value
                        ),
                      })
                    }
                    className={INPUT}
                  >
                    {roles.map((role) => (
                      <option key={role} value={role}>
                        {CREDIT_LABELS[role] ?? role}
                      </option>
                    ))}
                  </select>
                </td>
                <td className="space-y-1">
                  <input
                    aria-label={`ORCID of author ${index + 1}`}
                    value={author.orcid}
                    onChange={(e) =>
                      setAuthor(index, { orcid: e.target.value })
                    }
                    placeholder="0000-0000-0000-0000"
                    className={INPUT}
                  />
                  {identity && <OrcidBadge author={identity} />}
                  {own && (
                    <button
                      type="button"
                      onClick={() => verify.mutate()}
                      disabled={verify.isPending}
                      className={BUTTON}
                    >
                      <BadgeCheck className="h-3 w-3" />
                      Verify with ORCID
                    </button>
                  )}
                </td>
                <td>
                  {tip && identity ? (
                    <ApprovalCell
                      projectId={projectId}
                      tip={tip}
                      author={identity}
                      own={own}
                    />
                  ) : (
                    <span className="text-muted-foreground">
                      Save to approve {name}
                    </span>
                  )}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
      <button
        type="button"
        onClick={() =>
          setForm((f) => ({
            ...f,
            authors: [
              ...f.authors,
              {
                author_key: crypto.randomUUID().slice(0, 8),
                display_name: '',
                affiliations: '',
                email: '',
                corresponding: f.authors.length === 0,
                user_id: f.authors.length === 0 ? (userId ?? null) : null,
                orcid: '',
                credit_roles: [],
              },
            ],
          }))
        }
        className={BUTTON}
      >
        <Plus className="h-3 w-3" />
        Add author
      </button>
      <div className="grid gap-1">
        <label className="space-y-0.5">
          <span className="text-foreground">Funding</span>
          {missing.has('funding.text') && (
            <span className="ml-1 text-destructive">Missing</span>
          )}
          <textarea
            value={form.funding}
            onChange={(e) =>
              setForm((f) => ({ ...f, funding: e.target.value }))
            }
            className={INPUT}
          />
        </label>
        {STATEMENTS.map(({ key, label }) => (
          <label key={key} className="space-y-0.5">
            <span className="text-foreground">{label}</span>
            {missing.has(key) && (
              <span className="ml-1 text-destructive">Missing</span>
            )}
            <textarea
              value={form.text[key] ?? ''}
              onChange={(e) =>
                setForm((f) => ({
                  ...f,
                  text: { ...f.text, [key]: e.target.value },
                }))
              }
              className={INPUT}
            />
          </label>
        ))}
        <label className="space-y-0.5">
          <span className="text-foreground">Text licence (SPDX)</span>
          {missing.has('licenses.text') && (
            <span className="ml-1 text-destructive">Missing</span>
          )}
          <select
            value={form.license}
            onChange={(e) =>
              setForm((f) => ({ ...f, license: e.target.value }))
            }
            className={INPUT}
          >
            <option value="">Not chosen</option>
            <option value="CC-BY-4.0">CC-BY-4.0</option>
            <option value="CC-BY-SA-4.0">CC-BY-SA-4.0</option>
            <option value="CC0-1.0">CC0-1.0</option>
          </select>
        </label>
      </div>
      <button
        type="button"
        onClick={() => save.mutate()}
        disabled={save.isPending}
        className={BUTTON}
      >
        <Save className="h-3 w-3" />
        Save statement version
      </button>
      {(save.isError || verify.isError) && (
        <p role="alert" className="text-destructive">
          {[save.error, verify.error]
            .map((e) => (e instanceof Error ? e.message : null))
            .filter(Boolean)
            .join('; ') || 'Request failed'}
        </p>
      )}
    </section>
  );
};

export const ManuscriptStatementsPanel: React.FC<{ projectId: string }> = ({
  projectId,
}) => {
  const { data } = useQuery({
    queryKey: statementsQueryKey(projectId),
    queryFn: () => projectService.listStatements(projectId),
    retry: false,
  });
  const tip = data?.tip ?? null;
  return (
    <StatementsEditor
      key={tip?.id ?? 'none'}
      projectId={projectId}
      tip={tip}
      roles={data?.credit_roles ?? Object.keys(CREDIT_LABELS)}
      vocabulary={data?.credit_vocabulary ?? 'credit/1'}
    />
  );
};

export default ManuscriptStatementsPanel;
