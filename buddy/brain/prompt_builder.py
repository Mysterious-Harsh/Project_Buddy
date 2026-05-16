from __future__ import annotations

from buddy.context.token_calculator import count_tokens as _count_tokens


def _tok(text: str) -> int:
    return _count_tokens(text) if text else 0


def _trim_memory_tail(memories: str) -> str:
    """Remove the last memory entry (lowest-ranked) from the memories block."""
    parts = [p for p in memories.split("\n\n") if p.strip()]
    if len(parts) > 1:
        return "\n\n".join(parts[:-1])
    lines = [l for l in memories.split("\n") if l.strip()]
    if len(lines) > 1:
        return "\n".join(lines[:-1])
    return ""


def _trim_history_head(chat_history: str) -> str:
    """Remove the oldest turn-pair (user + assistant) from ChatML history."""
    blocks = [b for b in chat_history.split("<|im_start|>") if b.strip()]
    if len(blocks) <= 2:
        return ""
    return "<|im_start|>" + "<|im_start|>".join(blocks[2:])


def _fit_soft_context(
    hard_tokens: int,
    history: str,
    memories: str,
    max_prompt_tokens: int,
) -> tuple[str, str]:
    """
    Trim memories (end-first) then history (start-first) until
    hard + soft fits within max_prompt_tokens. Best-effort.
    """
    while True:
        total = hard_tokens + _tok(history) + _tok(memories)
        if total <= max_prompt_tokens:
            break
        new_mem = _trim_memory_tail(memories)
        if new_mem != memories:
            memories = new_mem
            continue
        new_hist = _trim_history_head(history)
        if new_hist != history:
            history = new_hist
            continue
        break
    return history, memories


def build_prompt(
    system: str,
    context: str,
    task_input: str,
    username: str,
    think_tag: str = "<think>",
) -> str:
    """
    Assembles a complete Qwen ChatML prompt.

    system     → static rules, identity, output schema  (never changes per call)
    context    → dynamic data: memories, history, tools  (changes every call)
    task_input → the thing to act on: user query, intent, instruction
    think_tag  → your custom tag (default <think> to match Qwen3 native format)

    Final token layout the model sees:
      [SYSTEM]  /think + static rules
      [USER]    dynamic context (memories, history, tools...)
      [ASST]    "Understood. Ready."           ← closes the briefing
      [USER]    task_input                     ← isolated, model weights this highest
      [ASST]    <think>                        ← prefill forces reasoning start
    """
    prefill = f"<|im_start|>assistant\n{think_tag}\n"

    sys_block = f"<|im_start|>system\n/think\n{system}\n<|im_end|>"

    ctx_block = (
        "<|im_start|>user\n"
        f"{context}\n"
        "<|im_end|>\n"
        "<|im_start|>assistant\n"
        f"Buddy: Understood {username}. I am Ready.\n"
        "<|im_end|>"
    )

    msg_block = f"<|im_start|>user\nUser:{task_input}\n<|im_end|>"

    return "\n".join([sys_block, ctx_block, msg_block, prefill])


def build_retrieval_prompt(
    system: str,
    chat_history: str,
    datetime_block: str,
    current_message: str,
    think_tag: str = "<think>",
) -> str:
    """
    Assembles the retrieval gate ChatML prompt.

    system          → BUDDY_IDENTITY + RETRIEVAL_GATE_PROMPT + output schema
    chat_history    → ChatML-formatted turns from get_recent_conversations()
    datetime_block  → current time info
    current_message → raw user message for this turn

    Final token layout the model sees:
      [SYSTEM]   /think + system
      [CHAT]     real user/assistant turns (chat_history already ChatML)
      [USER]     <context><datetime>...</datetime></context>
      [USER]     User [time]: current_message
      [ASST]     <think>                 ← prefill forces reasoning start
    """
    prefill = f"<|im_start|>assistant\n{think_tag}\n"

    sys_block = f"<|im_start|>system\n/think\n{system}\n<|im_end|>"

    ctx_block = (
        "<|im_start|>user\n"
        f"<context>\n<datetime>\n{datetime_block}\n</datetime>\n</context>\n"
        "<|im_end|>"
    )

    msg_block = (
        "<|im_start|>user\n"
        "<incoming_message>\n"
        f"{current_message}\n"
        "</incoming_message>\n"
        "What specific facts, preferences, habits, goals, or prior context from memory"
        " would help you respond to this message?\n"
        "<|im_end|>"
    )

    parts = [sys_block]
    if chat_history:
        parts.append(chat_history)
    parts += [ctx_block, msg_block, prefill]

    return "\n".join(parts)


