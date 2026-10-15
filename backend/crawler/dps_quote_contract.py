"""Validate a DPS calculator selection against captured browser-origin controls.

This builds an exact scoped pricing request. A syntactically valid request is not
proof that the supplier will sell the combination; source price and rendered
availability checks remain separate completion requirements.
"""
from __future__ import annotations

import copy
import re
from decimal import Decimal

from .dps_pilot import SCRIPT_SHA256, number


def _rendered_dimension(control: dict, raw: object) -> str:
    value = number(raw, positive=True)
    if control.get("kind") != "select":
        raise ValueError("source_dimension_control_unresolved")
    if not any(
        not choice.get("disabled") and Decimal(number(choice.get("value"), positive=True)) == Decimal(value)
        for choice in control.get("choices", [])
    ):
        raise ValueError("dimension_not_in_source_dropdown")
    return value


def _rendered_quantity(control: dict, raw: object) -> str:
    value = number(raw, integer=True, positive=True)
    if control.get("kind") == "select":
        if not any(
            not choice.get("disabled") and Decimal(number(choice.get("value"), integer=True, positive=True)) == Decimal(value)
            for choice in control.get("choices", [])
        ):
            raise ValueError("quantity_not_in_source_dropdown")
    elif control.get("kind") == "number":
        low = Decimal(number(control.get("minimum"), integer=True, positive=True))
        high = Decimal(number(control.get("maximum"), integer=True, positive=True))
        step = Decimal(number(control.get("step") or "1", integer=True, positive=True))
        if not low <= Decimal(value) <= high or (Decimal(value) - low) % step:
            raise ValueError("quantity_outside_source_control")
    else:
        raise ValueError("source_quantity_control_unresolved")
    return value


def _encoded_visible_groups(groups: list, selected: dict, used: set, choices: list) -> list[str]:
    encoded = []
    for group in groups:
        group_id = number(group.get("id"), integer=True, positive=True)
        if group_id in used or group_id not in selected:
            raise ValueError("visible_option_missing_or_duplicate")
        used.add(group_id)
        chosen_id = number(selected[group_id], integer=True, positive=True)
        matches = [choice for choice in group.get("choices", []) if choice.get("id") == chosen_id]
        if len(matches) != 1 or matches[0].get("redirects"):
            raise ValueError("option_not_selectable_on_source_page")
        choices.append(matches[0])
        encoded.append(group_id + "E" + chosen_id)
        encoded.extend(_encoded_visible_groups(matches[0].get("children", []), selected, used, choices))
    return encoded


def _check_published_size_limits(choices: list, width: str, height: str) -> None:
    dimensions = sorted((Decimal(width), Decimal(height)))
    for choice in choices:
        label = choice.get("label", "")
        match = re.search(r'Max\s+Size\s+(\d+(?:\.\d+)?)\s*"\s*x\s*(\d+(?:\.\d+)?)\s*"', label, re.I)
        if match:
            bounds = sorted((Decimal(match.group(1)), Decimal(match.group(2))))
            if any(size > bound for size, bound in zip(dimensions, bounds)):
                raise ValueError("selection_exceeds_published_size_limit")


def scoped_request(pilot: dict, *, width: object, height: object, quantity: object,
                   selected_options: dict) -> dict:
    """Return the five params used by the pinned source GetPrice function.

    `selected_options` maps source option-group IDs to source choice IDs.
    Child groups are required only when their parent choice is selected.
    """
    if (pilot.get("status") != "completed" or pilot.get("source_contract_verified") is not True
            or pilot.get("source_script_sha256") != SCRIPT_SHA256):
        raise ValueError("completed_pinned_source_pilot_required")
    observations = pilot.get("observations") or []
    if not observations or not isinstance(selected_options, dict):
        raise ValueError("source_controls_unresolved")
    context = observations[0]["context"]
    controls = observations[0]["controls"]
    option_responses = [r for r in pilot.get("responses", []) if r.get("kind") == "options"]
    if len(option_responses) != 1:
        raise ValueError("source_option_tree_unresolved")
    selected = {
        number(group_id, integer=True, positive=True): number(choice_id, integer=True, positive=True)
        for group_id, choice_id in selected_options.items()
    }
    if len(selected) != len(selected_options):
        raise ValueError("duplicate_normalized_option_id")
    used: set[str] = set()
    selected_choices: list = []
    encoded = _encoded_visible_groups(option_responses[0]["option_domain"], selected, used, selected_choices)
    if used != set(selected):
        raise ValueError("hidden_or_unrecognized_option_supplied")
    selected_width = _rendered_dimension(controls["width"], width)
    selected_height = _rendered_dimension(controls["height"], height)
    _check_published_size_limits(selected_choices, selected_width, selected_height)
    return {
        "width": selected_width,
        "height": selected_height,
        "quantity": _rendered_quantity(controls["quantity"], quantity),
        "turnaroundId": number(context["turnaroundId"], integer=True),
        "options": "C".join(encoded),
    }


def annotate_scoped_price_report(report: dict, pilot: dict) -> dict:
    """Add UI-completeness flags without altering immutable price evidence."""
    annotated = copy.deepcopy(report)
    complete = incomplete = 0
    for result in annotated.get("results", []):
        if "price" not in result:
            continue
        request = result["request"]
        try:
            selected = dict(pair.split("E") for pair in request["options"].split("C"))
            expected = scoped_request(
                pilot, width=request["width"], height=request["height"],
                quantity=request["quantity"], selected_options=selected,
            )
            if expected != request:
                raise ValueError("scoped_request_not_current_ui_selection")
        except (KeyError, TypeError, ValueError) as exc:
            result["ui_selection_complete"] = False
            result["ui_validation_reason"] = str(exc)
            incomplete += 1
        else:
            result["ui_selection_complete"] = True
            complete += 1
    annotated["ui_complete_quote_count"] = complete
    annotated["ui_incomplete_quote_count"] = incomplete
    annotated["availability"] = "unverified"
    annotated["final_eligible"] = False
    return annotated
