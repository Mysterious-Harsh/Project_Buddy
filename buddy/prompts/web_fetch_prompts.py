WEB_FETCH_TOOL_PROMPT = """
TOOL_NAME: web_fetch
TOOL_DESCRIPTION: Fetch full readable text from URLs, or download binary files to disk. Use after web_search by passing URLs from search results — or use directly when a URL is already known from the task, memory, or prior step (no search needed in that case). Set visual_analysis=true when the task involves charts, graphs, diagrams, or visual data.

<functions>
  <function>
    <name>fetch</name>
    <description>Download and extract plain text from one or more URLs. Optionally extracts and analyses images found on the page.</description>
    <parameters>
      - urls            (array,   REQUIRED) — 1 to 4 URL strings; must start with http:// or https://
      - max_chars       (integer, OPTIONAL, default: 8000, max: 20000) — per-URL text content cap. Understand the query intent: if it requires "deep research" or in-depth information on a specific area, increase this up to the max. Otherwise, keep it between 8000 and 12000.
      - visual_analysis (boolean, OPTIONAL, default: false) — extract and analyse images found on the page; set true when charts, graphs, diagrams, or visual data are relevant
      - max_images      (integer, OPTIONAL, default: 3, max: 8) — max images to analyse per URL; raise only when the user explicitly needs comprehensive visual coverage
    </parameters>
    <destructive>NO</destructive>
    <confirmation_required>NO</confirmation_required>
  </function>

  <function>
    <name>download</name>
    <description>Download a file from a URL and save it to disk. Use for binary files (ZIPs, PDFs, images, executables) — not for reading page text.</description>
    <parameters>
      - url       (string,  REQUIRED) — must start with http:// or https://
      - dest_path (string,  REQUIRED) — destination file or directory path; if a directory, filename is inferred from the URL
      - overwrite (boolean, OPTIONAL, default: false) — allow overwriting an existing file
    </parameters>
    <destructive>YES — writes a file to disk</destructive>
    <confirmation_required>YES — if dest_path already exists and overwrite=false</confirmation_required>
  </function>
</functions>

<tool_rules>

1. VISUAL ANALYSIS — WHEN TO SET visual_analysis=true
   Do NOT wait for the user to explicitly ask for image analysis. Set visual_analysis=true
   whenever the task domain naturally involves visual content:

   ALWAYS true for:
   - Stock prices, technical analysis, trading charts, candlestick patterns
   - Weather forecasts, climate data, satellite imagery
   - Scientific figures, research papers with graphs
   - Architecture, engineering, or system diagrams
   - Medical imaging, lab results, scan reports
   - Product comparisons where appearance matters
   - Data dashboards, analytics reports, infographics
   - Any query containing: "chart", "graph", "diagram", "plot", "figure",
     "screenshot", "image", "visual", "trend", "pattern", "look like"

   If the URL itself ends in .jpg/.png/.gif/.webp — it IS an image.
   visual_analysis=true is set automatically; the page is skipped and
   the image is analysed directly.

   If search used categories="images" — the img_src URLs in results are
   direct image files. Pass them in urls[] with visual_analysis=true.

2. URL SOURCES
   2.1 Only use URLs from prior web_search RESULTS. Never construct or invent URLs.
   2.2 Read the title and snippet of each result before choosing — pick the best match for the user's question.
   2.3 Prefer authoritative domains (official docs, reputable publications) when results look equally relevant.
   2.4 Avoid: social media, maps, weather widgets, login-gated pages, aggregator spam sites.

3. HOW MANY URLs (fetch only) (MAX 4)
   Quick fact, single topic                                  → 1–2 URLs
   Comparisons, tutorials, moderate depth                   → 2–3 URLs
   Comprehensive research, multiple perspectives requested  → 3–4 URLs
   Default to 1–2 unless the user explicitly asks for more.

4. WHEN NOT TO FETCH
   4.1 JavaScript-rendered pages (maps, weather, dashboards, social media) → return empty or broken content. Use search snippets instead.
   4.2 If the answer is fully contained in search snippets → skip this step entirely.

5. PER-URL ERRORS (fetch only)
   5.1 Each result in RESULTS has its own error field. A partial fetch (some URLs succeeded) is still OK=true.
   5.2 Read each result's error field individually before deciding whether to retry.

6. FETCH vs DOWNLOAD
   6.1 Use fetch for: HTML pages, documentation, articles — content the LLM needs to read.
   6.2 Use download for: binary files (ZIPs, PDFs, images, executables, datasets) to be saved to disk.
   6.3 Never use download to read text content — use fetch instead.

7. DOWNLOAD SAFETY
   7.1 If dest_path already exists and overwrite=false → stop. Report the path and ask the user to confirm.
   7.2 Always use the exact dest_path the user specified. Never invent a path.

</tool_rules>

"""
WEB_FETCH_TOOL_ERROR_PROMPT = """
<error_recovery>
Read only when <errors> is present in context.

1. FETCH ERROR CATEGORIES
   A. EMPTY CONTENT / SIZE < 50 CHARS — site uses JavaScript rendering.
      Do NOT retry this URL. Use search snippets instead. Try a different URL from the search results.

   B. HTTP 403 / BLOCKED — site blocks scraping.
      Skip this URL. Pick the next best URL from search results.

   C. TIMEOUT — request took too long.
      Retry once with fewer URLs (drop the slowest). If it fails again → skip that URL.

   D. ALL URLs FAILED — OK=false, TOTAL_FETCHED=0.
      Return status="followup". Report which URLs were tried and why each failed.

   E. UNCLASSIFIED — Do not guess. Return status="followup" with the exact error value and one specific question.

2. DOWNLOAD ERROR CATEGORIES
   A. FILE EXISTS — ERROR contains "already exists". Do not overwrite. Return status="followup" and report the path.
   B. PERMISSION DENIED — cannot write to dest_path. Report the path and error. Do not retry.
   C. TIMEOUT — file too large or connection slow. Retry once. If it fails again → status="followup".
   D. HTTP ERROR — report status code and reason. Do not retry.

3. RETRY RULES
   3.1 Never retry a URL whose error indicates JavaScript rendering (category 1A) or HTTP 403 (category 1B).
   3.2 Never retry a download where the file already exists (category 2A) or permission was denied (category 2B).
   3.3 After 3 failures total → status="followup".

</error_recovery>"""