def build_brain_prompt(
    system: str,
    chat_history: str,
    datetime_block: str,
    current_message: str,
    memories: str,
    think_tag: str = "<think>",
    budget=None,
) -> str:
    """
    Assembles the Brain ChatML prompt.

    system          → BUDDY_IDENTITY + BUDDY_BEHAVIOR + BUDDY_MEMORY + BRAIN_PROMPT + schema
    chat_history    → ChatML-formatted turns from get_recent_conversations()
    datetime_block  → current time info
    current_message → raw user message for this turn (with timestamp)
    memories        → retrieved memory entries as formatted text
    budget          → ContextBudget; when provided, trims soft context to fit

    Final token layout the model sees:
      [SYSTEM]    /think + system
      [CHAT]      real user/assistant turns (chat_history already ChatML)
      [USER]      <context><datetime>...</datetime></context>
      [USER]      current_message
      [ASST]      <memories>...</memories>
                  I have everything I need. Generating best possible response now.
                  <think>         ← open prefill, model continues from here
    """
    sys_block = f"<|im_start|>system\n/think\n{system}\n<|im_end|>"

    ctx_block = (
        "<|im_start|>user\n"
        f"<context>\n<datetime>\n{datetime_block}\n</datetime>\n</context>\n"
        "<|im_end|>"
    )

    msg_block = f"<|im_start|>user\n{current_message}\n<|im_end|>"

    # memory_block overhead (fixed text around the memories content)
    _mem_overhead = (
        "<|im_start|>assistant\n<memories>\n\n</memories>\n"
        "I have everything I need. Generating the best possible response now.\n"
        f"{think_tag}\n"
    )

    if budget is not None and budget.max_prompt_tokens > 0:
        hard = (
            _tok(sys_block)
            + _tok(ctx_block)
            + _tok(msg_block)
            + _tok(_mem_overhead)
            + 30
        )
        chat_history, memories = _fit_soft_context(
            hard, chat_history or "", memories or "", budget.max_prompt_tokens
        )

    memory_block = (
        "<|im_start|>assistant\n"
        f"<memories>\n{memories}\n</memories>\n"
        "I have everything I need. Generating the best possible response now.\n"
        f"{think_tag}\n"
    )

    parts = [sys_block]
    if chat_history:
        parts.append(chat_history)
    parts += [ctx_block, msg_block, memory_block]

    return "\n".join(parts)


