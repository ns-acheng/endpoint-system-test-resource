"""Handle, trend and proposal figures (zhTW and EN). REAL numbers: results DB, stAgentSvc.exe, windows runs, queried 2026-10-30."""
from guide_figs import CY, FAIL, FG, GRID, MUT, PASS, WARN, L, compose, line, rect, t


def fig_handles(lang):
    pts = [(0, 3304), (1, 790), (2, 820), (3, 880), (4, 910), (5, 920), (6, 930), (7, 925)]
    Lm, T, ph = 56, 10, 230
    ymax = 3600.0

    def X(i):
        return Lm + i / 7.0 * (400 - Lm)

    def Y(v):
        return T + (ymax - v) / ymax * ph

    b = []
    for g in (0, 1500, 3000):
        b.append(line(Lm, Y(g), 760, Y(g), WARN if g == 1500 else GRID, 1.5 if g == 1500 else 1, "5 4" if g == 1500 else ""))
        b.append(t(Lm - 6, Y(g) + 4, str(g), 11, WARN if g == 1500 else MUT, "end"))
    b.append(f'<polyline fill="none" stroke="{PASS}" stroke-width="2.5" points="' + " ".join(f"{X(i):.1f},{Y(v):.1f}" for i, v in pts) + '"/>')
    b.append(t(X(0) + 8, Y(3304) + 4, L(lang, "啟動瞬間 3304", "3304 at start"), 12, PASS))
    b.append(t(X(1) + 6, Y(790) + 18, L(lang, "10 秒後 790,兩小時後約 920", "790 after 10 s, about 920 after 2 h"), 12, PASS))
    leak = [(0, 700), (1, 900), (2, 1100), (3, 1300), (4, 1500), (5, 1650), (6, 1700), (7, 1780)]
    ox = 430
    b.append(f'<polyline fill="none" stroke="{FAIL}" stroke-width="2.5" points="' + " ".join(f"{ox + i * 46:.1f},{Y(v):.1f}" for i, v in leak) + '"/>')
    b.append(t(ox, Y(1780) - 12, L(lang, "右(示意):漲過 1500 且停在上面 → 失敗", "Right (illustration): stays above 1500: FAIL"), 13, FAIL, "start", 600))
    title = [(L(lang, "handles 規則:只有「到了天花板而且最後還在天花板」才算洩漏",
                "Handles rule: a leak only if the count reached the ceiling and is still there at the end"), 14, FG, 600)]
    notes = [(L(lang, "左:STEER-07 build 74 的真實情況(程式註解記載)→ 峰值回落,不算洩漏,通過",
                "Left: the real STEER-07 build 74 case (from the code comment): the peak fell back, not a leak, passes"), 13, PASS, 600),
             (L(lang, "門檻 1500;目前 stAgentSvc.exe 的 worst_handles = 1400,離門檻只差 100",
                "Ceiling 1500; stAgentSvc.exe worst_handles is 1400 today, 100 below the ceiling"), 13, FG, 400)]
    return compose(780, title, "".join(b), ph + 20, notes)


