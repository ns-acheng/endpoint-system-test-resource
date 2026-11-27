"""Shared CSS for the resource guide page: same palette and components as report.py (card, h2, table, cpu-explain, badge)."""

CSS = """
:root{--bg:#05081a;--card:#111827;--card2:#1a2235;--border:rgba(255,255,255,0.08);--border2:rgba(0,199,230,0.2);
--fg:#f0f4ff;--muted:#94a3b8;--cyan:#00c7e6;--grad:linear-gradient(135deg,#0052cc 0%,#00c7e6 100%);
--pass:#10b981;--fail:#ef4444;--warn:#f59e0b;--pass-bg:rgba(16,185,129,0.12);--fail-bg:rgba(239,68,68,0.12);
--warn-bg:rgba(245,158,11,0.12);--pass-border:rgba(16,185,129,0.3);--fail-border:rgba(239,68,68,0.3);
--warn-border:rgba(245,158,11,0.3);--radius:12px;--radius-sm:6px;--shadow:0 4px 24px rgba(0,0,0,0.4)}
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:'Inter','Microsoft JhengHei','Noto Sans TC','Helvetica Neue',sans-serif;background:var(--bg);color:var(--fg);
font-size:16px;line-height:1.7}
.topbar{background:rgba(5,8,26,0.95);border-bottom:1px solid var(--border);padding:0 32px;height:56px;display:flex;
align-items:center;gap:12px;position:sticky;top:0;z-index:100}
.topbar .logo{font-size:17px;font-weight:700;background:var(--grad);-webkit-background-clip:text;-webkit-text-fill-color:transparent}
.topbar .tag{font-size:13px;padding:2px 10px;border-radius:999px;background:rgba(0,199,230,0.1);border:1px solid var(--border2);
color:var(--cyan);font-weight:600}
.topbar a{font-size:13px;padding:3px 11px;border-radius:999px;border:1px solid var(--border);color:var(--muted);text-decoration:none;font-weight:600}
.topbar a:hover{color:var(--cyan);border-color:var(--border2)}
.page{max-width:1280px;margin:0 auto;padding:24px 28px 48px}
.card{background:var(--card);border:1px solid var(--border);border-radius:var(--radius);padding:20px 24px;margin-bottom:20px;box-shadow:var(--shadow)}
.card.hero{background:linear-gradient(135deg,#0a1628 0%,#0d1f3c 100%);border-color:var(--border2)}
h1{font-size:28px;font-weight:700}
h2{font-size:17px;font-weight:600;letter-spacing:.8px;color:var(--cyan);margin-bottom:14px;padding-bottom:8px;border-bottom:1px solid var(--border)}
h3{font-size:16px;font-weight:600;color:var(--fg);margin:14px 0 8px}
p{margin:8px 0;color:var(--muted)}
p strong,li strong{color:var(--fg)}
ul{margin:8px 0 8px 22px;color:var(--muted)}
li{margin:4px 0}
code,td.mono{font-family:'JetBrains Mono',Consolas,monospace;color:var(--cyan);font-size:14px}
.badge{padding:3px 12px;border-radius:999px;font-weight:600;font-size:13px;display:inline-block}
.badge.pass{background:var(--pass-bg);color:var(--pass);border:1px solid var(--pass-border)}
.badge.fail{background:var(--fail-bg);color:var(--fail);border:1px solid var(--fail-border)}
.badge.warn{background:var(--warn-bg);color:var(--warn);border:1px solid var(--warn-border)}
.badge.info{background:rgba(0,199,230,0.1);color:var(--cyan);border:1px solid var(--border2)}
table{width:100%;border-collapse:collapse;font-size:15px;margin:10px 0}
th{text-align:left;padding:9px 14px;color:var(--muted);font-size:13px;letter-spacing:.5px;font-weight:600;
border-bottom:1px solid var(--border);background:rgba(255,255,255,0.03);white-space:nowrap}
td{padding:8px 14px;border-bottom:1px solid rgba(255,255,255,0.04);vertical-align:top;color:var(--muted)}
td:first-child{color:var(--fg);font-family:'JetBrains Mono',Consolas,monospace;white-space:nowrap}
tbody tr:hover{background:rgba(255,255,255,0.03)}
.explain{background:rgba(0,199,230,0.05);border:1px solid var(--border2);border-radius:var(--radius-sm);padding:12px 16px;
font-size:15px;margin:12px 0;color:var(--muted);line-height:1.75}
.explain strong{color:var(--fg)}
.explain em{color:var(--cyan);font-style:normal}
.warnbox{background:var(--warn-bg);border:1px solid var(--warn-border);border-radius:var(--radius-sm);padding:12px 16px;margin:12px 0;color:var(--muted)}
.warnbox strong{color:var(--warn)}
.okbox{background:var(--pass-bg);border:1px solid var(--pass-border);border-radius:var(--radius-sm);padding:12px 16px;margin:12px 0;color:var(--muted)}
.okbox strong{color:var(--pass)}
.fig{background:var(--card2);border:1px solid var(--border);border-radius:var(--radius);padding:14px 16px;margin:14px 0}
.fig svg{width:100%;height:auto;display:block}
.fig .cap{font-size:14px;color:var(--muted);margin-top:8px}
.fig .cap strong{color:var(--fg)}
.row2{display:grid;grid-template-columns:1fr 1fr;gap:16px}
@media(max-width:900px){.row2{grid-template-columns:1fr}}
.toc{display:flex;flex-wrap:wrap;gap:8px;margin-top:10px}
.toc a{font-size:13px;padding:3px 11px;border-radius:999px;border:1px solid var(--border2);color:var(--cyan);text-decoration:none;font-weight:600}
.foot{color:var(--muted);font-size:14px;margin-top:8px}
.topbar .push{margin-left:auto}
.topbar a.cur{color:var(--cyan);border-color:var(--border2)}
"""
