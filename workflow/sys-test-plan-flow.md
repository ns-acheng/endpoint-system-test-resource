---
nplan: null
type: workflow
categories:
  - SOP / Workflow
related_nplans: []
status: active
source: frontmatter
---

# System Test Plan Composition Flow

How a `sysplan-nplan-XXXX.md` is assembled from multiple knowledge sources.

```mermaid
flowchart TB
    %% === SOURCE LAYER ===
    subgraph SOURCES[Knowledge Sources]
        direction LR
        SOP[systest-01.md<br/>Core SOP]
        NPLAN[nplan-XXXX.md<br/>Feature Test Plan]
        RELIABILITY[plan-reliability.md<br/>STRESS-01 to STRESS-20]
        IMF[imfs_overall.md<br/>46 Production Incidents]
        BUGS[escalation_bugs_overall.md<br/>250+ Customer Bugs]
        INTEROP[plan-9002-interop-test.md<br/>P0 Third-Party Products]
        ARCH[Architecture Chapters<br/>ch04 Config, ch07 Tunnel<br/>ch11 FailClose, ch03 Service]
    end

    %% === ANALYSIS LAYER ===
    subgraph ANALYSIS[Risk Analysis]
        direction LR
        SCOPE[Extract Scope<br/>What components change?]
        RISK[Map 10 Risk Categories<br/>Config, Cascade, Tunnel<br/>FailClose, Service, Steering<br/>Upgrade, Cross-plat, Interop, Scale]
        MATCH[Match Risks to IMFs<br/>and Escalation Bugs]
    end

    %% === PRIORITIZATION LAYER ===
    subgraph PRIORITY[Prioritization Engine]
        direction LR
        P0_RULE{Has IMF link<br/>or disables all users?}
        P1_RULE{Has escalation bug<br/>or recoverable failure?}
        P2_RULE[No incident link<br/>Edge case]
        P0_RULE -->|Yes| P0[P0: Max 10 cases<br/>Mermaid required<br/>IMF link required]
        P0_RULE -->|No| P1_RULE
        P1_RULE -->|Yes| P1[P1: Should Have<br/>Shorter format]
        P1_RULE -->|No| P2_RULE
    end

    %% === ENRICHMENT LAYER ===
    subgraph ENRICH[Enrichment]
        direction LR
        STRESS[Apply Stress Patterns<br/>STRESS-04 Config Shock<br/>STRESS-05 Service Chaos<br/>STRESS-06 Network Storm<br/>STRESS-07 Fail-Close]
        INTEROP_ADD[Add Interop Variants<br/>AnyConnect + FailClose swap<br/>CrowdStrike + file I/O<br/>Citrix VDI multi-user]
        EVIDENCE[Define Evidence Standard<br/>nsconfig before/after<br/>crash dumps, logs<br/>tunnel state, timestamps]
    end

    %% === OUTPUT LAYER ===
    subgraph OUTPUT[Generated Artifact]
        direction LR
        PLAN[sysplan-nplan-XXXX.md]
        PLAN --> SECTIONS[IMF Risk Table<br/>P0 Cases with Mermaid<br/>P1/P2 Cases<br/>Priority Rationale<br/>Phased Execution<br/>Exit Criteria]
    end

    %% === CONNECTIONS ===
    SOP --> SCOPE
    NPLAN --> SCOPE
    ARCH --> RISK
    SCOPE --> RISK
    IMF --> MATCH
    BUGS --> MATCH
    RISK --> MATCH

    MATCH --> P0_RULE

    RELIABILITY --> STRESS
    INTEROP --> INTEROP_ADD
    P0 --> STRESS
    P1 --> STRESS
    STRESS --> INTEROP_ADD
    INTEROP_ADD --> EVIDENCE

    EVIDENCE --> PLAN

    %% === STYLES ===
    style SOURCES fill:#1565C0,color:#fff
    style ANALYSIS fill:#4527A0,color:#fff
    style PRIORITY fill:#541E01,color:#fff
    style ENRICH fill:#2E7D32,color:#fff
    style OUTPUT fill:#37474F,color:#fff
    style P0 fill:#e74c3c,color:#fff
    style P1 fill:#FF9800,color:#fff
    style P2_RULE fill:#2196F3,color:#fff
    style PLAN fill:#4CAF50,color:#fff
```

## Summary

| Stage | Input | Output | Key Decision |
|-------|-------|--------|--------------|
| **Sources** | 7 document types | Raw knowledge | Which sources are accessible? |
| **Risk Analysis** | Feature scope + architecture | 10 risk categories mapped | Which components does this NPLAN touch? |
| **Prioritization** | Risks + IMFs + bugs | P0/P1/P2 classification | Does this risk have an IMF link? |
| **Enrichment** | Prioritized cases | Stress variants + interop combos | Would this also fail with a P0 product active? |
| **Output** | Enriched test cases | sysplan-nplan-XXXX.md | All P0 under cap of 10? |

## Source Contribution Map

| Source | Contributes To | Example |
|--------|---------------|---------|
| systest-01.md | Scope definition, evidence standard, defect template | Section 9 defect filing format |
| nplan-XXXX.md | Feature scope, affected components, out-of-scope boundary | Which APIs change, which platforms |
| plan-reliability.md | Stress injection methodology for P0 cases | STRESS-04 rapid config push pattern |
| imfs_overall.md | P0 justification, risk table, failure patterns | IMF-1073 clients disabled |
| escalation_bugs_overall.md | P1 justification, related bug references | ENG-591725 tunnel not established |
| plan-9002-interop-test.md | Interop P0 cases, coexistence risks | AnyConnect FailClose swap ENG-991833 |
| Architecture chapters | Callback cascade detail, state machines, code paths | Non-atomic onConfigUpdate sequence |
