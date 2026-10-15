import unittest
from crawler.browser_discovery import match_titles


class TitleMatching(unittest.TestCase):
    def test_exact_matching_prefers_observed_order_link(self):
        result = match_titles(
            [
                {"title": "Business Cards", "url": "https://example.com/cards"},
                {
                    "title": "Business Cards",
                    "url": "https://example.com/products/cards/order",
                },
            ],
            [" business  cards "],
        )
        self.assertEqual(
            result["matches"][0]["url"], "https://example.com/products/cards/order"
        )

    def test_ambiguous_and_fuzzy_matches_require_selection(self):
        candidates = [
            {"title": "Labels", "url": "https://example.com/one/order"},
            {"title": "Labels", "url": "https://example.com/two/order"},
            {"title": "Premium Cards", "url": "https://example.com/three/order"},
        ]
        result = match_titles(candidates, ["Labels", "Cards"])
        self.assertEqual(result["matches"], [])
        self.assertEqual(len(result["unresolved"]), 2)
        self.assertEqual(len(result["unresolved"][0]["candidates"]), 2)


if __name__ == "__main__":
    unittest.main()
