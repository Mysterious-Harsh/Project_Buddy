# 🔒 LOCKED — web_search_prompts.py
# Contract: tool_call schema → { action, query, max_results, region, safe_search, url, max_chars }
# Result fields: OK, ERROR, ACTION, RESULTS, TOTAL_FOUND, URL, CONTENT, TITLE, SIZE_CHARS
# Allowed: bug fixes, error recovery guidance.
# Not allowed: adding/removing actions, changing result field names.

WEB_SEARCH_TOOL_PROMPT = """
WEB SEARCH TOOL

Actions: search | fetch

RULES:
- Use search to find information from the web (DuckDuckGo).
- Use fetch to read the full text of a specific URL.
- Never make up URLs — only use URLs from PRIOR_OUTPUTS or user message.
- Keep queries short and specific. No question marks in queries.
- max_results default is 5. Never request more than 20.

SCHEMA:
  action  : required — "search" or "fetch"

  search  → query (string, required), max_results (default 5), region (default "wt-wt"), safe_search (default true)
  fetch   → url (string, required), max_chars (default 8000)

RESULT KEY FIELDS:
  OK, ERROR, ACTION,
  RESULTS  (list of {title, url, snippet})  — search only
  TOTAL_FOUND                               — search only
  URL, CONTENT, TITLE, SIZE_CHARS          — fetch only
"""


WEB_SEARCH_ERROR_RECOVERY_PROMPT = """
WEB SEARCH ERROR RECOVERY

Read ERROR field. Fix before retrying. Never repeat identical call.

  network error     → retry once; if fails again status="followup"
  no results        → broaden query, remove specific terms, try simpler keywords
  fetch blocked     → site blocked scraping; try a different URL from search results
  3 failures        → status="followup"
"""


tool_call_format = """
{
  "action": "search",
  "query": "python async tutorial",
  "max_results": 5
}
"""
