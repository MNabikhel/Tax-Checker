"""Canada's Business Registries as a source of official names by business number (taxcheck/cbr.py)."""

import unittest

from taxcheck.cbr import BusinessRegistries, parse_docs

# Shaped like the real search's JSON (2026-10-16); names and numbers are made up.
PAYLOAD = {"totalResults": 4, "count": 4, "docs": [
    {"Jurisdiction": "MB", "BN": "123456782", "Company_Name": "PRAIRIE WIDGET INDUSTRIAL PRODUCTS LTD.",
     "Status_State": "Inactive", "text": ["123456782", "239119", "PRAIRIE WIDGET INDUSTRIAL PRODUCTS LTD."]},
    {"Jurisdiction": "AB", "BN": "123456782RC0001", "Status_State": "Active",
     "text": ["123456782RC0001", "2023708452", "WIDGETCO INC."]},  # no Company_Name: the name is in "text"
    {"Jurisdiction": "MB", "BN": "999999998", "Company_Name": "PRAIRIE WIDGET, LTD.", "Status_State": "Active"},
    {"Jurisdiction": "ON", "Company_Name": "PRAIRIE WIDGET LTD", "Status_State": "Active"},  # no number at all
]}


class ParseDocs(unittest.TestCase):
    def test_only_this_numbers_registrations_active_first(self):
        self.assertEqual(parse_docs(PAYLOAD, "123456782"), ["WIDGETCO INC.", "PRAIRIE WIDGET INDUSTRIAL PRODUCTS LTD."])

    def test_nothing_for_another_number(self):
        self.assertEqual(parse_docs(PAYLOAD, "111111118"), [])
        self.assertEqual(parse_docs({}, "123456782"), [])


class Lookups(unittest.TestCase):
    def test_cached_and_shared_and_failures_dont_stop_the_run(self):
        calls = []

        class Session:
            def get(self, url, params, timeout):
                calls.append(params["fq"])
                if "000000000" in params["fq"]:
                    raise ConnectionError("down")

                class R:
                    def raise_for_status(self):
                        pass

                    def json(self):
                        return PAYLOAD
                return R()

        shared = {}
        a = BusinessRegistries(session=Session(), delay=0, cache=shared)
        b = BusinessRegistries(session=Session(), delay=0, cache=shared)
        self.assertEqual(a.names("123456782")[0], "WIDGETCO INC.")
        self.assertEqual(b.names("123456782")[0], "WIDGETCO INC.")  # from the shared cache
        self.assertEqual(calls, ["keyword:{123456782}"])
        a.health.give_up_after = 0
        self.assertEqual(a.names("000000000"), [])
        self.assertIn("lookups failed", a.error)


if __name__ == "__main__":
    unittest.main()
