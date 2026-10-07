"""Parsing of real responses saved from the government services (tests/fixtures)."""

import json
import unittest
from pathlib import Path

from taxcheck.gst import parse_registry_result
from taxcheck.qst import QstChecker
from taxcheck.result import ERROR, INVALID, NOT_CONFIRMED, NOT_REGISTERED, REGISTERED

FIXTURES = Path(__file__).parent / "fixtures"


def fixture(name):
    return (FIXTURES / name).read_text(encoding="utf-8")


class CraRegistryPages(unittest.TestCase):
    def test_registered(self):
        self.assertEqual(parse_registry_result(fixture("cra_registered.html"))[0], "registered")

    def test_name_or_number_mismatch(self):
        self.assertEqual(parse_registry_result(fixture("cra_insufficient.html"))[0], "no_match")

    def test_not_registered_on_date(self):
        self.assertEqual(parse_registry_result(fixture("cra_not_registered_on_date.html"))[0], "not_registered")

    def test_invalid_number(self):
        self.assertEqual(parse_registry_result(fixture("cra_invalid_number.html"))[0], "invalid")

    def test_future_date_is_an_error_not_a_result(self):
        with self.assertRaisesRegex(RuntimeError, "future date"):
            parse_registry_result(fixture("cra_future_date.html"))

    def test_unrecognised_page_raises(self):
        with self.assertRaises(RuntimeError):
            parse_registry_result("<html><body>Service unavailable</body></html>")


class FakeResponse:
    def __init__(self, status, body):
        self.status_code = status
        self.text = body

    def json(self):
        return json.loads(self.text)


class FakeSession:
    def __init__(self, response):
        self.response = response
        self.urls = []

    def get(self, url, timeout=None):
        self.urls.append(url)
        return self.response


def qst_with(status, body):
    checker = QstChecker(delay=0)
    checker.session = FakeSession(FakeResponse(status, body))
    return checker


class QstApiResponses(unittest.TestCase):
    def test_registered(self):
        checker = qst_with(200, fixture("qst_registered.json"))
        res = checker.check("1019288451TQ0004")
        self.assertEqual(res.status, REGISTERED)
        self.assertIn("2017-08-01", res.detail)
        self.assertEqual(res.registered_name, "AVIENT CANADA ULC (trade name: SPARTECH PLASTIQUES)")
        self.assertTrue(checker.session.urls[0].endswith("/1019288451TQ0004"))

    def test_invalid(self):
        self.assertEqual(qst_with(400, fixture("qst_invalid.json")).check("1234567890TQ0001").status, INVALID)

    def test_not_found(self):
        # Documented response for a well-formed number that isn't registered (Revenu Québec guide SW-340-V).
        body = '{"Resultat":null,"OperationReussie":false,"MessagesFonctionnels":[{"CodeMessage":"GX.AucuneDonneeTrouvee"}]}'
        self.assertEqual(qst_with(404, body).check("1234567891TQ0001").status, NOT_REGISTERED)

    def test_cancelled(self):
        res = qst_with(200, fixture("qst_cancelled.json")).check("1019288451TQ0001")
        self.assertEqual(res.status, NOT_REGISTERED)
        self.assertIn("cancelled since 2007-09-02", res.detail)

    def test_assumed_suffix_is_called_out_when_not_registered(self):
        res = qst_with(200, fixture("qst_cancelled.json")).check("1019288451TQ0001", suffix_assumed=True)
        self.assertIn("No TQ suffix was given", res.detail)
        res = qst_with(200, fixture("qst_registered.json")).check("1019288451TQ0001", suffix_assumed=True)
        self.assertNotIn("No TQ suffix", res.detail)

    def test_unknown_status_needs_review(self):
        body = json.dumps({
            "Resultat": {"StatutSousDossierUsager": "X", "DescriptionStatut": "Autre",
                         "DateStatut": "2024-01-31T00:00:00", "NomEntreprise": "X INC", "RaisonSociale": None},
            "OperationReussie": True, "MessagesFonctionnels": [],
        })
        res = qst_with(200, body).check("1019288451TQ0004")
        self.assertEqual(res.status, NOT_CONFIRMED)
        self.assertIn("Autre", res.detail)

    def test_non_json_is_error(self):
        self.assertEqual(qst_with(503, "<html>down</html>").check("1019288451TQ0004").status, ERROR)


if __name__ == "__main__":
    unittest.main()
