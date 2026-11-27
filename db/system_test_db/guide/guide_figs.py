"""Inline SVG figures for the resource guide (zhTW and EN). Numbers marked REAL come from the results DB (queried 2026-10-30)."""

FG, MUT, CY, PASS, FAIL, WARN, GRID = "#f0f4ff", "#94a3b8", "#00c7e6", "#10b981", "#ef4444", "#f59e0b", "rgba(255,255,255,0.08)"
FONT = "font-family:Inter,'Microsoft JhengHei',sans-serif"


def L(lang, zh, en):
    return zh if lang == "zh" else en


def svg(w, h, body):
    return f'<svg viewBox="0 0 {w} {h}" xmlns="http://www.w3.org/2000/svg" style="{FONT}">{body}</svg>'


def t(x, y, s, size=13, fill=MUT, anchor="start", weight=400):
    return f'<text x="{x}" y="{y}" font-size="{size}" fill="{fill}" text-anchor="{anchor}" font-weight="{weight}">{s}</text>'


def line(x1, y1, x2, y2, color=GRID, w=1, dash=""):
    d = f' stroke-dasharray="{dash}"' if dash else ""
    return f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" stroke="{color}" stroke-width="{w}"{d}/>'


def rect(x, y, w, h, fill, rx=3, stroke=""):
    s = f' stroke="{stroke}"' if stroke else ""
    return f'<rect x="{x:.1f}" y="{y:.1f}" width="{w:.1f}" height="{max(h, 0):.1f}" rx="{rx}" fill="{fill}"{s}/>'


def _units(s):
    """Split into wrap units: a run of ASCII non-space characters stays one word, every other character is its own unit."""
    out, cur = [], ""
    for ch in s:
        if ord(ch) < 0x2E80 and not ch.isspace():
            cur += ch
            continue
        if cur:
            out.append(cur)
            cur = ""
        out.append(ch)
    if cur:
        out.append(cur)
    return out


def _w(unit, size):
    return sum(size * (1.0 if ord(c) >= 0x2E80 else 0.58) for c in unit)


def wrap(s, size, maxw):
    lines, cur, w = [], "", 0.0
    for u in _units(s):
        uw = _w(u, size)
        if cur and w + uw > maxw and not u.isspace():
            lines.append(cur.rstrip())
            cur, w = "", 0.0
            if u.isspace():
                continue
        cur += u
        w += uw
    if cur.strip():
        lines.append(cur.rstrip())
    return lines or [""]


def block(items, x, y, maxw):
    """Stack wrapped text lines below y. items = [(text, size, fill, weight)]. Returns (svg, y after the block)."""
    out = []
    for s, size, fill, weight in items:
        for ln in wrap(s, size, maxw):
            y += size * 1.4
            out.append(t(x, round(y, 1), ln, size, fill, "start", weight))
        y += 4
    return "".join(out), y


def compose(w, title, plot, plot_h, notes):
    """Title block on top, the plot (its own coordinates from y=0) in the middle, wrapped notes below; the height follows the text."""
    head, hy = block(title, 20, 4, w - 40)
    dy = hy + 8
    foot, fy = block(notes, 20, dy + plot_h + 4, w - 40)
    return svg(w, round(fy + 10), head + f'<g transform="translate(0,{dy:.1f})">{plot}</g>' + foot)


