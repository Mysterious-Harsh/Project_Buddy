# 🔒 LOCKED — web_fetch_prompts.py
# Contract: tool_call schema → { urls, max_chars }
# Result fields: OK, RESULTS [{url, title, content, size_chars, error}], TOTAL_FETCHED, ERROR
# Allowed: bug fixes, guidance text improvements.
# Not allowed: adding/removing schema fields, changing result field names.

WEB_FETCH_TOOL_PROMPT = """
WEB FETCH TOOL  —  fetch full page content

Downloads and extracts full readable text from one or more URLs.
Use this AFTER web_search when snippets are not enough.

HOW TO USE WITH web_search:
  - Step 1: web_search  → get RESULTS list (titles, urls, snippets)
  - Step 2: web_fetch   → pass the best URLs from step 1 as input
  Always use URLs from prior search results. Never invent URLs.

WHEN TO USE:
  Full article body, documentation pages, blog posts, source code pages,
  news articles where the snippet was incomplete.

WHEN NOT TO USE:
  Weather sites, maps, social media, dashboards — these use JavaScript
  and will return empty or broken content. Use search snippets instead.

SCHEMA:
  urls       : list of strings  (required) — 1 to 5 URLs from prior search results
  max_chars  : int  (default 8000 per URL, max 20000)

OUTPUT:
  OK            : bool
  RESULTS       : [
                    {
                      url       : string,
                      title     : string,
                      content   : string  — extracted plain text,
                      size_chars: int,
                      error     : string | null
                    }
                  ]
  TOTAL_FETCHED : int   — number of URLs successfully fetched
  ERROR         : string | null
"""

WEB_FETCH_ERROR_RECOVERY_PROMPT = """
WEB FETCH ERROR RECOVERY

Check each result's error field individually. Fix before retrying.

  empty content / tiny  → site uses JavaScript (maps, weather, social);
                          do NOT retry — use search snippets instead
  HTTP 403/blocked      → site blocks scraping; skip this URL, try another from search results
  timeout               → retry once with fewer URLs; if fails again skip
  3 failures            → status="followup"
"""

tool_call_format = """
{
  "urls": ["https://example.com/article"],
  "max_chars": 8000
}
"""
