import importlib.util
import pathlib
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("umu_monitor", ROOT / "scripts" / "umu_monitor.py")
umu = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(umu)


class UMUMonitorTests(unittest.TestCase):
    def test_allowed_official_hosts(self):
        suffixes = [".um.es", "um.es", "sede.um.es"]
        self.assertTrue(umu.allowed_url("https://sede.um.es/sede/test.pdf", suffixes))
        self.assertTrue(umu.allowed_url("https://dafi.inf.um.es/documentos/RRI.pdf", suffixes))
        self.assertFalse(umu.allowed_url("https://example.com/reglamento.pdf", suffixes))

    def test_regulatory_scoring(self):
        score, hits = umu.regulatory_score(
            "Reglamento de Régimen Interno de la Delegación",
            "https://dafi.inf.um.es/documentos/RRI-DAFI.pdf",
            ["reglamento", "régimen interno", "rri", "delegación"],
        )
        self.assertGreaterEqual(score, 4)

    def test_diff(self):
        diff = umu.make_diff("Artículo 1\nTexto antiguo", "Artículo 1\nTexto nuevo")
        joined = "\n".join(diff)
        self.assertIn("-Texto antiguo", joined)
        self.assertIn("+Texto nuevo", joined)


if __name__ == "__main__":
    unittest.main()
