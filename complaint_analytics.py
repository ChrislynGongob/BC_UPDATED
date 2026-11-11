import re
import colorsys
from datetime import date, datetime, timedelta, timezone


PH_TIMEZONE = timezone(timedelta(hours=8))
DEFAULT_COLORS = (
    "#2563EB", "#0F9D8A", "#E68A1E", "#8B5CF6", "#D9466F",
    "#0891B2", "#65A30D", "#C2410C", "#4F46E5", "#0F766E",
)
MODULES = {
    "complaints": {
        "table": "complaints",
        "field": "category",
        "label": "Complaint Analytics",
        "title": "Most Reported Category",
        "singular": "complaint",
        "plural": "complaints",
        "preference_label": "complaint",
    },
    "document-requests": {
        "table": "requests",
        "field": "request_type",
        "label": "Document Request Analytics",
        "title": "Most Requested Document Type",
        "singular": "document request",
        "plural": "document requests",
        "preference_label": "document request",
    },
}
COLOR_PATTERN = re.compile(r"#[0-9A-Fa-f]{6}\Z")
MONTH_PATTERN = re.compile(r"\d{4}-(0[1-9]|1[0-2])\Z")
YEAR_PATTERN = re.compile(r"\d{4}\Z")


def current_filters(args, now=None):
    now = now or datetime.now(PH_TIMEZONE)
    return {
        "period": args.get("period", "monthly"),
        "daily_date": args.get("daily_date") or now.date().isoformat(),
        "month": args.get("month") or now.strftime("%Y-%m"),
        "year": args.get("year") or now.strftime("%Y"),
        "start_date": args.get("analytics_start", ""),
        "end_date": args.get("analytics_end", ""),
    }


def _iso_date(value, label):
    try:
        parsed = date.fromisoformat(value)
    except (TypeError, ValueError):
        raise ValueError(f"Choose a valid {label}.")
    if parsed.isoformat() != value or parsed == date.max:
        raise ValueError(f"Choose a valid {label}.")
    return parsed


def period_utc_bounds(filters):
    period = filters["period"]
    if period == "daily":
        local_start = _iso_date(filters["daily_date"], "date")
        local_end = local_start + timedelta(days=1)
    elif period == "monthly":
        month = filters["month"]
        if not MONTH_PATTERN.fullmatch(month):
            raise ValueError("Choose a valid month and year.")
        year, month_number = (int(value) for value in month.split("-"))
        if year < 1:
            raise ValueError("Choose a valid month and year.")
        if year >= 9999:
            raise ValueError("Choose a month before 9999.")
        local_start = date(year, month_number, 1)
        local_end = date(year + (month_number == 12), month_number % 12 + 1, 1)
    elif period == "yearly":
        year_text = filters["year"]
        if not YEAR_PATTERN.fullmatch(year_text):
            raise ValueError("Choose a valid year.")
        year = int(year_text)
        if year < 1 or year >= 9999:
            raise ValueError("Choose a year before 9999.")
        local_start = date(year, 1, 1)
        local_end = date(year + 1, 1, 1)
    elif period == "custom":
        local_start = _iso_date(filters["start_date"], "start date")
        last_day = _iso_date(filters["end_date"], "end date")
        if last_day < local_start:
            raise ValueError("The end date must be on or after the start date.")
        local_end = last_day + timedelta(days=1)
    else:
        raise ValueError("Choose a valid complaint analytics period.")

    start_local = datetime.combine(local_start, datetime.min.time(), PH_TIMEZONE)
    end_local = datetime.combine(local_end, datetime.min.time(), PH_TIMEZONE)
    utc = timezone.utc
    return (
        start_local.astimezone(utc).strftime("%Y-%m-%d %H:%M:%S"),
        end_local.astimezone(utc).strftime("%Y-%m-%d %H:%M:%S"),
    )


def empty_payload(filters, error=None):
    return {
        "filters": filters,
        "items": [],
        "total": 0,
        "leaders": [],
        "error": error,
    }


def _default_color(index):
    if index < len(DEFAULT_COLORS):
        return DEFAULT_COLORS[index]
    hue = (index * 0.618033988749895) % 1
    red, green, blue = colorsys.hsv_to_rgb(hue, 0.66, 0.82)
    return "#{:02X}{:02X}{:02X}".format(
        round(red * 255), round(green * 255), round(blue * 255)
    )