def build_planner_prompt(
    system: str,
    datetime_block: str,
    available_tools: str,
    planner_instructions: str,
    memories: str,
    followups: str = "",
    think_tag: str = "<think>",
    budget=None,
) -> str:
    """
    Assembles the Planner ChatML prompt.

    system               → BUDDY_IDENTITY + BUDDY_MEMORY + PLANNER_PROMPT + schema
    datetime_block       → current time info
    available_tools      → tool registry descriptions
    planner_instructions → self-contained task from Brain
    memories             → retrieved memory entries as formatted text
    followups            → ChatML-formatted Q&A turns from FollowupStack (optional)
    budget               → ContextBudget; when provided, trims memories to fit

    Final token layout the model sees:
      [SYSTEM]   /think + system
      [TOOL]     <context><datetime>...</datetime><available_tools>...</available_tools></context>
      [ASST]     <memories>...</memories>
                 I know the user's context. Reading the task now.   ← closed
      [USER]     <task>{planner_instructions}</task>
      [ASST]     {followup question}    ← real ChatML turns if followup happened
      [USER]     {user answer}
      [ASST]     <think>                ← open prefill
    """
    sys_block = f"<|im_start|>system\n/think\n{system}\n<|im_end|>"

    ctx_block = (
        "<|im_start|>tool\n"
        "<context>\n"
        f"<datetime>\n{datetime_block}\n</datetime>\n"
        f"<available_tools>\n{available_tools}\n</available_tools>\n"
        "</context>\n"
        "<|im_end|>"
    )

    _mem_overhead = "<|im_start|>assistant\n<memories>\n\n</memories>\n<|im_end|>"
    task_block = (
        f"<|im_start|>user\n<task>\n{planner_instructions}\n</task>\n<|im_end|>"
    )
    prefill = f"<|im_start|>assistant\n{think_tag}\n"

    if budget is not None and budget.max_prompt_tokens > 0:
        hard = (
            _tok(sys_block)
            + _tok(ctx_block)
            + _tok(_mem_overhead)
            + _tok(task_block)
            + _tok(prefill)
            + _tok(followups)
            + 30
        )
        _, memories = _fit_soft_context(
            hard, "", memories or "", budget.max_prompt_tokens
        )

    memory_block = (
        f"<|im_start|>assistant\n<memories>\n{memories}\n</memories>\n<|im_end|>"
    )

    parts = [sys_block, ctx_block, memory_block, task_block]
    if followups and followups.strip():
        parts.append(followups)
    parts.append(prefill)

    return "\n".join(parts)


def build_responder_prompt(
    system: str,
    datetime_block: str,
    memories: str,
    execution_results: str,
    responder_instruction: str,
    think_tag: str = "<think>",
    budget=None,
) -> str:
    """
    Assembles the Responder ChatML prompt.

    system                → BUDDY_IDENTITY + BUDDY_BEHAVIOR + RESPOND_PROMPT + schema
    datetime_block        → current time info
    memories              → retrieved memory entries (for tone + personalization)
    execution_results     → step_execution_map as JSON string
    responder_instruction → planner's briefing on what to synthesize
    budget                → ContextBudget; when provided, trims memories to fit

    Final token layout the model sees:
      [SYSTEM]   /think + system
      [TOOL]     <context><datetime>...</datetime></context>
      [ASST]     <memories>...</memories>
      [TOOL]     <task>{responder_instruction}</task>
      [TOOL]     {execution_results JSON}
      [ASST]     <think>     ← open prefill, model continues from here
    """
    sys_block = f"<|im_start|>system\n/think\n{system}\n<|im_end|>"

    ctx_block = (
        "<|im_start|>tool\n"
        "<context>\n"
        f"<datetime>\n{datetime_block}\n</datetime>\n"
        "</context>\n"
        "<|im_end|>"
    )

    _mem_overhead = "<|im_start|>assistant\n<memories>\n\n</memories>\n<|im_end|>"
    tool_block = f"<|im_start|>tool\n<execution_result_map>\n{execution_results}\n</execution_result_map>\n<|im_end|>"
    task_block = (
        f"<|im_start|>tool\n<task>\n{responder_instruction}\n</task>\n<|im_end|>"
    )
    prefill = f"<|im_start|>assistant\n{think_tag}\n"

    if budget is not None and budget.max_prompt_tokens > 0:
        hard = (
            _tok(sys_block)
            + _tok(ctx_block)
            + _tok(_mem_overhead)
            + _tok(tool_block)
            + _tok(task_block)
            + _tok(prefill)
            + 30
        )
        _, memories = _fit_soft_context(
            hard, "", memories or "", budget.max_prompt_tokens
        )

    memory_block = (
        f"<|im_start|>assistant\n<memories>\n{memories}\n</memories>\n<|im_end|>"
    )

    return "\n".join(
        [sys_block, ctx_block, memory_block, task_block, tool_block, prefill]
    )


