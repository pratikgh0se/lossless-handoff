# Live check: SubagentStart additionalContext (2026-11-18, Claude Code 2.1.326, haiku)
Project with only a SubagentStart hook that returned a random token as additionalContext. The brief to the subagent never held the token.
- Run 1: token KILO-4817; subagent replied "KILO-4817"; main transcript has no hook_additional_context record; token enters main only via the subagent's completion notice. Cost $0.0287.
- Run 2: token KILO-9352; same result. Cost $0.0119.
- Hook input keys: session_id, transcript_path, cwd, prompt_id, agent_id, agent_type, hook_event_name.
- Subagent transcript: token at line 2, attachment type hook_additional_context.
Verdict: reaches the subagent (2/2); does not leak into the main context (2/2).
