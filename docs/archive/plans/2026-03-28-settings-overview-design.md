# Settings Overview Hub Design

**Date:** 2026-03-28
**Route:** `/settings`
**Related routes:** `/settings/organization`, `/settings/api-keys`

## Goal

Replace the current sparse, centered settings panel with a SaaS-style settings overview hub that matches the NOUS product model: account context, workspace governance, security posture, usage visibility, and developer access.

## Product Context

The design is grounded in the current NOUS product context documented in:

- `memory/projects/nous-platform.md`
- `memory/glossary.md`
- `docs/frontend/NOUS-UX-UI-Critique.md`
- `brand/NOUS-Project-Structure.md`
- `brand/NOUS-API-Reference.md`

These references establish that NOUS is not a generic personal productivity app. It is an enterprise multimodal intelligence platform with:

- workspaces and tenant context
- RBAC and audit/security concerns
- external AI provider connectivity
- API and developer access needs
- documented organization, workspace, and security endpoints

## Design References

### Internal references

- `docs/frontend/NOUS-UX-UI-Critique.md`
  - Settings is explicitly called out as too sparse and in need of more information density.
  - The critique says to preserve NOUS's monospaced uppercase header system, rounded card treatment, and strong dark/light parity.
- `docs/plans/2026-02-18-settings-page-design.md`
  - Useful as history for the current implementation.
  - Its centered split-panel concept is now too limited for the current NOUS product shape.
- `brand/NOUS-Project-Structure.md`
  - Source of truth for NOUS brand palette and typography.

### External reference patterns

- GitHub account/settings patterns for overview plus deeper configuration areas
- Slack-style notification and preference grouping
- Vercel-style account, team, and access-token organization

These references are used for structure and interaction patterns, not for copying brand or layout.

## Visual Direction

The page should follow the current NOUS interface language rather than generic shadcn dashboard defaults.

### Keep

- monospaced uppercase section labels where they create hierarchy
- NOUS gold accent usage via theme variables
- subtle bordered cards with consistent radius
- strong dark/light mode parity
- dashboard-shell layout inside the existing app frame

### Avoid

- modal-style centered settings shell
- empty placeholder panels with large unused areas
- generic pastel SaaS styling disconnected from NOUS
- personal-only framing that ignores workspace and platform administration

## Information Architecture

### `/settings`

Acts as the overview hub.

Sections:

1. Page header
   - Title: `Settings`
   - Description: concise platform-oriented copy such as "Manage your account, workspace governance, model access, and platform controls."
   - Context summary for user and workspace

2. Quick status row
   - Workspace
   - Role
   - Plan
   - Security
   - API access

3. Settings overview card grid
   - Profile & Preferences
   - Workspace & Access
   - Security & Compliance
   - Usage & Billing
   - Developer Access
   - Connected Systems

4. Inline personal preferences block
   - theme
   - compact mode
   - email notifications
   - desktop notifications
   - other low-risk local preferences

### `/settings/organization`

Deeper route for:

- organization/workspace identity
- members and roles
- usage and billing
- workspace policies
- security/compliance posture

### `/settings/api-keys`

Deeper route for:

- API keys
- scopes
- token rotation guidance
- provider access
- integration/developer controls

## Content Model For `/settings`

### Header

- Primary heading: `Settings`
- Supporting copy grounded in the NOUS platform model
- Small context strip with current email and workspace label where available

### Quick Status Strip

Each item is a compact card or metric tile.

- `Workspace`
  - current workspace name or default workspace
- `Role`
  - admin/member/operator summary
- `Plan`
  - current tier and current period usage snapshot
- `Security`
  - posture summary such as audit enabled / encryption active / review recommended
- `API Access`
  - token count or provider readiness summary

### Overview Cards

#### Profile & Preferences

Purpose:
- user identity and quick personal defaults

Content:
- primary email
- display name placeholder
- timezone placeholder
- preference summary

Actions:
- quick edit affordances for local-only preferences where safe

#### Workspace & Access

Purpose:
- communicate tenant/workspace context and access model

Content:
- workspace name
- member/role summary
- access/governance summary

Action:
- link to `/settings/organization`

#### Security & Compliance

Purpose:
- surface platform trust and administrative control

Content:
- authentication/session summary
- audit/compliance summary
- encryption/HITL policy summary

Action:
- link to `/settings/organization`

#### Usage & Billing

Purpose:
- make plan and capacity visible without forcing a drill-down

Content:
- plan tier
- credits or compute usage
- reset or renewal date
- storage/documents summary

Action:
- link to `/settings/organization`

#### Developer Access

Purpose:
- surface API and token posture

Content:
- token count placeholder
- last rotation placeholder
- scope summary

Action:
- link to `/settings/api-keys`

#### Connected Systems

Purpose:
- reflect NOUS's platform integrations

Content:
- AI provider readiness summary
- external connector summary
- integration state placeholder

Action:
- link to `/settings/api-keys`

### Personal Preferences Block

This block stays on the overview page because it is low-friction and user-specific.

Candidate controls:

- theme
- density / compact mode
- email notifications
- desktop notifications
- optional default workspace behavior

These controls can begin as local UI state if persistence is not already implemented.

## Data Strategy

Use live data only where the codebase already provides it cleanly.

### Live now

- authenticated user email from `useAuth()`
- route navigation to existing settings destinations
- theme-aware styling from `frontend/app/globals.css`

### Placeholder or derived summary for this pass

- plan tier
- credits/usage snapshot
- security score/posture text
- token count
- provider connection status
- workspace/member counts if no stable frontend source exists in settings yet

The page should look complete, but it must not pretend to save or manage server state that does not yet exist in the frontend.

## Interaction Rules

- `/settings` is a normal in-app page, not a dismissible modal
- all primary deep-link cards should clearly indicate their destination
- preserve strong keyboard focus states and semantic heading structure
- responsive layout should stack cleanly on mobile
- avoid overwhelming users with too many editable controls in one page

## Testing Expectations

- route renders inside the dashboard shell without modal affordances
- overview cards and CTAs are visible on desktop and mobile
- tests assert key headings and deep-link actions
- page remains accessible via role/name-based queries

## Notes On Existing Docs

- `docs/plans/2026-02-18-settings-page-design.md` describes the current centered-panel design and should be treated as superseded by this document.
- `memory/projects/nous-platform.md` still mentions an older accent framing; active visual direction should follow `docs/frontend/NOUS-UX-UI-Critique.md` and `frontend/app/globals.css`, which reflect the current NOUS gold-led brand implementation.
