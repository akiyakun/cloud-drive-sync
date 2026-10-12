"""同期のしかた（○分ごと／毎日○時）から、次に同期する時刻を決める"""

import re
from datetime import datetime, timedelta

MIN_INTERVAL, MAX_INTERVAL = 10, 1440  # ○分ごと: 10 分〜24 時間
MAX_DAILY_TIMES = 24
TIME_PATTERN = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")


def parse_daily_times(text):
    """「9:00, 13:30」のような文字を ["09:00", "13:30"] にする。誤りがあれば ValueError"""
    times = set()
    for part in re.split(r"[,\s、，]+", text.strip()):
        if not part:
            continue
        m = TIME_PATTERN.match(part)
        if not m:
            raise ValueError(f"「{part}」は時刻として読めません（例: 09:00）")
        times.add(f"{int(m.group(1)):02d}:{m.group(2)}")
    if not times:
        raise ValueError("時刻を 1 つ以上入れてください（例: 09:00, 13:00）")
    if len(times) > MAX_DAILY_TIMES:
        raise ValueError(f"時刻は {MAX_DAILY_TIMES} 個までです")
    return sorted(times)


def parse_interval(text):
    try:
        minutes = int(text)
    except (TypeError, ValueError):
        raise ValueError("「○分ごと」には数字を入れてください") from None
    if not MIN_INTERVAL <= minutes <= MAX_INTERVAL:
        raise ValueError(f"「○分ごと」は {MIN_INTERVAL}〜{MAX_INTERVAL} 分にしてください")
    return minutes


def next_run(schedule, now, last_started=None):
    """次に自動で同期する時刻。自動同期を止めているときは None

    - interval: 前回の同期の開始（手動も含む）から interval_minutes 後。前回が無ければ 1 分後
    - daily: daily_times のうち、now より後でいちばん早い時刻（今日か明日）
    now は時差情報付きの datetime。last_started も同じ時差の datetime か None
    """
    if schedule.get("paused"):
        return None
    if schedule.get("mode") == "daily":
        candidates = []
        for day in (0, 1):
            date = (now + timedelta(days=day)).date()
            for hhmm in schedule.get("daily_times", []):
                hour, minute = map(int, hhmm.split(":"))
                at = datetime(date.year, date.month, date.day, hour, minute, tzinfo=now.tzinfo)
                if at > now:
                    candidates.append(at)
        return min(candidates) if candidates else None
    interval = timedelta(minutes=int(schedule.get("interval_minutes", 60)))
    if last_started is None:
        return now + timedelta(minutes=1)
    return max(last_started + interval, now)
