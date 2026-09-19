import importlib.util
import pathlib
import unittest
import xml.etree.ElementTree as ET

ROOT = pathlib.Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("boe_monitor", ROOT / "scripts" / "boe_monitor.py")
boe = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(boe)


class BOEMonitorTests(unittest.TestCase):
    def test_extract_blocks_from_json(self):
        payload = {
            "status": {"code": "200", "text": "ok"},
            "data": {
                "bloque": [
                    {
                        "id": "a1",
                        "titulo": "Artículo 1",
                        "fecha_actualizacion": "20220915",
                        "url": "https://example.invalid/a1",
                    },
                    {
                        "id": "df",
                        "titulo": "Disposición final",
                        "fecha_actualizacion": "20221115",
                        "url": "https://example.invalid/df",
                    },
                ]
            },
        }
        blocks = boe.extract_blocks(payload)
        self.assertEqual(blocks["a1"]["titulo"], "Artículo 1")
        self.assertEqual(blocks["df"]["fecha_actualizacion"], "20221115")

    def test_parse_versions_and_latest(self):
        xml = """
        <response>
          <status><code>200</code><text>ok</text></status>
          <data>
            <bloque>
              <version id_norma="BOE-A-2023-1" fecha_publicacion="20230101" fecha_vigencia="20230102">
                <p class="articulo">Artículo 1.</p>
                <p class="parrafo">Texto original.</p>
              </version>
              <version id_norma="BOE-A-2024-2" fecha_publicacion="20240203" fecha_vigencia="20240204">
                <p class="articulo">Artículo 1.</p>
                <p class="parrafo">Texto modificado.</p>
                <blockquote><p>Se modifica por la norma posterior.</p></blockquote>
              </version>
            </bloque>
          </data>
        </response>
        """
        versions = boe.parse_block_xml(ET.fromstring(xml))
        self.assertEqual(len(versions), 2)
        self.assertEqual(versions[-1]["id_norma"], "BOE-A-2024-2")
        self.assertIn("Texto modificado.", versions[-1]["texto"])
        self.assertEqual(len(versions[-1]["notas"]), 1)

    def test_date_format(self):
        self.assertEqual(boe.compact_date_to_es("20240802"), "02/08/2024")


if __name__ == "__main__":
    unittest.main()
