SEARCH_FILE_PROMPT = """
<tool_description>
SEARCH_FILE TOOL — find files by name or find lines matching text inside files.
</tool_description>

<actions>
ACTIONS:
  search → find FILES whose name matches a pattern or whose content contains text
           returns file-level results: path, name, size, modified
  grep   → find LINES matching text inside files
           returns line-level results: path, line_number, line, context
</actions>

<when_to_use>
WHEN TO USE WHICH:
  "find which file contains X"   → search (content_query="X") — gives you the file path
  "find the line number of X"    → grep   (content_query="X") — gives you line number for read_file
  "find a file named config"     → search (pattern="*config*")
  "find all .py files"           → search (pattern="*.py")
</when_to_use>

<call_schema>
SCHEMA:
  action        : "search" | "grep" — default "search"
  path          : required — directory to search in
  pattern       : file name glob — "*.py" | "*config*" | "settings.py"
                  default "*" (all files)
  content_query : text or regex to find inside files
  recursive     : default true
  max_results   : default 20
  context_lines : grep only — lines shown before/after each match, default 2
  file_types    : ["py", "txt"] — filter by extension (without dot)
  case_sensitive: default false
  regex         : default false
</call_schema>

<examples>
EXAMPLES:
  {"action": "search", "path": "~",         "pattern": "*config*"}
  {"action": "search", "path": "/project",  "content_query": "def login", "pattern": "*.py"}
  {"action": "grep",   "path": "/project",  "content_query": "def login", "pattern": "*.py"}
  {"action": "grep",   "path": "/app.log",  "content_query": "ERROR", "context_lines": 3}
</examples>

<fallback_chain>
FALLBACK CHAIN (0 results):
  1. Remove file_types filter
  2. Broaden pattern: "exact.py" → "*exact*" → "*.py" → "*"
  3. Try parent directory in path
  4. Simplify content_query (fewer words)
  5. Check NOTE field — it gives specific hints
</fallback_chain>

<result_fields>
RESULT FIELDS:
  OK, ACTION, PATH, ERROR
  RESULTS     — search: [{name, path, type, size_bytes, modified, match?}]
                grep:   [{path, line_number, line, context_before?, context_after?}]
  TOTAL_FOUND — total matches (may exceed max_results)
  NOTE        — hint when 0 results or truncated
</result_fields>
"""

SEARCH_FILE_ERROR_PROMPT = """
SEARCH_FILE ERROR RECOVERY — read ERROR carefully, fix before retrying.

  0 results (search)   → follow fallback chain: remove file_types, broaden pattern, try parent dir
  0 results (grep)     → simplify content_query, try case_sensitive=false, try regex=false
  path not found       → verify path with read_file info=true before searching
  not a directory      → path must be a directory for search/grep across files
  glob pattern error   → avoid special chars other than * ? [] in pattern
  3 failures           → status="followup", describe what was tried
"""

tool_call_format = '{"action": "search" | "grep", "path": "/abs/path", "pattern": "*.py", "content_query": "text to find"}'
