EXCEL_TOOL_PROMPT = """
TOOL_NAME: excel
TOOL_DESCRIPTION: Create, read, edit, and search Excel (.xlsx) workbooks. Does not handle .csv or .txt — use fs_read for reading those, fs_write for creating/editing them. For format conversion (xlsx → pdf, csv, etc.) use the converter tool.

<functions>
  <function>
    <name>create</name>
    <description>Create a new .xlsx workbook with one or more sheets.</description>
    <parameters>
      - path (string, REQUIRED) — absolute path; must end in .xlsx
      - sheets (array, REQUIRED) — list of: { "name": str, "headers": [str, ...], "rows": [[val, ...], ...] }
      - confirmed (boolean, OPTIONAL, default: false) — must be true when file already exists
    </parameters>
    <destructive>CONDITIONAL — overwrites existing file and all its sheets</destructive>
    <confirmation_required>YES — when file already exists</confirmation_required>
  </function>

  <function>
    <name>read</name>
    <description>Read workbook structure and preview data. Always call this before edit to confirm sheet names and column headers.</description>
    <parameters>
      - path (string, REQUIRED) — absolute path to .xlsx file
      - sheet (string, OPTIONAL) — specific sheet name to read in detail; omit to get all-sheet summary + active sheet detail
      - preview_rows (integer, OPTIONAL, default: 5) — number of data rows to include in preview
    </parameters>
    <destructive>NO</destructive>
    <confirmation_required>NO</confirmation_required>
  </function>

  <function>
    <name>edit</name>
    <description>Apply a list of operations to an existing workbook. All ops run in order; file is saved only if ALL succeed — no partial saves.</description>
    <parameters>
      - path (string, REQUIRED) — absolute path to .xlsx file
      - sheet (string, OPTIONAL) — default sheet for cell/row ops; individual ops can override with their own "sheet" key
      - operations (array, REQUIRED) — ordered list of op objects:

        set_cell:     { "op": "set_cell",     "cell": "B3",        "value": 9500 }
        formula:      { "op": "set_cell",     "cell": "C1",        "value": "=SUM(B2:B100)" }
        add_row:      { "op": "add_row",      "row": [val, ...] }
        insert_row:   { "op": "insert_row",   "at_index": 5,       "row": [val, ...] }
        delete_row:   { "op": "delete_row",   "at_index": 5 }
        add_sheet:    { "op": "add_sheet",    "name": "Q2",        "headers": ["Date", "Amount"] }
        delete_sheet: { "op": "delete_sheet", "name": "OldData",   "confirmed": true }
        rename_sheet: { "op": "rename_sheet", "name": "Sheet1",    "new_name": "Sales" }
    </parameters>
    <destructive>YES — modifies file in place</destructive>
    <confirmation_required>YES — delete_sheet requires "confirmed": true inside the op object</confirmation_required>
  </function>

  <function>
    <name>search</name>
    <description>Find rows by text match or numeric/string condition. Two modes — never mix them.</description>
    <parameters>
      MODE 1 — text search (use when looking for a value by content):
      - path (string, REQUIRED)
      - sheet (string, OPTIONAL) — defaults to active sheet
      - query (string, REQUIRED) — text to find, case-insensitive
      - column (string, OPTIONAL) — limit to this column header; omit to search all columns

      MODE 2 — condition filter (use when filtering rows by comparison):
      - path (string, REQUIRED)
      - sheet (string, OPTIONAL)
      - column (string, REQUIRED) — exact column header name
      - operator (string, REQUIRED) — one of: = != > < >= <= contains starts_with ends_with
      - value (any, REQUIRED) — comparison value
    </parameters>
    <destructive>NO</destructive>
    <confirmation_required>NO</confirmation_required>
  </function>

</functions>

<tool_rules>

1. READ BEFORE EDIT
   1.1 Always call read before edit unless you already have sheet names and column headers from earlier in this turn.
   1.2 Sheet names are case-sensitive. "sales" ≠ "Sales". Never guess — read first.
   1.3 If SHEET_NOT_FOUND is returned, call read to get AVAILABLE_SHEETS, then retry with the exact name.

2. CELL AND ROW NOTATION
   2.1 Cell addresses use Excel A1 notation: column letter(s) + row number. Examples: A1, B3, C10, AA2.
   2.2 Row indices (at_index) are 1-based. Row 1 is the header row; data starts at row 2.
   2.3 Formulas are plain strings starting with =. Examples: "=SUM(A2:A100)", "=IF(B2>1000,\"High\",\"Low\")".

3. EDIT ATOMICITY
   3.1 All operations in a single edit call are atomic — if any op fails, the file is NOT saved and remains unchanged.
   3.2 Batch related changes into one edit call. This is safer and more efficient than multiple calls.
   3.3 On failure, RESULTS[] shows exactly which op failed and why. Fix that op and retry the full batch.

4. SEARCH MODES — NEVER MIX
   4.1 Text search: provide query. Do NOT provide operator or value.
   4.2 Condition search: provide column + operator + value. Do NOT provide query.
   4.3 Use search results (row indices from MATCHES[]) in a follow-up edit call to modify found rows.

5. SAFETY
   5.1 WHAT IS DESTRUCTIVE:
       - create with confirmed=true: overwrites the ENTIRE file — all existing sheets are lost.
       - delete_sheet: permanently removes a sheet and ALL its data. Cannot be undone.
   5.2 THE GATE:
       - create: if file exists and confirmed is not true, return NEEDS_CONFIRMATION.
       - delete_sheet: requires "confirmed": true INSIDE the op object (not at top-level).

6. CHECKLIST
   □ path is absolute and ends in .xlsx
   □ sheet name matches exactly (case-sensitive) — verify with read if unsure
   □ cell addresses use A1 notation (letter + number, no spaces)
   □ row indices are 1-based integers
   □ formulas are strings starting with =
   □ search: query OR (column + operator + value) — not both

   DESTRUCTIVE GATE:
   □ create: file already exists? Set confirmed=true only after user explicitly confirms overwrite
   □ delete_sheet: user explicitly asked to delete this sheet? Add "confirmed": true in the op

</tool_rules>
"""
EXCEL_TOOL_ERROR_PROMPT = """
<error_recovery>
Read only when <errors> is present in context.

1. ERROR CATEGORIES
   A. SHEET_NOT_FOUND     — sheet name wrong or sheet was deleted
   B. FILE_NOT_FOUND      — wrong path or file moved/renamed
   C. INVALID_CELL        — cell address format error
   D. COLUMN_NOT_FOUND    — column header mismatch in search or condition
   E. ROW_OUT_OF_RANGE    — at_index exceeds sheet row count
   F. IMPORT_ERROR        — openpyxl not installed
   G. PERMISSION_DENIED   — file locked or no write access

2. RETRY RULES
   2.1 SHEET_NOT_FOUND    → check AVAILABLE_SHEETS in the error, retry with exact name.
                            If AVAILABLE_SHEETS is missing, call read first.
   2.2 FILE_NOT_FOUND     → verify the path. Use fs_browse.find to locate the file if path is uncertain.
   2.3 INVALID_CELL       → use strict A1 format: one or more capital letters followed by a positive integer.
                            Valid: A1, B10, AA3. Invalid: 1A, B, A0, A 1.
   2.4 COLUMN_NOT_FOUND   → check AVAILABLE_COLUMNS in the error, retry with exact header string.
                            Column matching is case-insensitive in search but check spelling.
   2.5 ROW_OUT_OF_RANGE   → call read to get actual row_count, then use a valid at_index.
   2.6 IMPORT_ERROR       → report to user: "openpyxl is required. Install: pip install openpyxl". Do not retry.
   2.7 PERMISSION_DENIED  → file may be open in Excel or another app. Ask user to close it, then retry.

3. RECOVERY CHECKLIST
   □ Read the ERROR field — it always includes AVAILABLE_SHEETS or AVAILABLE_COLUMNS when relevant
   □ Never guess sheet names or column names — use read to confirm exact strings
   □ SAVED: false in edit result means the file was NOT modified — safe to fix and retry
   □ If file changed between read and edit (another app modified it), re-read before retrying

   DESTRUCTIVE GATE:
   □ Do NOT retry delete_sheet without re-confirming with user that the sheet should be deleted

</error_recovery>"""
