---
nplan: null
type: sop
categories:
  - IPC
  - SOP / Workflow
related_nplans: []
status: active
source: frontmatter
---

# SYSTEST-01: SOP Detailed: NPLAN Endpoint System Test

## Source
- Confluence: [SOP Detailed: NPLAN Endpoint System Test](https://netskope.atlassian.net/wiki/spaces/CDTBA/pages/8056112696/SOP+Detailed+NPLAN+Endpoint+System+Test)
- Page ID: 8056112696
- Date fetched: 2026-06-08

## SOP: NPLAN Endpoint System Test
This SOP defines how to plan, execute, and report a comprehensive Endpoint System Test for NPLANs. It focuses on end-to-end, production-realistic validation of the NSClient endpoint agent — its platform-specific interception layers, service lifecycle, traffic steering, tunnel management, FailClose enforcement, and cross-component interactions — emphasizing systemic risks, failure modes, and customer impact over feature-by-feature correctness.

## Team
- Kenmin Lin
- Austin Cheng

## 1. Definition and Objectives
Endpoint System Test is the practice of proactively discovering architectural failure modes, resilience gaps, and latent incident motifs across NSClient endpoint components, platform-specific drivers/extensions, inter-process communication, and infrastructure dependencies by combining code-path analysis, dependency mapping, and failure injection in production-like environments.
- Validate platform-specific interception: Windows WFP driver, macOS Network Extension, Linux TUN/VIF, Android VPN Service, iOS Network Extension, and ChromeOS extension paths.
- Exercise service lifecycle, config download, traffic steering, tunnel establishment, GSLB selection, FailClose enforcement, NPA dual-tunnel coordination, DEM, IPC, and observability under stress and partial failures.
- Prioritize customer outcomes: graceful degradation, accurate steering, recoverable operations, and enforced security policy when tunnels are down.

## 2.1 In Scope
- Platform-specific interception layers : Windows WFP driver stadrv.sys callout registration and self-protection; macOS System Extension transparent proxy and nsAuxSvc; Linux TUN/VIF with lwIP and iptables/nftables; Android/iOS VPN/Network Extension; ChromeOS extension behavior.
- Service lifecycle : stAgentSvc startup sequence, constructor wiring, OnInit, config.init, onLogon, onConfigReady, shutdown order, Watchdog/launchd/systemd recovery, AOAC/Modern Standby, sleep/wake, and VDI multi-user sessions.
- Traffic steering : CLOUD/WEB/FIREWALL/NONE modes, Dynamic Steering, exception priority, DNS security, IPv6, large steering configs, config update race conditions, and custom ports.
- Tunnel management : 22-state tunnel state machine, TLS/DTLS fallback, SPDY-like handshake, reconnect strategies, DPD, proxy handling, GSLB POP selection, and SWG + NPA dual-tunnel recovery.
- FailClose : 7-state FailClose state machine, config priority resolution, captive portal detection, FilterDevice rule persistence, upgrade/reboot behavior, enforce enrollment, and per-session isolation.
- Installation, upgrade, enrollment, config, certificate, IPC, security, DEM, Device Classification, proxy, bypass, NPA, and supportability scenarios that can trigger cross-component failures.
- Reproduction of systemic defects with exact module paths such as lib/nsConfig, lib/nsTunnel, lib/nsFilterDevice, lib/npa_core, lib/nscom2, lib/nsDEM, stAgent/stAgentSvc, and stAgent/stadrv.

## 2.2 Out of Scope
- Routine functional testing of isolated features covered by component or feature QE.
- Micro-benchmarks and single-component performance tests without systemic context.
- UI-only cosmetics that do not affect reliability, security enforcement, or customer impact.
- Cloud-side testing of Management Plane, Data Plane gateway internals, or cloud policy engine except where endpoint behavior depends on their contracts.

## 3. Applicability to NPLANs
Not every NPLAN requires Endpoint System Test. Prioritize when the change touches any of the following:
- Interception layers, steering decision logic, WFP/NE/TUN/VIF behavior, DNS handling, or bypass logic.
- Tunnel state machine, DTLS/TLS fallback, reconnect strategy, DPD, proxy path, or GSLB POP selection.
- FailClose, captive portal detection, FilterDevice rules, upgrade/reboot persistence, or VDI session isolation.
- Config download, JWT/digest validation, encryption, config update callback cascade, or large config handling.
- Install, upgrade, enrollment, secure enrollment tokens, self-protection, Watchdog, or platform package scripts.
- Cross-platform shared code where Android/iOS fixes could regress Windows/macOS/Linux, as seen in ENG-707767 to ENG-918451.
- NPA, DEM, BWAN, EPDLP, NSCom2/NSMsg2 IPC, Device Classification, certificate management, or proxy integration.

## 4. Entry Criteria
142
f7a64a4b-d9e1-4c02-838a-1e15e5f150ad
incomplete
 Joint planning completed with QE, service owners, and SRE: scenarios, environments, ownership, and expected outcomes aligned. 


143
0f242bd5-7074-4558-a322-f0945fd1b329
incomplete
 No open P0 functional defects blocking critical endpoint flows on the target branch. 


144
e282fda3-b7e3-480e-9b6b-4220904b255a
incomplete
 Sanity suite passing and health checks green for stAgentSvc, WFP/Network Extension/TUN, tunnel connectivity, and MP reachability. 


145
8c9aa078-b8cd-438a-b513-8f0ee54ed350
incomplete
 Production-like configs, feature flags, seeded tenants, test data, and observability hooks are ready. 


146
364d0299-591a-4632-9baa-29c4d7954364
incomplete
 Dependency map validated: Stadrv to TCPIP/BFE, stAgentSvc to Stadrv, MP APIs V2/V5/V7, cert stores, proxy, GSLB, NPA, DEM, and IPC. 


147
d2e3f7e7-4df8-42f0-8ac3-b9f6a978a599
incomplete
 Platform matrix defined: Windows 10/11 x86/x64/ARM64, macOS Big Sur+ Intel/Apple Silicon, Linux Ubuntu/RHEL/Debian, Android device diversity, iOS, and ChromeOS 138+.

## 5.1 Dependency and Component Mapping
- Document stAgentSvc dependency graph: CConfig → CNSCom2 → CNetworkMonitor → CNSScheduler → CFailCloseMgr → CTunnelMgr → CNpaTunnelMgr → CDemMgr.
- Map config files: nsconfig.json, nssteering.json, nsbranding.json, nsbypass.json, nsbypasscat.json, nsexception.json, nsoverlap.json, nstunnelpolicy.json, nsdeviceid.json, nsinternal.json, certificates, dps.json, and eventcache.json.
- Track IPC messages: SET_STATUS, CONFIG_READY, SET_ALLOW_DISABLE, DIAG_CMD, DEBUG_MODE, SET_USER_LOG_LEVEL.
- Document tunnel framing: DATA, PING, SYN_TUNNEL, TEAR_DOWN_TUNNEL, UPDATE_PROPERTY, TLS_KEY, DTLS 10s keepalive, TLS 120s keepalive, and Android 45s behavior.

## 5.2 Scenario Categories
- Installation/enrollment: MSI/PKG/.run/.deb/.rpm/app-store install, enrollment priority Email → InstallParam → Branding → UPN → IdP → IdPOnly → EAM, secure enrollment, MDM/Autopilot, and cross-architecture upgrade.
- Lifecycle: cold start, shutdown, AOAC/sleep/wake, service kill recovery, Watchdog, launchd, systemd, VDI logon/logoff, and crash dump generation.
- Steering: Dynamic Steering NONE→WEB/ALL, exception priority, DNS over TCP, UDP 3478, IPv6 CIDR, wildcard label boundaries, 30K+ domains, config races, and cert-pinned app interactions.
- Tunnel: DTLS→TLS fallback, proxy CONNECT with NTLM/Kerberos, POP failover, DPD false positives, network change, GSLB refresh after reboot, pending packet queue behavior, and dual-tunnel recovery.
- FailClose: reboot persistence, WFP/NE/TUN rule cleanup, captive portal variants, service crash recovery, enforce enrollment, VDI isolation, active-block config disable, and 24-hour cycling.
- Cross-flow chain reactions: config update → steering change → tunnel disconnect → FailClose activation → DNS recovery; upgrade → service restart → FailClose init → tunnel reconnect; network change → SWG/NPA teardown → reconnect.

## 5.3 Risk Heuristics
- Cross-platform shared-code changes and mobile fixes impacting desktop platforms.
- Fix-regression chains: DNS TCP to BSOD, packet handling to cert-pinned bypass, Flexible Dynamic Steering to FailClose, Android on-prem fix to Windows tunnel reconnect regression.
- Non-atomic callback sequences, state machine transitions, VDI multi-user contention, AOAC/Modern Standby, large-scale configs, and proxy/VPN interop.

## 6.1 Environments and Tooling
Execute on a dedicated performance/system-test stack with production-like tenants and isolated fault-injection windows.
- Platforms : Windows 10/11, macOS Big Sur+, Linux Ubuntu/RHEL/Debian, Android Samsung/Xiaomi/Lenovo, iOS, ChromeOS 138+.
- Special environments : Citrix VDI, AOAC devices, explicit proxy with NTLM/Kerberos, PAC/WPAD, captive portal HTTP 302/meta refresh/JavaScript redirect, 802.1x WPA Enterprise, IPv6-only, and third-party VPNs.
- Traffic : HTTP/HTTPS, large file transfers, QUIC/WebRTC UDP 3478, DNS over TCP, NPA private apps, concurrent multi-user sessions, and DEM probes.
- Fault injection : WiFi/Ethernet/cellular toggles, UDP 443 blocking, gateway unreachability, process kill, DNS flush deadlock simulation, proxy credential changes, rapid MP config pushes, and packet loss/latency.
- Observability : nsdebuglog.log, npadebuglog.log, nsInstallation.log, crash dumps, nsdiag, debug mode packet captures, Windows Event Viewer/WFP, macOS system.log, Linux journalctl, and MP client status.

## 6.2 Step-by-Step
- Baseline golden metrics: tunnel establishment time, steering latency, config download time, DPD response, FailClose activation/deactivation, service startup duration, crash frequency.
- Validate installation, enrollment, secure tokens, cross-architecture upgrade, MDM install, and platform-specific service/driver/config verification.
- Exercise service lifecycle: startup order, shutdown no-reconnect race, Watchdog/launchd/systemd recovery, AOAC cycles, and VDI sessions.
- Validate traffic steering across modes, Dynamic Steering transitions, exception priority, DNS TCP, IPv6, custom ports, large configs, and config updates during active traffic.
- Test tunnel resilience: DTLS fallback, POP failover, network change, proxy persistence, DPD under 20% packet loss, pending queue limits, and SWG + NPA recovery.
- Test FailClose: reboot persistence, rule cleanup, VDI isolation, captive portal detection, config disable during active block, enforce enrollment, service crash recovery, and 24-hour cycling.
- Run cross-flow chain reactions and platform-specific regressions.
- Review observability: logs, nsdiag, packet captures, crash dumps, MP status, UI notifications, and alert fidelity.

## 6.3 Sampling of High-Value Endpoint Tests
- Upgrade + FailClose Chain Reaction : Trigger auto-upgrade with FailClose and self-protection enabled; verify no permanent block, DNS flush does not deadlock, tunnel reconnects.
- Dynamic Steering NONE→WEB with FailClose : Move from on-prem NONE to off-prem WEB; verify tunnel starts and FailClose does not falsely activate.
- Network Change with Dual Tunnel : SWG and NPA connected; WiFi → cellular; verify both recover within 30 seconds and repeat five times.
- VDI Multi-User FailClose Isolation : User A connected, User B FailClose active, User C logs in; verify no cross-session interference.
- AOAC/Modern Standby Recovery : DTLS tunnel connected; sleep/wake 10+ cycles; verify no Disabled due to error and no frequent disconnects.
- 30K+ Domain Boundary : Push 35,000 domains including 230–255 character names; verify no crash/OOM and correct steering/FailClose recovery.

## 7. Data Integrity and Consistency
- Validate SHA-256 digest in nsinternal.json on config reads and ensure backup-first writes use fsync, primary write, and digest save.
- Validate JWT pipeline: extraction, public key retrieval, RS256 signature verification, and SHA-256 hash comparison for V7/V5/V2 compatibility.
- Verify config encryption through CConfigSec, .enc migration, ignored files such as nsinternal.json and nspubkey.pem, and plaintext cleanup.
- Verify secure enrollment token preservation: DPAPI registry values on Windows, Keychain on macOS, encrypted Linux files, upgrade, rollback, and 32-bit/64-bit migration.
- Verify steering config consistency across ExceptionMgr, FilterDevice, TunnelMgr, and FailCloseMgr during rapid config pushes.
- Verify nsdeviceuid persistence across upgrades, reboots, VDI clones, and AOAC duplicate-device risks.

## 8. Observability and SLOs
- Log coverage: per-module log levels, log rotation, async logging, packet dump, nsdebuglog.log, npadebuglog.log, nsInstallation.log, and platform logs.
- Diagnostics: nsdiag status, config checks, debug mode, log collection, packet capture, and operation under tamperproof/self-protection.
- Crash dumps: stAgentSvc.dmp and stAgentUI.dmp generation with actionable stack traces for SSL cleanup, DEM thread overrun, use-after-free, and platform crashes.
- Client status: heartbeat interval, version reporting, uninstall reporting, device status in MP, UI notification accuracy, and SET_STATUS message fidelity.
- Quality signals: tunnel establishment time, steering decision accuracy, FailClose timing, config download success rate, reconnect time, crash frequency, and battery drain on Android.

## 9. Defect Filing Standard
Every finding must be reproducible locally and actionable:
- Problem statement and customer impact: affected endpoint flow, platform, expected vs. observed behavior, and blast radius.
- Exact code location: functions/modules, config toggles, feature flags, and related ENG/fix-regression chain.
- Minimal deterministic repro: OS, NSClient version, tenant config, flags, network environment, and action sequence.
- Evidence: timestamped logs, grep patterns, nsdiag output, packet captures, crash dump stack traces, MP status screenshots, and WFP/NE/systemd evidence.
- RCA: root cause, contributing state machine transition, race timing, platform-specific behavior, or dependency behavior.
- Recommended fix and guardrails: code change, flag gating, regression tests, automation priority, dashboards, and alert updates.

## 10. Exit Criteria
- Planned Endpoint System Test scenarios executed for the scoped release across all target platforms.
- All valid findings documented, triaged, and filed using the defect filing standard.
- Regression candidates identified and promoted to automation or scheduled chaos/system drills.
- Cross-platform regression impact assessed for shared code changes and fix-regression chains.
- QE, service owners, SRE, and program owner sign off that residual risk is understood and acceptable.

## 11. Example End-to-End Endpoint Scenarios
- Config update cascade under Dynamic Steering + FailClose : Push Off-Prem mode change while moving off-prem; verify steering, tunnel, FailClose, and DNS recovery without stale-state activation.
- Installation + enrollment + FailClose on VDI : Three concurrent Citrix users enroll; verify tokens, IPC, tunnels, and per-session FailClose isolation.
- macOS upgrade + NPA transparent proxy recovery : Upgrade R130→R131+ on macOS 15.x; verify System Extension active and NPA traffic tunneled.
- Linux 30K+ long domains on 802.1x : Ubuntu 24.04, WPA Enterprise, 35K long domains; verify no crash and TUN does not break authentication.
- Android battery drain + network switch + NPA recovery : Run 8+ hours, switch WiFi/cellular, verify SWG/NPA recovery and no auto-enable after user disable.

## 12. Roles and Responsibilities
- QE : Own planning, execution, evidence, defect filing, and regression promotion.
- Service Owners : Provide code references, config/feature flag documentation, seed data, and co-own RCA/fixes.
- SRE : Provide environment parity, fault-injection tooling, dashboard/alert validation, and production-risk guidance.
- Program Owner : Align timelines, track cross-platform risks, and drive sign-off.

## 13. Reporting and Dashboards
- Execution report: scenario matrix with pass/fail per platform, evidence links, logs, crash dumps, nsdiag output, and risk notes.
- Quality signals: before/after metrics for tunnel, steering, FailClose, config download, reconnect, and crashes.
- Defect summary grouped by state machine transitions, cross-component races, platform-specific failures, scale/boundary, and fix-regression chains.
- Bug distribution by platform, severity, gap type, and automation coverage.

## 14. Best Practices
- Plan in parallel with development and treat interception contracts, state machines, and callback sequences as first-class test artifacts.
- Use production-like environments: AOAC hardware, real VDI, proxy/captive portal setups, 802.1x, IPv6, and third-party VPNs.
- Use feature flags safely: enableMacOsAOACSupport, disableWinStopServiceProtection, enableExceptionCheckForTcpDns, handleExceptionsAtDriver, dtlsFallback, avoidDisruptingBypass, bypassAllExistingConnections, enable_aoac_by_prelogon, allowProcessReadPermission.
- Apply regression gates: steering changes require FailClose, tunnel reconnect, and DNS security; tunnel changes require DTLS, POP, and FailClose; mobile/shared changes require desktop validation.
- Promote high-value scenarios to automation and scheduled chaos/system drills.

## 15. Risks and Mitigations
- AOAC instability → Dedicated sleep/wake suite on AOAC hardware covering frequent disconnect and Disabled due to error clusters.
- Cross-platform regression → Mandatory full-platform validation for shared code and mobile platform fixes.
- Non-atomic config cascade → Rapid config push testing during active traffic and FailClose monitoring.
- VDI contention → 3+ concurrent users, per-session tunnel and FailClose isolation, 20s establishment threshold.
- Large-scale config → 35K+ domain boundary tests across all platforms.
- Environment drift → Version pinning, config snapshots, parity checks, and dedicated test windows.

## 16. Artifacts and Templates
- Component Dependency Map: interception mechanisms, stAgentSvc module order, config inventory, IPC catalog.
- Scenario Matrix: test ID, objective, platform, fault model, expected/observed outcome, evidence links.
- Defect Report: follows Section 9 and includes code references, RCA, guardrails, and regression assessment.
- Platform Verification Checklists: Windows service/driver/registry/filesystem, macOS daemon/extension/config/branding, Linux systemd/binary/config/logs.

## 17. Suggested Regression Promotion Candidates
- FailClose persistence after reboot — ENG-895081.
- Upgrade + FailClose chain reaction — ENG-733657, ENG-751720.
- Dynamic Steering NONE→WEB transition — ENG-384041, ENG-591725.
- Config update race condition — ENG-422599, ENG-739968.
- DNS steering recovery after FailClose — ENG-561500.
- NPA + SWG dual-tunnel network change recovery — ENG-393015, ENG-441957.
- DTLS→TLS fallback under FailClose — ENG-503501.
- VDI multi-user FailClose isolation — ENG-928461, ENG-570306.
- AOAC sleep/wake stability — ENG-754190, ENG-766069, ENG-783149, ENG-830275.
- 30K+ domain config boundary — ENG-872456, ENG-948106.
- macOS NPA transparent proxy after upgrade — ENG-773191.
- Captive portal variants — ENG-482990, ENG-750658, ENG-795413.
- Cross-platform regression gates for Android/iOS/shared-library fixes.

## References
- 00. NSClient Architecture Overview
- 01. Installation, Upgrade and Uninstallation
- 02. Enrollment Flow
- 03. Service Lifecycle
- 04. Config Download and Management
- 05. Traffic Steering Configuration
- 07. Tunnel Establishment and Management
- 11. FailClose Mechanism
- Endpoint - System Testing
- SOP Detailed: NPLAN Backend System Test

## Test Cases

### TC-001: 5.2 Scenario Categories
- **Steps**:
  1. Installation/enrollment: MSI/PKG/.run/.deb/.rpm/app-store install, enrollment priority Email → InstallParam → Branding → UPN → IdP → IdPOnly → EAM, secure enrollment, MDM/Autopilot, and cross-architecture upgrade.
  2. Lifecycle: cold start, shutdown, AOAC/sleep/wake, service kill recovery, Watchdog, launchd, systemd, VDI logon/logoff, and crash dump generation.
  3. Steering: Dynamic Steering NONE→WEB/ALL, exception priority, DNS over TCP, UDP 3478, IPv6 CIDR, wildcard label boundaries, 30K+ domains, config races, and cert-pinned app interactions.
  4. Tunnel: DTLS→TLS fallback, proxy CONNECT with NTLM/Kerberos, POP failover, DPD false positives, network change, GSLB refresh after reboot, pending packet queue behavior, and dual-tunnel recovery.
  5. FailClose: reboot persistence, WFP/NE/TUN rule cleanup, captive portal variants, service crash recovery, enforce enrollment, VDI isolation, active-block config disable, and 24-hour cycling.
  6. Cross-flow chain reactions: config update → steering change → tunnel disconnect → FailClose activation → DNS recovery; upgrade → service restart → FailClose init → tunnel reconnect; network change → SWG/NPA teardown → reconnect.

### TC-002: 11. Example End-to-End Endpoint Scenarios
- **Steps**:
  1. Config update cascade under Dynamic Steering + FailClose: Push Off-Prem mode change while moving off-prem; verify steering, tunnel, FailClose, and DNS recovery without stale-state activation.
  2. Installation + enrollment + FailClose on VDI: Three concurrent Citrix users enroll; verify tokens, IPC, tunnels, and per-session FailClose isolation.
  3. macOS upgrade + NPA transparent proxy recovery: Upgrade R130→R131+ on macOS 15.x; verify System Extension active and NPA traffic tunneled.
  4. Linux 30K+ long domains on 802.1x: Ubuntu 24.04, WPA Enterprise, 35K long domains; verify no crash and TUN does not break authentication.
  5. Android battery drain + network switch + NPA recovery: Run 8+ hours, switch WiFi/cellular, verify SWG/NPA recovery and no auto-enable after user disable.
