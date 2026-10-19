"""Bound conversation history without counting image transport bytes as text."""
import json


def context_chars(messages):
    size = 0
    for message in messages:
        content = message.get("content")
        if isinstance(content, list):
            content = [
                {"type": "image_url", "image_url": {"url": "[image]"}}
                if part.get("type") == "image_url" else part
                for part in content
            ]
        size += len(json.dumps({**message, "content": content}, ensure_ascii=False,
                               separators=(",", ":")))
    return size


def compact_history(messages, max_chars=60000, history_chars=12000):
    """Keep complete tool exchanges, including the latest result even for a large base."""
    base, groups = messages[:2], []
    for message in messages[2:]:
        if message["role"] == "tool" and groups:
            groups[-1].append(message)
        else:
            groups.append([message])
    if not groups:
        return messages
    budget = min(history_chars, max(0, max_chars - context_chars(base)))
    latest_exchange = next((index for index in range(len(groups) - 1, -1, -1)
                            if groups[index][0]["role"] == "assistant"), len(groups) - 1)
    retained, used = [], 0
    for index in range(len(groups) - 1, -1, -1):
        group = groups[index]
        size = context_chars(group)
        if index < latest_exchange and used + size > budget:
            break
        retained.append(group)
        used += size
    return [*base, *(message for group in reversed(retained) for message in group)]


def without_empty_fields(value):
    """Omit empty object fields, preserving zero, false, unknowns and array positions."""
    if isinstance(value, dict):
        return {key: without_empty_fields(item) for key, item in value.items()
                if item is not None and item != "" and item != [] and item != {}}
    if isinstance(value, list):
        return [without_empty_fields(item) for item in value]
    return value
