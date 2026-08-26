# Current scope

The detector uses the EUMETNET OPERA DBZH maximum-reflectivity composite as its sole primary observation source for Sofia. Craiova's OPERA volume is retained as an experimental vertical-profile context source.

When web research is disabled, send one Telegram **Severe Reflectivity Alert** per tracked storm when a fresh, confirmed OPERA track has a maximum observed band of at least 65 dBZ and its medium-or-higher confidence trajectory enters the configured target radius within 30 minutes.

When web research is enabled, send potentially problematic, target-bound tracks to the internet-enabled risk agent with their radar, motion, location, target, and available experimental-volume context. The agent must return structured JSON with a 0–10 hail-risk rating and description. Alert only if its rating is strictly greater than the configured threshold (default `7`), using that description as the alert content. Do not mutate alert state or send a message from stale, incomplete, or failed research data.

The system reports radar reflectivity signatures, not official meteorological warnings or confirmed hail.
