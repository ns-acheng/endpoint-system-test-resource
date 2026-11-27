"""Resource guide, zhTW, sections 0-3."""
from guide_figs import fig_memgrowth, fig_sampling, fig_sustained
from guide_figs2 import fig_handles


def fig(svg_html, cap):
    return f'<div class="fig">{svg_html}<div class="cap">{cap}</div></div>'


def part_a():
    h = []
    h.append("""
<div class="card hero" id="top">
  <h1>資源監控解說</h1>
  <p>這一頁解釋報表裡「Resource peaks per process」「Resource peaks per process across versions」「Memory per process across versions」每個欄位與每張圖的意思、為什麼要看、哪些會讓測試失敗、哪些不能單獨下結論。所有圖都用報表資料庫的真實數字畫成(標明「示意」的除外)。</p>
  <div class="toc">
    <a href="#s0">30 秒版</a><a href="#s1">資料怎麼來</a><a href="#s2">逐欄解說</a><a href="#s3">三張圖解</a>
    <a href="#s4">跨版本 CPU</a><a href="#s5">跨版本記憶體</a><a href="#s6">要不要開 ticket</a><a href="#s7">名詞</a>
  </div>
</div>

<div class="card" id="s0">
  <h2>0. 先看這裡(30 秒版)</h2>
  <div class="explain">
    <strong>只有三個數字會讓測試失敗:</strong>
    <em>worst_sustained_s</em>(CPU 持續太久)、<em>worst_mem_growth</em>(記憶體一路漲)、<em>worst_handles</em>(handle 一路漲)。
    其他欄位(平均峰值、最高峰值、平均成長)都是「參考」,不會判定失敗。<br>
    <strong>跨版本的圖是把各版本所有測試放在一起算,</strong>每個版本跑的測試不一樣,所以線往上走不代表產品變差(第 4 節有真實例子)。
  </div>
  <table>
    <thead><tr><th>欄位</th><th>會讓 run 失敗?</th><th>一句話</th><th>重要性</th></tr></thead>
    <tbody>
      <tr><td>avg_cpu_peak</td><td><span class="badge info">否</span></td><td>各次 run 的最高 CPU 取平均</td><td>低</td></tr>
      <tr><td>worst_cpu_peak</td><td><span class="badge info">否</span></td><td>所有 run 裡單一最高的 CPU 讀數</td><td>中</td></tr>
      <tr><td>worst_sustained_s</td><td><span class="badge fail">是,≥ 60 秒</span></td><td>CPU 連續高於門檻的最長秒數</td><td>高</td></tr>
      <tr><td>cpu_gate</td><td>—</td><td>上面那個門檻(每核,100 = 1 核)</td><td>看門檻用</td></tr>
      <tr><td>avg_mem_growth</td><td><span class="badge info">否</span></td><td>各次 run 記憶體成長的平均</td><td>低</td></tr>
      <tr><td>worst_mem_growth</td><td><span class="badge fail">是,&gt; 100 MB</span></td><td>單次 run 最大的記憶體成長</td><td>高</td></tr>
      <tr><td>worst_handles</td><td><span class="badge fail">是,≥ 1500 且結束時仍在</span></td><td>任一取樣點的最大 handle 數</td><td>高</td></tr>
    </tbody>
  </table>
  <p class="foot">例外:<code>demAgentSvc.exe</code>(Windows DEM agent)和 <code>epais.exe</code>(EPAIS AI 安全服務)目前是 observe-only,數字會記錄、會出現在表上,但不會判定失敗;只有在 client 設定開啟 EPAIS 而 <code>epais.exe</code> 沒在跑時,run 才會失敗。存活時間不到 50% 的 process 也不套用閘門(被故意殺掉重啟的情況)。</p>
</div>

<div class="card" id="s1">
  <h2>1. 資料怎麼來</h2>
  <ul>
    <li><strong>取樣器</strong>在測試期間每 <strong>10 秒</strong>讀一次每個被監控的 process(少數測試改成 5 秒)。Windows 監控 <code>stAgentSvc.exe</code>、<code>stAgentSvcMon.exe</code>、<code>stAgentUI.exe</code>,另外記錄 <code>demAgentSvc.exe</code> 和 <code>epais.exe</code>,並記下每個 process 的 thread 數;Mac 監控 <code>nsAuxiliarySvc</code>、<code>Netskope Client</code>、<code>NetskopeClientMacAppProxy</code> 等。</li>
    <li><strong>每次測試(一個 test case 的一次 run)</strong>把這些樣本濃縮成一列摘要:最高 CPU、持續秒數、記憶體基準與成長、最大 handles。報表的表格是把這些摘要再依 process 彙總。</li>
    <li><strong>CPU 單位 = 每核</strong>:100 代表整整用掉 1 個核心,190 代表約 1.9 個核心。跟 VM 有幾個核心無關。</li>
    <li><strong>記憶體單位 = MB</strong>:Windows 是 Private Bytes,Mac 是 RSS,兩者<strong>不能互相比較</strong>。</li>
  </ul>
""")
    h.append(fig(fig_sampling("zh"), "<strong>圖 1</strong>:每個 CPU 數字都需要「上一次取樣」才算得出來,所以第一個樣本沒有 CPU 值,重開機或 process 重啟後也會重算。"))
    h.append("</div>")
    h.append("""
<div class="card" id="s2">
  <h2>2. 「Resource peaks per process」逐欄解說</h2>
  <h3>avg_cpu_peak — 平均的最高 CPU</h3>
  <ul><li><strong>意思:</strong>每次 run 先取該 process 的最高 CPU,再把所有 run 取平均。</li>
  <li><strong>為什麼看:</strong>當作「一般情況下 CPU 大概多忙」的粗略基準。</li>
  <li><strong>怎麼讀:</strong>它是「最高值的平均」,測試種類(壓力測試 vs 閒置測試)占比一變它就跟著變。<strong>只適合同一批測試之間比較。</strong></li></ul>
  <h3>worst_cpu_peak — 最高的 CPU 讀數</h3>
  <ul><li><strong>意思:</strong>所有 run、所有取樣點裡最大的那一個 10 秒平均值。</li>
  <li><strong>怎麼讀:</strong>它是單一個點,說明「曾經到過這個高度」,不說明「持續」。不會讓 run 失敗。用途是看離門檻還有多遠,例如 254.8 對門檻 280。</li></ul>
  <h3>worst_sustained_s — 持續高於門檻的最長秒數</h3>
  <ul><li><strong>意思:</strong>某次 run 裡,CPU 連續高於門檻的最長時間(見第 3 節圖 2)。</li>
  <li><strong>為什麼看:</strong>抓失控的迴圈或卡死空轉。短暫尖峰在重連、載入設定時都是正常的,只有「持續」才是問題。</li>
  <li><strong>怎麼讀:</strong>0 = 沒有任何一個樣本到門檻;10 = 恰好 1 個樣本到門檻;<strong>達 60 秒才失敗</strong>。幾乎都是 0,因為所有 process 的峰值都遠低於 280(歷史最高 261.2)。</li></ul>
  <h3>cpu_gate — 門檻</h3>
  <ul><li><strong>意思:</strong>判定「持續」所用的 CPU 值(每核),<strong>所有測試統一是 280</strong>(2.8 個核心),要連續 60 秒才失敗。圖上的黃色虛線就是這個門檻。</li>
  <li><div class="warnbox"><strong>注意:</strong>峰值高過黃色虛線不等於失敗,要連續 60 秒高於才失敗;<code>demAgentSvc.exe</code> 是 observe-only,有量測但不會失敗。較早的 run 當時各測試用過 140 或 240,報表統一用 280 重新計算 <em>worst_sustained_s</em>:最高讀數沒到 280 的 run,高於門檻的時間就是 0。</div></li></ul>
  <h3>avg_mem_growth / worst_mem_growth — 記憶體成長</h3>
  <ul><li><strong>意思:</strong>成長 = 「近期平均」減「基準」(見第 3 節圖 3)。負數 = 縮小。</li>
  <li><strong>為什麼看:</strong>記憶體洩漏的訊號。<em>worst</em> 超過 100 MB 判失敗。</li>
  <li><strong>怎麼讀:</strong><em>avg</em> 會正負相抵,洩漏被平均掉,參考價值低;<em>worst</em> 才是有用的那個。成長跟 run 的長度有關,跑得越久越容易看到成長。</li></ul>
  <h3>worst_handles — 最大 handle 數</h3>
  <ul><li><strong>意思:</strong>任一取樣點的 handle 數最大值。</li>
  <li><strong>為什麼看:</strong>handle 洩漏最終會讓 process 耗盡資源。</li>
  <li><strong>怎麼讀:</strong>到 1500 而且<strong>最後一個樣本仍在 1500 以上</strong>才失敗(圖 4)。目前 <code>stAgentSvc.exe</code> 是 1400,離線 100,值得留意。</li></ul>
</div>
""")
    h.append('<div class="card" id="s3"><h2>3. 三張圖解</h2>')
    h.append(fig(fig_sustained("zh"), "<strong>圖 2:怎麼知道「持續」?</strong>每根柱是一個 10 秒窗口,黃色虛線是門檻 280。左圖是真實 run:最高的一根只有 143.28,沒有任何一根碰到門檻,所以 worst_sustained_s = 0。如果剛好有 1 根高於門檻,值會是 10 秒:秒數 = (最後一根時間 − 第一根時間)+10,1 根就是 0+10;連續 6 根 = 50+10 = 60 秒,達到失敗線(右圖,示意)。注意它量的是 10 秒平均,短於 10 秒的尖峰會被攤平。"))
    h.append(fig(fig_memgrowth("zh"), "<strong>圖 3:記憶體成長怎麼算</strong>(真實 run)。基準 = 前 3 個存活樣本的中位數,近期 = 最後三分之一的平均,成長 = 近期 − 基準。每次開機算一段(重開機後記憶體歸零,跨開機比較沒有意義),取成長最大的那一段。"))
    h.append("""<div class="explain"><strong>剛安裝完的 client 呢?</strong>程式沒有暖機豁免:基準永遠取該次測試最前面 3 個存活樣本。一般測試開始時 client 已經在跑,基準是穩定值(例如 STRESS-01、STRESS-08、STEER-06 的基準是 25–29 MB,成長只有 5–8 MB);暖機增幅遠小於 100 MB 的門檻,不會誤判。
    若要「等 process 準備好再取基準」,需要先有 thread 數資料,這個工作已列入待辦。</div>""")
    h.append(fig(fig_handles("zh"), "<strong>圖 4:handles 規則</strong>。啟動瞬間衝高、之後回落不算洩漏(STEER-07 真實情況:3304 → 790 → 約 920);只有一路漲過天花板、結束時還在上面才失敗。"))
    h.append("</div>")
    return "".join(h)
