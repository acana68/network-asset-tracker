"""Streamlit dashboard for the asset tracker.

    streamlit run dashboard/app.py

Reads demo.db or assets.db from the project folder. The database is opened
read-only, so the dashboard can never change it, even while a scheduled scan
is writing to it. "Export CSV" writes exports/devices.csv and
exports/sightings.csv for Power BI or Excel.

Each subnet gets its own row in `scans`, so scans of different subnets that
run back to back are grouped into one "run". Online counts, uptime, and the
heatmap are all per run. Online/offline status uses the same rule as the
`list` command (tracker/status.py).
"""

import sqlite3
import sys
from contextlib import closing
from datetime import timedelta
from pathlib import Path

import altair as alt
import pandas as pd
import streamlit as st

PROJECT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_DIR))
from tracker import config as tracker_config, status as tracker_status  # noqa: E402

DATABASES = ["demo.db", "assets.db"]
EXPORT_DIR = PROJECT_DIR / "exports"

# A scan starting more than this long after the previous one finished begins a new run
RUN_GAP = timedelta(minutes=5)

# Every chart is a single series, so they share one accent color, stepped for
# each theme. The heatmap uses one hue, fading toward the background near zero.
THEMES = {
    "light": dict(accent="#2a78d6", ink="#52514e", muted="#898781", surface="#ffffff",
                  ramp=["#cde2fb", "#86b6ef", "#3987e5", "#1c5cab", "#0d366b"]),
    "dark": dict(accent="#3987e5", ink="#c3c2b7", muted="#898781", surface="#0e1117",
                 ramp=["#0d366b", "#1c5cab", "#3987e5", "#86b6ef", "#cde2fb"]),
}

alt.data_transformers.disable_max_rows()


# ---------- loading ----------

def connect_readonly(path):
    # mode=ro: SQLite refuses every write, and won't create a missing file
    return sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)


def default_offline_hours():
    path = PROJECT_DIR / tracker_config.DEFAULT_PATH
    try:
        return tracker_status.offline_hours(tracker_config.load(str(path)) if path.exists() else {})
    except SystemExit:  # unreadable config.json: the CLI reports it, the dashboard just uses the default
        return float(tracker_status.DEFAULT_HOURS)


@st.cache_data(show_spinner=False)
def load(path_str, mtime, offline_hours):
    """Raw tables plus each device's status. `mtime` is part of the cache key, so new
    scans show up on rerun."""
    with closing(connect_readonly(Path(path_str))) as conn:
        devices = pd.read_sql_query("SELECT * FROM devices", conn)
        scans = pd.read_sql_query("SELECT * FROM scans ORDER BY started_at, id", conn)
        sightings = pd.read_sql_query("SELECT * FROM sightings ORDER BY id", conn)
        statuses = tracker_status.device_statuses(conn, offline_hours)
    devices["status"] = devices.id.map(statuses)
    if "watched" not in devices:  # a database the scanner hasn't upgraded yet
        devices["watched"] = 0
    for df, cols in [(devices, ["first_seen", "last_seen"]),
                     (scans, ["started_at", "finished_at"]),
                     (sightings, ["seen_at"])]:
        for col in cols:
            df[col] = pd.to_datetime(df[col], format="ISO8601", errors="coerce")
    return devices, scans, sightings


def assign_runs(scans):
    """Number finished scans by run: a new run starts when a subnet repeats or after a gap."""
    scans = scans[scans.finished_at.notna()].copy()
    run_ids, run, subnets, prev_end = [], 0, set(), None
    for s in scans.itertuples():
        if prev_end is None or s.subnet in subnets or s.started_at - prev_end > RUN_GAP:
            run += 1
            subnets = set()
        subnets.add(s.subnet)
        run_ids.append(run)
        prev_end = s.finished_at
    scans["run"] = run_ids
    return scans


