"""Resource guide, English, sections 4-7."""
from content_zh_a import fig
from guide_figs2 import fig_mem_mix, fig_trend_naive


def part_b():
    h = []
    h.append("""
<div class="card" id="s4">
  <h2>4. "Resource peaks per process across versions" (CPU across versions)</h2>
  <div class="explain"><strong>The per-version average (avg) trend line was removed</strong> because it moves with the test mix and cannot be read as a regression signal. What remains is the per-version "worst" chart; below is how to read it and why avg was dropped.</div>
  <h3>What is drawn now</h3>
  <ul>
    <li>X axis = client major version (from R140, one more point per release), one line per process; split into heavy (peak &ge; 30%) and light (&lt; 30%) so light processes are not flattened. Mac has its own chart, from R143.</li>
    <li>Each point is the <strong>single highest CPU reading</strong> of that version (worst). It is one 10 s average, no proof of a sustained load, and never fails a run. Read it as "has been this high", not as a trend.</li>
  </ul>
  <h3>Why avg (the per-version average) was removed</h3>
  <ul>
    <li><strong>The test mix differs per version:</strong> R140 ran 15 test kinds, R142 36, R143 57, R144 only 21. A higher share of stress tests lifts the average by itself.</li>
    <li><strong>Each point is the highest of a 10 s average:</strong> the longer and more often a test runs, the more likely one high point appears.</li>
    <li><strong>One run is very unstable:</strong> the same test on the same version, STRESS-26 peaks between 49 and 162 over 16 runs.</li>
  </ul>
""")
    h.append(fig(fig_trend_naive("en"), "<strong>Figure 5 (real data)</strong>: left is how the old trend chart averaged, right compares only the tests both versions ran. For 143→144 the left rises 3.7, the same 14 tests rise 17: an average can exaggerate and it can hide."))
    h.append("""<div class="warnbox"><strong>Note:</strong> on R142 the same tests really did go up (17 of 24 by more than 20%) and came back down on R143. The data does not show why (product, VM or sampling conditions); that needs its own investigation, not a ticket opened from a trend line.</div>
</div>

<div class="card" id="s5">
  <h2>5. "Memory per process across versions" (charts and table)</h2>
  <table>
    <thead><tr><th>Column</th><th>Meaning</th><th>How to read</th></tr></thead>
    <tbody>
      <tr><td>Avg baseline MB</td><td>Memory at the start of each test, averaged</td><td>Depends on what the previous test left behind (STRESS-03 starts at 144 or 192 MB, ordinary tests at about 25-29 MB)</td></tr>
      <tr><td>Avg peak MB</td><td>Highest memory of each test, averaged</td><td>Depends on how long the test runs</td></tr>
      <tr><td>Worst peak MB</td><td>Highest memory of one test</td><td>A single point; above 500 MB fails the run</td></tr>
      <tr><td>Samples</td><td>Number of "test x run" rows in the average</td><td>Too few (for example &lt; 5): draw no conclusion</td></tr>
      <tr><td>Tests</td><td>How many different tests ran on that version</td><td><strong>Read this first:</strong> if the two versions differ a lot, do not compare the numbers directly</td></tr>
      <tr><td>Runs</td><td>Number of runs on that version</td><td>Repetitions</td></tr>
    </tbody>
  </table>
  <ul>
    <li><strong>Private Bytes on Windows, RSS on Mac:</strong> not comparable, hence two separate charts.</li>
    <li><strong>The per-version "Avg growth" column was removed:</strong> growth is positive for some runs and negative for others, so the average cancels out and shows no leak. For leaks, read worst_mem_growth in the "Resource peaks per process" table.</li>
    <li><strong>"Large steps" notes:</strong> when the average baseline of a process rises by 50% or more and by more than 10 MB between neighbouring versions, the report writes a line and recomputes it on the tests both versions ran, so a change of test mix cannot pass for a product change.</li>
  </ul>
""")
    h.append(fig(fig_mem_mix("en"), "<strong>Figure 6 (real data)</strong>: average baseline and number of test kinds per version. It doubles from R142; the report notes NPA was enabled, but the test mix changed in the same version, and the two causes cannot be separated."))
    h.append("""</div>

<div class="card" id="s6">
  <h2>6. A number went up: open a ticket?</h2>
  <div class="explain">Ask three questions; open a ticket only if all three answers are "yes":<br>
    <strong>1. Is the test mix the same?</strong> Read the Tests column, or compare the same test across versions.<br>
    <strong>2. Did the same test go up too?</strong> And in more than one run.<br>
    <strong>3. Did a gate trigger?</strong> sustained, memory growth or handles.</div>
  <table>
    <thead><tr><th>Situation</th><th>Open a ticket?</th><th>Why</th></tr></thead>
    <tbody>
      <tr><td>A gate triggered (sustained &ge; 60 s, growth &gt; 100 MB, handles not coming back)</td><td><span class="badge fail">Yes</span></td><td>The test already failed; this is a product signal</td></tr>
      <tr><td>The same test is clearly higher on two versions in a row</td><td><span class="badge warn">Investigate first</span></td><td>VM and sampling conditions must be ruled out</td></tr>
      <tr><td>Memory Avg baseline went up but the Tests count differs</td><td><span class="badge pass">No</span></td><td>The test mix changed; not the same set compared</td></tr>
      <tr><td>One high point in the worst chart (worst_cpu_peak)</td><td><span class="badge pass">No</span></td><td>One 10 s average, not a sustained load</td></tr>
      <tr><td>worst_sustained_s = 10 or 20</td><td><span class="badge pass">No</span></td><td>One or two windows, far from the 60 s fail line</td></tr>
      <tr><td>worst_handles close to 1500 (for example 1400)</td><td><span class="badge warn">Watch</span></td><td>Not failing yet; keep an eye on the trend</td></tr>
    </tbody>
  </table>
</div>

<div class="card" id="s7">
  <h2>7. Glossary</h2>
  <table>
    <thead><tr><th>Term</th><th>Meaning</th></tr></thead>
    <tbody>
      <tr><td>sample / window</td><td>Read every 10 s; each CPU value is the 10 s average compared with the previous read</td></tr>
      <tr><td>per-core CPU</td><td>100 = one core, independent of the VM core count</td></tr>
      <tr><td>gate</td><td>The check of the resource numbers after a test; above a threshold the run fails</td></tr>
      <tr><td>sustained</td><td>Time continuously above the threshold; 60 s by default fails the run</td></tr>
      <tr><td>baseline / recent</td><td>Start of memory growth (median of the first 3 living samples) / end (average of the last third)</td></tr>
      <tr><td>observe-only</td><td>A process that is recorded but never fails a run (demAgentSvc.exe and epais.exe; with the Shuttle/Keystone monitor change also shuttle-ctl.exe, sovin.exe and, on Mac, keystone-auth and shuttle-launcher)</td></tr>
      <tr><td>Private Bytes / RSS</td><td>Memory metric on Windows / Mac; not comparable</td></tr>
    </tbody>
  </table>
  <p class="foot">Source: SystemTest results database (queried 2026-10-30, stAgentSvc.exe, Windows). Thresholds and formulas come from health_gate.py and test_data.py.</p>
</div>
""")
    return "".join(h)
