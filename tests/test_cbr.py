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


class PossibleNumber(unittest.TestCase):
    NAME_SEARCH = {"docs": [
        {"BN": "123456782", "Company_Name": "PRAIRIE WIDGET MODELS INC.", "Status_State": "Active"},
        {"BN": "123456782", "Company_Name": "PRAIRIE WIDGET MODELS INC.", "Status_State": "Active"},  # another province
        {"BN": "999999998", "Company_Name": "PRAIRIE WIDGET MODELS GP INC.", "Status_State": "Active"},  # another entity
        {"Company_Name": "PRAIRIE WIDGET MODELS LTD", "Status_State": "Active"},  # no number
    ]}

    def test_only_exactly_the_vendors_name(self):
        from taxcheck.cbr import numbers_by_name

        self.assertEqual(numbers_by_name(self.NAME_SEARCH, "Prairie Widget Models Ltd."),
                         [("123456782", "PRAIRIE WIDGET MODELS INC.")])
        self.assertEqual(numbers_by_name(self.NAME_SEARCH, "PRAIRIE WIDGET MODELS LP"), [])

    def checker(self, found, cra_answer):
        import datetime as dt

        from taxcheck.checks import RowChecker

        class Source:
            label = "Canada's Business Registries"

            def numbers_for_name(self, name):
                return found

        class Gst:
            name_sources = [Source()]
            asked = []

            def registry_lookup(self, bn9, name, date):
                self.asked.append((bn9, name))
                return cra_answer

            def check(self, *a, **k):
                raise AssertionError("no number: CRA isn't asked about the sheet's number")

        gst = Gst()
        rc = RowChecker(gst, None, available={"name", "gst", "province"})
        return rc.check({"name": "PRAIRIE WIDGET MODELS INC", "gst": None, "province": "MB"}, dt.date(2026, 9, 1))["gst"], gst

    def test_lead_when_cra_confirms(self):
        from taxcheck.report import gst_issue
        from taxcheck.result import MISSING

        res, gst = self.checker([("123456782", "PRAIRIE WIDGET MODELS INC.")], "registered")
        self.assertEqual(res.status, MISSING)  # still a missing number: a lead, not a fact about the vendor record
        self.assertEqual(res.facts["possible_number"], "123456782")
        self.assertEqual(gst_issue(res)[0], "No GST/HST number - possible number found")
        self.assertIn("123456782RT0001", gst_issue(res)[1])

    def test_no_lead_when_cra_doesnt_confirm_or_names_are_ambiguous(self):
        res, _ = self.checker([("123456782", "PRAIRIE WIDGET MODELS INC.")], "no_match")
        self.assertNotIn("possible_number", res.facts)
        res, gst = self.checker([("123456782", "X"), ("999999998", "Y")], "registered")
        self.assertNotIn("possible_number", res.facts)
        self.assertEqual(gst.asked, [])


if __name__ == "__main__":
    unittest.main()
