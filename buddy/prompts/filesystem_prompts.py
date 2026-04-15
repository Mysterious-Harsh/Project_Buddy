# 🔒 LOCKED — filesystem_prompts.py
# Contract: tool_call schema → { action, path, confirmed, ...action-specific fields }
# Result fields: OK, ACTION, PATH, ERROR, NEEDS_CONFIRMATION, PREVIEW, NOTE,
#   RESULTS, TOTAL_FOUND, CONTENT, SIZE_BYTES, LINE_COUNT, START_LINE, END_LINE,
#   EXISTS, IS_FILE, IS_DIR, MODIFIED, CREATED, OPENED, TREE_TEXT, DIFF_TEXT, IDENTICAL, DESTINATION,
#   FORMAT, ROWS_TOTAL, ROWS_AFTER_FILTER, COLUMNS, SHEET, TRUNCATED, OCCURRENCES
# Allowed: bug fixes, error recovery guidance, clarifying action docs.
# Not allowed: adding/removing actions, changing result field names, changing confirmed gate rules.

FILESYSTEM_TOOL_PROMPT = """
FILESYSTEM TOOL

═══════════════════════════════════════════════
§1. CHOOSE THE RIGHT ACTION
═══════════════════════════════════════════════
Map your goal to exactly one action:

  GOAL                                           ACTION
  ─────────────────────────────────────────────────────
  Does path exist? What is its size/type?      → info
  List files in a directory                    → list
  See directory structure (recursive)          → tree
  Find a file by name                          → search   (pattern="*name*")
  Find files CONTAINING text                   → search   (content_query="text")
  Find exact LINES matching text in files      → grep     (content_query="text")
  Read a text / code file                      → read
  Read a CSV, Excel, Parquet file              → read   (use pandas_query / search_pattern to filter)
  Read a PDF document                          → read
  Read a Word document (.docx)                 → read
  Read a specific line range of a text file    → read_lines
  Create or overwrite a file                   → write    (confirmed gate)
  Add text to end of file                      → append
  Replace specific text inside a file          → patch    (confirmed gate)
  Create a directory                           → mkdir
  Delete a file or directory                   → delete   (confirmed gate)
  Copy a file or directory                     → copy
  Rename or move a file                        → move     (confirmed gate)
  Open with default application                → open
  Compare two text files                       → diff

search vs grep — critical difference:
  search content_query → returns WHICH FILES contain the text (file-level)
  grep   content_query → returns WHICH LINES match + context (line-level)
  Use grep when you need the line number to read_lines or edit a specific spot.
  Use search when you just need to locate which file to open.

═══════════════════════════════════════════════
§2. PATH RESOLUTION — NEVER GUESS
═══════════════════════════════════════════════
Always get paths from a reliable source. Do NOT invent paths.

  Source 1 — PRIOR_OUTPUTS: use RESULTS[n].path or DESTINATION directly.
  Source 2 — OS_PROFILE: home, desktop, documents paths are always valid.
  Source 3 — CURRENT_STEP: paths explicitly given by the user.
  Source 4 — Discover: when path is unknown, search for it.

Discovery workflow (when path unknown):
  Step A: {"action": "search", "path": "~", "pattern": "*filename*", "recursive": true}
  Step B: Take RESULTS[0].path from the result.
  Step C: Use that path in the next call (read/grep/delete/etc.).

  If Step A returns 0 results → see §3 fallback strategy.

Note: ~ and $ENV_VAR in path are auto-expanded. info on ~ is always valid.

═══════════════════════════════════════════════
§3. SEARCH & GREP — PATTERN GUIDE + FALLBACK
═══════════════════════════════════════════════
Pattern matches FILE NAMES (not content). Examples:

  *.py          → all Python files
  *.{py,js}     → NOT supported — run two separate searches
  *config*      → any file with "config" anywhere in its name
  settings.py   → exact filename
  *             → everything (combine with file_types to narrow)
  README*       → files starting with README

Always start broad. If 0 results, follow this fallback chain:
  1. Remove file_types filter
  2. Broaden pattern: "exact.py" → "*exact*" → "*.py" → "*"
  3. Try parent directory in path
  4. If content_query set, remove it first and find the file by name only
  Check NOTE field — it gives specific narrowing hints.

For grep — content_query is what you search FOR; pattern filters WHICH FILES:
  {"action": "grep", "path": "/project", "content_query": "def login", "pattern": "*.py"}
  This finds all lines matching "def login" inside .py files under /project.

If grep returns 0 results:
  1. Try case_sensitive=false (already default)
  2. Simplify the query (fewer words)
  3. Try regex=false (already default)
  4. Run search with content_query first to confirm files contain the text

═══════════════════════════════════════════════
§4. SCHEMA
═══════════════════════════════════════════════
  action     : required — one of the actions in §1
  path       : required — auto-resolved (~ and $VAR expanded)
  confirmed  : default false — set true only after user approves destructive actions

  search     → pattern (glob), content_query (text inside files),
               recursive (default true), max_results (default 20),
               file_types (["py","txt"]), case_sensitive (default false), regex (default false)
  read       → max_chars (default 8000), encoding (default "utf-8")
               search_pattern  — text/regex: filters lines (text/pdf/docx) or rows (table)
               pandas_query    — tabular only: "amount > 500 and status == 'pending'"
               columns         — tabular only: ["col1","col2"] to narrow wide files
               sheet_name      — Excel only: sheet name, default first sheet
               Reads ALL formats: .txt .py .md .json .toml .yaml .log (text),
               .csv .tsv .xlsx .xls .parquet .feather .orc (table via pandas),
               .pdf (pdfplumber/PyPDF2), .docx .doc (python-docx).
               Unknown/other extensions: tries text first, then binary gate.
  read_lines → start_line (1-indexed), end_line (1-indexed), max_chars (default 8000)
               text files only — use after grep to read a known line range
  list       → show_hidden (default false)
  tree       → depth (default 3, max 10), show_hidden (default false), max_results (default 20)
  open       → no extra fields
  info       → no extra fields
  write      → content (string), confirmed
               overwrite (default false) — only needed for the first unconfirmed call;
               once confirmed=true the write proceeds regardless of overwrite flag.
  append     → content (string — appended to end, file created if missing)
  patch      → old_str (exact text to find), new_str (replacement — empty string = delete),
               replace_all (default false),  confirmed
               replace_all=false: fails if 0 matches; fails if 2+ matches (add context to old_str)
               replace_all=true:  replaces every occurrence
  delete     → confirmed
  copy       → destination (absolute path) — if dest is a dir, file is placed inside it
  move       → destination (absolute path), confirmed — if dest is dir, file placed inside it
  mkdir      → no extra fields (creates all parents, idempotent)
  grep       → content_query (text/regex to find), pattern (file glob, e.g. "*.py"),
               recursive (default true), context_lines (default 2),
               max_results (default 20), case_sensitive (default false), regex (default false)
  diff       → destination (path of second file)

═══════════════════════════════════════════════
§5. CONFIRMATION GATE (write / delete / move)
═══════════════════════════════════════════════
These three actions require two calls:

  Call 1 — confirmed=false (default):
    Tool returns: OK=false, NEEDS_CONFIRMATION=true, PREVIEW="what would happen"
    → Use PREVIEW as your followup_question text to ask the user.
    → Do NOT execute yet. status="followup".

  Call 2 — after user says yes:
    Set confirmed=true and repeat the exact same call.
    → Tool executes and returns OK=true.

  If user says no → status="abort".
  append / copy / mkdir do NOT need confirmation — execute directly.

═══════════════════════════════════════════════
§6. RESULT FIELDS
═══════════════════════════════════════════════
  OK, ERROR
  NEEDS_CONFIRMATION, PREVIEW  — confirmation gate (§5)
  NOTE       — actionable hint (truncation, 0 results, overflow)
  RESULTS    — list for search/list: {name, path, type, size_bytes, modified}
                 + optional match: {line_number, line} when content_query matched a specific line
               list for grep: {path, line_number, line, context_before, context_after}
                 context_before / context_after only present when non-empty
  TOTAL_FOUND — total matches (may exceed max_results); also match count when search_pattern used on read
  CONTENT    — file text or rendered table (read, read_lines)
  FORMAT     — "text" | "table" | "pdf" | "docx" | "binary"  (read only)
  ROWS_TOTAL — total row count before any filter (table reads)
  ROWS_AFTER_FILTER — row count after pandas_query / search_pattern filter
  COLUMNS    — list of column names (table reads) — use these for pandas_query field names
  SHEET      — Excel sheet name that was read
  LINE_COUNT, START_LINE, END_LINE — read_lines
  SIZE_BYTES, MODIFIED — read (text), write, info
  TRUNCATED  — true when content was cut at max_chars
  EXISTS, IS_FILE, IS_DIR, CREATED — info
  OPENED     — open
  TREE_TEXT  — tree output string
  DIFF_TEXT, IDENTICAL — diff
  DESTINATION — resolved final path (copy, move)
  OCCURRENCES — number of matches found/replaced (patch)

═══════════════════════════════════════════════
§7. MULTI-FORMAT READ — HOW TO USE
═══════════════════════════════════════════════
read handles all common file types automatically. Just set the path.

TEXT files (.py .txt .md .json .toml .yaml .sh .log .env .cfg etc.):
  {"action": "read", "path": "/file.py"}
  → returns FORMAT="text", CONTENT=file text
  Add search_pattern to filter lines:
  {"action": "read", "path": "/app.log", "search_pattern": "ERROR", "max_chars": 8000}

TABULAR files (.csv .tsv .xlsx .xls .parquet .feather .orc):
  {"action": "read", "path": "/data.csv"}
  → small file: returns FORMAT="table", CONTENT=pipe-separated table, COLUMNS=[...]
  → large file: returns NEEDS_CONFIRMATION=true with COLUMNS, ROWS_TOTAL, sample
     Then filter: {"action": "read", "path": "/data.csv", "pandas_query": "amount > 500"}

  Filter options (can combine):
    pandas_query  → "col > val and other_col == 'x'"   (use exact COLUMNS names)
    search_pattern→ "keyword"                            (scans all cells)
    columns       → ["col1","col2"]                      (select columns)
    sheet_name    → "Sheet2"                             (Excel only)

PDF files (.pdf):
  {"action": "read", "path": "/report.pdf"}
  → returns FORMAT="pdf", CONTENT=extracted text
  Add search_pattern to find specific sections.
  If NOTE says "scanned image" → use open action instead.

DOCX / DOC files (.docx .doc):
  {"action": "read", "path": "/doc.docx"}
  → returns FORMAT="docx", CONTENT=paragraph text
  Add search_pattern to find specific sections.

PATCH — replace specific text in a file (text files only):
  Step 1 — read the file first to get the exact text including whitespace/indentation.
  Step 2 — use patch with old_str set to that exact text:
  {"action": "patch", "path": "/file.py", "old_str": "def foo():\n    pass", "new_str": "def foo():\n    return 42", "confirmed": false}
  → PREVIEW shows a unified diff of exactly what will change.
  Step 3 — call again with confirmed=true to apply.

  Multiple occurrences:
  {"action": "patch", ..., "replace_all": true, "confirmed": false}
  → replaces ALL occurrences (e.g. rename a variable everywhere in the file).

BINARY / UNKNOWN (images, audio, video, archives):
  read returns NEEDS_CONFIRMATION + FORMAT="binary"
  → Call again with confirmed=true to open with default app.
  → Or use open action directly.
"""


