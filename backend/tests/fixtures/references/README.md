# Reference export fixtures (GOO-317)

Independent validators for the CSL JSON and RIS serializers in
`src/services/research/bibliography_service.py`. Nothing here imports `src`.

- `csl-data.schema.json`: the official CSL-data JSON Schema, vendored
  unmodified from `citation-style-language/schema`, release tag `v1.0.2`
  (commit `506040022f6c37846343edd36658b23e85b5b8ff`, path
  `schemas/input/csl-data.json`, git blob
  `085a8124fded125c0f765ac56e340fbc7be75496`, sha256
  `257f86d4c3f19e15c7208657512ad4c9559a3b0d83b03360ed006c5f02d2977c`).
  License: MIT, Copyright (c) 2007-2018 Citation Style Language and
  contributors. Vendored 2026-10-01; tests are offline.
- `ris_reader.py`: a strict test-only RIS reader written from the RIS tag
  grammar (`TAG  - value` lines, `TY` ... `ER` records, known tags only).
- `records_v1.json`: canonical reference records covering a journal article
  with Unicode authors, a corporate author without a year, an arXiv-only
  preprint, an untitled record whose citation had a snippet, an unknown
  type and a Japanese title.
