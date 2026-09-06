import unittest

from graph.predicates import (
    PredicateError,
    describe,
    matches,
    validate_predicate,
)


def change(changed_keys=(), before=None, after=None):
    return {
        "changed_keys": list(changed_keys),
        "before": before or {},
        "after": after or {},
    }


PROMOTION = change(
    changed_keys=["department"],
    before={"department": "Support", "geo": "IN"},
    after={"department": "Engineering", "geo": "IN"},
)


class ValidateTests(unittest.TestCase):
    def test_valid_predicate_passes(self):
        validate_predicate(
            {"changed": ["department"], "match": {"department": "Engineering"}}
        )

    def test_empty_predicate_is_rejected(self):
        # Would fire on every change to the entity type - almost never intended.
        with self.assertRaisesRegex(PredicateError, "every change"):
            validate_predicate({})

    def test_unknown_clause_is_rejected(self):
        with self.assertRaisesRegex(PredicateError, "unknown predicate clause"):
            validate_predicate({"whenever": ["department"]})

    def test_changed_must_be_a_list_of_strings(self):
        with self.assertRaisesRegex(PredicateError, "list of attribute names"):
            validate_predicate({"changed": "department"})

    def test_unknown_operator_is_rejected(self):
        with self.assertRaisesRegex(PredicateError, "unknown operator"):
            validate_predicate({"match": {"tenure": {"roughly": 90}}})

    def test_operator_object_must_hold_exactly_one_operator(self):
        with self.assertRaisesRegex(PredicateError, "exactly one operator"):
            validate_predicate({"match": {"tenure": {"gt": 1, "lt": 2}}})


class ChangedClauseTests(unittest.TestCase):
    def test_fires_when_a_named_attribute_changed(self):
        self.assertTrue(matches({"changed": ["department"]}, PROMOTION))

    def test_does_not_fire_when_a_different_attribute_changed(self):
        self.assertFalse(matches({"changed": ["title"]}, PROMOTION))

    def test_any_of_the_named_attributes_is_enough(self):
        self.assertTrue(matches({"changed": ["title", "department"]}, PROMOTION))


class MatchClauseTests(unittest.TestCase):
    def test_all_attributes_must_match(self):
        self.assertTrue(
            matches({"match": {"department": "Engineering", "geo": "IN"}}, PROMOTION)
        )
        self.assertFalse(
            matches({"match": {"department": "Engineering", "geo": "US"}}, PROMOTION)
        )

    def test_match_alone_fires_on_every_later_write(self):
        """Why 'changed' matters, expressed as a test.

        A predicate with only 'match' is true on any save where the state still
        holds - so an onboarding workflow would re-run every time anyone edited
        an unrelated field on that employee.
        """
        unrelated_edit = change(
            changed_keys=["title"],
            before={"department": "Engineering", "title": "SWE I"},
            after={"department": "Engineering", "title": "SWE II"},
        )
        self.assertTrue(matches({"match": {"department": "Engineering"}}, unrelated_edit))
        self.assertFalse(
            matches(
                {"changed": ["department"], "match": {"department": "Engineering"}},
                unrelated_edit,
            )
        )


class WasClauseTests(unittest.TestCase):
    def test_expresses_a_transition(self):
        predicate = {
            "changed": ["department"],
            "match": {"department": "Engineering"},
            "was": {"department": {"ne": "Engineering"}},
        }
        self.assertTrue(matches(predicate, PROMOTION))

    def test_blocks_a_move_within_the_same_department(self):
        # Someone already in Engineering whose department field was rewritten
        # should not be onboarded a second time.
        internal = change(
            changed_keys=["department", "title"],
            before={"department": "Engineering"},
            after={"department": "Engineering"},
        )
        predicate = {
            "changed": ["department"],
            "match": {"department": "Engineering"},
            "was": {"department": {"ne": "Engineering"}},
        }
        self.assertFalse(matches(predicate, internal))


class OperatorTests(unittest.TestCase):
    C = change(
        changed_keys=["status"],
        after={"status": "unhealthy", "tenure": 120, "tags": ["prod", "critical"],
               "region": None},
    )

    def test_in_and_not_in(self):
        self.assertTrue(matches({"match": {"status": {"in": ["unhealthy", "down"]}}}, self.C))
        self.assertFalse(matches({"match": {"status": {"in": ["healthy"]}}}, self.C))
        self.assertTrue(matches({"match": {"status": {"not_in": ["healthy"]}}}, self.C))

    def test_ordered_comparisons(self):
        self.assertTrue(matches({"match": {"tenure": {"gt": 90}}}, self.C))
        self.assertFalse(matches({"match": {"tenure": {"lt": 90}}}, self.C))
        self.assertTrue(matches({"match": {"tenure": {"gte": 120}}}, self.C))

    def test_comparing_incompatible_types_is_a_non_match_not_a_crash(self):
        # Attributes are schemaless, so a string can turn up where a number was
        # meant. One bad row must not stop the dispatcher draining the outbox.
        self.assertFalse(matches({"match": {"status": {"gt": 5}}}, self.C))

    def test_exists(self):
        self.assertTrue(matches({"match": {"tenure": {"exists": True}}}, self.C))
        self.assertFalse(matches({"match": {"region": {"exists": True}}}, self.C))
        self.assertTrue(matches({"match": {"missing": {"exists": False}}}, self.C))

    def test_contains_works_on_lists_and_strings(self):
        self.assertTrue(matches({"match": {"tags": {"contains": "prod"}}}, self.C))
        self.assertFalse(matches({"match": {"tags": {"contains": "staging"}}}, self.C))
        self.assertTrue(matches({"match": {"status": {"contains": "health"}}}, self.C))

    def test_contains_on_a_missing_attribute_is_false(self):
        self.assertFalse(matches({"match": {"nope": {"contains": "x"}}}, self.C))


class DescribeTests(unittest.TestCase):
    def test_summarises_a_predicate(self):
        text = describe(
            {
                "changed": ["department"],
                "match": {"department": "Engineering"},
                "was": {"department": {"ne": "Engineering"}},
            }
        )
        self.assertIn("when department changes", text)
        self.assertIn("Engineering", text)


if __name__ == "__main__":
    unittest.main()
