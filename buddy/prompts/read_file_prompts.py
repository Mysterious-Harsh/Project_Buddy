READ_FILE_PROMPT = """
<tool_description>
READ_FILE TOOL — reads any file and returns its content.
</tool_description>

<formats>
FORMATS HANDLED AUTOMATICALLY (detect by extension):
  text / code  → .py .txt .md .json .toml .yaml .sh .log .env .cfg and all others
  tabular      → .csv .tsv .xlsx .xls .parquet .feather .orc
  document     → .pdf .docx .doc
  binary       → returns NEEDS_CONFIRMATION to open with default app instead
</formats>

<actions>
ACTIONS:
  read content   → set path only (format auto-detected)
  read lines     → set start_line / end_line (text files only)
  list directory → set path to a directory
  tree structure → set path to a directory, set depth (default 3)
  file metadata  → set path, set info=true
</actions>

<call_schema>
SCHEMA:
  path           : required
  start_line     : 1-indexed, read from this line
  end_line       : 1-indexed, read up to this line
  search_pattern : filter lines (text/pdf/docx) or rows (table) matching this text
  pandas_query   : tabular only — "amount > 500 and status == 'active'"
  columns        : tabular only — ["col1", "col2"] to select specific columns
  sheet_name     : Excel only — sheet name, default first sheet
  max_chars      : default 8000 — cap output size
  encoding       : default "utf-8"
  depth          : directory tree depth, default 3 (used when path is a directory)
  info           : default false — set true to get file size, type, modified date
</call_schema>

<examples>
EXAMPLES:
  {"path": "/project/app.py"}
  {"path": "/data.csv", "pandas_query": "score > 90", "columns": ["name", "score"]}
  {"path": "/report.pdf", "search_pattern": "revenue"}
  {"path": "/file.py", "start_line": 40, "end_line": 80}
  {"path": "~/Documents", "depth": 2}
  {"path": "/file.txt", "info": true}
</examples>

<result_fields>
RESULT FIELDS:
  OK, PATH, ERROR
  CONTENT        — file text or rendered table
  FORMAT         — "text" | "table" | "pdf" | "docx" | "binary"
  TRUNCATED      — true when output was cut at max_chars
  LINE_COUNT, START_LINE, END_LINE — line range reads
  SIZE_BYTES, MODIFIED, CREATED   — file metadata
  ROWS_TOTAL, ROWS_AFTER_FILTER, COLUMNS, SHEET — tabular reads
  RESULTS        — list entries for directory reads: {name, path, type, size_bytes, modified}
  TREE_TEXT      — directory tree string
  EXISTS, IS_FILE, IS_DIR — info reads
  NEEDS_CONFIRMATION, PREVIEW — binary files: call again with confirmed=true to open
  NOTE           — actionable hint (truncation, 0 matches, how to filter)
</result_fields>
"""

READ_FILE_ERROR_PROMPT = """
READ_FILE ERROR RECOVERY — read ERROR carefully, fix before retrying.

  file not found       → use info action (info=true) to check path exists; search_file to locate it
  path is a directory  → remove start_line/end_line; result is directory listing
  binary file          → call again with confirmed=true to open with default app
  truncated            → add search_pattern to filter, or use start_line/end_line range
  0 rows / 0 matches   → broaden search_pattern; check COLUMNS for exact column names
  pandas_query error   → column names are in COLUMNS field — check spelling exactly
  large table          → add pandas_query, search_pattern, or columns to filter
  PDF no text          → PDF is likely a scanned image — call again with confirmed=true to open
  encoding error       → try encoding="latin-1"
  3 failures           → status="followup", describe what was tried
"""

tool_call_format = '{"path": "/abs/path", "max_chars": 8000}'
