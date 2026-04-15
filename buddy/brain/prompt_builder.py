def build_prompt(
    system: str,
    context: str,
    task_input: str,
    thinking: bool = True,
    think_tag: str = "<THINK>",
) -> str:
    """
    Assembles a complete Qwen ChatML prompt.

    system     → static rules, identity, output schema  (never changes per call)
    context    → dynamic data: memories, history, tools  (changes every call)
    task_input → the thing to act on: user query, intent, instruction
    thinking   → /think or /no_think
    think_tag  → your custom tag (default <THINK> to match your existing prompts)

    Final token layout the model sees:
      [SYSTEM]  /think + static rules
      [USER]    dynamic context (memories, history, tools...)
      [ASST]    "Understood. Ready."           ← closes the briefing
      [USER]    task_input                     ← isolated, model weights this highest
      [ASST]    <THINK>                        ← prefill forces reasoning start
    """
    mode = "/think" if thinking else "/no_think"
    prefill = f"<|im_start|>assistant\n{think_tag}\n"

    sys_block = f"<|im_start|>system\n{mode}\n{system}\n<|im_end|>"

    ctx_block = (
        "<|im_start|>user\n"
        f"{context}\n"
        "<|im_end|>\n"
        "<|im_start|>assistant\n"
        "Understood. Ready.\n"
        "<|im_end|>"
    )

    msg_block = f"<|im_start|>user\n{task_input}\n<|im_end|>"

    return "\n".join([sys_block, ctx_block, msg_block, prefill])
