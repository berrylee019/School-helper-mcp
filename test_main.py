"""학교 급식·일정 도우미 테스트 (NEIS 응답은 모의값으로 대체: python -m unittest test_main -v)"""
import inspect
import os
import unittest
from datetime import date

import main

SCH = {"ATPT_OFCDC_SC_CODE": "J10", "ATPT_OFCDC_SC_NM": "경기도교육청", "SD_SCHUL_CODE": "7530001",
       "SCHUL_NM": "가나초등학교", "SCHUL_KND_SC_NM": "초등학교", "FOND_SC_NM": "공립", "LCTN_SC_NM": "경기도",
       "ORG_RDNMA": "경기도 하남시 어딘가로 1"}
SCH2 = dict(SCH, SD_SCHUL_CODE="7530002", ORG_RDNMA="경기도 성남시 어딘가로 2")


def fake(table):
    """table: endpoint -> (code, rows) 또는 예외"""
    calls = []

    def f(endpoint, params, ttl=0):
        calls.append((endpoint, dict(params)))
        v = table[endpoint]
        if isinstance(v, Exception):
            raise v
        return v
    f.calls = calls
    return f


class Base(unittest.TestCase):
    def setUp(self):
        self._orig = main.neis_get

    def tearDown(self):
        main.neis_get = self._orig


