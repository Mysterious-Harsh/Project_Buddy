CONVERTER_TOOL_PROMPT = """
TOOL_NAME: converter
TOOL_DESCRIPTION: Convert files between formats. One function — specify source, destination format, and output directory.

<functions>
  <function>
    <name>convert</name>
    <description>Convert a file (or multiple image files) to a different format. Output is placed in output_dir with the source filename and new extension.</description>
    <parameters>
      - source (string OR array, REQUIRED) — absolute path to source file; use array of image paths to merge multiple images into one PDF
      - destination_format (string, REQUIRED) — target format (see SUPPORT MATRIX)
      - output_dir (string, REQUIRED) — absolute path to output directory; created if it does not exist
      - slide_range (string, OPTIONAL) — pptx → png/jpg only; "all" | "1-3" | "2" — which slides to keep
      - page_range (string, OPTIONAL) — pdf → png/jpg/txt only; "all" | "1-3" | "2"
      - sheet (string, OPTIONAL) — xlsx → csv only; sheet name to export; omit to use active sheet
    </parameters>
    <destructive>NO — only writes new files into output_dir</destructive>
    <confirmation_required>NO</confirmation_required>
  </function>
</functions>

SUPPORT MATRIX:
  source format    →  destination_format options
  ─────────────────────────────────────────────────────────
  docx             →  pdf  html
  xlsx             →  pdf  html  csv
  pptx             →  pdf  png  jpg
  odt / odp / ods  →  pdf  (odp also: png)  (ods also: csv)
  pdf              →  txt  png  jpg
  html / htm       →  pdf  docx
  md               →  html  pdf  docx
  txt              →  pdf  docx
  png/jpg/bmp/gif  →  pdf  (cross-format: jpg↔png↔bmp)
  webp/tiff        →  pdf  png  jpg
  img[] (array)    →  pdf  (merges all images into one PDF in order)

BACKENDS (informational — choose source+destination_format, tool picks the right backend):
  LibreOffice  — office/text/html ↔ pdf/docx/html  (must be installed)
  pypdfium2    — pdf → png/jpg  (built-in)
  pdfplumber   — pdf → txt  (built-in)
  Pillow       — images → pdf, image format conversion  (built-in)
  markdown     — .md → html  (built-in)

OUTPUT NAMING:
  Single source:   output_dir/{source_stem}.{destination_format}
  Array source:    output_dir/{first_stem}.pdf
  pdf/pptx→images: output_dir/{source_stem}-001.png, -002.png, ...

<tool_rules>

1. ALWAYS VERIFY SOURCE EXISTS
   1.1 source must be an absolute path to an existing file.
   1.2 For array source, every path must be absolute and exist.
   1.3 If unsure of the path, use fs_browse.find first.

2. FORMAT RULES
   2.1 destination_format is lowercase with no dot: "pdf" not ".pdf", "png" not ".PNG".
   2.2 Array source is only valid when destination_format is "pdf" — images merged in order.
   2.3 slide_range only applies to pptx → png/jpg. Ignored otherwise.
   2.4 page_range only applies to pdf → png/jpg/txt. Ignored otherwise.
   2.5 sheet only applies to xlsx/ods → csv. Ignored otherwise.

3. LIBREOFFICE REQUIREMENT
   3.1 docx/xlsx/pptx/odt/odp/ods/html/md/txt conversions require LibreOffice installed.
   3.2 If LibreOffice is not found, the tool returns a clear install instruction. Do not retry.

4. CHECKLIST
   □ source is absolute path and file exists (or all array paths exist)
   □ destination_format is lowercase without dot
   □ output_dir is an absolute path (need not exist yet)
   □ Array source: destination_format must be "pdf"
   □ slide_range / page_range: use "all", a single number, or "start-end" format

</tool_rules>

<error_recovery>
Read only when <errors> is present in context.

1. ERROR CATEGORIES
   A. FILE_NOT_FOUND        — source path wrong or file moved/renamed
   B. UNSUPPORTED           — source→destination_format combo not in support matrix
   C. LIBREOFFICE_MISSING   — LibreOffice not installed
   D. IMPORT_ERROR          — pypdfium2, pdfplumber, or Pillow not installed
   E. PERMISSION_DENIED     — cannot write to output_dir
   F. CONVERSION_FAILED     — LibreOffice exited with error (check LO_OUTPUT)

2. RETRY RULES
   2.1 FILE_NOT_FOUND       → verify path. Use fs_browse.find to locate the file.
   2.2 UNSUPPORTED          → check SUPPORTED_FORMATS in error. Pick a supported combination.
   2.3 LIBREOFFICE_MISSING  → report to user: install LibreOffice. Do not retry.
   2.4 IMPORT_ERROR         → report install command from error. Do not retry.
   2.5 PERMISSION_DENIED    → choose a different output_dir the user has write access to.
   2.6 CONVERSION_FAILED    → check LO_OUTPUT in error for LibreOffice's error message.

3. RECOVERY CHECKLIST
   □ destination_format has no dot and is lowercase
   □ Array source: all paths exist and destination_format is "pdf"
   □ slide_range / page_range format: "all", "3", or "1-5"

</error_recovery>
"""
