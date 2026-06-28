import unittest
from time import monotonic
from unittest.mock import patch

from hermes.governor_results.area_resolution import (
    DistrictLookup,
    extract_area_numbers_from_text,
    format_area_label,
    infer_area_id_from_document_hints,
    normalize_area_id_value,
    parse_area_id_from_text,
    strip_area_prefix,
)


class AreaResolutionTests(unittest.TestCase):
    def _lookup(self) -> DistrictLookup:
        lookup = DistrictLookup(url="https://example.test/districts.json")
        lookup._districts = [
            {
                "id": 3,
                "provinceCode": 10,
                "districtNameTh": "หนองจอก",
                "districtNameEn": "Nong Chok",
                "areaNumber": 3,
            },
            {
                "id": 13,
                "provinceCode": 10,
                "districtNameTh": "สายไหม",
                "districtNameEn": "Sai Mai",
                "areaNumber": 13,
            },
            {
                "id": 37,
                "provinceCode": 10,
                "districtNameTh": "ราชเทวี",
                "districtNameEn": "Ratchathewi",
                "areaNumber": 37,
            },
        ]
        lookup._by_id = {str(d["id"]): d for d in lookup._districts}
        lookup._by_name = {
            "หนองจอก": lookup._districts[0],
            "nong chok": lookup._districts[0],
            "เขตหนองจอก": lookup._districts[0],
            "สายไหม": lookup._districts[1],
            "sai mai": lookup._districts[1],
            "เขตสายไหม": lookup._districts[1],
            "ราชเทวี": lookup._districts[2],
            "ratchathewi": lookup._districts[2],
            "เขตราชเทวี": lookup._districts[2],
        }
        lookup._cached_at = monotonic()
        return lookup

    def test_strip_area_prefix(self) -> None:
        self.assertEqual(strip_area_prefix("เขต หนองจอก"), "หนองจอก")
        self.assertEqual(strip_area_prefix("เขตราชเทวี"), "ราชเทวี")
        self.assertEqual(strip_area_prefix("area: 13"), "13")
        self.assertEqual(strip_area_prefix("เขต. 37"), "37")

    def test_extract_area_numbers_from_text(self) -> None:
        self.assertEqual(extract_area_numbers_from_text("เขต37"), ["37"])
        self.assertEqual(extract_area_numbers_from_text("37เขต"), ["37"])
        self.assertEqual(extract_area_numbers_from_text("ลำดับเขต 13"), ["13"])
        self.assertEqual(extract_area_numbers_from_text("เขต 37 กทม"), ["37"])

    def test_infer_area_id_from_ratchathewi_notes(self) -> None:
        lookup = self._lookup()
        with patch("hermes.governor_results.area_resolution.get_district_lookup", return_value=lookup):
            self.assertEqual(parse_area_id_from_text("เขตราชเทวี"), "37")
            self.assertEqual(parse_area_id_from_text("'เขตราชเทวี'"), "37")
            self.assertEqual(parse_area_id_from_text("เขต37"), "37")
            self.assertEqual(parse_area_id_from_text("37เขต"), "37")
            self.assertEqual(parse_area_id_from_text("Ratchathewi"), "37")
            self.assertEqual(
                infer_area_id_from_document_hints(
                    area_id=None,
                    notes="หัวตารางระบุ 'เขตราชเทวี' ไม่มีเลขเขตชัดเจน area_id จึงใส่ null",
                    summary_text="พบคะแนนผู้สมัครเขตราชเทวี ผู้มีสิทธิเลือกตั้ง 47,507 คน",
                ),
                "37",
            )
            self.assertEqual(
                infer_area_id_from_document_hints(
                    area_id="เขตราชเทวี",
                    notes=None,
                    summary_text=None,
                ),
                "37",
            )
            self.assertEqual(lookup.format_area_label("37"), "เขต: เขต 37 ราชเทวี")

    def test_resolve_area_id_from_number_or_name(self) -> None:
        lookup = self._lookup()
        self.assertEqual(lookup.resolve_area_id("13"), "13")
        self.assertEqual(lookup.resolve_area_id("เขต หนองจอก"), "3")
        self.assertEqual(lookup.resolve_area_id("สายไหม"), "13")
        self.assertEqual(lookup.resolve_area_id("เขต37"), "37")
        self.assertIsNone(lookup.resolve_area_id("ไม่มีเขตนี้"))

    def test_format_area_label(self) -> None:
        lookup = self._lookup()
        self.assertEqual(lookup.format_area_label("3"), "เขต: เขต 3 หนองจอก")
        self.assertEqual(lookup.format_area_label(None), "เขต: ยังไม่พบ")
        self.assertEqual(lookup.format_area_label("999"), "เขต: 999")

    def test_parse_area_id_from_text(self) -> None:
        with patch("hermes.governor_results.area_resolution.get_district_lookup", return_value=self._lookup()):
            self.assertEqual(parse_area_id_from_text("แก้ไข เขต 15"), "15")
            self.assertEqual(parse_area_id_from_text("เขต 13"), "13")
            self.assertEqual(parse_area_id_from_text("เขต หนองจอก"), "3")
            self.assertEqual(parse_area_id_from_text("แก้ไข สายไหม"), "13")
            self.assertEqual(parse_area_id_from_text("13 เขต"), "13")
            self.assertEqual(parse_area_id_from_text("แก้ เขต=37"), "37")
            self.assertEqual(parse_area_id_from_text("area 37"), "37")

    def test_normalize_area_id_value(self) -> None:
        with patch("hermes.governor_results.area_resolution.get_district_lookup", return_value=self._lookup()):
            self.assertEqual(normalize_area_id_value("หนองจอก"), "3")
            self.assertEqual(normalize_area_id_value("14"), "14")

    def test_infer_from_raw_model_text(self) -> None:
        lookup = self._lookup()
        raw = (
            '{"area_id":null,"notes":"หัวตารางระบุ \'เขตราชเทวี\'",'
            '"summary_text":"พบคะแนนผู้สมัครเขตราชเทวี"}'
        )
        with patch("hermes.governor_results.area_resolution.get_district_lookup", return_value=lookup):
            self.assertEqual(
                infer_area_id_from_document_hints(
                    area_id=None,
                    notes=None,
                    summary_text=None,
                    raw_model_text=raw,
                ),
                "37",
            )

    def test_format_area_label_without_lookup(self) -> None:
        with patch("hermes.governor_results.area_resolution.get_district_lookup", return_value=None):
            self.assertEqual(format_area_label("13"), "เขต: 13")
            self.assertEqual(format_area_label(None), "เขต: ยังไม่พบ")


if __name__ == "__main__":
    unittest.main()
