"""Autonomous phone agent.

Flow: hooks produce triggers → runtime schedules sessions → engine composes
producers → loop selects a turn → dispatch validates and executes its tools.

`llm.producer` owns the model's conversation and retries; `llm.context_policy`
defines its prompt and compaction settings. `engine.policy` holds behavior
rules; `engine.facts` exposes neutral observations and action receipts to all
producers. `layout` stores learned phone geometry; `trace` records sessions and
provides the offline viewer. Persistent jobs, memory and skills live under
`engine`; their storage formats are separate from the turn driver.
"""
