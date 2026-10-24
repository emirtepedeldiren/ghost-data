# Development tools (not used by the frozen run)

| Tool | Purpose |
|---|---|
| `list_models.py` | Lists the Gemini models available to the API key in `.env` (model IDs only, never the key); used to choose models (D10, D17). Makes one API call. |
| `vision_compare.py` | Re-runs the D17 screenshot-model comparison (two vision models on all 11 PNGs, scored against the catalog claims). Makes live API calls; results are in `evaluation_report.md` §5.4. |