def fig_sampling(lang):
    """Concept: how one CPU point is made (cumulative CPU time diff over one window)."""
    b = []
    xs = [60, 260, 460, 660]
    cum = [100.0, 128.0, 139.0, 150.0]
    b.append(line(40, 60, 740, 60, MUT, 2))
    for i, x in enumerate(xs):
        b.append(f'<circle cx="{x}" cy="60" r="6" fill="{CY}"/>')
        b.append(t(x, 88, L(lang, f"第 {i * 10} 秒", f"{i * 10} s"), 13, FG, "middle"))
        b.append(t(x, 40, L(lang, f"累積 {cum[i]:.1f} 秒", f"total {cum[i]:.1f} s"), 13, CY, "middle"))
    vals = [280, 110, 110]
    for i in range(3):
        b.append(rect(xs[i] + 10, 110, xs[i + 1] - xs[i] - 20, 58, "rgba(0,199,230,0.10)", 6, "rgba(0,199,230,0.35)"))
        mx = (xs[i] + xs[i + 1]) / 2
        b.append(t(mx, 132, L(lang, "這一段 10 秒", "this 10 s window"), 12, MUT, "middle"))
        b.append(t(mx, 154, f"= {vals[i]}", 20, WARN if i == 0 else PASS, "middle", 700))
    title = [(L(lang, "取樣器每 10 秒讀一次「這個 process 累積用掉的 CPU 秒數」,相鄰兩次相減,就是這個點的值",
                "Every 10 s the sampler reads the process's cumulative CPU seconds; the difference of two reads is the value of one point"), 15, FG, 600)]
    notes = [(L(lang, "第一段:(128.0 − 100.0)秒 ÷ 10 秒 = 2.8 個核心 = 280。100 = 整整用掉 1 個核心。表上的 CPU 數字全部是這種「10 秒窗口的平均」,不是瞬間值。",
                "First window: (128.0 - 100.0) s / 10 s = 2.8 cores = 280. 100 = one core fully busy. Every CPU number in the tables is such a 10 s window average, not an instant value."), 13, MUT, 400),
             (L(lang, "(示意數字,計算方式取自取樣器 windows_system_info.py)", "(illustrative numbers; the formula is the one in the sampler, windows_system_info.py)"), 12, MUT, 400)]
    return compose(780, title, "".join(b), 180, notes)


def fig_sustained(lang):
    """REAL left: LOCAL2 build 550 STRESS-03 stAgentSvc, 19 samples around its highest reading. Right: what 60 s of 'sustained' looks like."""
    real = [39.88, 56.84, 13.2, 102.4, 25.64, 13.04, 74.44, 23.4, 19.92, 143.28, 47.92, 15.84, 72.56, 25.28, 10.56, 72.6, 27.28, 19.8, 84.0]
    gate = 280
    Lm, T, ph = 50, 44, 230
    ymax = 320.0

    def y(v):
        return T + ph - v / ymax * ph

    b = [t(20, 14, L(lang, "左:真實資料(LOCAL2 build 550)", "Left: real data (LOCAL2 build 550)"), 14, FG, "start", 600),
         t(20, 32, L(lang, "STRESS-03,stAgentSvc,每根柱 = 10 秒平均", "STRESS-03, stAgentSvc, bar = 10 s average"), 12)]
    for g in (0, 100, 200, gate):
        b.append(line(Lm, y(g), 390, y(g), WARN if g == gate else GRID, 2 if g == gate else 1, "7 5" if g == gate else ""))
        b.append(t(Lm - 6, y(g) + 4, str(g), 11, WARN if g == gate else MUT, "end"))
    bw = (390 - Lm) / len(real)
    for i, v in enumerate(real):
        b.append(rect(Lm + i * bw + 2, y(v), bw - 4, T + ph - y(v), FAIL if v >= gate else "rgba(0,199,230,0.55)", 2))
    b.append(t(Lm + 9 * bw + bw / 2, y(143.28) - 6, "143.28", 12, FG, "middle", 700))
    lft, _ = block([(L(lang, "沒有任何一根碰到門檻 280 → 連續高於門檻 = 0 個窗口", "No bar reaches the 280 gate, so 0 windows above it"), 13, FG, 400),
                    (L(lang, "→ worst_sustained_s = 0(最高的一根只有 143.28)", "worst_sustained_s = 0 (the highest bar is only 143.28)"), 13, PASS, 600)],
                   Lm - 20, T + ph + 14, 350)
    b.append(lft)
    b.append(t(430, 14, L(lang, "右:如果真的「持續」(示意)", "Right: a real sustained load"), 14, FG, "start", 600))
    b.append(t(430, 32, L(lang, "連續 6 根都 ≥ 門檻 280", "6 bars in a row, all at or above 280"), 12))
    for g in (0, 100, 200, gate):
        b.append(line(450, y(g), 760, y(g), WARN if g == gate else GRID, 2 if g == gate else 1, "7 5" if g == gate else ""))
    hyp = [60, 120, 300, 310, 290, 305, 315, 300, 90, 60]
    bw2 = (760 - 450) / len(hyp)
    for i, v in enumerate(hyp):
        b.append(rect(450 + i * bw2 + 2, y(v), bw2 - 4, T + ph - y(v), FAIL if v >= gate else "rgba(0,199,230,0.55)", 2))
    rgt, _ = block([(L(lang, "秒數 = 時間差 50 秒 + 1 個窗口 10 秒 = 60 秒", "seconds = 50 s span + one 10 s window = 60 s"), 13, FG, 400),
                    (L(lang, "→ 達到 60 秒 = 這個 run 判定失敗(只有 1 根高於門檻則是 10 秒)", "60 s reached: the run FAILS (a single bar above the line would read 10 s)"), 13, FAIL, 600)], 430, T + ph + 14, 340)
    b.append(rgt)
    return compose(780, [], "".join(b), 385, [])


