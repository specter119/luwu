import json
import unittest

from luwu.errors import LuwuError
from luwu.ownership import classify_fields


def _baseline(values: dict[str, object], owners: dict[str, str] | None = None) -> bytes:
    return json.dumps(
        {
            "schema_version": 1,
            "resource": "resource",
            "source": "source",
            "target": "target",
            "owners": owners
            or {"source_field": "source", "live_field": "live", "ignored": "ignore"},
            "values": values,
        }
    ).encode()


class OwnershipTests(unittest.TestCase):
    def test_classifies_three_way_states_and_decisions(self) -> None:
        desired = b'{"unchanged":1,"converged":2,"source_field":3,"live_field":4,"conflict":5,"ignored":9,"extra":1}'
        live = b'{"unchanged":1,"converged":2,"source_field":1,"live_field":8,"conflict":6,"ignored":10,"extra":2}'
        baseline = _baseline(
            {
                "unchanged": 1,
                "converged": 0,
                "source_field": 1,
                "live_field": 4,
                "conflict": 1,
            },
            {
                "unchanged": "source",
                "converged": "source",
                "source_field": "source",
                "live_field": "live",
                "conflict": "merge",
                "ignored": "ignore",
            },
        )
        result = classify_fields(
            desired,
            live,
            fields={
                "unchanged": "source",
                "converged": "source",
                "source_field": "source",
                "live_field": "live",
                "conflict": "merge",
                "ignored": "ignore",
            },
            baseline=baseline,
            resource_name="resource",
            source_name="source",
            target_name="target",
        )
        by_name = {field["name"]: field for field in result.to_dict()["fields"]}
        self.assertEqual(by_name["unchanged"]["status"], "unchanged")
        self.assertEqual(by_name["converged"]["status"], "converged")
        self.assertEqual(by_name["source_field"]["decision"], "forward_candidate")
        self.assertEqual(by_name["live_field"]["decision"], "reverse_candidate")
        self.assertEqual(by_name["conflict"]["decision"], "review")
        self.assertEqual(by_name["ignored"]["status"], "ignored")
        self.assertTrue(result.undeclared_changed)

    def test_unbased_has_no_candidate_and_distinguishes_missing_from_null(self) -> None:
        result = classify_fields(
            b'{"missing":null}',
            b"{}",
            fields={"missing": "source"},
            baseline=None,
            resource_name="resource",
            source_name="source",
            target_name="target",
        )
        field = result.to_dict()["fields"][0]
        self.assertEqual(field["status"], "unbased")
        self.assertEqual(field["decision"], "none")
        self.assertEqual(result.baseline_status, "absent")

    def test_baseline_envelope_is_closed_and_bound_to_declaration(self) -> None:
        for baseline in (
            b'{"schema_version":1,"resource":"resource","source":"source","target":"target","owners":{"source_field":"source"},"values":{"unknown":1}}',
            b'{"schema_version":true,"resource":"resource","source":"source","target":"target","owners":{"source_field":"source"},"values":{}}',
            b'{"schema_version":1,"resource":"other","source":"source","target":"target","owners":{"source_field":"source"},"values":{}}',
        ):
            with self.assertRaises(LuwuError) as raised:
                classify_fields(
                    b'{"source_field":1}',
                    b'{"source_field":1}',
                    fields={"source_field": "source"},
                    baseline=baseline,
                    resource_name="resource",
                    source_name="source",
                    target_name="target",
                )
            self.assertEqual(str(raised.exception), "ownership input is invalid")
            self.assertNotIn("unknown", str(raised.exception))
            self.assertNotIn("other", str(raised.exception))

    def test_rejects_non_object_or_non_strict_json_without_echoing_content(
        self,
    ) -> None:
        for desired in (b"[]", b'{"secret":NaN}', b'{"secret":1,"secret":2}'):
            with self.assertRaises(LuwuError) as raised:
                classify_fields(
                    desired,
                    b"{}",
                    fields={"secret": "source"},
                    baseline=None,
                    resource_name="resource",
                    source_name="source",
                    target_name="target",
                )
            self.assertEqual(raised.exception.code, "ownership_invalid_input")
            self.assertNotIn("secret", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
