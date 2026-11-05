"""학교 급식·일정 도우미 - School Helper MCP 서버

학교 이름으로 오늘(또는 지정일)의 급식 식단과 학사일정을 조회해 알려 주는 참고용 도구입니다.
- 데이터: 교육부 NEIS 교육정보 개방 포털(open.neis.go.kr) 공식 오픈API (학교기본정보·급식식단정보·학사일정)
- 인증키는 환경변수 NEIS_KEY로만 받습니다(코드·로그·응답에 절대 넣지 않음).
- 학교 이름·날짜 등 입력값은 저장하지 않으며, 로그에는 툴 이름·처리시간·상태·NEIS 결과코드만 남깁니다.
- 급식 알레르기 표기는 학교가 입력한 번호를 그대로 옮기며, 최종 확인은 학교 공지로 안내합니다.
"""
import functools
import logging
import os
import re
import time
from datetime import date, datetime, timedelta, timezone
from typing import Annotated

import uvicorn
from fastmcp import FastMCP
from pydantic import Field
from starlette.requests import Request
from starlette.responses import PlainTextResponse

SERVICE = "학교 급식·일정 도우미 - School Helper"   # 콘솔 등록명과 글자 하나까지 동일해야 함(심사 DESCRIPTION_POLICY)
KST = timezone(timedelta(hours=9))

mcp = FastMCP(
    "학교 급식·일정 도우미 - School Helper",
    instructions=(
        "학교 이름으로 급식 식단과 학사일정을 조회하는 도구입니다. 학교 이름이 모호하면(같은 이름이 여러 곳) 지역이나 학교급을 되물으세요. "
        "응답에 표시된 날짜·출처·알레르기 안내를 그대로 전달하고, 응답에 없는 메뉴나 일정을 지어내지 마세요. "
        "알레르기 정보는 반드시 학교 공지로 최종 확인하도록 안내하세요."
    ),
)

# ================================================================ 설정
NEIS_BASE = "https://open.neis.go.kr/hub"
NEIS_TIMEOUT = 2.5            # 초. PlayMCP 지연 기준(p99 3초) 안에서 한 번만 시도
CACHE_TTL_SCHOOL = 24 * 3600  # 학교 정보는 하루 캐시
CACHE_TTL_DATA = 10 * 60      # 급식·일정은 10분 캐시(일일 호출 한도 보호)
WEEKDAYS = ["월", "화", "수", "목", "금", "토", "일"]
MEAL_CODES = {"조식": "1", "중식": "2", "석식": "3"}

# 학교급식법 시행규칙의 알레르기 유발 식재료 19종 (식단 표기 번호)
ALLERGENS = {1: "난류", 2: "우유", 3: "메밀", 4: "땅콩", 5: "대두", 6: "밀", 7: "고등어", 8: "게", 9: "새우",
             10: "돼지고기", 11: "복숭아", 12: "토마토", 13: "아황산류", 14: "호두", 15: "닭고기", 16: "쇠고기",
             17: "오징어", 18: "조개류(굴·전복·홍합 포함)", 19: "잣"}

SOURCE = "출처: 교육부 NEIS 교육정보 개방 포털(open.neis.go.kr)"
RELAY_NOTE = "※ 답변 시 위 날짜·메뉴·일정·출처를 그대로 전달하고, 이 응답에 없는 내용을 덧붙이지 마세요."
ALLERGY_NOTE = "※ 알레르기 번호는 학교가 입력한 표기를 옮긴 것입니다. 식단은 학교 사정으로 바뀔 수 있으니 알레르기는 학교 공지로 꼭 최종 확인하세요."


# ================================================================ 공통
class NeisUnavailable(Exception):
    """NEIS 호출 실패(네트워크·형식 오류 등). 메시지에는 URL/키를 담지 않는다."""


class NeisConfigError(Exception):
    """NEIS_KEY 미설정"""


def today_kst() -> date:
    return datetime.now(KST).date()


def parse_date(s: str):
    s = (s or "").strip()
    for f in ("%Y-%m-%d", "%Y%m%d"):
        try:
            return datetime.strptime(s, f).date()
        except ValueError:
            pass
    return None


