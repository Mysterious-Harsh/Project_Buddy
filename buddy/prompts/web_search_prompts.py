WEB_SEARCH_TOOL_PROMPT = """
TOOL_NAME: web_search
TOOL_DESCRIPTION: Search the web. Returns title, URL, and snippet (≤400 chars) per result.

<functions>
  <function>
    <name>search</name>
    <description>Run a web search query.</description>
    <parameters>
      - query       (string,  REQUIRED) — short, specific, no question marks
      - max_results (integer, OPTIONAL, default: 5, max: 20)
      - region      (string,  OPTIONAL, default: "wt-wt")
      - safe_search (boolean, OPTIONAL, default: true)
    </parameters>
    <returns>OK, ENGINE, QUERY, RESULTS [{title, url, snippet}], TOTAL_FOUND, ERROR</returns>
    <destructive>NO</destructive>
    <confirmation_required>NO</confirmation_required>
  </function>
</functions>

<tool_rules>

1. QUERY CONSTRUCTION
   1.1 Write the query as short, specific keywords — not a full sentence or question.
   1.2 Remove filler words: "what is", "how do I", "tell me about".
   1.3 Include version or qualifier when relevant (e.g. "python 3.11 asyncio timeout").

2. RESULT COUNT
   Quick facts, definitions, current info   → max_results=5   (default)
   Comparisons, tutorials, moderate depth   → max_results=8
   Comprehensive research, multiple angles  → max_results=15

3. SNIPPET SUFFICIENCY
   3.1 Read all snippets before planning a follow-up fetch.
   3.2 If the answer is fully contained in snippets → you are done. No fetch needed.
   3.3 Snippets are enough for: weather, prices, scores, dates, short definitions.

4. FOLLOW-UP FETCH
   4.1 If full article body, documentation, or source code is needed → plan a web_fetch step after this one.
   4.2 Do not fetch: weather, maps, or social media sites — JavaScript renders them; snippets are better.

</tool_rules>

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

</error_recovery>
"""
