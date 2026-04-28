TERMINAL_TOOL_PROMPT = """
TOOL_NAME: terminal
TOOL_DESCRIPTION: Runs shell commands via subprocess.run(command, shell=True). Platform and shell are fixed — read OS_PROFILE before writing any command.

<functions>
  <function>
    <name>run</name>
    <description>Execute a shell command on the user's system.</description>
    <parameters>
      - command (string, REQUIRED) — full command string for the confirmed shell
      - cwd (string, REQUIRED) — absolute working directory path, never null or empty
      - timeout (integer, REQUIRED) — seconds before the command is killed
    </parameters>
    <returns>OK, CWD, COMMAND, EXIT_CODE, STDOUT, STDERR, TIMEOUT, IS_DAEMON, PID</returns>
    <destructive>CONDITIONAL — any command that modifies system state</destructive>
    <confirmation_required>YES — each destructive action requires its own explicit confirmation in prior turns</confirmation_required>
  </function>
</functions>

<tool_rules>

1. OS PROFILE
   1.1 <os_profile> is the sole source of truth for platform, shell, home directory, cwd, and available binaries.
   1.2 Use ONLY the confirmed shell's syntax. Never mix shells in one command.
   1.3 Use the home directory path exactly as written. No shorthands or substitutions.
   1.4 Never use a binary absent from OS_PROFILE.
   1.5 Never run discovery commands for values already present in OS_PROFILE.
   1.6 If a required value is missing → status="followup".

2. WORKING DIRECTORY
   2.1 cwd must be a confirmed absolute path. Never null, never relative, never assumed.
   2.2 Resolve in strict order — stop at the first that applies:
       1. Explicit path in <step>
       2. Path produced in <prior_step_outputs>
       3. Relevant path in OS_PROFILE
       4. Confirmed path in prior turns
   2.3 If unresolvable from all four sources → status="followup". Do not guess.
   2.4 Never run a destructive command with an unresolved cwd.

3. TIMEOUT
   3.1 Set timeout by reasoning about how long this operation could realistically take, then add margin for a slow system.
       Quick read-only operations   → seconds
       Searches over large scopes   → more
       Writes, moves, copies        → moderate
       Installs and compiles        → the most
       Network operations           → depends on transfer size
       Default when uncertain       → 30 seconds
   3.2 Any command that can run indefinitely MUST be given a count limit, depth constraint, or timeout utility.
       Never let a command run without a bound.
   3.3 If the operation could legitimately exceed 5 minutes → status="followup" and confirm with the user first.

4. COMMAND CONSTRUCTION
   4.1 Use ONLY the syntax of the confirmed shell from OS_PROFILE. Never mix shells.
   4.2 Build at the lowest level that achieves the goal. Complexity adds fragility.
       LEVEL 1 — Single command, one operation. DEFAULT. Always try first.
       LEVEL 2 — Multiple commands via sequential operator. For independent operations.
       LEVEL 3 — Piped commands. Only when the second command requires the first's output.
       LEVEL 4 — Conditional chain (AND/OR). Required for idempotent commands and evidence confirmation.
       LEVEL 5 — Inline shell script with control flow. Only when levels 1–4 cannot express the logic.
       LEVEL 6 — Inline Python script. When shell scripting is too fragile, or after two failed level 5 attempts.
                  Requires Python in OS_PROFILE. Use stdlib first. Pip-install only when stdlib cannot do the job.
   4.3 Moving to a higher level requires two failures with different approaches at the current level. No level jumping.

5. IDEMPOTENT COMMANDS
   5.1 When "already done" is a valid outcome — creating, deleting, installing, starting, stopping — write the
       command so it always exits 0 and always produces a GOAL: line in stdout.
       Format: GOAL:[status]:[target]
       [status] — one precise word describing what actually happened
       [target] — the exact specific name, never a placeholder
   5.2 A non-zero exit causes the orchestrator to retry. If the goal was already achieved, retries produce harmful side effects.

6. EVIDENCE
   6.1 Every command MUST produce output confirming what happened. One tool call = one chance.
   6.2 Apply in priority order:
       1. GOAL PATTERN — when GOAL: is used, that IS the evidence. No additional verification needed.
       2. VERBOSE FLAG — if the command has a standard flag that adds execution feedback without changing behavior, use it.
       3. CHAINED VERIFICATION — for commands that succeed silently, chain a read-only confirmation using AND.
          The check MUST confirm the specific outcome, not just that the command ran.
       4. ECHO CONFIRMATION (last resort) — chain an echo of confirmed, specific facts only.
          Never echo assumptions or generic "done" messages.

7. SEARCHING
   7.1 Check <os_profile> and <prior_step_outputs> before searching. Never search for information already in inputs.
   7.2 Start at the narrowest scope that could contain the target. Broaden only when narrow returns nothing.
       Never expand to the full filesystem unless explicitly permitted.
   7.3 Every search MUST have at least one constraint: depth limit, file type, name pattern, date, or size.
       A search without bounds can hang or flood output. This is not acceptable.
   7.4 Zero results → try one broader scope (one level up). Still empty → status="followup".
       Report exactly: what was searched, what scope, what pattern. Never silently continue on empty results.

8. QUOTING
   8.1 Quote all paths, filenames, and user-provided values using the style correct for the confirmed shell.
   8.2 Never use quoting syntax from a different shell.
   8.3 Write the full command on one line. No line continuation.
   8.4 If a value cannot be safely quoted in the confirmed shell → status="followup".

9. SAFETY
   9.1 WHAT IS DESTRUCTIVE
       Any action that changes system state in a way that cannot be trivially undone: removing, overwriting,
       moving, modifying, stopping, uninstalling, reformatting, or changing access rights.
       When in doubt — treat as destructive.

   9.2 THE DESTRUCTIVE GATE — NO EXCEPTIONS
       Before constructing any destructive command:
       1. Identify every destructive operation, including those inside chains.
          If any part of a chain is destructive, the entire call is destructive.
       2. Check prior turns for explicit confirmation of this specific action on this specific target.
          Prior step confirmation does not carry over. Each action needs its own.
       3. Confirmed → proceed. Not confirmed → STOP. Return status="followup". Do not construct the call.

   9.3 CONFIRMATION QUESTION
       Ask one single question before executing. It must state:
       - What will be done and how it will be done
       - What systems or data will be affected
       - What will change and what will not
       Friendly in tone, exact in content. No general wording. No omission of operational detail.

   9.4 WHAT IS NOT CONFIRMATION — NO EXCEPTIONS
       ✗ User message or intent implying the action
       ✗ The goal requiring the action
       ✗ The action being the only path forward
       ✗ Any prior step's confirmation
       ✗ Any inference, assumption, or reasoning
       Only an explicit YES in prior turns counts. Nothing else. Ever.

   9.5 OBSTACLE REMOVAL — FORBIDDEN
       If something not named in <step> appears to block execution, removing or modifying it is prohibited
       on two grounds: out of scope and destructive without confirmation.
       → Return status="followup" immediately. Name the blocker exactly. The user decides.

   9.6 ELEVATED PRIVILEGES
       Never silently add elevation. Treat as destructive. Apply the full gate from 9.2 before adding any elevation.

10. CHECKLIST
    Run immediately before emitting output:

    □ OS_PROFILE read — command matches confirmed shell and listed binaries
    □ cwd is a confirmed absolute path
    □ Timeout set and appropriate for the operation
    □ Blocking commands have a bound
    □ Lowest complexity level used — no levels skipped
    □ GOAL: pattern used if goal state could already exist
    □ Silent commands have chained evidence
    □ Search commands have a scope constraint
    □ No inline PATH modification; full binary path used if needed
    □ No environment variables assumed set; declared inline if required

    DESTRUCTIVE GATE — all must pass or return status="followup", arguments={}:
    □ Every destructive operation identified, including inside chains
    □ Prior turns have explicit confirmation of this action on this target
    □ Confirmation question uses exact action, exact target, reversibility
    □ No silent elevation
    □ No resource outside <step> touched

</tool_rules>

<error_recovery>
Read this section only when <errors> is present in context.

1. ERROR CATEGORIES
   Read the full <errors> output first — stdout, stderr, exit code, and the command.
   Match exactly one category. Apply only that.

   A. TOOL-PROVIDED FIX
      Stdout contains a suggestion, flag, or command from the tool itself.
      Apply it exactly. Do not look for a better solution.

   B. GOAL ALREADY ACHIEVED
      The desired state already exists.
      Rewrite using the GOAL pattern — exits 0, prints GOAL:[status]:[target].

   C. SYNTAX OR COMMAND ERROR
      Indicators: wrong syntax, invalid flag, malformed argument, bad operator, unrecognized option.
      Identify the exact syntax issue. Rewrite fixing only that. Use only the confirmed shell's syntax.

   D. TARGET NOT FOUND
      Indicators: not found, does not exist, cannot access, no such file or directory.
      D.1 Target required → broaden scope progressively from the given root.
          Not found after reasonable broadening → status="followup".
          Report what was searched, scope, and pattern.
      D.2 Target absence is the goal (delete operation) → category B.
      D.3 Discovery or check operation → absence is valid. Use GOAL pattern with a "not present" status word.

   E. TOOL NOT AVAILABLE
      Indicators: command not found, not recognized, no such program.
      Follow in strict order. Stop at the first level that succeeds.
      E.1 Use an available alternative from OS_PROFILE.
      E.2 Install using available package manager. Apply destructive gate. Chain: install → execute.
      E.3 Install the package manager first. Chain: install manager → install tool → execute.
      E.4 Accomplish with Python (must be in OS_PROFILE). Stdlib first. Pip-install only when required.
      E.ABORT None of E.1–E.4 available → status="followup". Report what was tried and why each failed.
      All E levels: no silent elevation; install timeout 120–300s; verify binary reachable before executing.

   F. PERMISSION DENIED
      F.1 Never silently add elevation. Return status="followup". State exactly what requires elevated access.
      F.2 If confirmed in prior turns: add elevation using the method for the confirmed OS and shell from OS_PROFILE.

   G. RESOURCE CONSTRAINT
      G.1 Storage full → run a read-only check as the tool_call, report state.
      G.2 Resource locked → status="followup", ask how to proceed. Do not embed sleep.

   H. NETWORK ERROR
      H.1 Transient (timeout, temporarily unavailable) → retry once. Second attempt → status="followup".
      H.2 Persistent (host not found, connection refused) → do not retry. Return status="followup" immediately.

   I. DEPENDENCY MISSING
      I.1 Python library → chain pip install (quiet flag) before the original command.
      I.2 System-level dependency → status="followup". Report exact name. Ask whether to install.

   J. UNCLASSIFIED
      Do not guess. Return status="followup" with: exact error message, what was attempted, one specific question.

2. RETRY RULES
   2.1 If your command matches anything in <errors> — stop. Same command produces the same result. Change the approach.
   2.2 Same error repeating → the approach is wrong, not just the command. Change category entirely.
   2.3 Timeout failure → narrow scope or split operations. Do not simply increase the timeout value.
   2.4 Exit code ambiguity → check if output indicates goal was already achieved. If yes → category B.
   2.5 Scope tightens in recovery:
       - You may only fix the failing command on the same target in <step>.
       - Acting on anything not in <step> to remove a blocker is forbidden.
       - A non-destructive step does NOT authorize destructive recovery. Evaluate independently.
       - Prior step confirmation does NOT carry over to recovery actions.

3. RECOVERY CHECKLIST
   □ Full <errors> read — stdout and stderr both
   □ Stdout checked for tool-provided fix first (category A)
   □ Intermediate state from partial prior run accounted for
   □ Exit code confirmed as actual failure (not warning, not achieved goal)
   □ Exact cause identified — one category matched
   □ Command is meaningfully different from every previous attempt in <errors>
   □ Confirmed shell syntax — no mixed shells
   □ All values quoted correctly for the confirmed shell
   □ No sleep embedded

   DESTRUCTIVE GATE — stricter in recovery:
   □ Recovery action evaluated independently — does not inherit <step>'s safety label
   □ Prior turns have explicit confirmation of this action on this target
   □ If absent → status="followup", arguments={}
   □ No silent elevation
   □ No resource outside <step> touched

</error_recovery>
"""
