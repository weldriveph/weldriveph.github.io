#!/usr/bin/env python3
"""WelDrive PH: copy booked days from Apple Calendar into book/busy.json.

The APPLE_CALENDARS secret holds one or more iCloud "Public Calendar" links
(separated by spaces or new lines). This script reads those calendars and
writes ONLY the dates that have events, so the booking page can block them.
Names, phone numbers and other event details are never written or printed.
Standard library only, so the GitHub Action needs no installs.
"""
import datetime as dt
import json
import os
import re
import sys
import urllib.request
from zoneinfo import ZoneInfo

HOME_TZ = ZoneInfo("Asia/Manila")
UTC = dt.timezone.utc
OUT_FILE = os.environ.get("BUSY_FILE", "book/busy.json")
DAYS_AHEAD = 400
MAX_PERIODS = 40000     # safety cap when expanding repeating events
WEEKDAYS = ["MO", "TU", "WE", "TH", "FR", "SA", "SU"]
ONE_DAY = dt.timedelta(days=1)


# ---------- reading the .ics text ----------
def unfold(text):
    return re.sub(r"\r?\n[ \t]", "", text).splitlines()


def split_prop(line):
    """'DTSTART;TZID=Asia/Manila:20261019T080000' -> ('DTSTART', {'TZID': ...}, '20261019T080000')."""
    quoted = False
    for i, ch in enumerate(line):
        if ch == '"':
            quoted = not quoted
        elif ch == ":" and not quoted:
            head, value = line[:i], line[i + 1:]
            parts = re.findall(r'(?:[^;"]|"[^"]*")+', head)
            if not parts:
                return None
            params = {}
            for p in parts[1:]:
                if "=" in p:
                    k, v = p.split("=", 1)
                    params[k.strip().upper()] = v.strip().strip('"')
            return parts[0].strip().upper(), params, value
    return None


def read_events(text):
    """Each VEVENT as {NAME: [(params, value), ...]}; alarms and other nested parts are skipped."""
    found, cur, depth = [], None, 0
    for line in unfold(text):
        up = line.strip().upper()
        if cur is None:
            if up == "BEGIN:VEVENT":
                cur, depth = {}, 0
            continue
        if up.startswith("BEGIN:"):
            depth += 1
        elif up.startswith("END:"):
            if depth == 0:
                found.append(cur)
                cur = None
            else:
                depth -= 1
        elif depth == 0:
            prop = split_prop(line)
            if prop:
                cur.setdefault(prop[0], []).append((prop[1], prop[2]))
    return found


def first(ev, name):
    return ev[name][0][1].strip() if name in ev else ""


# ---------- dates and times ----------
def zone(params):
    name = params.get("TZID")
    if name:
        try:
            return ZoneInfo(name)
        except Exception:
            pass
    return HOME_TZ


def parse_when(params, value):
    """A date for all-day values, otherwise an aware datetime."""
    value = value.strip()
    if params.get("VALUE", "").upper() == "DATE" or re.fullmatch(r"\d{8}", value):
        return dt.datetime.strptime(value[:8], "%Y%m%d").date()
    m = re.fullmatch(r"(\d{8}T\d{6})(Z?)", value)
    if not m:
        raise ValueError("unreadable date")
    naive = dt.datetime.strptime(m.group(1), "%Y%m%dT%H%M%S")
    return naive.replace(tzinfo=UTC if m.group(2) else zone(params))


def parse_duration(value):
    m = re.fullmatch(r"([+-])?P(?:(\d+)W)?(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?", value.strip())
    if not m:
        raise ValueError("unreadable duration")
    sign, w, d, h, mi, s = m.groups()
    length = dt.timedelta(weeks=int(w or 0), days=int(d or 0), hours=int(h or 0),
                          minutes=int(mi or 0), seconds=int(s or 0))
    return -length if sign == "-" else length


def is_dt(v):
    return isinstance(v, dt.datetime)


# ---------- repeating events ----------
def parse_rule(value):
    rule = {}
    for part in value.strip().split(";"):
        if "=" in part:
            k, v = part.split("=", 1)
            rule[k.strip().upper()] = [x.strip().upper() for x in v.split(",") if x.strip()]
    return rule


def add_months(year, month, n):
    m = month - 1 + n
    return year + m // 12, m % 12 + 1


def days_in_month(year, month):
    y, m = add_months(year, month, 1)
    return (dt.date(y, m, 1) - ONE_DAY).day


