"""Naju -> Seoul/Yongsan/Suseo KTX: read-only Telegram seat monitor.

No login, booking, payment, CAPTCHA workaround or network restriction bypass.
"""
from __future__ import annotations

import json
import os
import random
import signal
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any

from monitor import (
    AVAILABLE, WAITLIST, AccessDeniedError, DATE, DESTINATIONS, GRADE_NAMES, KST, ORIGIN, STOP_AT,
    ScanResult, Service, apply_states, availability_changes, detailed_summary,
    query_status, short_summary,
)

BOOKING_LINK = "https://www.korail.com/ticket/search/general"
EXIT = False


def handle_signal(_signum: int, _frame: object) -> None:
    global EXIT
    EXIT = True


for s in (signal.SIGINT, signal.SIGTERM):
    signal.signal(s, handle_signal)


def now_kst() -> datetime:
    return datetime.now(KST)


def log(message: str) -> None:
    print(f"[{now_kst():%Y-%m-%d %H:%M:%S KST}] {message}", flush=True)


def integer_env(key: str, default: int, lo: int, hi: int) -> int:
    try:
        val = int(os.getenv(key, str(default)))
    except ValueError as exc:
        raise ValueError(f"{key} must be an integer") from exc
    if not lo <= val <= hi:
        raise ValueError(f"{key} must be between {lo} and {hi}")
    return val


def wait_safe(seconds: float) -> None:
    target = time.monotonic() + max(seconds, 0)
    while not EXIT and now_kst() < STOP_AT and time.monotonic() < target:
        time.sleep(min(0.75, max(0.05, target - time.monotonic())))


class Telegram:
    def __init__(self) -> None:
        self.token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        self.chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
        if not self.token or not self.chat_id:
            raise RuntimeError("TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID must be set")

    def send(self, text: str) -> bool:
        body = urllib.parse.urlencode({
            "chat_id": self.chat_id, "text": text, "disable_web_page_preview": "true",
        }).encode("utf-8")
        request = urllib.request.Request(
            f"https://api.telegram.org/bot{self.token}/sendMessage", body, method="POST"
        )
        try:
            with urllib.request.urlopen(request, timeout=12) as resp:
                data = json.loads(resp.read(2048).decode("utf-8"))
                return resp.status == 200 and data.get("ok") is True
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as err:
            log(f"Telegram delivery error: {type(err).__name__} (http {getattr(err, 'code', '-')})")
            return False


class State:
    def __init__(self, path: str) -> None:
        self.path = Path(path)
        self.data: dict[str, Any] = {"statuses": {}, "scans": 0}
        if self.path.exists():
            try:
                loaded = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict) and isinstance(loaded.get("statuses"), dict):
                    self.data.update(loaded)
            except (OSError, ValueError):
                log("State file unreadable: using fresh state")

    @property
    def statuses(self) -> dict[str, str]:
        return self.data["statuses"]

    def save(self) -> None:
        tmp_path: Path | None = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=self.path.parent,
                prefix=".korail_", suffix=".tmp", delete=False,
            ) as fh:
                json.dump(self.data, fh, ensure_ascii=False, indent=2)
                tmp_path = Path(fh.name)
            os.replace(tmp_path, self.path)
        except OSError as err:
            log(f"Could not persist state: {type(err).__name__}")
            if tmp_path is not None:
                try:
                    tmp_path.unlink(missing_ok=True)
                except OSError:
                    pass


def is_rate_limited(exc: Exception) -> bool:
    typename = type(exc).__name__
    if typename in {
        "KorailRateLimitError", "KorailDynaPathError", "KorailQueueRejectedError",
        "KorailNetFunnelError", "KorailQueueError",
    }:
        return True
    code = str(getattr(exc, "code", "") or "").strip()
    if code in {"403", "429", "BT019", "BT023", "-2000"}:
        return True
    if getattr(getattr(exc, "response", None), "status_code", None) in (403, 429):
        return True
    cause = getattr(exc, "__cause__", None)
    if getattr(getattr(cause, "response", None), "status_code", None) in (403, 429):
        return True
    return False


def is_access_denied(exc: Exception) -> bool:
    if isinstance(exc, AccessDeniedError):
        return True
    code = str(getattr(exc, "code", "") or "").strip()
    text = " ".join((str(exc), str(getattr(exc, "message", "") or "")))
    return (code == "-8202" or "-8202" in text or
            ("데이터센터" in text and "VPN" in text.upper()) or
            ("data center" in text.lower() and "vpn" in text.lower()))


def short_error(exc: Exception) -> str:
    return f"{type(exc).__name__}(code={getattr(exc, 'code', '-') or '-'})"