FILESYSTEM_ERROR_RECOVERY_PROMPT = """
FILESYSTEM ERROR RECOVERY

Read ERROR carefully. Never repeat the identical call. Fix first, then retry.

  path not found       → go back to §2: use info or search to discover the real path
  permission denied    → cannot fix — status="followup", tell user
  not a directory      → used list/tree on a file — switch to read or grep
  is a directory       → used read on a directory — switch to list or tree
  binary file          → use open to launch with default application
  0 search results     → follow §3 fallback chain (broaden pattern, remove filters)
  0 grep results       → simplify content_query, check NOTE field for suggestions
  overwrite=false      → not an error on confirmed calls; only affects unconfirmed preview
  start_line too large → call info first to check file exists, then use valid line range
  diff needs two files → set path = file1, destination = file2 (both must be files)
  NEEDS_CONFIRMATION   → read PREVIEW, ask user via followup, then call again with confirmed=true
  same file error      → source and destination resolve to same path — fix destination
  3 failures           → status="followup", describe what was tried
  pandas_query error   → column names are in COLUMNS field — check spelling and case exactly
  large table          → use pandas_query, search_pattern, or columns to filter before reading
  PDF no text found    → PDF may be scanned image — use open action to view in default app
  FORMAT=binary        → file is unreadable as text — call again with confirmed=true to open it
  patch old_str not found   → read the file first, copy the exact text including whitespace
  patch 2+ occurrences      → add more surrounding lines to old_str, or use replace_all=true
  patch new_str empty/null  → omit new_str or set to "" to delete the matched text
"""