def month_days(year, month, rule, start_day):
    """Days of one month picked by BYDAY ('SU', '1SU', '-1FR') or BYMONTHDAY."""
    last = days_in_month(year, month)
    out = []
    if "BYDAY" in rule:
        for item in rule["BYDAY"]:
            m = re.fullmatch(r"([+-]?\d+)?(MO|TU|WE|TH|FR|SA|SU)", item)
            if not m:
                continue
            same = [dt.date(year, month, d) for d in range(1, last + 1)
                    if dt.date(year, month, d).weekday() == WEEKDAYS.index(m.group(2))]
            if m.group(1):
                n = int(m.group(1))
                if 1 <= n <= len(same):
                    out.append(same[n - 1])
                elif -len(same) <= n <= -1:
                    out.append(same[n])
            else:
                out.extend(same)
        if "BYMONTHDAY" in rule:
            wanted = {last + 1 + int(d) if int(d) < 0 else int(d) for d in rule["BYMONTHDAY"]}
            out = [d for d in out if d.day in wanted]
    else:
        for d in rule.get("BYMONTHDAY") or [str(start_day)]:
            n = int(d)
            n = last + 1 + n if n < 0 else n
            if 1 <= n <= last:
                out.append(dt.date(year, month, n))
    return out


def pick_positions(days, rule):
    days = sorted(set(days))
    if "BYSETPOS" not in rule:
        return days
    picked = []
    for p in rule["BYSETPOS"]:
        n = int(p)
        if 1 <= n <= len(days):
            picked.append(days[n - 1])
        elif -len(days) <= n <= -1:
            picked.append(days[n])
    return sorted(set(picked))


def candidate_days(start, rule):
    """Possible occurrence days, in order, period by period (start is a date)."""
    freq = (rule.get("FREQ") or [""])[0]
    step = max(1, int((rule.get("INTERVAL") or ["1"])[0]))
    bydays = [WEEKDAYS.index(x[-2:]) for x in rule.get("BYDAY", []) if x[-2:] in WEEKDAYS]
    wkst = WEEKDAYS.index((rule.get("WKST") or ["MO"])[0]) if (rule.get("WKST") or ["MO"])[0] in WEEKDAYS else 0
    week0 = start - dt.timedelta(days=(start.weekday() - wkst) % 7)
    for k in range(MAX_PERIODS):
        if freq == "DAILY":
            day = start + dt.timedelta(days=k * step)
            period = [day] if not bydays or day.weekday() in bydays else []
        elif freq == "WEEKLY":
            base = week0 + dt.timedelta(weeks=k * step)
            period = [base + dt.timedelta(days=(wd - wkst) % 7) for wd in (bydays or [start.weekday()])]
        elif freq == "MONTHLY":
            y, m = add_months(start.year, start.month, k * step)
            period = month_days(y, m, rule, start.day)
        elif freq == "YEARLY":
            y = start.year + k * step
            period = []
            for mo in [int(x) for x in rule.get("BYMONTH", [])] or [start.month]:
                if "BYDAY" in rule or "BYMONTHDAY" in rule:
                    period += month_days(y, mo, rule, start.day)
                elif start.day <= days_in_month(y, mo):
                    period.append(dt.date(y, mo, start.day))
        else:
            return
        yield from pick_positions(period, rule)


def after(a, b):
    """Is moment a later than limit b? (either may be a date or a datetime)"""
    if is_dt(a) and is_dt(b):
        return a > b
    if is_dt(a):
        return a.date() > b
    if is_dt(b):
        return a > b.astimezone(HOME_TZ).date()
    return a > b


def occurrences(start, rule, skip, last_day):
    """Start of each occurrence (date or datetime) up to last_day."""
    if not rule:
        return [start]
    until = rule.get("UNTIL", [None])[0]
    until = parse_when({}, until) if until else None
    count = int(rule["COUNT"][0]) if "COUNT" in rule else None
    skip_days = {x for x in skip if not is_dt(x)}
    out, n = [], 0
    day0 = start.date() if is_dt(start) else start
    for day in candidate_days(day0, rule):
        occ = dt.datetime.combine(day, start.timetz()) if is_dt(start) else day
        if occ < start:
            continue
        if until is not None and after(occ, until):
            break
        if count is not None and n >= count:
            break
        n += 1
        if (occ.astimezone(HOME_TZ).date() if is_dt(occ) else occ) > last_day:
            break
        if occ not in skip and day not in skip_days:
            out.append(occ)
    return out