def build_payload(conn, admin_id, filters, module):
    config = MODULES.get(module)
    if config is None:
        raise ValueError("Choose a valid analytics module.")
    start_utc, end_utc = period_utc_bounds(filters)
    label_expression = (
        f"COALESCE(NULLIF({config['field']}, ''), 'Unspecified')"
    )
    label_sort_expression = f"LOWER({label_expression})"
    rows = conn.execute(
        f"SELECT {label_expression} AS label, COUNT(*) AS count "
        f"FROM {config['table']} WHERE datetime(created_at)>=? AND datetime(created_at)<? "
        f"GROUP BY {label_expression}, {label_sort_expression} "
        f"ORDER BY count DESC, {label_sort_expression} ASC, {label_expression} ASC",
        (start_utc, end_utc),
    ).fetchall()
    items = [dict(label=row["label"], count=row["count"]) for row in rows]
    all_labels = [
        row["label"]
        for row in conn.execute(
            f"SELECT label FROM ("
            f"SELECT DISTINCT {label_expression} AS label, "
            f"{label_sort_expression} AS sort_label FROM {config['table']}"
            ") labels ORDER BY sort_label ASC, label ASC"
        ).fetchall()
    ]
    saved = {
        row["category"]: row["color"]
        for row in conn.execute(
            "SELECT category, color FROM admin_chart_preferences "
            "WHERE admin_id=? AND analytics_type=?",
            (admin_id, module),
        ).fetchall()
    }
    default_indices = {
        category: index
        for index, category in enumerate(all_labels)
    }
    color_preferences = [
        {
            "label": label,
            "color": saved.get(label, _default_color(default_indices[label])),
        }
        for label in all_labels
    ]
    color_map = {row["label"]: row["color"] for row in color_preferences}
    maximum = max((row["count"] for row in items), default=0)
    chart_width = max(640, 90 + len(items) * 100)
    for index, row in enumerate(items):
        row["color"] = color_map[row["label"]]
        row["x"] = 62 + index * 100
        row["bar_width"] = 46
        row["bar_height"] = max(1, round(row["count"] / maximum * 210)) if maximum else 0
        row["bar_y"] = 260 - row["bar_height"]
        row["label_x"] = row["x"] + 23
        row["label_y"] = 279
        row["value_y"] = row["bar_y"] - 8
    total = sum(row["count"] for row in items)
    leaders = []
    if items:
        highest = items[0]["count"]
        leaders = [row["label"] for row in items if row["count"] == highest]
    return {
        "filters": filters,
        "items": items,
        "total": total,
        "leaders": leaders,
        "error": None,
        "chart_width": chart_width,
        "chart_max": maximum,
        "module": module,
        "color_preferences": color_preferences,
    }


def build_purpose_payload(conn, filters, allowed_types=None, document_type="", office="all"):
    start_utc, end_utc = period_utc_bounds(filters)
    scope_clauses = []
    scope_params = []
    if allowed_types is not None:
        if not allowed_types:
            return empty_purpose_payload(filters, document_type, office)
        scope_clauses.append(
            "request_type IN (" + ",".join("?" for _ in allowed_types) + ")"
        )
        scope_params.extend(allowed_types)
    if document_type:
        scope_clauses.append("request_type=?")
        scope_params.append(document_type)
    scope_where = " AND ".join(scope_clauses) if scope_clauses else "1=1"
    date_params = [start_utc, end_utc] + scope_params
    purpose_expression = "COALESCE(NULLIF(TRIM(purpose), ''), 'Unspecified')"
    purpose_sort_expression = f"LOWER({purpose_expression})"
    rows = conn.execute(
        f"SELECT {purpose_expression} AS purpose, COUNT(*) AS count FROM requests "
        "WHERE datetime(created_at)>=? AND datetime(created_at)<? "
        f"AND {scope_where} "
        f"GROUP BY {purpose_expression}, {purpose_sort_expression} "
        f"ORDER BY count DESC, {purpose_sort_expression} ASC, {purpose_expression} ASC",
        date_params,
    ).fetchall()
    labels = [
        row["purpose"]
        for row in conn.execute(
            "SELECT purpose FROM ("
            f"SELECT DISTINCT {purpose_expression} AS purpose, "
            f"{purpose_sort_expression} AS sort_label FROM requests "
            f"WHERE {scope_where}"
            ") purposes ORDER BY sort_label ASC, purpose ASC",
            scope_params,
        ).fetchall()
    ]
    color_map = {label: _default_color(index) for index, label in enumerate(labels)}
    items = [
        {
            "purpose": row["purpose"],
            "label": row["purpose"],
            "count": row["count"],
            "color": color_map[row["purpose"]],
        }
        for row in rows
    ]
    maximum = max((row["count"] for row in items), default=0)
    chart_width = max(640, 90 + len(items) * 100)
    for index, row in enumerate(items):
        row["x"] = 62 + index * 100
        row["bar_width"] = 46
        row["bar_height"] = max(1, round(row["count"] / maximum * 210)) if maximum else 0
        row["bar_y"] = 260 - row["bar_height"]
        row["label_x"] = row["x"] + 23
        row["label_y"] = 279
        row["value_y"] = row["bar_y"] - 8
    total = sum(row["count"] for row in items)
    leaders = []
    if items:
        highest = items[0]["count"]
        leaders = [row["label"] for row in items if row["count"] == highest]
    return {
        "filters": filters,
        "items": items,
        "total": total,
        "leaders": leaders,
        "most_common_purpose": ", ".join(leaders) if leaders else None,
        "most_common_purpose_count": maximum,
        "document_type": document_type,
        "office": office,
        "chart_width": chart_width,
        "chart_max": maximum,
        "error": None,
    }


def empty_purpose_payload(filters, document_type="", office="all", error=None):
    return {
        "filters": filters,
        "items": [],
        "total": 0,
        "leaders": [],
        "most_common_purpose": None,
        "most_common_purpose_count": 0,
        "document_type": document_type,
        "office": office,
        "chart_width": 640,
        "chart_max": 0,
        "error": error,
    }


def valid_color(value):
    return isinstance(value, str) and COLOR_PATTERN.fullmatch(value) is not None
