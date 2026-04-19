# 🔒 LOCKED — web_search_prompts.py
# Contract: tool_call schema → { query, max_results, region, safe_search }
# Result fields: OK, ENGINE, QUERY, RESULTS [{title, url, snippet}], TOTAL_FOUND, ERROR
# Allowed: bug fixes, guidance text improvements.
# Not allowed: adding/removing schema fields, changing result field names.

WEB_SEARCH_TOOL_PROMPT = """
<tool_description>
WEB SEARCH TOOL  —  search only

Returns a ranked list of results: title, URL, and a short snippet (≤400 chars).
</tool_description>

<when_enough>
WHEN THIS IS ENOUGH:
  Snippets contain the full answer for weather, prices, scores, short facts.
  Read the snippets — if the answer is there, you are done. No fetch needed.
</when_enough>

<how_many_results>
HOW MANY RESULTS TO REQUEST:
  General queries (quick facts, definitions, how-to, current info)  → max_results=5
  Moderate queries (comparisons, tutorials, moderate research)       → max_results=8
  Deep research (comprehensive overview, multiple perspectives)      → max_results=15

  Default to 5 unless the user's intent is clearly research-oriented.
</how_many_results>

<when_to_fetch>
WHEN TO ALSO USE web_fetch:
  If you need the full article body, documentation, or source code,
  plan a web_fetch step AFTER this step and pass these results as input.
  Do not fetch weather/maps/social sites — they use JavaScript; snippets are better.
</when_to_fetch>

<call_schema>
SCHEMA:
  query        : string  (required) — short and specific, no question marks
  max_results  : int     (default 5, max 20)
  region       : string  (default "wt-wt")
  safe_search  : bool    (default true)
</call_schema>

<result_fields>
OUTPUT:
  OK           : bool
  ENGINE       : "searxng" | "duckduckgo"
  QUERY        : string
  RESULTS      : [ { title, url, snippet } ]   — snippet ≤ 400 chars
  TOTAL_FOUND  : int
  ERROR        : string | null
</result_fields>
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
