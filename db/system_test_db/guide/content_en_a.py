"""Resource guide, English, sections 0-3."""
from content_zh_a import fig
from guide_figs import fig_memgrowth, fig_sampling, fig_sustained
from guide_figs2 import fig_handles


def part_a():
    h = []
    h.append("""
<div class="card hero" id="top">
  <h1>Resource monitoring guide</h1>
  <p>What every column and every chart in "Resource peaks per process", "Resource peaks per process across versions" and "Memory per process across versions" means, why it is monitored, which numbers fail a test, and which ones must not be read as a conclusion on their own. All charts are drawn from real numbers in the report database unless marked "illustration".</p>
  <div class="toc">
    <a href="#s0">30-second version</a><a href="#s1">Where the data comes from</a><a href="#s2">Column by column</a><a href="#s3">Three diagrams</a>
    <a href="#s4">CPU across versions</a><a href="#s5">Memory across versions</a><a href="#s6">Ticket or not</a><a href="#s7">Glossary</a>
  </div>
</div>

<div class="card" id="s0">
  <h2>0. Start here (30 seconds)</h2>
  <div class="explain">
    <strong>Only three numbers fail a test:</strong>
    <em>worst_sustained_s</em> (CPU high for too long), <em>worst_mem_growth</em> (memory keeps growing), <em>worst_handles</em> (handles keep growing).
    The other columns (average peak, highest peak, average growth) are for reference and never fail a run.<br>
    <strong>Charts across versions pool all tests of a version.</strong> Each version runs a different set of tests, so a rising line does not mean the product got worse (real example in section 4).
  </div>
  <table>
    <thead><tr><th>Column</th><th>Fails the run?</th><th>In one line</th><th>Importance</th></tr></thead>
    <tbody>
      <tr><td>avg_cpu_peak</td><td><span class="badge info">No</span></td><td>The highest CPU of each run, averaged</td><td>Low</td></tr>
      <tr><td>worst_cpu_peak</td><td><span class="badge info">No</span></td><td>The single highest CPU reading of all runs</td><td>Medium</td></tr>
      <tr><td>worst_sustained_s</td><td><span class="badge fail">Yes, at 60 s</span></td><td>Longest time CPU stayed above the threshold</td><td>High</td></tr>
      <tr><td>cpu_gate</td><td>-</td><td>That threshold (per core, 100 = 1 core)</td><td>Context</td></tr>
      <tr><td>avg_mem_growth</td><td><span class="badge info">No</span></td><td>Memory growth of each run, averaged</td><td>Low</td></tr>
      <tr><td>worst_mem_growth</td><td><span class="badge fail">Yes, &gt; 100 MB</span></td><td>Largest memory growth of one run</td><td>High</td></tr>
      <tr><td>worst_handles</td><td><span class="badge fail">Yes, &ge; 1500 and still there at the end</span></td><td>Highest handle count at any sample</td><td>High</td></tr>
    </tbody>
  </table>
  <p class="foot">Exception: <code>demAgentSvc.exe</code> (the Windows DEM agent) and <code>epais.exe</code> (the EPAIS AI-security service) are observe-only for now: their numbers are recorded and shown but never fail a run. A run fails only when <code>epais.exe</code> is not running while the client config turns EPAIS on. A process alive for less than 50% of the samples is not gated either (it was killed and restarted on purpose). Builds that carry the Shuttle/Keystone monitor change add a second group: <code>shuttle-launcher.exe</code>, <code>shuttle.exe</code> and <code>keystone.exe</code> (Mac: <code>keystone</code>) are judged like the client processes, with a first memory ceiling of 1024 MB each (a starting value, not a measurement; it is replaced by the observed 95th percentile once real runs exist); <code>shuttle-ctl.exe</code> (a CLI that runs for seconds), <code>sovin.exe</code> (the old Shuttle name an upgrade can leave behind) and on Mac <code>keystone-auth</code> and <code>shuttle-launcher</code> (it hands over to <code>shuttle</code> and does not stay resident) are observe-only. A process that never appears in a run shows as not alive and is skipped, not failed.</p>
</div>

<div class="card" id="s1">
  <h2>1. Where the data comes from</h2>
  <ul>
    <li>The <strong>sampler</strong> reads every monitored process every <strong>10 seconds</strong> during a test (a few tests use 5 s). On Windows it watches <code>stAgentSvc.exe</code>, <code>stAgentSvcMon.exe</code>, <code>stAgentUI.exe</code> and also records <code>demAgentSvc.exe</code> and <code>epais.exe</code> (builds with the Shuttle/Keystone monitor change also <code>shuttle-launcher.exe</code>, <code>shuttle.exe</code>, <code>keystone.exe</code>, <code>shuttle-ctl.exe</code> and <code>sovin.exe</code>), with the thread count of each process; on Mac <code>nsAuxiliarySvc</code>, <code>Netskope Client</code>, <code>NetskopeClientMacAppProxy</code> and others (with the monitor change also <code>keystone</code>, <code>keystone-auth</code> and <code>shuttle-launcher</code>). Every process the sampler records appears in the per-process table and the cross-version charts as soon as a run holds rows for it.</li>
    <li>Each <strong>test run</strong> condenses its samples into one summary row: highest CPU, sustained seconds, memory baseline and growth, highest handles. The report tables aggregate those rows per process.</li>
    <li><strong>CPU unit = per core:</strong> 100 is one core fully busy, 190 is about 1.9 cores, independent of how many cores the VM has.</li>
    <li><strong>Memory unit = MB:</strong> Private Bytes on Windows, RSS on Mac. The two <strong>must not be compared</strong>.</li>
  </ul>
""")
    h.append(fig(fig_sampling("en"), "<strong>Figure 1</strong>: every CPU number needs the previous sample to be computed, so the first sample has no CPU value, and a reboot or process restart resets the calculation."))
    h.append("</div>")
    h.append("""
<div class="card" id="s2">
  <h2>2. "Resource peaks per process", column by column</h2>
  <h3>avg_cpu_peak - average of the highest CPU</h3>
  <ul><li><strong>Meaning:</strong> take the highest CPU of the process in each run, then average over all runs.</li>
  <li><strong>Why look:</strong> a rough baseline of "how busy is it in general".</li>
  <li><strong>How to read:</strong> it is an average of maxima, so it moves when the share of stress tests versus idle tests moves. <strong>Compare it only across the same set of tests.</strong></li></ul>
  <h3>worst_cpu_peak - the highest CPU reading</h3>
  <ul><li><strong>Meaning:</strong> the largest 10 s average over all runs and all samples.</li>
  <li><strong>How to read:</strong> one single point: "it has been this high once", not "it stayed there". Never fails a run. Useful to see how far it is from the threshold, for example 254.8 against 280.</li></ul>
  <h3>worst_sustained_s - longest time above the threshold</h3>
  <ul><li><strong>Meaning:</strong> within one run, the longest stretch with CPU continuously above the threshold (figure 2 in section 3).</li>
  <li><strong>Why look:</strong> catches runaway loops and busy spins. Short spikes are normal on reconnects and config loads; only a sustained load is a problem.</li>
  <li><strong>How to read:</strong> 0 = no sample reached the threshold; 10 = exactly one sample did; <strong>the run fails at 60 s</strong>. It is almost always 0 because every process peaks far below 280 (the highest on record is 261.2).</li></ul>
  <h3>cpu_gate - the threshold</h3>
  <ul><li><strong>Meaning:</strong> the CPU value (per core) used to decide "sustained". It is <strong>280 for every test</strong> (2.8 cores), and the run fails after 60 s in a row. The yellow dashed line in the chart is this gate.</li>
  <li><div class="warnbox"><strong>Careful:</strong> a peak above the yellow line is not a failure by itself: the run fails only after 60 s in a row above it; demAgentSvc.exe is observe-only, it is measured but never fails a run. Older runs used 140 or 240 per test; the report restates <em>worst_sustained_s</em> against 280: a run whose highest reading is under 280 has 0 s above the gate.</div></li></ul>
  <h3>avg_mem_growth / worst_mem_growth - memory growth</h3>
  <ul><li><strong>Meaning:</strong> growth = "recent average" minus "baseline" (figure 3 in section 3). Negative = it shrank.</li>
  <li><strong>Why look:</strong> the signal of a memory leak. <em>worst</em> above 100 MB fails the run.</li>
  <li><strong>How to read:</strong> <em>avg</em> mixes positive and negative values and hides leaks, so it is of low value; <em>worst</em> is the useful one. Growth also depends on how long the run is.</li></ul>
  <h3>worst_handles - highest handle count</h3>
  <ul><li><strong>Meaning:</strong> the highest handle count at any sample.</li>
  <li><strong>Why look:</strong> a handle leak eventually exhausts the process.</li>
  <li><strong>How to read:</strong> it fails only if it reached 1500 <strong>and the last sample is still at or above 1500</strong> (figure 4). <code>stAgentSvc.exe</code> is at 1400 today, 100 below the ceiling: worth watching.</li></ul>
</div>
""")
    h.append('<div class="card" id="s3"><h2>3. Three diagrams</h2>')
    h.append(fig(fig_sustained("en"), "<strong>Figure 2: how can we know \"sustained\"?</strong> Each bar is one 10 s window and the yellow dashed line is the 280 gate. The left chart is a real run: the highest bar is only 143.28, no bar reaches the gate, so worst_sustained_s = 0. If exactly one bar were above the gate the value would be 10 s: seconds = (last bar time - first bar time) + 10, so one bar is 0 + 10; six in a row = 50 + 10 = 60 s, the fail line (right chart, illustration). Note that it measures a 10 s average: a spike shorter than 10 s is flattened."))
    h.append(fig(fig_memgrowth("en"), "<strong>Figure 3: how memory growth is computed</strong> (real run). Baseline = median of the first 3 living samples, recent = average of the last third, growth = recent - baseline. Each boot is its own segment (memory resets on reboot, so growth across boots is meaningless); the largest segment growth is reported."))
    h.append("""<div class="explain"><strong>What about a client that was just installed?</strong> There is no warm-up exemption: the baseline is always the first 3 living samples of the test. Most tests start with the client already running, so the baseline is a settled value (for example STRESS-01, STRESS-08 and STEER-06 have baselines of 25-29 MB and grow only 5-8 MB); warm-up growth is far below the 100 MB threshold and does not cause false failures.
    Waiting until the process is ready before taking the baseline needs thread-count data first; that work is on the to-do list.</div>""")
    h.append(fig(fig_handles("en"), "<strong>Figure 4: handles rule.</strong> A burst right after start that falls back is not a leak (real STEER-07 case: 3304 → 790 → about 920); only a count that climbs past the ceiling and stays there fails."))
    h.append("</div>")
    return "".join(h)