def build_reader_prompt(
    system: str,
    datetime_block: str,
    rolling_context: str,
    query: str,
    section: str,
    think_tag: str = "<think>",
) -> str:
    """
    Assembles the Reader ChatML prompt (one section per call).

    system          → BUDDY_IDENTITY + READER_PROMPT + schema
    datetime_block  → current time info
    rolling_context → findings from previous sections
    query           → what the user is looking for (isolated as [USER])
    section         → READER_SECTION_TEMPLATE (paragraph + instruction)

    Final token layout the model sees:
      [SYSTEM]   /think + system
      [TOOL]     <context><datetime>...</datetime><prior_findings>...</prior_findings></context>
      [USER]     query                 ← isolated: what the user wants
      [TOOL]     section + instruction ← document data to process
      [ASST]     <think>              ← open prefill
    """
    sys_block = f"<|im_start|>system\n/think\n{system}\n<|im_end|>"

    ctx_block = (
        "<|im_start|>tool\n"
        "<context>\n"
        f"<datetime>\n{datetime_block}\n</datetime>\n"
        f"<prior_findings>\n{rolling_context}\n</prior_findings>\n"
        "</context>\n"
        "<|im_end|>"
    )

    query_block = f"<|im_start|>user\n<user_query>\n{query}\n</user_query>\n<|im_end|>"

    section_block = f"""
    <|im_start|>tool
    <section>
    {section}
    </section>
    <|im_end|>"""
    user_block = (
        f"<|im_start|>user\n>> Does any part or information of the <section> text"
        f" relate to the <user_query> ? If yes, write it in findings while keeping"
        f" every single relevant details, if unsure write it. If no, mark as not"
        f" relevant.\n<|im_end|>"
    )

    prefill = f"<|im_start|>assistant\n{think_tag}\n"

    return "\n".join(
        [sys_block, ctx_block, query_block, section_block, user_block, prefill]
    )


def build_memory_summary_prompt(
    system: str,
    memories: str,
    today: str = "",
    think_tag: str = "<think>",
) -> str:
    """
    Assembles the Memory Summary ChatML prompt.

    No user instruction — Buddy consolidates his own memories internally.

    system    → BUDDY_IDENTITY + BUDDY_MEMORY + MEMORY_SUMMARY_PROMPT + schema
    memories  → raw memory entries to consolidate (format: TIMESTAMP | TIER | imp=N | TEXT)
    today     → current datetime string injected as temporal anchor

    Final token layout the model sees:
      [SYSTEM]   /think + system
      [TOOL]     TODAY: {today}\n\n{memories}  ← temporal anchor + memories
      [ASST]     <think>                        ← open prefill, Buddy thinks about himself
    """
    sys_block = f"<|im_start|>system\n/think\n{system}\n<|im_end|>"

    today_line = f"TODAY: {today}\n\n" if today else ""
    tool_block = f"<|im_start|>tool\n{today_line}{memories}\n<|im_end|>"

    prefill = f"<|im_start|>assistant\n{think_tag}\n"

    return "\n".join([sys_block, tool_block, prefill])


def build_opener_prompt(
    system: str,
    recent_turns: str,
    think_tag: str = "<think>",
) -> str:
    """
    Assembles the session opener ChatML prompt.

    system       → BUDDY_IDENTITY + BUDDY_BEHAVIOR + OPENER_PROMPT
    recent_turns → ChatML-formatted turns from get_recent_conversations() (may be empty)

    Final token layout the model sees:
      [SYSTEM]   /think + system
      [USER]     <context><current_time>...</current_time><recent_conversations>...</recent_conversations></context>
      [ASST]     <think>   ← open prefill
    """
    import datetime as _dt

    sys_block = f"<|im_start|>system\n/think\n{system}\n<|im_end|>"

    now = _dt.datetime.now()
    time_str = now.strftime("%H:%M")
    history = recent_turns.strip() if recent_turns and recent_turns.strip() else "None."
    ctx_block = (
        "<|im_start|>user\n"
        "<context>\n"
        f"<current_time>{time_str}</current_time>\n"
        f"<conversation_history>\n{history}\n</conversation_history>\n"
        "</context>\n"
        "<|im_end|>"
    )

    prefill = f"<|im_start|>assistant\n{think_tag}\n"

    return "\n".join([sys_block, ctx_block, prefill])