def display_names(devices):
    """Nickname, else vendor, else hostname; duplicates get the end of their MAC."""
    fallback = devices.is_random_mac.map({1: "Private MAC"}).fillna("Unknown")
    name = devices.nickname.replace("", None)
    for col in (devices.vendor, devices.hostname, fallback):
        name = name.fillna(col.replace("", None))
    label = name.copy()
    dupes = label.duplicated(keep=False)
    label[dupes] = label[dupes] + " (" + devices.mac[dupes].str[-5:] + ")"
    return name, label


def build_model(devices, scans, sightings):
    scans = assign_runs(scans)
    runs = (scans.groupby("run")
            .agg(started=("started_at", "min"), finished=("finished_at", "max"),
                 subnets=("subnet", "nunique"))
            .reset_index())
    runs["hour"] = runs.started.dt.hour
    last_run = runs.run.max()

    # One row per device per run it answered in (sightings of unfinished scans are left out)
    seen = (sightings.merge(scans[["id", "run"]], left_on="scan_id", right_on="id",
                            suffixes=("", "_scan"))
            .drop_duplicates(["device_id", "run"]))

    d = devices.copy()
    d["name"], d["label"] = display_names(d)
    d["device_type"] = d.device_type.fillna("Unknown")
    d["trusted"] = d.trusted.astype(bool)
    d["watched"] = d.watched.astype(bool)
    d["last_ip"] = d.id.map(sightings.groupby("device_id").ip.last())
    first_run = seen.groupby("device_id").run.min()
    d["first_run"] = d.id.map(first_run)
    d["runs_seen"] = d.id.map(seen.groupby("device_id").run.nunique()).fillna(0).astype(int)
    d["runs_since_first"] = d.id.map(last_run - first_run + 1)
    d["uptime"] = d.runs_seen / d.runs_since_first
    d["in_last_run"] = d.id.isin(seen.loc[seen.run == last_run, "device_id"])

    online = (runs[["run", "started"]]
              .merge(seen.groupby("run").device_id.nunique().rename("online").reset_index(),
                     how="left")
              .fillna({"online": 0}))

    # Heatmap: of the runs at each hour since the device first appeared, how many it answered
    seen_by_hour = seen.merge(runs[["run", "hour"]]).groupby(["device_id", "hour"]).size()
    grid = (first_run.rename("first_run").reset_index()
            .merge(runs[["run", "hour"]], how="cross"))
    possible = grid[grid.run >= grid.first_run].groupby(["device_id", "hour"]).size()
    heat = pd.concat({"seen": seen_by_hour, "possible": possible}, axis=1).fillna(0).reset_index()
    heat["share"] = heat.seen / heat.possible
    heat = heat.merge(d[["id", "label"]], left_on="device_id", right_on="id")

    return d, runs, seen, online, heat


# ---------- charts ----------

def online_chart(online, t):
    hover = alt.selection_point(fields=["started"], nearest=True, on="pointerover",
                                empty=False, clear="pointerout")
    # Date at midnight, time of day otherwise
    time_labels = ("hours(datum.value) == 0 && minutes(datum.value) == 0"
                   " ? timeFormat(datum.value, '%b %d') : timeFormat(datum.value, '%H:%M')")
    base = alt.Chart(online).encode(
        x=alt.X("started:T", title="Scan run", axis=alt.Axis(labelExpr=time_labels)),
        y=alt.Y("online:Q", title="Devices online", axis=alt.Axis(format="d", tickMinStep=1)),
    )
    line = base.mark_line(color=t["accent"], strokeWidth=2, interpolate="monotone")
    points = base.mark_point(color=t["accent"], filled=True, size=70).encode(
        opacity=alt.condition(hover, alt.value(1), alt.value(0)),
        tooltip=[alt.Tooltip("started:T", title="Run", format="%b %d, %H:%M"),
                 alt.Tooltip("online:Q", title="Devices online")],
    ).add_params(hover)
    rule = base.mark_rule(color=t["muted"]).encode(
        opacity=alt.condition(hover, alt.value(0.6), alt.value(0)))
    return (line + rule + points).properties(height=260)


