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
        f"<|im_start|>user\n{current_message}\n\nTo give best possible response all"
        " fulfil the request: which memories do you need to recall?\n<|im_end|>"
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
) -> str:
    """
    Assembles the Brain ChatML prompt.

    system          → BUDDY_IDENTITY + BUDDY_BEHAVIOR + BUDDY_MEMORY + BRAIN_PROMPT + schema
    chat_history    → ChatML-formatted turns from get_recent_conversations()
    datetime_block  → current time info
    current_message → raw user message for this turn (with timestamp)
    memories        → retrieved memory entries as formatted text

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
) -> str:
    """
    Assembles the Planner ChatML prompt.

    system               → BUDDY_IDENTITY + BUDDY_MEMORY + PLANNER_PROMPT + schema
    datetime_block       → current time info
    available_tools      → tool registry descriptions
    planner_instructions → self-contained task from Brain
    memories             → retrieved memory entries as formatted text
    followups            → ChatML-formatted Q&A turns from FollowupStack (optional)

    Final token layout the model sees:
      [SYSTEM]   /think + system
      [USER]     <context><datetime>...</datetime><available_tools>...</available_tools></context>
      [ASST]     <memories>...</memories>
                 I know the user's context. Reading the task now.   ← closed
      [USER]     <task>{planner_instructions}</task>
      [ASST]     {followup question}    ← real ChatML turns if followup happened
      [USER]     {user answer}
      [ASST]     <think>                ← open prefill
    """
    sys_block = f"<|im_start|>system\n/think\n{system}\n<|im_end|>"

    ctx_block = (
        "<|im_start|>user\n"
        "<context>\n"
        f"<datetime>\n{datetime_block}\n</datetime>\n"
        f"<available_tools>\n{available_tools}\n</available_tools>\n"
        "</context>\n"
        "<|im_end|>"
    )

    memory_block = (
        "<|im_start|>assistant\n"
        f"<memories>\n{memories}\n</memories>\n"
        "I know the user's context. Reading the task now.\n"
        "<|im_end|>"
    )

    task_block = (
        f"<|im_start|>user\n<task>\n{planner_instructions}\n</task>\n<|im_end|>"
    )

    prefill = f"<|im_start|>assistant\n{think_tag}\n"

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
) -> str:
    """
    Assembles the Responder ChatML prompt.

    system                → BUDDY_IDENTITY + BUDDY_BEHAVIOR + RESPOND_PROMPT + schema
    datetime_block        → current time info
    memories              → retrieved memory entries (for tone + personalization)
    execution_results     → step_execution_map as JSON string
    responder_instruction → planner's briefing on what to synthesize

    Final token layout the model sees:
      [SYSTEM]   /think + system
      [USER]     <context><datetime>...</datetime></context>
      [ASST]     <memories>...</memories>
                 I know who I'm talking to. Reading the execution results now.
      [TOOL]     {execution_results JSON}
      [USER]     <task>{responder_instruction}</task>
      [ASST]     <think>     ← open prefill, model continues from here
    """
    sys_block = f"<|im_start|>system\n/think\n{system}\n<|im_end|>"

    ctx_block = (
        "<|im_start|>user\n"
        "<context>\n"
        f"<datetime>\n{datetime_block}\n</datetime>\n"
        "</context>\n"
        "<|im_end|>"
    )

    memory_block = (
        f"<|im_start|>assistant\n<memories>\n{memories}\n</memories>\n<|im_end|>"
    )

    tool_block = f"<|im_start|>tool\n<step_execution_result>\n{execution_results}\n</step_execution_result>\n<|im_end|>"

    task_block = (
        f"<|im_start|>user\n<task>\n{responder_instruction}\n</task>\n<|im_end|>"
    )

    prefill = f"<|im_start|>assistant\n{think_tag}\n"

    return "\n".join(
        [sys_block, ctx_block, memory_block, tool_block, task_block, prefill]
    )


def build_reader_prompt(
    system: str,
    datetime_block: str,
    rolling_context: str,
    task: str,
    think_tag: str = "<think>",
) -> str:
    """
    Assembles the Reader ChatML prompt (one paragraph per call).

    system          → BUDDY_IDENTITY + READER_PROMPT + schema
    datetime_block  → current time info
    rolling_context → findings from previous paragraphs
    task            → READER_TASK_TEMPLATE (query + paragraph)

    Final token layout the model sees:
      [SYSTEM]   /think + system
      [USER]     <context><datetime>...</datetime><prior_findings>...</prior_findings></context>
      [USER]     <paragraph>{task}</paragraph>
      [ASST]     <think>     ← open prefill
    """
    sys_block = f"<|im_start|>system\n/think\n{system}\n<|im_end|>"

    ctx_block = (
        "<|im_start|>user\n"
        "<context>\n"
        f"<datetime>\n{datetime_block}\n</datetime>\n"
        f"<prior_findings>\n{rolling_context}\n</prior_findings>\n"
        "</context>\n"
        "<|im_end|>"
    )

    paragraph_block = f"<|im_start|>user\n{task}\n<|im_end|>"

    prefill = f"<|im_start|>assistant\n{think_tag}\n"

    return "\n".join([sys_block, ctx_block, paragraph_block, prefill])


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


def build_executor_prompt(
    system: str,
    datetime_block: str,
    tool_info: str,
    instruction: str,
    prior_outputs: str = "",
    step_errors: str = "",
    followups: str = "",
    think_tag: str = "<think>",
) -> str:
    """
    Assembles the Executor ChatML prompt.

    system          → BUDDY_IDENTITY + EXECUTOR_PROMPT + schema (tool_call_format injected)
    datetime_block  → current time info
    tool_info       → tool prompt + exact call format for this step's tool
    instruction     → single step instruction from planner
    prior_outputs   → outputs from previous steps (optional)
    step_errors     → errors from previous attempts at this step (optional)
    followups       → ChatML-formatted Q&A turns from FollowupStack (optional)

    Final token layout the model sees:
      [SYSTEM]   /think + system
      [USER]     <context> datetime + tool_instructions + prior_outputs </context>
      [TOOL]     {step_errors}          ← failed tool results as ground truth
      [USER]     <step>{instruction}</step>
      [ASST]     {followup question}    ← real ChatML turns if followup happened
      [USER]     {user answer}
      [ASST]     <think>                ← open prefill
    """
    sys_block = f"<|im_start|>system\n/think\n{system}\n<|im_end|>"

    ctx_parts = [
        "<|im_start|>user",
        "<context>",
        f"<datetime>\n{datetime_block}\n</datetime>",
        f"<tool_instructions>\n{tool_info}\n</tool_instructions>",
    ]
    if prior_outputs and prior_outputs.strip():
        ctx_parts.append(
            f"<prior_step_outputs>\n{prior_outputs}\n</prior_step_outputs>"
        )
    ctx_parts += ["</context>", "<|im_end|>"]
    ctx_block = "\n".join(ctx_parts)

    step_block = f"<|im_start|>user\n<step_instruction>\n{instruction}\n</step_instruction>\n<|im_end|>"

    prefill = f"<|im_start|>assistant\n{think_tag}\n"

    parts = [sys_block, ctx_block]
    if step_errors and step_errors.strip():
        parts.append(
            f"<|im_start|>tool\n<errors>\n{step_errors}\n</errors>\n<|im_end|>"
        )
    parts.append(step_block)
    if followups and followups.strip():
        parts.append(followups)
    parts.append(prefill)

    return "\n".join(parts)