def ymd(d: date) -> str:
    return d.strftime("%Y%m%d")


def fmt(d: date) -> str:
    return f"{d.isoformat()}({WEEKDAYS[d.weekday()]})"


def from_ymd(s: str):
    try:
        return datetime.strptime(s, "%Y%m%d").date()
    except (ValueError, TypeError):
        return None


def _annotations(title: str) -> dict:
    # PlayMCP 필수: 5개 속성 모두 지정. 공공 API를 조회하므로 openWorldHint=True
    return {
        "title": title,
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    }


# httpx는 INFO에서 요청 URL(인증키 포함)을 기록하므로 반드시 낮춘다
for _n in ("httpx", "httpcore"):
    logging.getLogger(_n).setLevel(logging.WARNING)

logger = logging.getLogger("schoolhelper")
if not logger.handlers:
    _h = logging.StreamHandler()
    _h.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
    logger.addHandler(_h)
    logger.setLevel(logging.INFO)
    logger.propagate = False


def _logged(func):
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        t0 = time.perf_counter()
        status, result = "exception", ""
        try:
            result = func(*args, **kwargs)
            if (result or "").startswith("[입력 확인]"):
                status = "invalid_input"
            elif (result or "").startswith("[조회 실패]"):
                status = "upstream_error"
            else:
                status = "ok"
            return result
        finally:
            ms = int((time.perf_counter() - t0) * 1000)
            logger.info("TOOL name=%s status=%s ms=%d len=%d", func.__name__, status, ms, len(result or ""))
    return wrapper


def bad(msg: str) -> str:
    return f"[입력 확인] {msg}"


def fail(msg: str) -> str:
    return f"[조회 실패] {msg}"


# ================================================================ NEIS 호출 (캐시 포함)
_CACHE: dict = {}


def _cache_get(k):
    v = _CACHE.get(k)
    if v and v[0] > time.time():
        return v[1]
    return None


def _cache_put(k, val, ttl):
    if len(_CACHE) > 500:
        _CACHE.clear()
    _CACHE[k] = (time.time() + ttl, val)


def neis_get(endpoint: str, params: dict, ttl: int = CACHE_TTL_DATA):
    """(결과코드, rows) 반환. 키는 요청에만 쓰고 캐시 키·로그·예외 메시지에는 넣지 않는다."""
    key = os.environ.get("NEIS_KEY", "").strip()
    if not key:
        raise NeisConfigError()
    ck = (endpoint, tuple(sorted(params.items())))
    hit = _cache_get(ck)
    if hit is not None:
        return hit
    t0 = time.perf_counter()
    try:
        import httpx
        r = httpx.get(f"{NEIS_BASE}/{endpoint}", params={"KEY": key, "Type": "json", **params}, timeout=NEIS_TIMEOUT)
        r.raise_for_status()
        data = r.json()
    except Exception:   # 예외 문자열에는 키가 들어간 URL이 있을 수 있어 사용하지 않는다
        logger.info("NEIS endpoint=%s code=HTTP_OR_PARSE_ERROR", endpoint)
        raise NeisUnavailable() from None
    code, rows = "UNKNOWN", []
    try:
        if endpoint in data:
            for sec in data[endpoint]:
                for h in sec.get("head", []):
                    if "RESULT" in h:
                        code = h["RESULT"].get("CODE", code)
                rows += sec.get("row", [])
        elif "RESULT" in data:
            code = data["RESULT"].get("CODE", code)
    except (AttributeError, TypeError):
        raise NeisUnavailable() from None
    logger.info("NEIS endpoint=%s code=%s ms=%d", endpoint, code, int((time.perf_counter() - t0) * 1000))
    if code in ("INFO-000", "INFO-200"):
        _cache_put(ck, (code, rows), ttl)
    return code, rows


