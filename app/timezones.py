from datetime import timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def get_timezone(name):
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        # Windows has no IANA database. Shanghai has had no DST since 1991;
        # the archive accepts events from 2000 onward only.
        if name == "Asia/Shanghai":
            return timezone(timedelta(hours=8), name="Asia/Shanghai")
        if name in ("UTC", "Etc/UTC"):
            return timezone.utc
        raise ValueError("此时区需要系统 IANA 时区数据库：" + name)
