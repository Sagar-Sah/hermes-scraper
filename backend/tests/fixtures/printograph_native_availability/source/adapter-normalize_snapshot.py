def normalize_snapshot(snapshot: dict) -> dict:
    # Preserve diagnostics independently of the finite visible product controls.
    snapshot["service_context"] = deepcopy(snapshot.get("service_context", []))
    seen = set()
    for control in snapshot["controls"]:
        raw = field_key(control["raw_key"])
        labelled = field_key(control["label"])
        control["key"] = (
            labelled
            if labelled
            in set(ALIASES.values())
            | {
                "size",
                "shape",
                "finish",
                "paper",
                "color",
                "orientation",
                "quantity",
                "coating",
            }
            else raw
        )
        if control["key"] in seen:
            raise AdapterError(
                "unsupported_controls",
                f"Ambiguous visible controls named {control['key']}",
            )
        seen.add(control["key"])
    return snapshot