def neis_problem(code: str) -> str:
    if code in ("INFO-100", "ERROR-290", "INFO-300"):
        return fail("서비스 설정 문제로 지금은 조회할 수 없습니다(관리자 확인 필요). 잠시 후 다시 시도해 주세요.")
    if code == "ERROR-337":
        return fail("오늘 조회 가능한 횟수를 넘었습니다. 내일 다시 시도해 주세요.")
    return fail("교육정보 개방 포털에서 정상 응답을 받지 못했습니다. 잠시 후 다시 시도해 주세요.")


def call(endpoint: str, params: dict, ttl: int = CACHE_TTL_DATA):
    """(rows, 오류문구). 오류가 있으면 rows는 None."""
    try:
        code, rows = neis_get(endpoint, params, ttl)
    except NeisConfigError:
        return None, fail("서비스 설정이 아직 완료되지 않았습니다(관리자 확인 필요).")
    except NeisUnavailable:
        return None, fail("교육정보 개방 포털에 연결하지 못했습니다. 잠시 후 다시 시도해 주세요.")
    if code == "INFO-000":
        return rows, None
    if code == "INFO-200":
        return [], None
    return None, neis_problem(code)


# ================================================================ 학교 찾기
def school_line(r: dict) -> str:
    addr = (r.get("ORG_RDNMA") or "").strip()
    return (f"{r.get('SCHUL_NM','')} — {r.get('SCHUL_KND_SC_NM','')}·{r.get('FOND_SC_NM','')}, "
            f"{r.get('ATPT_OFCDC_SC_NM','')}" + (f", {addr}" if addr else ""))


def find_schools(name: str, region: str, level: str):
    """(후보 목록, 오류문구)"""
    name = (name or "").strip()
    if len(name) < 2:
        return None, bad("학교 이름을 2글자 이상 입력해 주세요. 예: 미사강변초등학교")
    rows, err = call("schoolInfo", {"SCHUL_NM": name, "pIndex": "1", "pSize": "100"}, CACHE_TTL_SCHOOL)
    if err:
        return None, err
    region, level = (region or "").strip(), (level or "").strip()
    cands = rows
    if region:
        cands = [r for r in cands if region in " ".join(str(r.get(k) or "") for k in
                                                       ("LCTN_SC_NM", "ATPT_OFCDC_SC_NM", "ORG_RDNMA"))]
    if level:
        cands = [r for r in cands if level in (r.get("SCHUL_KND_SC_NM") or "")]
    exact = [r for r in cands if (r.get("SCHUL_NM") or "") == name]
    return (exact or cands), None


def resolve_school(name: str, region: str, level: str):
    """(학교 row, 안내/오류문구). 하나로 특정될 때만 row를 돌려준다."""
    cands, err = find_schools(name, region, level)
    if err:
        return None, err
    if not cands:
        return None, bad("해당하는 학교를 찾지 못했습니다. 학교 이름(정식 명칭)이나 지역을 다시 확인해 주세요.")
    if len(cands) > 1:
        lines = [f"· {school_line(r)}" for r in cands[:8]]
        more = f"\n(그 밖에 {len(cands) - 8}곳 더 있음)" if len(cands) > 8 else ""
        return None, bad(f"같은 조건의 학교가 {len(cands)}곳입니다. 지역(시·군·구)이나 학교급(초·중·고)을 함께 알려 주세요.\n"
                         + "\n".join(lines) + more)
    return cands[0], None


def school_title(r: dict) -> str:
    return f"{r.get('SCHUL_NM','')}({r.get('SCHUL_KND_SC_NM','')}, {r.get('ATPT_OFCDC_SC_NM','')})"


