def edit(path, pairs):
    s = open(path, encoding="utf-8").read()
    for old, new in pairs:
        assert old in s, f"not found in {path}: {old[:60]!r}"
        s = s.replace(old, new)
    open(path, "w", encoding="utf-8").write(s)


edit("busgps/pipeline.py", [
    ('def run(gtfs, gps_path, out, train_until=None, tz=5.5, cols=None):\n    os.makedirs(out, exist_ok=True)',
     'def cutoff_t(train_until):\n    """\'YYYY-MM-DD\' or \'YYYY-MM-DD HH:MM\' (Delhi time) -> seconds on the same clock as df.t."""\n'
     '    return (pd.Timestamp(train_until) - pd.Timestamp("1970-01-01")).total_seconds() if train_until else None\n\n\n'
     'def run(gtfs, gps_path, out, train_until=None, tz=5.5, cols=None, min_n=None, min_days=None):\n'
     '    global MIN_N, MIN_DAYS\n    if min_n is not None:\n        MIN_N = min_n\n    if min_days is not None:\n        MIN_DAYS = min_days\n    os.makedirs(out, exist_ok=True)'),
    ('        df = df[df.date < train_until]', '        df = df[df.t < cutoff_t(train_until)]'),
    ('train_until=train_until or df.date.max(),', 'train_until=train_until or df.date.max(), train_until_t=cutoff_t(train_until),'),
    ('    ap.add_argument("--tz", type=float, default=5.5, help="hours to add to UTC timestamps (Delhi 5.5)")',
     '    ap.add_argument("--tz", type=float, default=5.5, help="hours to add to UTC timestamps (Delhi 5.5)")\n'
     '    ap.add_argument("--min-days", type=int, default=None)\n    ap.add_argument("--min-n", type=int, default=None)'),
    ('a.tz, json.loads(a.cols) if a.cols else None)', 'a.tz, json.loads(a.cols) if a.cols else None, a.min_n, a.min_days)'),
])
edit("busgps/validate.py", [
    ('    test = df[df.date >= model.meta["train_until"]]',
     '    cut = model.meta.get("train_until_t")\n    test = df[df.t >= cut] if cut is not None else df[df.date >= model.meta["train_until"]]'),
])
print("patched OK")