def format_found(changes: list[tuple[Service, tuple[str, ...]]], found_at: datetime) -> str:
    seat_found = any(g in ("general", "special") for _, gs in changes for g in gs)
    wait_found = any(g == "waiting" for _, gs in changes for g in gs)
    heading = ("🚄 KTX 좌석·예약대기 발견!" if seat_found and wait_found else
               "🚄 KTX 취소표 발견!" if seat_found else
               "🕓 KTX 예약대기 신청 가능!")
    lines = [heading, "2026-10-24(토) 나주 → 서울·용산·수서 / 성인 1명", ""]
    for svc, grades in changes:
        lines.append(f"🚆 {svc.train_name} {svc.train_no} | {ORIGIN} {svc.departure} → {svc.destination} {svc.arrival}")
        for grade in grades:
            if grade == "waiting":
                lines.append("   🕓 예약대기: 신청 가능 (좌석 확보·발권 아님)")
            else:
                price = getattr(svc, f"{grade}_fare")
                lines.append(f"   ✅ {GRADE_NAMES[grade]}: 즉시 예매 가능" +
                             (f" / 표시 운임 {price}" if price else ""))
    lines += ["", f"확인 시각: {found_at:%m/%d %H:%M:%S} KST",
              "※ 입석+좌석·입석 제외. 예약대기는 좌석 확정이 아닙니다.",
              "※ 최종 상태는 코레일 웹/앱에서 직접 확인해 주세요.", BOOKING_LINK]
    return "\n".join(lines)