tool_call_format = """
FILESYSTEM TOOL CALL — pick exactly ONE block below. Include only the fields shown.

── FIND ──────────────────────────────────────────────────────────────
  {"action": "search",     "path": "/abs/path", "pattern": "*.py", "recursive": true, "max_results": 20}
  {"action": "grep",       "path": "/abs/path", "content_query": "def login", "pattern": "*.py", "context_lines": 2}

── READ ──────────────────────────────────────────────────────────────
  {"action": "info",       "path": "/abs/path"}
  {"action": "list",       "path": "/abs/path"}
  {"action": "tree",       "path": "/abs/path", "depth": 3}
  {"action": "read",       "path": "/abs/path", "max_chars": 8000}
  {"action": "read",       "path": "/data.csv",  "pandas_query": "col > val", "columns": ["a","b"]}
  {"action": "read_lines", "path": "/abs/path", "start_line": 10, "end_line": 50}
  {"action": "diff",       "path": "/file1.txt", "destination": "/file2.txt"}

── WRITE / MODIFY ────────────────────────────────────────────────────
  {"action": "write",  "path": "/abs/path", "content": "full file text", "confirmed": false}
  {"action": "append", "path": "/abs/path", "content": "text to add"}
  {"action": "patch",  "path": "/abs/path", "old_str": "exact text", "new_str": "replacement", "replace_all": false, "confirmed": false}

── MANAGE ────────────────────────────────────────────────────────────
  {"action": "mkdir",  "path": "/abs/path"}
  {"action": "copy",   "path": "/src",      "destination": "/dst"}
  {"action": "move",   "path": "/src",      "destination": "/dst/newname", "confirmed": false}
  {"action": "delete", "path": "/abs/path", "confirmed": false}
  {"action": "open",   "path": "/abs/path"}

RULES: action + path always required. confirmed=false → returns PREVIEW, no changes made.
       Call again with confirmed=true to execute write/patch/move/delete.
"""
