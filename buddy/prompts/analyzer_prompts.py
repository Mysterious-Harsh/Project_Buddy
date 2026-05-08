ANALYZER_TOOL_PROMPT = """
TOOL_NAME: analyzer
TOOL_DESCRIPTION: Pure reasoning — use when execution is not needed and thinking over prior step outputs is enough. Categorize files, make decisions, extract key findings, or condense long results into structured text a downstream step can act on. No filesystem, terminal, or network calls are made.

<functions>
  <function>
    <name>analyze</name>
    <description>Produce structured analysis or categorization text from prior step outputs — use when downstream steps need the result as data (lists, labels, key:value pairs)</description>
    <parameters>
      - result (string, REQUIRED) — complete analysis output: categorizations, decisions, findings, or any structured text that dependent steps need
    </parameters>
    <returns>ANALYSIS</returns>
    <destructive>NO</destructive>
    <confirmation_required>NO</confirmation_required>
  </function>

  <function>
    <name>summarize</name>
    <description>Produce a condensed summary of prior step outputs — use when a long result needs to be distilled before being passed downstream or presented to the user</description>
    <parameters>
      - result (string, REQUIRED) — the condensed summary; include every key fact, drop only redundant or verbose detail
    </parameters>
    <returns>SUMMARY</returns>
    <destructive>NO</destructive>
    <confirmation_required>NO</confirmation_required>
  </function>
</functions>

<tool_rules>

1. CHOOSE THE RIGHT FUNCTION
   1.1 analyze  — when the output is structured data a downstream step will parse
                  (file lists by category, extracted values, decisions with reasons)
   1.2 summarize — when the output is prose a downstream step or the responder reads
                   (condensed findings, key takeaways from a long search result)

2. WRITE COMPLETE OUTPUT
   2.1 The result argument is the ONLY output downstream steps will receive.
       Write everything a dependent step needs — names, paths, categories, counts.
   2.2 For analyze: structure the result — use labels, lists, or key:value pairs.
       Example: "PDFs: [file_a.pdf, file_b.pdf]\nImages: [img1.jpg]\nDocs: [notes.txt]"
   2.3 For summarize: keep every key fact; drop only redundant or verbose detail.
   2.4 Never leave result empty or write a placeholder.

3. NO ACTIONS
   3.1 This tool performs no filesystem, terminal, or network operations.
   3.2 Your only input is prior step outputs. Your only output is the function call.

4. CHECKLIST
   □ Correct function chosen (analyze vs summarize)
   □ result contains everything downstream steps or the responder will need
   □ No information from prior steps was silently dropped

</tool_rules>
"""
