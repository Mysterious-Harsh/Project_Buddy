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
  - Step 2: web_fetch   → pick the best URLs from step 1 based on title + snippet relevance
  Always use URLs from prior search results. Never invent URLs.

HOW MANY URLS TO FETCH:
  General queries (quick facts, how-to, single topic)               → fetch 1–2 best URLs
  Moderate queries (comparisons, tutorials, some depth needed)       → fetch 2–3 URLs
  Deep research (comprehensive, multiple perspectives, user asked for more) → fetch 3–5 URLs

  Default to 1–2 for general queries. Only fetch more when the user
  explicitly asks for deeper information or the topic genuinely requires it.

HOW TO PICK THE BEST URLs:
  Read the title and snippet of each search result before choosing.
  Prefer URLs whose title and snippet directly address the user's question.
  Avoid: social media, weather widgets, maps, login-gated pages, forums
         with low-quality answers, and aggregator spam sites.
  If two results look equally relevant, prefer the one from a more
  authoritative domain (official docs, reputable publications).
  Never pick URLs at random — always justify by title/snippet match.

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
