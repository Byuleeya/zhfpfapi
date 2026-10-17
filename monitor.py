"""Pure seat-watch logic. One-day Naju -> Seoul/Yongsan/Suseo KTX read-only inventory checks.

No login, reservation, seat hold or payment actions.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable
import re

KST = timezone(timedelta(hours=9))
DATE = "20261024"
ORIGIN = "나주"
DESTINATIONS = ("서울", "용산", "수서")
STOP_AT = datetime(2026, 10, 25, 0, 0, tzinfo=KST)
GRADE_NAMES = {"general": "일반실", "special": "특실", "waiting": "예약대기"}
SEAT_GRADES = ("general", "special")

# User requested that train 456 never triggers alerts regardless of its Naju departure time.
# Count it in the first 10 KTX before excluding so API scope resembles the v8 first screen.
EXCLUDED_SERVICE = "456"

AVAILABLE = "AVAILABLE"
WAITLIST = "WAITLIST"
SOLD_OUT = "SOLD_OUT"
UNKNOWN = "UNKNOWN"

class AccessDeniedError(RuntimeError):
    """Recognizable remote access restriction; never retry or circumvent."""


def check_access_notice(message: object) -> None:
    msg = str(message or "")
    if ("-8202" in msg
            or ("데이터센터" in msg and "VPN" in msg.upper())
            or ("data center" in msg.lower() and "vpn" in msg.lower())):
        raise AccessDeniedError("Korail server rejected VPN/data-center access (CODE -8202)")


NON_SEAT_MARKERS = ("입석", "예약대기", "대기", "자유석", "환승", "매진", "마감")
PRICE_PATTERN = re.compile(r"(?<!\d)\d{1,3}(?:,\d{3})+\s*원|(?<!\d)\d{4,7}\s*원")


def clean_number(value: object) -> str:
    raw = str(value or "").strip()
    return str(int(raw)) if raw.isdigit() else raw


def clean_hhmm(value: object) -> str:
    # Korail search often sends HHMMSS or HHMM.
    numbers = "".join(ch for ch in str(value or "") if ch.isdigit())
    if len(numbers) < 4:
        return ""
    hh, mm = int(numbers[:2]), int(numbers[2:4])
    return f"{hh:02d}:{mm:02d}" if 0 <= hh < 24 and 0 <= mm < 60 else ""


def train_date(train: Any) -> str:
    date = getattr(train, "departure_date", None) or getattr(train, "run_date", None)
    return str(date or "").replace("-", "")[:8]


def departure_at(hhmm: str) -> datetime:
    hh, mm = map(int, hhmm.split(":"))
    return datetime(2026, 10, 24, hh, mm, tzinfo=KST)


def is_ktx(train: Any) -> bool:
    text = " ".join(str(getattr(train, key, "") or "") for key in (
        "train_class_name", "train_group_name"
    ))
    return "KTX" in text.upper() or "케이티엑스" in text


def is_excluded(train_no: str, dep_hhmm: str = "", dest: str = "") -> bool:
    return train_no == EXCLUDED_SERVICE


def grade_status(train: Any, grade: str) -> str:
    """Bookable seats only. Stand+seat, waiting list, sold out are NOT bookable.

    Third-party Korail+ client documents reservation code '11' as a bookable seat.
    Price strings are DISPLAY fields, not reliable standalone availability indicators.
    """
    label = str(getattr(train, f"{grade}_availability_name", "") or "")
    if any(x in label.replace(" ", "") for x in NON_SEAT_MARKERS):
        return SOLD_OUT
    code = getattr(train, f"{grade}_reservation_code", None)
    if str(code or "").strip() == "11":
        return AVAILABLE
    if code is None or str(code).strip() == "":
        return UNKNOWN
    return SOLD_OUT


def waiting_status(train: Any) -> str:
    """Return waiting-list application status, not actual seat availability.

    The unofficial Korail+ API documents ' 9' for an open waitlist.
    """
    flag = getattr(train, "wait_reservation_flag", None)
    if str(flag if flag is not None else "").strip() == "9":
        return WAITLIST
    for grade in SEAT_GRADES:
        label = re.sub(r"\s+", "", str(getattr(train, f"{grade}_availability_name", "") or ""))
        if label in {"예약대기", "예약대기가능"}:
            return WAITLIST
    if flag is None or not str(flag).strip():
        return UNKNOWN
    return SOLD_OUT


def fare_if_present(train: Any, grade: str) -> str | None:
    val = str(getattr(train, f"{grade}_fare_text", None) or "").strip()
    match = PRICE_PATTERN.search(val)
    return match.group(0).replace(" ", "") if match else None


@dataclass(frozen=True)
class Service:
    train_no: str
    train_name: str
    origin: str
    destination: str
    departure: str
    arrival: str
    general: str
    special: str
    waiting: str
    general_fare: str | None
    special_fare: str | None
    general_label: str
    special_label: str

    @property
    def key(self) -> str:
        return f"{DATE}:{self.train_no}:{self.departure}:{self.destination}"

    def status(self, grade: str) -> str:
        return getattr(self, grade)


def service_from_row(train: Any, query_destination: str, now: datetime) -> Service | None:
    """Only exact date/origin/dest, future departures and KTX."""
    number = clean_number(getattr(train, "train_no", None))
    dep = clean_hhmm(getattr(train, "departure_time", None))
    arr = clean_hhmm(getattr(train, "arrival_time", None))
    if not number.isdigit() or not dep or train_date(train) != DATE:
        return None
    origin = str(getattr(train, "departure_station_name", None) or "").strip()
    dest = str(getattr(train, "arrival_station_name", None) or "").strip()
    if origin != ORIGIN or dest != query_destination or dest not in DESTINATIONS:
        return None
    if not is_ktx(train):
        return None
    if now.astimezone(KST) >= departure_at(dep):
        return None
    train_name = str(getattr(train, "train_class_name", None) or "KTX").strip()
    return Service(
        train_no=number, train_name=train_name, origin=origin, destination=dest,
        departure=dep, arrival=arr, general=grade_status(train, "general"),
        special=grade_status(train, "special"),
        waiting=waiting_status(train),
        general_fare=fare_if_present(train, "general"),
        special_fare=fare_if_present(train, "special"),
        general_label=str(getattr(train, "general_availability_name", "") or ""),
        special_label=str(getattr(train, "special_availability_name", "") or ""),
    )


@dataclass(frozen=True)
class RouteResult:
    destination: str
    services: dict[str, Service]
    rows_seen: int
    calls: int
    complete: bool
    reason: str


@dataclass(frozen=True)
class ScanResult:
    services: dict[str, Service]
    routes: tuple[RouteResult, ...]
    api_calls: int
    rows_seen: int

    @property
    def complete(self) -> bool:
        return len(self.routes) == len(DESTINATIONS) and all(r.complete for r in self.routes)

    @property
    def nonempty_routes(self) -> int:
        return sum(r.rows_seen > 0 for r in self.routes)


def read_route(
    client: Any, query_type: Callable[..., Any], destination: str,
    now: datetime, max_pages: int = 4
) -> RouteResult:
    """Read pagination with conservative boundaries. No attempt to bypass queues."""
    if max_pages < 1:
        raise ValueError("max_pages must >= 1")
    query = query_type(
        departure_station_code=ORIGIN, arrival_station_code=destination,
        departure_date=DATE,
        # On travel day, only request services that have not yet departed;
        # otherwise later trains could be buried beyond the pagination cap.
        departure_time=(now.astimezone(KST).strftime("%H%M%S")
                        if now.astimezone(KST).date() == STOP_AT.date() - timedelta(days=1)
                        else "000000"),
        passengers=1, include_nearby_stations=False, include_srt=True,
    )
    continuation = None
    found: dict[str, Service] = {}
    calls = 0
    rows_seen = 0
    visited: set[tuple[str, str, str]] = set()
    fingerprints: set[tuple[str, ...]] = set()
    reason = "page limit"
    complete = False

    while calls < max_pages:
        marker = (str(query.departure_date), str(query.departure_time), repr(continuation))
        if marker in visited:
            reason = "repeating pagination cursor"
            break
        visited.add(marker)
        try:
            if continuation is None:
                response = client.search_trains(query)
            else:
                response = client.search_trains(query, continuation=continuation)
        except Exception as error:
            check_access_notice(str(error))
            # One destination may genuinely have no direct services for the date.
            # Do not abort the other two routes for a documented no-results code.
            code = str(getattr(error, "code", "") or "").strip()
            if continuation is None and (
                type(error).__name__ in {"KorailNoDirectTrainError", "KorailNoResultsError"}
                or code in {"WRD000061"}
            ):
                return RouteResult(destination, found, rows_seen, calls + 1,
                                   True, "no direct train")
            raise
        calls += 1
        metadata = getattr(response, "metadata", None)
        for notice in (getattr(metadata, "notice_message", None),
                       getattr(response, "h_msg_txt", None),
                       getattr(response, "message", None)):
            check_access_notice(notice)
        rows = list(getattr(response, "trains", []) or [])
        rows_seen += len(rows)
        page_fp = tuple(f"{clean_number(getattr(t,'train_no',None))}:{getattr(t,'departure_time',None)}" for t in rows)
        if page_fp and page_fp in fingerprints:
            reason = "repeated page"
            break
        fingerprints.add(page_fp)
        for train in rows:
            candidate = service_from_row(train, destination, now)
            if candidate is not None:
                found[candidate.key] = candidate
        next_flag = str(getattr(metadata, "next_page_flag", "") or "").upper().strip()
        next_page = response.next_page()
        if next_page is not None:
            continuation = next_page
            continue
        if next_flag == "Y":
            nxt = response.next_query_from_last_departure(query)
            if nxt is None or (nxt.departure_date, nxt.departure_time) <= (
                query.departure_date, query.departure_time
            ):
                reason = "next page declared but no usable cursor"
                break
            query, continuation = nxt, None
            continue
        complete = True
        reason = "end of result"
        break

    return RouteResult(destination, found, rows_seen, calls, complete, reason)


def query_status(
    client: Any, query_type: Callable[..., Any], now: datetime,
    max_pages_per_route: int = 4,
    max_trains: int = 10,
) -> ScanResult:
    routes: list[RouteResult] = []
    all_services: dict[str, Service] = {}
    for dest in DESTINATIONS:
        one = read_route(client, query_type, dest, now, max_pages_per_route)
        routes.append(one)
        all_services.update(one.services)
    # Unlike the 3-route API response, the official v8 web page displays a
    # chronologically ordered, KTX-filtered first page (up to 10 entries).
    # Apply the 10-train limit BEFORE omitting excluded train 456.
    first_page = sorted(all_services.values(),
                        key=lambda t: (t.departure, t.train_no, t.destination))[:max_trains]
    selected = {s.key: s for s in first_page if not is_excluded(s.train_no)}
    return ScanResult(
        services=selected, routes=tuple(routes),
        api_calls=sum(r.calls for r in routes),
        rows_seen=sum(r.rows_seen for r in routes),
    )


def availability_changes(previous: dict[str, str], scan: ScanResult) -> list[tuple[Service, tuple[str, ...]]]:
    found: list[tuple[Service, tuple[str, ...]]] = []
    for service in sorted(scan.services.values(), key=lambda s: (s.departure, s.destination, s.train_no)):
        grades = tuple(
            grade for grade in GRADE_NAMES
            if service.status(grade) == (WAITLIST if grade == "waiting" else AVAILABLE)
            and previous.get(f"{service.key}:{grade}") != (WAITLIST if grade == "waiting" else AVAILABLE)
        )
        if grades:
            found.append((service, grades))
    return found


def apply_states(previous: dict[str, str], scan: ScanResult, telegram_succeeded: bool) -> dict[str, str]:
    updated = previous.copy()
    for service in scan.services.values():
        for grade in GRADE_NAMES:
            status = service.status(grade)
            key = f"{service.key}:{grade}"
            if status == UNKNOWN:
                continue
            desired = WAITLIST if grade == "waiting" else AVAILABLE
            if status == desired and not telegram_succeeded and previous.get(key) != desired:
                continue
            updated[key] = status
    return updated


def short_summary(scan: ScanResult) -> str:
    seats = [f"{s.train_no}{'일' if grade=='general' else '특'}" for s in scan.services.values()
             for grade in SEAT_GRADES if s.status(grade) == AVAILABLE]
    waits = [s.train_no for s in scan.services.values() if s.waiting == WAITLIST]
    missing = [r.destination for r in scan.routes if not r.complete]
    return (
        f"KTX첫목록 {len(scan.services)}편(456제외) / 좌석예매가능={len(seats)}({','.join(seats[:12]) or '-'})"
        f" / 예약대기가능={len(waits)}({','.join(waits[:12]) or '-'})"
        f" / 조회행={scan.rows_seen} / API요청={scan.api_calls}"
        f" / 페이지불완전={','.join(missing) or '-'}"
    )


def detailed_summary(scan: ScanResult, cap: int = 40) -> str:
    status_names = {AVAILABLE: "가능", WAITLIST: "신청가능", SOLD_OUT: "매진·제외", UNKNOWN: "불명"}
    rows = [
        f"{s.train_no} {s.departure} {ORIGIN}→{s.destination}"
        f" 일반:{status_names[s.general]} 특실:{status_names[s.special]}"
        f" 대기:{status_names[s.waiting]}"
        for s in sorted(scan.services.values(), key=lambda x: (x.departure, x.destination))
    ]
    return "\n".join(rows[:cap]) or "유효 열차 없음"
