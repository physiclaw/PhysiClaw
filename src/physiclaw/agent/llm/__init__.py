"""The LLM turn producer and its context policy.

producer.py owns the conversation, inference retries and response handling.
context_policy.py declares prompt preferences and compaction cadence.
Import from those modules directly so policy reads never load the producer.
"""
