"""Synthetic native chat responses for engine and replay tests."""

import json


def response(vendor, value, index, *, finish=None):
    if vendor == "openai":
        return {
            "id": f"r{index}",
            "model": "fixture",
            "status": "failed"
            if finish == "error"
            else "incomplete"
            if finish in {"length", "content_filter"}
            else "completed",
            "incomplete_details": {
                "reason": "max_output_tokens"
                if finish == "length"
                else "content_filter"
            }
            if finish
            else None,
            "output": [
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": json.dumps(value)}],
                }
            ],
            "usage": {"input_tokens": 10, "output_tokens": 20},
        }
    if vendor == "anthropic":
        return {
            "id": f"r{index}",
            "model": "fixture",
            "type": "message",
            "role": "assistant",
            "content": [
                {
                    "type": "thinking",
                    "thinking": "opaque",
                    "signature": f"signed{index}",
                },
                {
                    "type": "tool_use",
                    "id": f"envelope{index}",
                    "name": "submit_turn",
                    "input": value,
                },
            ],
            "stop_reason": finish or "tool_use",
            "usage": {"input_tokens": 10, "output_tokens": 20},
        }
    call = {
        "id": f"envelope{index}",
        "type": "function",
        "function": {"name": "submit_turn", "arguments": json.dumps(value)},
    }
    if vendor == "google":
        call["extra_content"] = {"google": {"thought_signature": f"signed{index}"}}
    return {
        "id": f"r{index}",
        "model": "fixture",
        "choices": [
            {
                "finish_reason": finish or "tool_calls",
                "message": {
                    "role": "assistant",
                    "content": "",
                    "reasoning_content": f"thinking{index}",
                    "tool_calls": [call],
                    "reasoning_details": [
                        {"type": "reasoning.encrypted", "data": f"opaque{index}"}
                    ],
                },
            }
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 20},
    }
