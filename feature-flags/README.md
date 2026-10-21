# feature-flags/

LaunchDarkly-shaped flag configuration. `launchdarkly-config.json` defines the
project key, environment, eight flags (`multimodal_processing`,
`advanced_analytics`, `evaluation_metrics`, `real_time_processing`,
`experimental_ui`, `ai_model_optimization`, `file_upload_limits`,
`search_result_count`) and segments. `examples/ab_testing_usage_examples.py` is
an illustrative script.

**The backend does not load this file.** `backend/src/services/infrastructure/feature_flags.py`
looks for `../../feature-flags/launchdarkly-config.json` relative to its own
folder, which resolves to `backend/src/feature-flags/` (nonexistent), so it falls
back to its hard-coded defaults. Treat the JSON as reference data until a real
consumer exists. Details and editing rules: [`AGENTS.md`](AGENTS.md).