def build_compactor_prompt(
    system: str,
    task: str,
    output: str,
    think_tag: str = "<think>",
) -> str:
    """
    Assembles the Compactor ChatML prompt (single tool output → compressed text).

    system  → COMPACTOR_PROMPT + output schema
    task    → user's original task (preserves task-relevant facts)
    output  → single tool result to compress

    Final token layout:
      [SYSTEM]   /think + system
      [USER]     <task>...</task>  <tool_result>...</tool_result>  >> instruction
      [ASST]     <think>   ← open prefill
    """
    sys_block = f"<|im_start|>system\n/think\n{system}\n<|im_end|>"
    user_block = (
        "<|im_start|>user\n"
        f"<task>\n{task}\n</task>\n"
        f"<tool_result>\n{output}\n</tool_result>\n"
        ">> Compress the tool result. Preserve every fact needed for the task."
        " Remove only noise.\n"
        "<|im_end|>"
    )
    prefill = f"<|im_start|>assistant\n{think_tag}\n"
    return "\n".join([sys_block, user_block, prefill])


def build_executor_prompt(
    system: str,
    datetime_block: str,
    instruction: str,
    end_goal: str = "",
    prior_outputs: str = "",
    step_errors: str = "",
    followups: str = "",
    think_tag: str = "<think>",
) -> str:
    """
    Assembles the Executor ChatML prompt.

    system          → BUDDY_IDENTITY + EXECUTOR_PROMPT + schema (tool_call_format injected)
    datetime_block  → current time info
    end_goal        → the overall task planner is trying to achieve (orientation only —
                      executor must NOT attempt to execute it; only the current step)
    instruction     → single step instruction from planner
    prior_outputs   → outputs from previous steps (optional)
    step_errors     → errors from previous attempts at this step (optional)
    followups       → ChatML-formatted Q&A turns from FollowupStack (optional)

    Final token layout the model sees:
      [SYSTEM]   /think + system
      [TOOL]     <context> datetime + prior_outputs </context>
      [TOOL]     {step_errors}          ← failed tool results as ground truth
      [TOOL]     <end_goal>             ← overall goal for orientation, NOT to execute
      [TOOL]     <current_step>{instruction}</current_step>
      [ASST]     {followup question}    ← real ChatML turns if followup happened
      [USER]     {user answer}          ← actual user input, stays [USER]
      [ASST]     <think>                ← open prefill
    """
    sys_block = f"<|im_start|>system\n/think\n{system}\n<|im_end|>"

    ctx_parts = [
        "<|im_start|>tool",
        "<context>",
        f"<datetime>\n{datetime_block}\n</datetime>",
    ]
    if prior_outputs and prior_outputs.strip():
        ctx_parts.append(
            f"<prior_step_outputs>\n{prior_outputs}\n</prior_step_outputs>"
        )
    ctx_parts += ["</context>", "<|im_end|>"]
    ctx_block = "\n".join(ctx_parts)

    step_block = (
        f"<|im_start|>tool\n<current_step>\n{instruction}\n</current_step>\n<|im_end|>"
    )

    prefill = f"<|im_start|>assistant\n{think_tag}\n"

    parts = [sys_block, ctx_block]
    if step_errors and step_errors.strip():
        parts.append(
            f"<|im_start|>tool\n<errors>\n{step_errors}\n</errors>\n<|im_end|>"
        )
    if end_goal and end_goal.strip():
        parts.append(
            '<|im_start|>tool\n<end_goal READ_ONLY="true">\nREAD ONLY — do not execute'
            " this. This is the overall task being accomplished\nacross all steps."
            " Your job is ONLY the current step below. Use this purely\nto understand"
            " the intent and context behind what you are"
            f" doing.\n\n{end_goal}\n</end_goal>\n<|im_end|>"
        )
    parts.append(step_block)
    if followups and followups.strip():
        parts.append(followups)
    parts.append(prefill)

    return "\n".join(parts)