def fig_memgrowth(lang):
    """REAL: LOCAL2 build 549 STRESS-03 stAgentSvc memory curve (subsampled). baseline 144.11, growth 43.04."""
    pts = [(0, 144.11), (100, 144.11), (201, 144.69), (604, 70.07), (706, 120.41), (1004, 127.25), (1503, 131.16), (2004, 133.34),
           (2106, 168.73), (2503, 174.93), (3004, 175.63), (3504, 176.82), (4006, 178.34), (4500, 181.11), (5001, 180.95),
           (5500, 183.36), (6004, 184.98), (6504, 184.94), (7003, 186.83), (7506, 186.87), (8000, 188.38), (8502, 177.33),
           (9007, 188.07), (9508, 189.83)]
    W, Lm, R, T, ph = 780, 56, 24, 8, 270
    xmax, ymin, ymax = 9600.0, 60.0, 200.0

    def X(v):
        return Lm + v / xmax * (W - Lm - R)

    def Y(v):
        return T + (ymax - v) / (ymax - ymin) * ph

    b = []
    for g in (80, 120, 160, 200):
        b.append(line(Lm, Y(g), W - R, Y(g)))
        b.append(t(Lm - 6, Y(g) + 4, str(g), 11, MUT, "end"))
    b.append(f'<polyline fill="none" stroke="{CY}" stroke-width="2" points="' + " ".join(f"{X(a):.1f},{Y(m):.1f}" for a, m in pts) + '"/>')
    b.append(line(Lm, Y(144.11), W - R, Y(144.11), WARN, 1.5, "6 4"))
    b.append(t(Lm + 4, Y(144.11) - 6, L(lang, "基準 144.11(最前面 3 個樣本的中位數)", "baseline 144.11 (median of the first 3 samples)"), 12, WARN))
    ry = 187.15
    rx0 = X(9600 * 2 / 3)
    b.append(line(rx0, Y(ry), W - R, Y(ry), PASS, 2, "6 4"))
    b.append(rect(rx0, T, W - R - rx0, ph, "rgba(16,185,129,0.07)", 0))
    b.append(t(rx0 + 4, Y(ry) - 22, L(lang, "最後三分之一", "last third"), 12, PASS))
    b.append(t(rx0 + 4, Y(ry) - 8, L(lang, "的平均 ≈ 187.15", "average ≈ 187.15"), 12, PASS))
    b.append(line(W - R - 6, Y(ry), W - R - 6, Y(144.11), FG, 2))
    b.append(t(W - R - 12, (Y(ry) + Y(144.11)) / 2 + 4, L(lang, "成長 +43.04 MB", "growth +43.04 MB"), 13, FG, "end", 700))
    b.append(t(X(706) + 8, Y(70.07) + 4, L(lang, "中途曾掉到約 70 MB(不影響基準)", "dipped to about 70 MB on the way (baseline unaffected)"), 12, MUT))
    b.append(t(X(0), T + ph + 18, L(lang, "0 秒", "0 s"), 11, MUT))
    b.append(t(X(9600) - 4, T + ph + 18, L(lang, "9600 秒", "9600 s"), 11, MUT, "end"))
    title = [(L(lang, "真實資料:LOCAL2 build 549,STRESS-03,stAgentSvc 的記憶體(MB),約 9500 秒",
                "Real data: LOCAL2 build 549, STRESS-03, stAgentSvc memory (MB), about 9500 s"), 14, FG, 600)]
    notes = [(L(lang, "mem_growth_mb = 近期平均 − 基準;超過 100 MB 判定記憶體洩漏。這個 run 是 +43.04,遠低於 100。",
                "mem_growth_mb = recent average - baseline; above 100 MB counts as a memory leak. This run is +43.04, far below 100."), 13, FG, 400)]
    return compose(W, title, "".join(b), ph + 26, notes)
