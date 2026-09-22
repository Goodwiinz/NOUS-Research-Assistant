# AGENTS.md

Brand-specific guidance. The repository-root `AGENTS.md` still applies.

## Scope and sources of truth

This directory owns NOUS identity and presentation artifacts: editorial
Markdown/HTML, SVG marks and logos, screenshots, and the product-overview
DOCX. Use the [repository README](../README.md), the [design reference](../DESIGN.md),
the [invalid-pattern audit](../docs/audits/2026-09-14-invalid-patterns-audit.md),
and the consuming page or document as the evidence for an asset change.

- Markdown, HTML, and SVG are source/editorial assets. Preserve SVG identity
  and accessibility metadata when editing them.
- PNG files under `brand/screenshots/` and the DOCX are rendered/exported
  artifacts. They are not interchangeable with source text or SVG, and a
  tracked artifact is not by itself proof of a current product surface.
- `README.md` is a confirmed screenshot consumer: its image paths reference
  files under `brand/screenshots/`. Trace any other consumer before replacing
  an asset.

## Invalid patterns

- Do not bulk-regenerate, overwrite, or normalize binary screenshots or the
  DOCX without naming the consumer, the source revision, and the intended
  replacement relationship.
- Do not strip `viewBox`, title, description, role, or other identity and
  accessibility metadata from SVGs to make a rendering tool accept them.
- Do not replace a source SVG/HTML/Markdown file with a rendered PNG/DOCX, or
  treat a screenshot as current UI evidence without checking its provenance.
- Do not update a screenshot because a path looks unused. Its consumer may be
  a README, showcase, export, or external publication whose reachability is
  not visible in application imports.

## Required workflow

- Before editing, identify the canonical source and every known consumer,
  including `README.md`, and record whether the candidate is source,
  generated/rendered, historical, or unresolved.
- For a replacement, preserve the filename and dimensions when the consumer
  requires them; otherwise update the consumer deliberately in the same
  change. Record capture inputs such as source revision, theme, viewport,
  date, and rendering/export tool as provenance.
- Review rendered output visually at the consumer's intended size and theme.
  Keep a human review of binary changes separate from the text diff; a clean
  text diff cannot prove that a PNG or DOCX is correct.
- Keep alt text, accessible names, SVG identity, and brand tokens aligned with
  the consuming surface. Do not expose private screenshots, document contents,
  or other sensitive artifacts in reports.

## Verification

There is no root-local automated validator for `brand/`. For text/source
assets, run `git diff --check`. For screenshots, SVG renders, and DOCX
exports, perform targeted rendering and visual review with the consumer's
available toolchain; if that toolchain is unavailable, report the visual check
as `NOT RUN` rather than treating a text diff as binary validation.
