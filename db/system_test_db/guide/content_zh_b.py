"""Resource guide, zhTW, sections 4-7."""
from content_zh_a import fig
from guide_figs2 import fig_mem_mix, fig_trend_naive


def part_b():
    h = []
    h.append("""
<div class="card" id="s4">
  <h2>4. 「Resource peaks per process across versions」(CPU 跨版本圖)</h2>
  <div class="explain"><strong>已移除「各版本平均峰值(avg)」的趨勢線,</strong>因為它會隨測試組成而變,不能當成回歸訊號。目前保留的是「各版本 worst」圖,下面說明怎麼讀它,以及 avg 為什麼被拿掉。</div>
  <h3>現在畫的是什麼</h3>
  <ul>
    <li>X 軸是 client 主版本(R140 起,每個新版本多一個點),每個 process 一條線;分成 heavy(峰值 ≥ 30%)與 light(&lt; 30%)兩組,避免輕的 process 被壓成一條直線。Mac 另有自己的圖,從 R143 起。</li>
    <li>每個點是該版本<strong>單一最高的 CPU 讀數</strong>(worst)。它是單一個 10 秒平均,不是持續的證據,也不會讓 run 失敗。請當成「曾經到過這個高度」,不要當成趨勢。</li>
  </ul>
  <h3>為什麼 avg(各版本平均)被移除</h3>
  <ul>
    <li><strong>測試組成每版都不同:</strong>R140 跑 15 種、R142 跑 36 種、R143 跑 57 種、R144 只跑 21 種。壓力測試占比一高,平均自然升高。</li>
    <li><strong>每個點是 10 秒平均的最高值,</strong>跑得越久、重複越多次,越容易遇到一個高點。</li>
    <li><strong>單次 run 本來就很不穩定:</strong>同一個測試、同一個版本,STRESS-26 的峰值在 16 次 run 之間是 49 到 162。</li>
  </ul>
""")
    h.append(fig(fig_trend_naive("zh"), "<strong>圖 5(真實資料)</strong>:左邊是舊趨勢圖的算法,右邊只比較兩個版本都跑過的測試。143→144 左邊只升 3.7,右邊同樣 14 種測試升了 17:平均會誇大,也會掩蓋。"))
    h.append("""<div class="warnbox"><strong>注意:</strong>R142 同一批測試確實上升(24 種裡 17 種升超過 20%),R143 又降回來。資料看不出原因(可能是產品、VM 或取樣條件),這該另外查證,而不是靠趨勢圖開 ticket。</div>
</div>

<div class="card" id="s5">
  <h2>5. 「Memory per process across versions」(記憶體跨版本圖與表格)</h2>
  <table>
    <thead><tr><th>欄位</th><th>意思</th><th>怎麼讀</th></tr></thead>
    <tbody>
      <tr><td>Avg baseline MB</td><td>各次測試開始時的記憶體,取平均</td><td>受前一個測試留下的狀態影響(例如 STRESS-03 的基準是 144 或 192 MB,一般測試約 25–29 MB)</td></tr>
      <tr><td>Avg peak MB</td><td>各次測試最高記憶體,取平均</td><td>跟測試時間長短有關</td></tr>
      <tr><td>Worst peak MB</td><td>單次測試的最高記憶體</td><td>單一點;超過 500 MB 會判失敗</td></tr>
      <tr><td>Samples</td><td>參與平均的「測試 × run」列數</td><td>太少(例如 &lt; 5)不要下結論</td></tr>
      <tr><td>Tests</td><td>該版本跑過幾種不同的測試</td><td><strong>先看這欄:</strong>兩版的數字差很多,就不能直接比</td></tr>
      <tr><td>Runs</td><td>該版本的 run 數</td><td>重複次數</td></tr>
    </tbody>
  </table>
  <ul>
    <li><strong>Windows 是 Private Bytes,Mac 是 RSS</strong>,兩者不可互比,所以分開兩張圖。</li>
    <li><strong>各版本的「平均記憶體成長(Avg growth)」欄已移除:</strong>成長有正有負,平均會相抵,看不出洩漏。要看洩漏請看「Resource peaks per process」表的 worst_mem_growth。</li>
    <li><strong>「Large steps」註記:</strong>相鄰版本的平均基準上升 50% 以上且超過 10 MB 時,報表會自動寫一行,並用「兩版都跑過的測試」重算一次,避免把測試組成的改變當成產品變化。</li>
  </ul>
""")
    h.append(fig(fig_mem_mix("zh"), "<strong>圖 6(真實資料)</strong>:每版平均基準與測試數。R142 起翻倍,報表註記是 NPA 功能開啟;但同一版的測試組成也改變了,兩個原因分不開。"))
    h.append("""</div>

<div class="card" id="s6">
  <h2>6. 數字上升,要開 ticket 嗎?</h2>
  <div class="explain">先問三個問題,三個都「是」才值得開:<br>
    <strong>1. 測試組成一樣嗎?</strong>看 Tests 欄,或改看「同一個測試跨版本」。<br>
    <strong>2. 同一個測試本身也升了嗎?</strong>並且不是只有一次 run。<br>
    <strong>3. 有 gate 命中嗎?</strong>sustained、memory growth、handles 任一個。</div>
  <table>
    <thead><tr><th>情況</th><th>要開 ticket?</th><th>原因</th></tr></thead>
    <tbody>
      <tr><td>Gate 命中(sustained ≥ 60 秒、growth &gt; 100 MB、handles 不降回)</td><td><span class="badge fail">要</span></td><td>測試已經失敗,這是產品訊號</td></tr>
      <tr><td>同一個測試連續兩個版本都明顯上升</td><td><span class="badge warn">先查再決定</span></td><td>需排除 VM 與取樣條件</td></tr>
      <tr><td>記憶體 Avg baseline 上升,但 Tests 數量不同</td><td><span class="badge pass">不用</span></td><td>測試組成改變,不是同一批比較</td></tr>
      <tr><td>worst 圖出現一個高點(worst_cpu_peak)</td><td><span class="badge pass">不用</span></td><td>單一 10 秒平均,不代表持續</td></tr>
      <tr><td>worst_sustained_s = 10 或 20</td><td><span class="badge pass">不用</span></td><td>1 到 2 個窗口,距 60 秒的失敗線很遠</td></tr>
      <tr><td>worst_handles 接近 1500(例如 1400)</td><td><span class="badge warn">盯著</span></td><td>還沒失敗,留意趨勢</td></tr>
    </tbody>
  </table>
</div>

<div class="card" id="s7">
  <h2>7. 名詞</h2>
  <table>
    <thead><tr><th>名詞</th><th>意思</th></tr></thead>
    <tbody>
      <tr><td>取樣 / 窗口</td><td>每 10 秒讀一次;每個 CPU 值是「與上一次相比」的 10 秒平均</td></tr>
      <tr><td>每核 CPU</td><td>100 = 1 個核心,與 VM 核心數無關</td></tr>
      <tr><td>gate(閘門)</td><td>測試結束後檢查資源數字,超過門檻就判這個 run 失敗</td></tr>
      <tr><td>sustained</td><td>連續高於門檻的時間,預設 60 秒才失敗</td></tr>
      <tr><td>baseline / recent</td><td>記憶體成長的起點(前 3 個存活樣本中位數)/ 終點(最後三分之一平均)</td></tr>
      <tr><td>observe-only</td><td>只記錄、不判失敗的 process(demAgentSvc.exe 和 epais.exe;有 Shuttle/Keystone 監控改動時還有 shuttle-ctl.exe、sovin.exe,以及 Mac 的 keystone-auth、shuttle-launcher)</td></tr>
      <tr><td>Private Bytes / RSS</td><td>Windows / Mac 的記憶體度量,不可互比</td></tr>
    </tbody>
  </table>
  <p class="foot">資料來源:SystemTest 結果資料庫(2026-10-30 查詢,stAgentSvc.exe,Windows)。門檻與算法取自 health_gate.py 與 test_data.py。</p>
</div>
""")
    return "".join(h)