# ---------- busy days ----------
def day_span(occ, length):
    """First and last calendar day (Philippine time) that an occurrence touches."""
    if is_dt(occ):
        s = occ.astimezone(HOME_TZ)
        e = (occ.astimezone(UTC) + length).astimezone(HOME_TZ) if length else s
        last = e.date()
        if e > s and e.time() == dt.time(0):
            last -= ONE_DAY            # ends exactly at midnight
        return s.date(), max(s.date(), last)
    days = length.days if length else 1  # all-day: end date is exclusive
    return occ, occ + dt.timedelta(days=max(days, 1) - 1)


def busy_days(text, today, last_day):
    """Set of busy dates between today and last_day, plus how many events could not be read."""
    evs = read_events(text)
    moved = {}
    for ev in evs:
        if "RECURRENCE-ID" in ev and "UID" in ev:
            try:
                moved.setdefault(first(ev, "UID"), set()).add(parse_when(*ev["RECURRENCE-ID"][0]))
            except Exception:
                pass
    days, unreadable = set(), 0
    for ev in evs:
        if first(ev, "STATUS").upper() == "CANCELLED" or first(ev, "TRANSP").upper() == "TRANSPARENT":
            continue
        if "DTSTART" not in ev:
            continue
        try:
            start = parse_when(*ev["DTSTART"][0])
            if "DTEND" in ev:
                length = parse_when(*ev["DTEND"][0]) - start
            elif "DURATION" in ev:
                length = parse_duration(first(ev, "DURATION"))
            else:
                length = None
            rule = None
            if "RRULE" in ev and "RECURRENCE-ID" not in ev:
                rule = parse_rule(first(ev, "RRULE"))
            skip = set()
            for params, value in ev.get("EXDATE", []):
                for v in value.split(","):
                    if v.strip():
                        skip.add(parse_when(params, v))
            if rule:
                skip |= moved.get(first(ev, "UID"), set())
            for occ in occurrences(start, rule, skip, last_day):
                a, b = day_span(occ, length)
                d = max(a, today)
                while d <= min(b, last_day):
                    days.add(d)
                    d += ONE_DAY
        except Exception:
            unreadable += 1
    return days, unreadable


# ---------- main ----------
def calendar_links():
    raw = os.environ.get("APPLE_CALENDARS", "")
    return [re.sub(r"^webcal://", "https://", u, flags=re.I) for u in re.split(r"[\s,]+", raw) if u]


def download(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (WelDrive calendar sync)"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read().decode("utf-8", "replace")


def problem(message):
    """Report a broken link. Fail the run (which emails the owner) only when run by hand
    or during the 8 AM hour, so a dead link sends one alert a day instead of one every 15 min."""
    print(message + " Keeping the previous dates.")
    now = dt.datetime.now(HOME_TZ)
    manual = os.environ.get("GITHUB_EVENT_NAME") == "workflow_dispatch"
    return 1 if manual or (now.hour == 8 and now.minute < 30) or not os.environ.get("GITHUB_ACTIONS") else 0


def main():
    links = calendar_links()
    if not links:
        print("No calendar link yet: add the APPLE_CALENDARS secret. Nothing changed.")
        return 0
    today = dt.datetime.now(HOME_TZ).date()
    last_day = today + dt.timedelta(days=DAYS_AHEAD)
    busy = set()
    for i, url in enumerate(links, 1):
        try:
            text = download(url)
        except Exception as ex:   # print only the error type, never the link
            return problem(f"Calendar {i}: could not download ({type(ex).__name__}).")
        if "BEGIN:VCALENDAR" not in text[:2000].upper():
            return problem(f"Calendar {i}: the link did not return a calendar.")
        found, unreadable = busy_days(text, today, last_day)
        note = f" ({unreadable} event(s) skipped)" if unreadable else ""
        print(f"Calendar {i}: {len(found)} busy day(s) from today on{note}")
        busy |= found
    data = json.dumps({"busy": sorted(d.isoformat() for d in busy)}, indent=1) + "\n"
    old = open(OUT_FILE, encoding="utf-8").read() if os.path.exists(OUT_FILE) else ""
    if data == old:
        print("No change.")
        return 0
    os.makedirs(os.path.dirname(OUT_FILE) or ".", exist_ok=True)
    with open(OUT_FILE, "w", encoding="utf-8") as f:
        f.write(data)
    print(f"Saved {len(busy)} busy day(s) to {OUT_FILE}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
