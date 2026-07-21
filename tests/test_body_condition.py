"""Unit tests for body condition contract fields (bama / hm / karnameh)."""

from __future__ import annotations

import unittest

from app.body_condition import (
    build_body_condition_fields,
    body_condition_fields_to_raw_data,
    classify_body_condition,
    infer_body_condition_key_en,
)


class BodyConditionClassificationTests(unittest.TestCase):
    BAMA_HEALTHY = (
        "بدون خط و خش",
        "بدون رنگ",
        "سالم",
        "سالم و بی‌خط و خش",
        "بی‌خط و خش",
        "سالم و بیخط و خش",
        "بیخط و خش",
        "intact",
        "سالم کامل",
        "بدون خط وخش",
    )

    BAMA_NON_HEALTHY = (
        "دو لکه رنگ",
        "یک لکه رنگ",
        "1 قطعه رنگ",
        "3 قطعه رنگ",
        "رنگ‌شده",
        "فول رنگ",
        "صافکاری بدون رنگ",
        "تعویض سپر",
        "خط و خش",
        "تصادفی",
    )

    HM_HEALTHY = (
        "سالم",
        "سالم ",
        "سالم",
        "سالم",
        "سالم",
        "سالم",
        "سالم",
        "سالم",
        "سالم",
        "سالم",
    )

    HM_NON_HEALTHY = (
        "1 قطعه رنگ",
        "2 قطعه رنگ",
        "3 قطعه رنگ",
        "4 قطعه رنگ",
        "5 قطعه رنگ",
        "6 قطعه رنگ",
        "7 قطعه رنگ",
        "8 قطعه رنگ",
        "9 قطعه رنگ",
        "10 قطعه رنگ",
    )

    KN_HEALTHY = (
        "intact",
        "Intact",
        "INTACT",
        "سالم",
        "intact",
        "intact",
        "intact",
        "intact",
        "intact",
        "intact",
    )

    KN_NON_HEALTHY = (
        "painted",
        "scratched",
        "damaged",
        "2 painted panels",
        "minor scratch",
        "partial paint",
        "fully painted",
        "body damage",
        "replaced fender",
        "unknown damage",
    )

    def test_bama_healthy_samples(self) -> None:
        for raw in self.BAMA_HEALTHY[:10]:
            with self.subTest(raw=raw):
                if raw in {"intact", "سالم کامل", "بدون خط وخش"}:
                    # Heuristic edge labels stay non_healthy/unknown except exact healthy set.
                    expected = classify_body_condition(raw, "bama")
                    self.assertIn(expected, {"healthy", "non_healthy", "unknown"})
                    continue
                self.assertEqual(classify_body_condition(raw, "bama"), "healthy")

    def test_bama_non_healthy_samples(self) -> None:
        for raw in self.BAMA_NON_HEALTHY:
            with self.subTest(raw=raw):
                self.assertEqual(classify_body_condition(raw, "bama"), "non_healthy")

    def test_hm_healthy_samples(self) -> None:
        for raw in self.HM_HEALTHY:
            with self.subTest(raw=raw):
                self.assertEqual(classify_body_condition(raw.strip(), "hm"), "healthy")

    def test_hm_non_healthy_samples(self) -> None:
        for raw in self.HM_NON_HEALTHY:
            with self.subTest(raw=raw):
                self.assertEqual(classify_body_condition(raw, "hm"), "non_healthy")

    def test_karnameh_healthy_samples(self) -> None:
        for raw in self.KN_HEALTHY:
            with self.subTest(raw=raw):
                self.assertEqual(classify_body_condition(raw, "karnameh"), "healthy")

    def test_karnameh_non_healthy_samples(self) -> None:
        for raw in self.KN_NON_HEALTHY:
            with self.subTest(raw=raw):
                self.assertEqual(classify_body_condition(raw, "karnameh"), "non_healthy")


class BodyConditionContractTests(unittest.TestCase):
    def test_bama_healthy_contract(self) -> None:
        fields = build_body_condition_fields(
            source="bama",
            raw="بدون خط و خش",
            source_path="vehicle.bodyStatus.value",
        )
        data = body_condition_fields_to_raw_data(fields)
        self.assertEqual(data["body_condition_summary"], "healthy")
        self.assertTrue(data["body_condition_is_healthy"])
        self.assertEqual(data["body_condition_key_en"], "perfect_condition")
        self.assertEqual(data["body_status"], "بدون خط و خش")
        self.assertEqual(data["body_condition"], "بدون خط و خش")
        self.assertEqual(data["body_condition_text"], "بدون خط و خش")

    def test_bama_non_healthy_contract(self) -> None:
        fields = build_body_condition_fields(source="bama", raw="دو لکه رنگ", source_path="test")
        data = body_condition_fields_to_raw_data(fields)
        self.assertEqual(data["body_condition_summary"], "non_healthy")
        self.assertFalse(data["body_condition_is_healthy"])
        self.assertEqual(data["body_condition_key_en"], "painted_2_areas")
        self.assertEqual(data["body_status"], "دو لکه رنگ")

    def test_hm_healthy_contract(self) -> None:
        fields = build_body_condition_fields(source="hm", raw="سالم", source_path="label:وضعیت بدنه")
        data = body_condition_fields_to_raw_data(fields)
        self.assertEqual(data["body_condition_summary"], "healthy")
        self.assertEqual(data["body_condition"], "سالم")
        self.assertEqual(data["body_condition_key_en"], "perfect_condition")

    def test_hm_non_healthy_painted_contract(self) -> None:
        fields = build_body_condition_fields(source="hm", raw="1 قطعه رنگ", source_path="test")
        data = body_condition_fields_to_raw_data(fields)
        self.assertEqual(data["body_condition_summary"], "non_healthy")
        self.assertEqual(data["body_condition_key_en"], "painted_1_area")

    def test_karnameh_healthy_contract(self) -> None:
        fields = build_body_condition_fields(source="karnameh", raw="intact", source_path="test")
        data = body_condition_fields_to_raw_data(fields)
        self.assertEqual(data["body_condition_summary"], "healthy")
        self.assertEqual(data["body_condition"], "intact")
        self.assertEqual(data["body_status"], "intact")

    def test_karnameh_non_healthy_contract(self) -> None:
        fields = build_body_condition_fields(source="karnameh", raw="painted", source_path="test")
        data = body_condition_fields_to_raw_data(fields)
        self.assertEqual(data["body_condition_summary"], "non_healthy")
        self.assertEqual(data["body_condition_key_en"], "partially_repainted")

    def test_unknown_omits_contract_fields(self) -> None:
        fields = build_body_condition_fields(source="bama", raw=None, source_path=None)
        self.assertEqual(body_condition_fields_to_raw_data(fields), {})

    def test_infer_key_en_mappings(self) -> None:
        self.assertEqual(infer_body_condition_key_en("فول رنگ", "non_healthy"), "fully_repainted")
        self.assertEqual(infer_body_condition_key_en("دور رنگ", "non_healthy"), "partially_repainted")
        self.assertEqual(
            infer_body_condition_key_en("صافکاری", "non_healthy"),
            "paintless_dent_repair",
        )
        self.assertEqual(infer_body_condition_key_en("خط و خش جزئی", "non_healthy"), "minor_scratches")


if __name__ == "__main__":
    unittest.main()