def type_chart(d, t):
    counts = d.groupby("device_type").size().rename("devices").reset_index()
    base = alt.Chart(counts).encode(
        y=alt.Y("device_type:N", sort="-x", title="Device type",
                axis=alt.Axis(labelLimit=260)),
        x=alt.X("devices:Q", title="Devices", axis=alt.Axis(format="d", tickMinStep=1)),
        tooltip=[alt.Tooltip("device_type:N", title="Type"),
                 alt.Tooltip("devices:Q", title="Devices")],
    )
    bars = base.mark_bar(color=t["accent"], cornerRadiusEnd=4, size=16)
    labels = base.mark_text(align="left", dx=5, color=t["ink"]).encode(text="devices:Q")
    return (bars + labels).properties(height=alt.Step(26))


def new_devices_chart(d, runs, t):
    """Devices by day first seen, leaving out the baseline (devices found by the first run)."""
    first_day, last_day = runs.started.min().normalize(), runs.finished.max().normalize()
    days = pd.date_range(min(first_day, d.first_seen.min().normalize()), last_day, freq="D")
    new = d[d.first_run != runs.run.min()]
    counts = (new.first_seen.dt.normalize().value_counts()
              .reindex(days, fill_value=0).rename_axis("day").rename("new").reset_index())
    counts["label"] = counts.day.dt.strftime("%b %d")
    # Whole-number ticks only; small counts otherwise get repeated labels like 0, 1, 1, 2, 2
    top = max(int(counts.new.max()), 1)
    step = -(-top // 5)
    ticks = list(range(0, top + step, step))
    base = alt.Chart(counts).encode(
        x=alt.X("label:O", sort=None, title="Day first seen", axis=alt.Axis(labelAngle=0)),
        y=alt.Y("new:Q", title="New devices", scale=alt.Scale(domain=[0, ticks[-1]]),
                axis=alt.Axis(format="d", values=ticks)),
        tooltip=[alt.Tooltip("day:T", title="Day", format="%a %b %d"),
                 alt.Tooltip("new:Q", title="New devices")],
    )
    bars = base.mark_bar(color=t["accent"], cornerRadiusTopLeft=4, cornerRadiusTopRight=4,
                         size=28)
    labels = (base.transform_filter("datum.new > 0")
              .mark_text(baseline="bottom", dy=-4, color=t["ink"]).encode(text="new:Q"))
    return (bars + labels).properties(height=260)


def heatmap(heat, d, t):
    order = d.sort_values(["uptime", "runs_seen", "label"],
                          ascending=[False, False, True]).label.tolist()
    return alt.Chart(heat).mark_rect(stroke=t["surface"], strokeWidth=2, cornerRadius=2).encode(
        x=alt.X("hour:O", title="Hour of day", axis=alt.Axis(labelAngle=0)),
        y=alt.Y("label:N", sort=order, title="Device", axis=alt.Axis(labelLimit=260)),
        color=alt.Color("share:Q", title="Seen",
                        scale=alt.Scale(domain=[0, 1], range=t["ramp"]),
                        legend=alt.Legend(format=".0%", gradientLength=160)),
        tooltip=[alt.Tooltip("label:N", title="Device"),
                 alt.Tooltip("hour:O", title="Hour"),
                 alt.Tooltip("share:Q", title="Seen", format=".0%"),
                 alt.Tooltip("seen:Q", title="Runs seen in"),
                 alt.Tooltip("possible:Q", title="Runs at this hour")],
    ).properties(height=alt.Step(22))


# ---------- export ----------

def export_csv(d, sightings, scans):
    EXPORT_DIR.mkdir(exist_ok=True)
    devices_out = d[["mac", "name", "nickname", "vendor", "hostname", "device_type",
                     "is_random_mac", "trusted", "watched", "status", "in_last_run",
                     "last_ip", "first_seen", "last_seen", "runs_seen",
                     "runs_since_first"]].copy()
    devices_out["uptime_pct"] = (d.uptime * 100).round(1)
    sightings_out = (sightings
                     .merge(scans[["id", "subnet"]], left_on="scan_id", right_on="id",
                            how="left", suffixes=("", "_scan"))
                     .merge(d[["id", "mac", "name", "device_type"]], left_on="device_id",
                            right_on="id", how="left", suffixes=("", "_device"))
                     .rename(columns={"id": "sighting_id"})
                     [["sighting_id", "seen_at", "scan_id", "subnet", "mac", "name",
                       "device_type", "ip"]])
    paths = [EXPORT_DIR / "devices.csv", EXPORT_DIR / "sightings.csv"]
    # utf-8-sig so Excel detects the encoding
    devices_out.to_csv(paths[0], index=False, encoding="utf-8-sig")
    sightings_out.to_csv(paths[1], index=False, encoding="utf-8-sig")
    return [(p, n) for p, n in zip(paths, [len(devices_out), len(sightings_out)])]


# ---------- page ----------

st.set_page_config(page_title="Network Asset Tracker", page_icon=":material/lan:",
                   layout="wide")
t = THEMES.get(st.context.theme.type, THEMES["light"])

with st.sidebar:
    st.header("Data")
    db_name = st.radio("Database", DATABASES,
                       help="demo.db holds fake devices (`python -m tracker demo`). "
                            "assets.db is your real scan history.")
    db_path = PROJECT_DIR / db_name
    st.caption("Opened read-only: the dashboard never writes to the database.")
    offline_hours = st.number_input(
        "Offline after (hours)", min_value=0.25, step=0.5, value=default_offline_hours(),
        help="A device is offline if it wasn't seen in this many hours before its subnet's "
             "latest reliable scan. The default comes from offline_after_hours in config.json.")

st.title("Network Asset Tracker")

if not db_path.exists():
    hint = "python -m tracker demo" if db_name == "demo.db" else "python -m tracker scan"
    st.error(f"{db_name} wasn't found in the project folder. Create it with `{hint}`.")
    st.stop()

try:
    devices, scans, sightings = load(str(db_path), db_path.stat().st_mtime, offline_hours)
except sqlite3.Error as e:
    st.error(f"Couldn't read {db_name}: {e}")
    st.stop()

if devices.empty or scans.finished_at.notna().sum() == 0:
    st.info(f"{db_name} has no finished scans yet.")
    st.stop()

d, runs, seen, online, heat = build_model(devices, scans, sightings)
last_scan = runs.finished.max()

with st.sidebar:
    st.header("Export")
    if st.button("Export CSV", icon=":material/download:", width="stretch",
                 help="Writes every device and sighting (unfiltered) to the exports/ folder."):
        for path, rows in export_csv(d, sightings, scans):
            st.success(f"{path.relative_to(PROJECT_DIR).as_posix()}: {rows:,} rows")

st.caption(f"{db_name} · last scan {last_scan:%b %d, %H:%M} · "
           f"{len(runs):,} scan runs across {scans.subnet.nunique()} subnet(s)")

# KPI cards
new_24h = (d.first_seen > last_scan - timedelta(hours=24)).sum()
watched_offline = d[d.watched & (d.status == tracker_status.OFFLINE)]
k1, k2, k3, k4, k5 = st.columns([0.8, 0.9, 0.95, 1.05, 1.15])  # sized to the labels
k1.metric("Devices tracked", len(d), border=True)
k2.metric("Currently online", int(d.in_last_run.sum()), border=True,
          help="Devices that answered the most recent scan run.")
k3.metric("Untrusted devices", int((~d.trusted).sum()), border=True,
          help="Devices nobody has marked trusted with `python -m tracker trust`.")
k4.metric("New in last 24 hours", int(new_24h), border=True,
          help="First seen in the 24 hours before the last scan.")
k5.metric("Watched devices offline", f"{len(watched_offline)} of {int(d.watched.sum())}",
          border=True,
          help=f"Watched devices not seen in the {offline_hours:g} hours before their subnet's "
               "latest scan" + (": " + ", ".join(watched_offline.name) if len(watched_offline) else "."))

st.subheader("Devices online over time")
st.caption("One point per scan run, all subnets combined.")
st.altair_chart(online_chart(online, t), width="stretch")

left, right = st.columns(2, gap="large")
with left:
    st.subheader("Device mix by type")
    st.caption("Every device ever seen.")
    st.altair_chart(type_chart(d, t), width="stretch")
with right:
    st.subheader("New devices over time")
    baseline = int((d.first_run == runs.run.min()).sum())
    st.caption(f"Devices by the day they were first seen. "
               f"First scan ({baseline} devices) excluded as baseline.")
    st.altair_chart(new_devices_chart(d, runs, t), width="stretch")

st.subheader("Device activity by hour")
st.caption("Share of scan runs at each hour of the day in which the device answered, "
           "counting only runs since it was first seen. Most consistently online first.")
st.altair_chart(heatmap(heat, d, t), width="stretch")

st.subheader("Devices")
f1, f2, f3 = st.columns([2, 2, 1])
query = f1.text_input("Search", placeholder="Name, vendor, MAC, or IP")
types = f2.multiselect("Type", sorted(d.device_type.unique()), placeholder="All types")
trust = f3.selectbox("Trusted", ["All", "Trusted", "Untrusted"])

table = d.sort_values(["trusted", "last_seen"], ascending=[True, False])
if query:
    haystack = table[["name", "vendor", "hostname", "mac", "last_ip"]].fillna("").agg(" ".join, axis=1)
    table = table[haystack.str.contains(query, case=False, regex=False)]
if types:
    table = table[table.device_type.isin(types)]
if trust != "All":
    table = table[table.trusted == (trust == "Trusted")]

STATUS_LABELS = {tracker_status.ONLINE: "🟢 Online", tracker_status.OFFLINE: "🔴 Offline"}
st.dataframe(
    table[["name", "status", "watched", "mac", "device_type", "last_ip", "trusted", "first_seen",
           "last_seen", "uptime"]]
    .assign(uptime=table.uptime * 100, status=table.status.map(STATUS_LABELS).fillna("–")),
    hide_index=True,
    height=min(len(table) + 1, 21) * 35 + 3,  # every row up to 20, then scroll
    column_config={
        # Widths fit a laptop screen without sideways scrolling
        "name": st.column_config.TextColumn("Nickname / vendor", width=135),
        "status": st.column_config.TextColumn(
            "Status", width=85,
            help=f"Offline = not seen in the {offline_hours:g} hours before its subnet's "
                 "latest reliable scan."),
        "watched": st.column_config.CheckboxColumn(
            "Watched", width=65, help="Alerts when it goes offline or comes back "
                                      "(`python -m tracker watch <mac>`)."),
        "mac": st.column_config.TextColumn("MAC", width=120),
        "device_type": st.column_config.TextColumn("Type", width=125),
        "last_ip": st.column_config.TextColumn("Last IP", width=95),
        "trusted": st.column_config.CheckboxColumn("Trusted", width=60),
        "first_seen": st.column_config.DatetimeColumn("First seen", format="MMM D", width=70),
        "last_seen": st.column_config.DatetimeColumn("Last seen", format="MMM D, HH:mm",
                                                     width=95),
        "uptime": st.column_config.ProgressColumn(
            "Uptime", min_value=0, max_value=100, format="%.0f%%", width=80,
            help="Share of scan runs since the device was first seen in which it answered."),
    },
)
st.caption(f"{len(table)} of {len(d)} devices")