def run() -> int:
    from korail_mobile_api import KorailClient, TrainSearchQuery

    min_sec = integer_env("POLL_MIN_SECONDS", 20, 20, 3600)
    max_sec = integer_env("POLL_MAX_SECONDS", 30, 20, 3600)
    if max_sec < min_sec:
        raise ValueError("POLL_MAX_SECONDS must be >= POLL_MIN_SECONDS")
    max_pages = integer_env("MAX_PAGES_PER_ROUTE", 4, 1, 8)
    max_trains = integer_env("MAX_TRAINS", 10, 1, 50)
    heartbeat = integer_env("HEARTBEAT_MINUTES", 60, 0, 1440)
    detail_every = integer_env("LOG_DETAIL_EVERY", 10, 1, 1000)
    state = State(os.getenv("STATE_FILE", "state_naju_v9.json"))
    telegram = Telegram()
    log("코레일 나주→서울·용산·수서 KTX API 감시 v9 시작")
    log("2026-10-24 00:00부터, 성인1명, KTX 최초 10편 / 일반실·특실 / 456편·입석+좌석·무궁화 제외 / 예약대기 포함")
    log(f"감시 시작 간격: 매회 랜덤 {min_sec}~{max_sec}초, 노선 3개 별도 조회, 노선별 최대 {max_pages}페이지, 열차 최대 {max_trains}편")
    log(f"자동 종료: {STOP_AT.isoformat()}")

    if now_kst() >= STOP_AT:
        log("종료 날짜를 지났으므로 조회하지 않습니다")
        telegram.send("🏁 코레일 KTX 감시 기간(10/24)이 종료되어 실행하지 않았습니다.")
        return 0

    if not telegram.send(
        "🔎 코레일 KTX 취소표 API 감시 v9 시작\n"
        "2026-10-24(토) 나주→서울·용산·수서, 성인 1명\n"
        f"KTX 출발순 최대 {max_trains}편 / 일반실·특실\n"
        "❌ 456편 전체, 무궁화, 입석+좌석 제외\n🕓 예약대기 신청 가능도 알림\n"
        f"⏱ {min_sec}~{max_sec}초 무작위 감시 / 10월 25일 00:00 자동 종료\n"
        "※ 코레일+ 비공식 조회 라이브러리 기반이며 웹 화면과 차이가 있을 수 있습니다."
    ):
        log("시작 Telegram 전송 실패: Variables 검증 후 다시 시작하세요")
        return 1

    client = KorailClient()
    scans = int(state.data.get("scans", 0))
    last_healthy = now_kst()
    last_heartbeat = now_kst()
    unhealthy_notified = False
    consecutive_errors = 0
    consecutive_blocks = 0
    incomplete_streak = 0
    try:
        while not EXIT and now_kst() < STOP_AT:
            started = time.monotonic()
            moment = now_kst()
            try:
                scan = query_status(client, TrainSearchQuery, moment, max_pages, max_trains)
                scans += 1
                log(f"#{scans}: {short_summary(scan)}")
                if scans <= 3 or scans % detail_every == 0:
                    log("조회 목록:\n" + detailed_summary(scan))
                    for route in scan.routes:
                        log(f"  나주→{route.destination}: {len(route.services)} KTX, "
                            f"API {route.calls}회, 페이지 완결={route.complete}({route.reason})")
                if scan.rows_seen == 0 or not scan.services:
                    raise RuntimeError("No verifiable KTX trains in query results")

                # An incomplete route can change which 10 KTX would appear on the
                # merged page. Avoid unverified alerts and state transitions.
                changes = availability_changes(state.statuses, scan) if scan.complete else []
                notified = True
                if changes:
                    notified = telegram.send(format_found(changes, moment))
                    log("좌석/예약대기 감지: " + ", ".join(
                        f"{s.train_no} {s.departure}→{s.destination} " + "/".join(GRADE_NAMES[g] for g in grades)
                        for s, grades in changes
                    ) + (" (Telegram 발송됨)" if notified else " (전송 실패, 재시도 예정)"))
                if scan.complete:
                    state.data["statuses"] = apply_states(state.statuses, scan, notified)
                state.data["scans"] = scans
                state.data["last_scan"] = moment.isoformat()
                state.save()

                if not scan.complete:
                    incomplete_streak += 1
                    if incomplete_streak == 3:
                        telegram.send("⚠️ KTX 감시 일부 노선의 페이지를 끝까지 확인하지 못했습니다.\n"
                                      + short_summary(scan) + "\nRailway 로그를 확인해 주세요.")
                        unhealthy_notified = True
                    # Do not hammer pagination failures indefinitely.
                    delay = max(60 if incomplete_streak >= 3 else 0, random.uniform(min_sec, max_sec))
                else:
                    incomplete_streak = 0
                    delay = random.uniform(min_sec, max_sec)

                if consecutive_errors or consecutive_blocks or (unhealthy_notified and scan.complete):
                    telegram.send(f"✅ KTX 조회 정상 복구 ({moment:%m/%d %H:%M:%S} KST)\n" + short_summary(scan))
                if scan.complete:
                    unhealthy_notified = False
                consecutive_errors = 0
                consecutive_blocks = 0
                last_healthy = moment
                if heartbeat and (moment - last_heartbeat).total_seconds() >= heartbeat * 60:
                    telegram.send("💓 KTX 감시 작동 확인\n" + short_summary(scan)
                                  + ("\n⚠️ 일부 결과 불완전" if not scan.complete else ""))
                    last_heartbeat = moment
                # The user asked for approximately one scan start every 20-30 seconds;
                # each scan itself makes at least 3 sequential destination queries.
                elapsed = time.monotonic() - started
                delay = max(5.0, delay - elapsed) if incomplete_streak < 3 else delay

            except Exception as exc:
                consecutive_errors += 1
                log(f"조회 오류 #{consecutive_errors}: {short_error(exc)}")
                if is_access_denied(exc):
                    log("코레일 서버가 VPN/데이터센터 접근을 거부했습니다(CODE -8202). 재시도 없이 감시 종료")
                    telegram.send("🛑 코레일 API 접속 거부 (CODE -8202)\n"
                                  "VPN/데이터센터 접속 제한으로 판단하여 즉시 감시를 중단했습니다.\n"
                                  "Railway 로그를 확인해 주세요. 우회 또는 자동 재시도하지 않습니다.")
                    return 0
                if is_rate_limited(exc):
                    consecutive_blocks += 1
                    if consecutive_blocks >= 3:
                        telegram.send("🛑 코레일 요청 제한/차단 3회 연속. 안전상 감시를 종료합니다.\n"
                                      f"오류: {short_error(exc)}\nRailway 로그를 확인해 주세요.")
                        log("접근 제한 연속 3회: 추가 요청 없이 종료")
                        return 0
                    delay = (300, 600)[consecutive_blocks - 1]
                    telegram.send(f"⚠️ 코레일 요청 제한 감지 #{consecutive_blocks}\n"
                                  f"{short_error(exc)}\n{delay // 60}분간 대기 후 재확인")
                    unhealthy_notified = True
                else:
                    consecutive_blocks = 0
                    delay = min(900, 60 * 2 ** min(4, consecutive_errors - 1))
                    if consecutive_errors == 3 or (
                        not unhealthy_notified and (now_kst() - last_healthy).total_seconds() >= 300
                    ):
                        telegram.send(f"⚠️ KTX 조회 오류 #{consecutive_errors}\n"
                                      f"최근 정상 시각: {last_healthy:%m/%d %H:%M KST}\n"
                                      f"{short_error(exc)}\n{delay // 60}분 후 재시도")
                        unhealthy_notified = True
                try:
                    client.close()
                except Exception:
                    pass
                client = KorailClient()
            log(f"다음 확인 약 {delay:.1f}초 후")
            wait_safe(delay)
    finally:
        try:
            client.close()
        except Exception:
            pass
    if EXIT:
        log("Railway SIGTERM/SIGINT: 감시 종료")
    else:
        log("10월 24일 감시 기간 종료")
        telegram.send("🏁 코레일 KTX 취소표 감시 종료: 2026-10-25 00:00 KST")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(run())
    except Exception as error:
        log("시작 실패: " + short_error(error))
        sys.exit(1)
