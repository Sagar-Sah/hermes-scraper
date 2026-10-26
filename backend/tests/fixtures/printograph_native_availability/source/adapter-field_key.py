def field_key(value: str) -> str:
    normalized = re.sub(r"[^a-z0-9]", "", value.casefold())
    return ALIASES.get(normalized, normalized)