# ================================================================ Tool 1: 학교 찾기
@mcp.tool(
    name="search_school",
    description=(
        f"Finds Korean schools by name using {SERVICE} (official NEIS school data): returns up to 10 matches with school level, "
        "founding type, education office, address, phone and homepage. Optional region (e.g. 하남시, 경기) and level "
        "(초등학교, 중학교, 고등학교) narrow the result. Use it when the school name may be ambiguous or the user wants school contact info. "
        "Always call this tool instead of answering from memory. Reference data, not an official certificate."
    ),
    annotations=_annotations("Search school"),
)
@_logged
def search_school(
    school_name: Annotated[str, Field(description="School name, e.g. 미사강변초등학교 (2+ characters)")],
    region: Annotated[str, Field(description="Optional city/district/province to narrow, e.g. 하남시 or 경기")] = "",
    school_level: Annotated[str, Field(description="Optional level: 초등학교, 중학교, 고등학교")] = "",
) -> str:
    cands, err = find_schools(school_name, region, school_level)
    if err:
        return err
    if not cands:
        return bad("해당하는 학교를 찾지 못했습니다. 학교 이름(정식 명칭)이나 지역을 다시 확인해 주세요.")
    out = [f"■ 학교 검색 결과 {len(cands)}곳 중 {min(len(cands), 10)}곳"]
    for r in cands[:10]:
        extra = " · ".join(x for x in (r.get("ORG_TELNO"), r.get("HMPG_ADRES")) if x)
        out.append(f"· {school_line(r)}" + (f" ({extra})" if extra else ""))
    out.append(SOURCE)
    out.append(RELAY_NOTE)
    return "\n".join(out)


# ================================================================ Tool 2: 급식
ALLERGY_TAIL = re.compile(r"\(?\s*((?:\d{1,2}\s*\.\s*)+\d{0,2}|\d{1,2})\s*\)?\s*\.?\s*$")


def parse_dish(raw: str):
    """'미역국 (5.6.13)' → ('미역국', [5, 6, 13])"""
    s = re.sub(r"<[^>]+>", "", raw or "").strip()
    m = ALLERGY_TAIL.search(s)
    codes = []
    if m and (("(" in s) or ("." in m.group(1))):
        nums = [int(x) for x in re.findall(r"\d{1,2}", m.group(1))]
        if nums and all(1 <= n <= 19 for n in nums):
            codes = nums
            s = s[:m.start()].strip()
    return s, codes


def dishes(ddish: str):
    parts = re.split(r"<br\s*/?>|\n|\\n", ddish or "")
    return [parse_dish(p) for p in parts if p.strip()]


def meal_block(row: dict, nutrition: bool) -> list:
    d = from_ymd(row.get("MLSV_YMD"))
    head = f"[{row.get('MMEAL_SC_NM','')}] {fmt(d) if d else row.get('MLSV_YMD','')}"
    out = [head]
    used = set()
    for name, codes in dishes(row.get("DDISH_NM", "")):
        if codes:
            used.update(codes)
            out.append(f"· {name} (알레르기: {', '.join(ALLERGENS[c] for c in codes)})")
        else:
            out.append(f"· {name}")
    if row.get("CAL_INFO"):
        out.append(f"열량: {row['CAL_INFO']}")
    if nutrition and row.get("NTR_INFO"):
        out.append("영양: " + re.sub(r"<br\s*/?>", " / ", row["NTR_INFO"]))
    return out, used


