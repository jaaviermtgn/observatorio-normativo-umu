import importlib.util
import pathlib
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("borm_monitor", ROOT / "scripts" / "borm_monitor.py")
borm = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(borm)


class BORMMonitorTests(unittest.TestCase):
    def test_parse_summary_wrapped_title(self):
        text = """
        Boletín Oficial de la REGIÓN de MURCIA
        Número 174 Viernes, 30 de julio de 2021
        SUMARIO
        I. Comunidad Autónoma
        1. Disposiciones Generales
        Consejo de Gobierno
        5111 Decreto de n.º 152/2021, de 29 de julio, por el que se establece
        la regulación de los precios públicos a satisfacer por la prestación
        de servicios académicos universitarios en la Comunidad Autónoma
        de la Región de Murcia. 22772
        5112 Resolución del Rector de la Universidad de Murcia, por la que se
        nombra a una Profesora Titular de Universidad. 22783
        """
        items = borm.parse_summary_items(text)
        self.assertEqual(items[0]["publicacion_numero"], "5111")
        self.assertIn("precios públicos", items[0]["titulo"])
        self.assertEqual(len(items), 2)

    def test_relevant_norm_scores_high(self):
        title = "Decreto n.º 8/2024 por el que se modifica el Decreto 152/2021 sobre precios públicos universitarios"
        score, reasons = borm.score_title(title)
        self.assertGreaterEqual(score, 6)
        self.assertIn("decreto", reasons)

    def test_appointment_scores_low(self):
        title = "Resolución del Rector de la Universidad de Murcia por la que se nombra Profesor Titular de Universidad"
        score, _ = borm.score_title(title)
        self.assertLess(score, 6)

    def test_classification(self):
        self.assertEqual(
            borm.classify("Decreto por el que se modifica el Decreto 152/2021"),
            "Posible modificación normativa",
        )


if __name__ == "__main__":
    unittest.main()
