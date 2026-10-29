from crawler.product_companion_mapping import compare_option_paths


FIELDS = ("coating", "colorspec", "quantity", "radiusofcorners", "shape", "size", "spotuvsides", "stock")


def fixtures():
    selection = {"coating": "No Coating", "colorspec": "4/0", "quantity": "100",
                 "radiusofcorners": "", "shape": "Rectangle", "size": '2" x 3.5"',
                 "spotuvsides": "", "stock": "14PT C2S"}
    site = {"fields": FIELDS, "rows": {("first",): {"selection": selection}}}
    contract = {"binding_complete": True, "rows": [{"child_id": "1", "fields": [
        {"label": key, "choice": value} for key, value in selection.items() if key != "quantity"]}]}
    return contract, site


def test_optional_empty_fields_participate_in_exact_mapping():
    contract, site = fixtures()
    result = compare_option_paths(contract, site)
    assert result["status"] == "match"
    assert result["source_option_path_count"] == 1
    site["rows"][("second",)] = {"selection": dict(site["rows"][("first",)]["selection"], stock="16PT C2S")}
    result = compare_option_paths(contract, site)
    assert result["status"] == "mismatch"
    assert result["extra_storefront_option_paths"][0]["stock"] == "16PT C2S"


def test_unproven_source_binding_cannot_confirm_mapping_from_counts():
    contract, site = fixtures()
    contract.update(binding_complete=False, reason_code="source_binding_hidden")
    assert compare_option_paths(contract, site) == {"status": "unknown", "reason": "source_binding_hidden"}


def test_only_inapplicable_optional_source_sentinels_normalize_to_empty():
    contract, site = fixtures()
    for field in contract["rows"][0]["fields"]:
        if field["label"] in {"radiusofcorners", "spotuvsides"}:
            field["choice"] = "-"
    result = compare_option_paths(contract, site)
    assert result["status"] == "match"
    assert result["representation_normalization"]["source_field_values_normalized"] == {
        "radiusofcorners": 1, "spotuvsides": 1}
    for field in contract["rows"][0]["fields"]:
        if field["label"] == "stock":
            field["choice"] = "-"
    assert compare_option_paths(contract, site)["status"] == "mismatch"


def test_spot_uv_back_full_uv_front_is_not_equated_with_spot_uv_back():
    contract, site = fixtures()
    for field in contract["rows"][0]["fields"]:
        if field["label"] == "spotuvsides":
            field["choice"] = "Spot UV Back - Full UV on the Front"
    site["rows"][("first",)]["selection"]["spotuvsides"] = "Spot UV Back"
    result = compare_option_paths(contract, site)
    assert result["status"] == "mismatch"
    assert result["missing_storefront_option_paths"][0]["spotuvsides"] == "Spot UV Back - Full UV on the Front"
    assert result["extra_storefront_option_paths"][0]["spotuvsides"] == "Spot UV Back"
