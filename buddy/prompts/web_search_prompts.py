# 🔒 LOCKED — web_search_prompts.py
# Contract: tool_call schema → { query, max_results, region, safe_search }
# Result fields: OK, ENGINE, QUERY, RESULTS [{title, url, snippet}], TOTAL_FOUND, ERROR
# Allowed: bug fixes, guidance text improvements.
# Not allowed: adding/removing schema fields, changing result field names.

WEB_SEARCH_TOOL_PROMPT = """
WEB SEARCH TOOL  —  search only

Returns a ranked list of results: title, URL, and a short snippet (≤400 chars).

WHEN THIS IS ENOUGH:
  Snippets contain the full answer for weather, prices, scores, short facts.
  Read the snippets — if the answer is there, you are done. No fetch needed.

WHEN TO ALSO USE web_fetch:
  If you need the full article body, documentation, or source code,
  plan a web_fetch step AFTER this step and pass these results as input.
  Do not fetch weather/maps/social sites — they use JavaScript; snippets are better.

SCHEMA:
  query        : string  (required) — short and specific, no question marks
  max_results  : int     (default 5, max 20)
  region       : string  (default "wt-wt")
  safe_search  : bool    (default true)

OUTPUT:
  OK           : bool
  ENGINE       : "searxng" | "duckduckgo"
  QUERY        : string
  RESULTS      : [ { title, url, snippet } ]   — snippet ≤ 400 chars
  TOTAL_FOUND  : int
  ERROR        : string | null
"""

WEB_SEARCH_ERROR_RECOVERY_PROMPT = """
WEB SEARCH ERROR RECOVERY

Read ERROR field. Fix before retrying. Never repeat identical call.

  no results    → broaden query, remove specific terms, try simpler keywords
  network error → retry once; if fails again status="followup"
  3 failures    → status="followup"
"""

tool_call_format = """
{
  "query": "weather saint john NB",
  "max_results": 5
}
"""
