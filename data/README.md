# data/

Sample and reference input data. Everything lives under `datasets/`:
`scientific_papers/papers.json`, `sample_documents/ai_overview.txt`,
`image_dataset/descriptions.json` and a loose `export.csv`.

These files are fixtures for scripts, demos and evaluations, not a customer data
boundary or a production ingestion contract. Treat them as historical samples
and check the consuming script or test before changing one. Do not add private
documents, credentials or personal data here. The codemap for this area is
[`docs/CODEMAPS/DATA.md`](../docs/CODEMAPS/DATA.md); editing rules are in
[`AGENTS.md`](AGENTS.md).