def fig_trend_naive(lang):
    """REAL: naive per-version average of per-run CPU peak vs the same tests in both versions."""
    ver = [(140, 38.2, 15), (141, 28.9, 24), (142, 65.5, 36), (143, 53.8, 57), (144, 57.5, 21)]
    pairs = [("140→141", 42.6, 33.2, 15), ("141→142", 27.3, 61.6, 24), ("142→143", 49.3, 43.0, 33), ("143→144", 69.2, 86.2, 14)]
    ymax, T, base = 100.0, 40, 250
    kinds = L(lang, "{n} 種測試", "{n} tests")

    def Y(v):
        return base - v / ymax * (base - T)

    b = [t(30, 14, L(lang, "A. 舊趨勢圖:每版所有測試的平均峰值", "A. Old chart: average peak, all tests"), 13, CY, "start", 600)]
    for g in (0, 50, 100):
        b.append(line(40, Y(g), 380, Y(g)))
        b.append(t(34, Y(g) + 4, str(g), 11, MUT, "end"))
    bw = 340 / 5
    for i, (v, a, n) in enumerate(ver):
        x = 40 + i * bw
        b.append(rect(x + 12, Y(a), bw - 24, base - Y(a), "rgba(0,199,230,0.6)"))
        b.append(t(x + bw / 2, Y(a) - 6, f"{a:g}", 12, FG, "middle", 700))
        b.append(t(x + bw / 2, base + 16, f"R{v}", 12, FG, "middle"))
        b.append(t(x + bw / 2, base + 32, kinds.format(n=n), 11, WARN, "middle"))
    b.append(t(410, 14, L(lang, "B. 同樣的測試:只算兩版都跑過的", "B. Like for like: tests both versions ran"), 13, CY, "start", 600))
    for g in (0, 50, 100):
        b.append(line(420, Y(g), 760, Y(g)))
    bw2 = 340 / 4
    for i, (lab, a, c, n) in enumerate(pairs):
        x = 420 + i * bw2
        b.append(rect(x + 8, Y(a), 30, base - Y(a), "rgba(148,163,184,0.7)"))
        b.append(rect(x + 42, Y(c), 30, base - Y(c), FAIL if c > a * 1.2 else (PASS if c < a / 1.2 else "rgba(0,199,230,0.6)")))
        b.append(t(x + 23, Y(a) - 5, f"{a:g}", 11, FG, "middle"))
        b.append(t(x + 57, Y(c) - 5, f"{c:g}", 11, FG, "middle", 700))
        b.append(t(x + 40, base + 16, lab, 12, FG, "middle"))
        b.append(t(x + 40, base + 32, kinds.format(n=n), 11, WARN, "middle"))
    title = [(L(lang, "同一份資料的兩種算法:舊的趨勢圖 vs 只比較「兩個版本都跑過的測試」(stAgentSvc)",
                "The same data two ways: the old trend chart vs only the tests both versions ran (stAgentSvc)"), 14, FG, 600)]
    notes = [(L(lang, "左:每版跑的測試數 15 → 24 → 36 → 57 → 21,組成一直在變。右:灰 = 前一版,彩色 = 後一版(紅 = 上升超過 20%,綠 = 下降超過 20%)。",
                "Left: test kinds per version 15 → 24 → 36 → 57 → 21, the mix keeps changing. Right: grey = earlier version, colour = later version (red = up more than 20%, green = down more than 20%)."), 12, MUT, 400),
             (L(lang, "143→144:左邊只升 3.7(53.8→57.5);右邊同樣的 14 種測試卻升了 17(69.2→86.2)。",
                "143→144: the left chart rises only 3.7 (53.8→57.5); the same 14 tests rise 17 (69.2→86.2)."), 13, FG, 400),
             (L(lang, "141→142:左邊升 36.6;右邊 24 種測試確實升了 34.3,而 143 又降回來。左邊的升降,有一部分是「跑了什麼測試」造成的。",
                "141→142: the left rises 36.6; the 24 common tests did rise 34.3, and 143 came back down. Part of what the left chart shows is which tests ran."), 13, FG, 400)]
    return compose(780, title, "".join(b), base + 44, notes)


def fig_mem_mix(lang):
    ver = [(140, 24.1, 15), (141, 22.2, 24), (142, 52.0, 36), (143, 49.8, 57), (144, 59.2, 21)]
    base, T = 170, 20

    def Y(v):
        return base - v / 70.0 * (base - T)

    b = []
    for g in (0, 35, 70):
        b.append(line(60, Y(g), 740, Y(g)))
        b.append(t(54, Y(g) + 4, str(g), 11, MUT, "end"))
    bw = 680 / 5
    for i, (v, a, n) in enumerate(ver):
        x = 60 + i * bw
        b.append(rect(x + 30, Y(a), bw - 60, base - Y(a), "rgba(0,199,230,0.6)"))
        b.append(t(x + bw / 2, Y(a) - 6, f"{a:g}", 12, FG, "middle", 700))
        b.append(t(x + bw / 2, base + 16, f"R{v}", 12, FG, "middle"))
        b.append(t(x + bw / 2, base + 32, L(lang, f"{n} 種測試", f"{n} tests"), 11, WARN, "middle"))
    title = [(L(lang, "stAgentSvc 每版的平均「記憶體基準」(MB)與跑過的測試數", "stAgentSvc average memory baseline (MB) per version, and the number of tests that ran"), 14, FG, 600)]
    notes = [(L(lang, "R142 起翻倍。報表已註記「R142 開啟 NPA 功能,記憶體較高是預期」;但測試組成同時也改變,兩個原因分不開。",
                "It doubles from R142. The report notes that NPA was enabled in R142, so higher memory is expected; the test mix changed in the same version, so the two causes cannot be separated."), 12, MUT, 400)]
    return compose(780, title, "".join(b), base + 40, notes)


