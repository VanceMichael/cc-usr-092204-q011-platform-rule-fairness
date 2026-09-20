import unittest

from src.catalog import load_context


class CatalogTest(unittest.TestCase):
    def test_context_has_domain_facts(self) -> None:
        context = load_context()
        self.assertTrue(context["project"])
        self.assertGreaterEqual(len(context["facts"]), 3)
        self.assertGreaterEqual(len(context["actors"]), 3)


if __name__ == "__main__":
    unittest.main()