class Dish(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(main.parse_dish("미역국 (5.6.13)"), ("미역국", [5, 6, 13]))
        self.assertEqual(main.parse_dish("쌀밥"), ("쌀밥", []))
        self.assertEqual(main.parse_dish("우유 2"), ("우유 2", []))   # 괄호·점 없는 숫자는 알레르기로 보지 않음
        self.assertEqual(main.parse_dish("김치(9)"), ("김치", [9]))
        self.assertEqual(main.parse_dish("떡볶이(99)")[1], [])
        self.assertEqual(len(main.ALLERGENS), 19)

    def test_split(self):
        self.assertEqual([n for n, _ in main.dishes("쌀밥<br/>미역국 (5.6)<br/>김치 (9)")], ["쌀밥", "미역국", "김치"])


class Meal(Base):
    def rows(self):
        return [{"MLSV_YMD": "20261105", "MMEAL_SC_CODE": "2", "MMEAL_SC_NM": "중식",
                 "DDISH_NM": "쌀밥<br/>미역국 (5.6)<br/>새우볶음 (9)", "CAL_INFO": "650.1 Kcal",
                 "NTR_INFO": "탄수화물(g) : 90.1<br/>단백질(g) : 30.2"}]

    def test_ok(self):
        f = fake({"schoolInfo": ("INFO-000", [SCH]), "mealServiceDietInfo": ("INFO-000", self.rows())})
        main.neis_get = f
        r = main.get_school_meal("가나초등학교", "2026-11-05")
        self.assertIn("2026-11-05(목)", r)
        self.assertIn("· 미역국 (알레르기: 대두, 밀)", r)
        self.assertIn("· 새우볶음 (알레르기: 새우)", r)
        self.assertIn("650.1 Kcal", r)
        self.assertNotIn("영양:", r)
        self.assertIn("5=대두", r)
        self.assertIn("학교 공지로 꼭 최종 확인", r)
        self.assertIn("출처: 교육부 NEIS", r)
        ep, p = f.calls[1]
        self.assertEqual((p["MLSV_FROM_YMD"], p["MLSV_TO_YMD"]), ("20261105", "20261105"))
        self.assertEqual(p["SD_SCHUL_CODE"], "7530001")
        self.assertNotIn("KEY", p)

    def test_nutrition_and_type(self):
        main.neis_get = fake({"schoolInfo": ("INFO-000", [SCH]), "mealServiceDietInfo": ("INFO-000", self.rows())})
        r = main.get_school_meal("가나초등학교", "2026-11-05", include_nutrition=True)
        self.assertIn("영양: 탄수화물(g) : 90.1 / 단백질(g) : 30.2", r)
        self.assertTrue(main.get_school_meal("가나초등학교", meal_type="간식").startswith("[입력 확인]"))

    def test_range_params(self):
        f = fake({"schoolInfo": ("INFO-000", [SCH]), "mealServiceDietInfo": ("INFO-200", [])})
        main.neis_get = f
        r = main.get_school_meal("가나초등학교", "2026-11-05", days=3, meal_type="중식")
        self.assertEqual(f.calls[1][1]["MLSV_TO_YMD"], "20261107")
        self.assertEqual(f.calls[1][1]["MMEAL_SC_CODE"], "2")
        self.assertIn("등록된 급식 정보가 없습니다", r)

    def test_no_data_code_via_call(self):
        main.neis_get = fake({"schoolInfo": ("INFO-200", [])})
        self.assertIn("찾지 못했습니다", main.get_school_meal("없는학교"))

    def test_bad_date(self):
        self.assertTrue(main.get_school_meal("가나초등학교", "내일").startswith("[입력 확인]"))


class Ambiguity(Base):
    def test_multi(self):
        main.neis_get = fake({"schoolInfo": ("INFO-000", [SCH, SCH2])})
        r = main.get_school_meal("가나초등학교", "2026-11-05")
        self.assertTrue(r.startswith("[입력 확인]"))
        self.assertIn("2곳", r)
        self.assertIn("하남시", r)
        self.assertIn("성남시", r)

    def test_region_narrow(self):
        f = fake({"schoolInfo": ("INFO-000", [SCH, SCH2]), "mealServiceDietInfo": ("INFO-200", [])})
        main.neis_get = f
        r = main.get_school_meal("가나초등학교", "2026-11-05", region="성남시")
        self.assertEqual(f.calls[1][1]["SD_SCHUL_CODE"], "7530002")
        self.assertIn("급식", r)

    def test_short_name(self):
        self.assertTrue(main.get_school_meal("가").startswith("[입력 확인]"))

    def test_search(self):
        main.neis_get = fake({"schoolInfo": ("INFO-000", [SCH, SCH2])})
        r = main.search_school("가나초등학교")
        self.assertIn("2곳", r)
        self.assertIn("어딘가로 1", r)


class Schedule(Base):
    def rows(self):
        return [
            {"AA_YMD": "20261120", "EVENT_NM": "현장체험학습", "SBTR_DD_SC_NM": "해당없음",
             "ONE_GRADE_EVENT_YN": "N", "TW_GRADE_EVENT_YN": "Y", "THREE_GRADE_EVENT_YN": "Y",
             "FR_GRADE_EVENT_YN": "N", "FIV_GRADE_EVENT_YN": "N", "SIX_GRADE_EVENT_YN": "N"},
            {"AA_YMD": "20261110", "EVENT_NM": "재량휴업일", "SBTR_DD_SC_NM": "휴업일",
             "ONE_GRADE_EVENT_YN": "Y", "TW_GRADE_EVENT_YN": "Y", "THREE_GRADE_EVENT_YN": "Y",
             "FR_GRADE_EVENT_YN": "Y", "FIV_GRADE_EVENT_YN": "Y", "SIX_GRADE_EVENT_YN": "Y"}]

    def test_default_range_and_sort(self):
        f = fake({"schoolInfo": ("INFO-000", [SCH]), "SchoolSchedule": ("INFO-000", self.rows())})
        main.neis_get = f
        r = main.get_school_schedule("가나초등학교", "2026-11-05")
        self.assertEqual(f.calls[1][1]["AA_TO_YMD"], "20261130")
        self.assertLess(r.index("재량휴업일"), r.index("현장체험학습"))
        self.assertIn("[휴업일]", r)
        self.assertNotIn("[해당없음]", r)

    def test_grade_filter(self):
        main.neis_get = fake({"schoolInfo": ("INFO-000", [SCH]), "SchoolSchedule": ("INFO-000", self.rows())})
        r = main.get_school_schedule("가나초등학교", "2026-11-05", grade=1)
        self.assertIn("재량휴업일", r)
        self.assertNotIn("현장체험학습", r)

    def test_invalid(self):
        self.assertTrue(main.get_school_schedule("가나초등학교", "2026-11-05", "2026-11-01").startswith("[입력 확인]"))
        self.assertTrue(main.get_school_schedule("가나초등학교", "2026-01-01", "2026-12-31").startswith("[입력 확인]"))

    def test_month_end(self):
        self.assertEqual(main.month_end(date(2026, 12, 5)), date(2026, 12, 31))
        self.assertEqual(main.month_end(date(2028, 2, 3)), date(2028, 2, 29))


class Errors(Base):
    def test_unavailable(self):
        main.neis_get = fake({"schoolInfo": main.NeisUnavailable()})
        self.assertTrue(main.get_school_meal("가나초등학교").startswith("[조회 실패]"))

    def test_config(self):
        main.neis_get = fake({"schoolInfo": main.NeisConfigError()})
        r = main.search_school("가나초등학교")
        self.assertTrue(r.startswith("[조회 실패]"))
        self.assertNotIn("NEIS_KEY", r)

    def test_code_mapping(self):
        main.neis_get = fake({"schoolInfo": ("ERROR-337", [])})
        self.assertIn("횟수", main.search_school("가나초등학교"))
        main.neis_get = fake({"schoolInfo": ("INFO-300", [])})
        self.assertIn("관리자", main.search_school("가나초등학교"))

    def test_real_neis_get_without_key(self):
        main.neis_get = self._orig
        os.environ.pop("NEIS_KEY", None)
        with self.assertRaises(main.NeisConfigError):
            main.neis_get("schoolInfo", {})


class Reference(unittest.TestCase):
    def test_ref(self):
        r = main.get_school_service_reference()
        for s in ("19종", "1=난류", "19=잣", "저장하지 않습니다", "10분"):
            self.assertIn(s, r)


class Registry(unittest.TestCase):
    def test_tools(self):
        import fastmcp
        reg = getattr(fastmcp, "REG", None)
        if reg is None:
            self.skipTest("스텁 fastmcp에서만 실행")
        self.assertEqual(set(reg), {"search_school", "get_school_meal", "get_school_schedule",
                                    "get_school_service_reference"})
        for name, (kw, fn) in reg.items():
            self.assertEqual(set(kw["annotations"]),
                             {"title", "readOnlyHint", "destructiveHint", "idempotentHint", "openWorldHint"})
            self.assertLessEqual(len(kw["description"]), 1024, name)
            self.assertIn("학교 급식·일정 도우미 - School Helper", kw["description"])
            self.assertNotIn("kakao", name.lower())
        self.assertLessEqual(len(main.SERVICE), 30)
        self.assertEqual(main.SERVICE, "학교 급식·일정 도우미 - School Helper")


if __name__ == "__main__":
    unittest.main()