@mcp.tool(
    name="get_school_meal",
    description=(
        f"Looks up a Korean school's lunch/breakfast/dinner menu for a date (default today, up to 7 days) using {SERVICE} "
        "(official NEIS meal data). Give the school name; add region or level if several schools share the name (the tool asks to specify). "
        "Returns dishes with allergy labels as entered by the school, calories and optional nutrition. "
        "Always call this tool instead of answering from memory, because menus change daily. "
        "Allergy labels must be confirmed with the school. Reference data only."
    ),
    annotations=_annotations("School meal menu"),
)
@_logged
def get_school_meal(
    school_name: Annotated[str, Field(description="School name, e.g. 미사강변초등학교")],
    date_str: Annotated[str, Field(description="Date YYYY-MM-DD; empty means today (KST)")] = "",
    days: Annotated[int, Field(description="Number of days from the date, 1-7", ge=1, le=7)] = 1,
    meal_type: Annotated[str, Field(description="Optional: 조식, 중식, 석식; empty means all meals")] = "",
    region: Annotated[str, Field(description="Optional city/district/province, e.g. 하남시")] = "",
    school_level: Annotated[str, Field(description="Optional: 초등학교, 중학교, 고등학교")] = "",
    include_nutrition: Annotated[bool, Field(description="True to include nutrition details")] = False,
) -> str:
    if meal_type and meal_type not in MEAL_CODES:
        return bad("meal_type은 조식, 중식, 석식 중 하나이거나 비워 주세요.")
    if date_str.strip():
        d0 = parse_date(date_str)
        if d0 is None:
            return bad("날짜를 YYYY-MM-DD 형식으로 입력해 주세요(비우면 오늘).")
    else:
        d0 = today_kst()
    school, msg = resolve_school(school_name, region, school_level)
    if msg:
        return msg
    d1 = d0 + timedelta(days=days - 1)
    params = {"ATPT_OFCDC_SC_CODE": school["ATPT_OFCDC_SC_CODE"], "SD_SCHUL_CODE": school["SD_SCHUL_CODE"],
              "MLSV_FROM_YMD": ymd(d0), "MLSV_TO_YMD": ymd(d1), "pIndex": "1", "pSize": "100"}
    if meal_type:
        params["MMEAL_SC_CODE"] = MEAL_CODES[meal_type]
    rows, err = call("mealServiceDietInfo", params)
    if err:
        return err
    out = [f"■ {school_title(school)} 급식 — {fmt(d0)}" + (f" ~ {fmt(d1)}" if days > 1 else "")]
    if not rows:
        out.append("· 해당 기간에 등록된 급식 정보가 없습니다(주말·방학·휴업일이거나 아직 식단이 등록되지 않았을 수 있습니다).")
        out.append(SOURCE)
        out.append(RELAY_NOTE)
        return "\n".join(out)
    used_all = set()
    for row in sorted(rows, key=lambda r: (r.get("MLSV_YMD", ""), r.get("MMEAL_SC_CODE", ""))):
        block, used = meal_block(row, include_nutrition)
        used_all |= used
        out += block
    if used_all:
        out.append("알레르기 번호: " + ", ".join(f"{n}={ALLERGENS[n]}" for n in sorted(used_all)))
    out.append(ALLERGY_NOTE)
    out.append(SOURCE)
    out.append(RELAY_NOTE)
    return "\n".join(out)


# ================================================================ Tool 3: 학사일정
def grade_flag(r: dict, grade: int) -> bool:
    keys = {1: "ONE_GRADE_EVENT_YN", 2: "TW_GRADE_EVENT_YN", 3: "THREE_GRADE_EVENT_YN",
            4: "FR_GRADE_EVENT_YN", 5: "FIV_GRADE_EVENT_YN", 6: "SIX_GRADE_EVENT_YN"}
    return (r.get(keys[grade]) or "").upper() == "Y"


def month_end(d: date) -> date:
    nxt = date(d.year + (d.month == 12), d.month % 12 + 1, 1)
    return nxt - timedelta(days=1)


