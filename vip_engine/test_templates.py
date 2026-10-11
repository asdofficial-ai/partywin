"""Structural guardrail for draft WhatsApp template payloads.

Passing this check does NOT mean Meta has approved or will approve templates.
"""
import json
import re
from pathlib import Path

def test_six_review_templates_use_static_button_labels():
    spec = json.loads((Path(__file__).parent / "whatsapp_templates.review.json").read_text())
    templates = spec["templates"]
    assert len(templates) == 6
    assert len({t["name"] for t in templates}) == 6
    for item in templates:
        assert item["category"] in {"UTILITY", "MARKETING"}
        assert item["language"] == "en_US"
        body = next(c for c in item["components"] if c["type"] == "BODY")
        ids = [int(x) for x in re.findall(r"\{\{(\d+)\}\}", body["text"])]
        assert sorted(set(ids)) == list(range(1, max(ids) + 1))
        for component in item["components"]:
            if component["type"] != "BUTTONS":
                continue
            for button in component["buttons"]:
                assert "{{" not in button["text"]
                if button["type"] == "URL":
                    assert button["url"].startswith("https://")
                    assert button["url"].endswith("{{1}}")
                    assert isinstance(button["example"][0], str)