def fig_proposal(lang):
    """REAL: proposed view kept for the board task. Fixed-workload tests, min / avg / max of per-run peak per version (stAgentSvc)."""
    data = {
        "STRESS-01": [(141, 0.2, 85.8, 183.8, 40), (142, 0.0, 162.3, 232.7, 120), (143, 1.2, 129.8, 180.2, 34), (144, 128.0, 150.1, 167.3, 6)],
        "STRESS-02": [(141, 1.2, 143.3, 243.7, 18), (142, 2.5, 192.3, 254.7, 73), (143, 158.6, 222.0, 254.8, 20), (144, 237.2, 237.2, 237.2, 1)],
        "STRESS-05": [(141, 7.0, 40.1, 48.8, 9), (142, 0.0, 91.4, 167.2, 112), (143, 35.2, 69.2, 132.4, 21), (144, 36.2, 70.1, 104.1, 2)],
        "STRESS-26": [(141, 1.9, 35.0, 103.3, 47), (142, 0.0, 56.9, 86.4, 6), (143, 47.6, 89.2, 161.7, 19), (144, 187.9, 187.9, 187.9, 1)],
    }
    b = []
    for k, (name, rows) in enumerate(data.items()):
        ox, oy = 30 + (k % 2) * 380, 14 + (k // 2) * 230
        b.append(t(ox, oy, name, 13, CY, "start", 600))
        top, bot = oy + 14, oy + 170

        def Y(v):
            return bot - v / 280.0 * (bot - top)

        for g in (0, 140, 280):
            b.append(line(ox + 30, Y(g), ox + 340, Y(g), WARN if g == 280 else GRID, 1.5 if g == 280 else 1, "6 4" if g == 280 else ""))
            b.append(t(ox + 24, Y(g) + 4, str(g), 10, WARN if g == 280 else MUT, "end"))
        for i, (v, lo, av, hi, n) in enumerate(rows):
            cx = ox + 30 + (i + 0.5) * 310 / 4
            b.append(line(cx, Y(hi), cx, Y(lo), MUT, 2))
            b.append(rect(cx - 8, Y(hi) - 1, 16, 2, MUT, 0))
            b.append(rect(cx - 8, Y(lo) - 1, 16, 2, MUT, 0))
            b.append(f'<circle cx="{cx:.1f}" cy="{Y(av):.1f}" r="5" fill="{CY}"/>')
            b.append(t(cx + 9, Y(av) + 4, f"{av:g}", 11, FG))
            b.append(t(cx, bot + 16, f"R{v}", 11, FG, "middle"))
            b.append(t(cx, bot + 30, f"n={n}", 10, WARN if n < 5 else MUT, "middle"))
    title = [(L(lang, "提案圖(草稿):同一個測試、跨版本,畫出每次 run 的 最低 / 平均 / 最高 峰值",
                "Proposal (draft): one test across versions, lowest / average / highest per-run peak"), 14, FG, 600)]
    notes = [(L(lang, "線段 = 該版本所有 run 的最低到最高;圓點 = 平均;n = run 次數(< 5 標橘色,不要下結論);虛線 = 預設門檻 140。",
                "Bar = lowest to highest run of that version; dot = average; n = runs (under 5 in orange, draw no conclusion); dashed yellow line = the gate, 280."), 12, MUT, 400)]
    return compose(780, title, "".join(b), 470, notes)