@mcp.tool(
    name="get_school_schedule",
    description=(
        f"Looks up a Korean school's academic calendar (학사일정: exams, holidays, ceremonies, vacation days) for a date range "
        f"(default: today to the end of this month, at most 62 days) using {SERVICE} (official NEIS schedule data). "
        "Give the school name; add region or level if several schools share the name. Optional grade (1-6) keeps only events for that grade. "
        "Always call this tool instead of answering from memory, because each school sets its own calendar. Reference data only."
    ),
    annotations=_annotations("School schedule"),
)
@_logged
def get_school_schedule(
    school_name: Annotated[str, Field(description="School name, e.g. 미사강변초등학교")],
    from_date: Annotated[str, Field(description="Start date YYYY-MM-DD; empty means today (KST)")] = "",
    to_date: Annotated[str, Field(description="End date YYYY-MM-DD; empty means the end of the start month")] = "",
    grade: Annotated[int, Field(description="Optional grade 1-6 to filter events; 0 means all", ge=0, le=6)] = 0,
    region: Annotated[str, Field(description="Optional city/district/province, e.g. 하남시")] = "",
    school_level: Annotated[str, Field(description="Optional: 초등학교, 중학교, 고등학교")] = "",
) -> str:
    if from_date.strip():
        d0 = parse_date(from_date)
        if d0 is None:
            return bad("시작일을 YYYY-MM-DD 형식으로 입력해 주세요(비우면 오늘).")
    else:
        d0 = today_kst()
    if to_date.strip():
        d1 = parse_date(to_date)
        if d1 is None:
            return bad("종료일을 YYYY-MM-DD 형식으로 입력해 주세요(비우면 시작월 말일).")
    else:
        d1 = month_end(d0)
    if d1 < d0:
        return bad("종료일이 시작일보다 앞섭니다.")
    if (d1 - d0).days > 62:
        return bad("조회 기간은 최대 62일까지입니다.")
    school, msg = resolve_school(school_name, region, school_level)
    if msg:
        return msg
    params = {"ATPT_OFCDC_SC_CODE": school["ATPT_OFCDC_SC_CODE"], "SD_SCHUL_CODE": school["SD_SCHUL_CODE"],
              "AA_FROM_YMD": ymd(d0), "AA_TO_YMD": ymd(d1), "pIndex": "1", "pSize": "200"}
    rows, err = call("SchoolSchedule", params)
    if err:
        return err
    if grade:
        rows = [r for r in rows if grade_flag(r, grade)]
    out = [f"■ {school_title(school)} 학사일정 — {fmt(d0)} ~ {fmt(d1)}" + (f", {grade}학년" if grade else "")]
    if not rows:
        out.append("· 해당 기간에 등록된 학사일정이 없습니다.")
    for r in sorted(rows, key=lambda x: x.get("AA_YMD", ""))[:60]:
        d = from_ymd(r.get("AA_YMD"))
        kind = (r.get("SBTR_DD_SC_NM") or "").strip()
        tag = f" [{kind}]" if kind and kind != "해당없음" else ""
        out.append(f"· {fmt(d) if d else r.get('AA_YMD','')} {r.get('EVENT_NM','')}{tag}")
    if len(rows) > 60:
        out.append(f"(그 밖에 {len(rows) - 60}건 더 있음 — 기간을 좁혀 조회하세요)")
    out.append("※ 학사일정은 학교 사정으로 변경될 수 있으니 중요한 일정은 학교 공지로 확인하세요.")
    out.append(SOURCE)
    out.append(RELAY_NOTE)
    return "\n".join(out)


# ================================================================ Tool 4: 기준·출처
@mcp.tool(
    name="get_school_service_reference",
    description=(
        f"Returns the data source, caching, limits and allergy-label list (19 items) used by {SERVICE}. "
        "Use it when the user asks where the data comes from, how fresh it is, or what the allergy numbers mean. Reference only."
    ),
    annotations=_annotations("School data sources"),
)
@_logged
def get_school_service_reference() -> str:
    legend = ", ".join(f"{n}={v}" for n, v in ALLERGENS.items())
    return "\n".join([
        "■ 학교 급식·일정 도우미 기준",
        f"■ 데이터: {SOURCE.replace('출처: ', '')}의 학교기본정보·급식식단정보·학사일정 오픈API",
        "■ 신선도: 급식·일정은 최대 10분, 학교 정보는 최대 24시간 전에 조회한 값을 재사용할 수 있습니다.",
        "■ 학교가 NEIS에 등록한 정보만 보여 주므로, 아직 등록되지 않았거나 급식을 운영하지 않는 날에는 결과가 없을 수 있습니다.",
        f"■ 알레르기 번호표(학교급식법 시행규칙 19종): {legend}",
        ALLERGY_NOTE,
        "입력한 학교 이름과 날짜는 저장하지 않습니다."])


# ================================================================ 서버
@mcp.custom_route("/health", methods=["GET", "HEAD"])
async def health(request: Request) -> PlainTextResponse:
    return PlainTextResponse("OK")


@mcp.custom_route("/", methods=["GET", "HEAD"])
async def root(request: Request) -> PlainTextResponse:
    return PlainTextResponse("OK")


try:
    app = mcp.http_app(path="/mcp", stateless_http=True)  # PlayMCP 권장: Stateless
except TypeError:
    app = mcp.http_app(path="/mcp")

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
