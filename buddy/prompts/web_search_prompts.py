WEB_SEARCH_TOOL_PROMPT = """
TOOL_NAME: web_search
TOOL_DESCRIPTION: Search the web for URLs and brief snippets (≤400 chars). Snippets alone almost never answer a real query — always follow with web_fetch to read actual content. Skip search entirely if a URL is already known from the task or memory. Image searches also return img_src, thumbnail, resolution, img_format.

<functions>
  <function>
    <name>search</name>
    <description>Run a web search query.</description>
    <parameters>
      - query       (string,  REQUIRED) — short, specific, no question marks
      - max_results (integer, OPTIONAL, default: 5, max: 20)
      - categories  (string,  OPTIONAL, default: "general") — one of: general | images | videos | news | map | music | it | science | files | social media
      - time_range  (string,  OPTIONAL, default: omitted — SearXNG default) — one of: day | week | month | year
      - region      (string,  OPTIONAL, default: "wt-wt")
      - safe_search (boolean, OPTIONAL, default: true)
    </parameters>
    <destructive>NO</destructive>
    <confirmation_required>NO</confirmation_required>
  </function>
</functions>

<tool_rules>

1. QUERY CONSTRUCTION
   1.1 Write the query as short, specific keywords — not a full sentence or question.
   1.2 Remove filler words: "what is", "how do I", "tell me about".
   1.3 Include version or qualifier when relevant (e.g. "python 3.11 asyncio timeout").

2. CATEGORY SELECTION
   2.1 Default is "general" — use it for most queries.
   2.2 Use "images"  when the user explicitly asks for images, charts, diagrams, or photos.
   2.3 Use "news"    when the user asks for recent events, breaking news, or "latest".
   2.4 Use "videos"  when the user asks for video content.
   2.5 Use "it"      for software, programming, or tech-product queries.
   2.6 Use "science" for academic papers, research, or scientific topics.
   2.7 Use "files"   when the user wants downloadable files (PDFs, ZIPs, datasets).
   2.8 Use "map"     only for location/address lookups.
   2.9 Never invent a category value — unknown values return 0 results.

3. TIME RANGE
   Ask yourself: how recent does this information need to be to actually answer the user's need?

   Omit time_range entirely when recency is not important to the answer — documentation,
   historical facts, explanations, tutorials, or anything where older results are just as
   valid as new ones. This is the default — let SearXNG decide.

   Set time_range only when the value of the information degrades with age:
   - "day"   — information that is only useful if from the last 24 hours
   - "week"  — information that needs to be from the past few days
   - "month" — information where results older than a month would be stale
   - "year"  — information where the current year's context matters

   Decide from the user's actual need, not from surface keywords. A question about
   "how Python asyncio works" needs no time_range even if the user says "quick".
   A question about an ongoing event, a live price, or a breaking situation likely does.
   Never guess — if you are uncertain whether recency matters, omit it.

4. RESULT COUNT
   Quick facts, definitions, current info   → max_results=5   (default)
   Comparisons, tutorials, moderate depth   → max_results=8
   Comprehensive research, multiple angles  → max_results=15

5. SNIPPET SUFFICIENCY — EXCEPTION, NOT THE DEFAULT
   5.1 Snippets are ≤400 chars. They almost never contain a complete answer.
   5.2 Skip fetch ONLY for these narrow cases: live weather, current price/score, today's date, one-word definitions.
   5.3 For everything else — how-to, docs, explanations, events, comparisons — snippets are not enough. Always fetch.

6. FOLLOW-UP FETCH
   6.1 If full article body, documentation, or source code is needed → plan a web_fetch step after this one.
   6.2 Do not fetch: weather, maps, or social media sites — JavaScript renders them; snippets are better.
   6.3 For "images" category results, img_src URLs can be passed directly to the vision tool — no fetch needed.

</tool_rules>

"""
WEB_SEARCH_TOOL_ERROR_PROMPT = """
<error_recovery>
Read only when <errors> is present in context.

1. ERROR CATEGORIES
   A. NO RESULTS — RESULTS is empty or TOTAL_FOUND is 0.
      Broaden the query: remove specific version numbers, qualifiers, or rare terms.
      Try a simpler synonym. Never repeat the same query.

   B. NETWORK / ENGINE ERROR — OK=false, ERROR field is set.
      Retry once with the identical call. If it fails again → status="followup".

   C. UNCLASSIFIED — Do not guess. Return status="followup" with the exact ERROR value and one specific question.

2. RETRY RULES
   2.1 Never repeat the identical call that already failed.
   2.2 After 3 failures on the same query → status="followup".

</error_recovery>"""
