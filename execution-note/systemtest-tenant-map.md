---
nplan: null
type: execution-note
categories:
  - Tenant / DC Mapping
  - SOP / Workflow
related_nplans: []
status: active
source: frontmatter
---

# SystemTest test ↔ tenant 對照表

**動工前先查這張表。** tenant 能力（NPA / CPA / DSE / DNS-Security / watchdog /
TLS-key）不對 = 整個 case 白跑。

## Lane 分類

**四類 —— 「不能跑」與「沒跑過」是不同的事,不要混在同一格:**

| 類別 | 意思 | 怎麼認定 |
|---|---|---|
| **BOTH** ✅ | 兩種 lane 都有實績 | 兩邊都有 build 編號 |
| **LOCAL-only** | REG(SSH)機制上不可能 | test 內 `if not local_test: pytest.skip` |
| **REG-only(code-gated)** | localTest 機制上不可能 | test 內 `if local_test: pytest.skip` |
| **LOCAL-untested** | **結構上可跑,只是沒人跑過** | signature 不收 `local_test` → 無任何 gate |

最後一類是空白而非限制 —— 要 LOCAL 跑就直接跑,不需改 code。

**規則:** reboot 類 case(POWER-04、FC-03、UPGRADE-02)已在 code 內 gate 掉 —— 想
改成 LOCAL 可跑必須先解決 self-runner 不跨重開機的問題,否則 reboot 後 task 死亡會
觸發 MISSING fail-fast(groovy 2026-07-31 新增)。

## 合併總表：Case ↔ Lane ↔ Tenant/dc ↔ 備注

| Case | Lane | qa tenant dc | stg tenant dc | 備注 (必要能力) |
|---|---|---|---|---|
| IPC-01 | LOCAL-only | **1119** systest | **1334** systeststatic<br>**1331** systest1331 | tray-icon截圖需真實UI |
| POWER-04 | REG-only(code-gated) | 1118 systest | **1334** systest<br>**1334** systest1334 | reboot類<br>self-runner不跨重開機 |
| STEER-01 | LOCAL-untested | 1118 systest | **1334** systeststatic(141)<br>**1331** systestcloud<br>mac:1334 systeststatic | 不需DSE<br>test自備cloud前置 |
| STEER-05 | REG-only | 1118 systest | **1334** systeststatic<br>mac:1334 systeststatic | LOCAL2本機runner對tenant UI API會斷線→用REG<br>cloud自切<br> |
| STRESS-01 | BOTH✅ | **1119** systest | **1331** systest<br>**1331** systest1331 | REG6兩tenant peak-conns=0是VM問題非tenant |
| STRESS-02 | LOCAL-untested | 1119 systest | **1331** systest1331 | - |
| STRESS-03 | LOCAL-only | **1119** systest | **1331** systest | NIC disable切斷SSH；需自帶rescue self-heal task |
| STRESS-04 | BOTH✅ | 1119 systest | **1334** systeststatic<br>**1334** systest1334<br>**1331** systest1331<br>**1331** systeststatic | 141 三lane皆✅ |
| STRESS-05 | BOTH(設計相容,LOCAL未證) | **1119** systest | 1331/1334 systest<br>**1334** systest1334 | 雙lane刻意設計 |
| STRESS-06 | LOCAL-untested | 1118 systest<br>**1119** systest | 1331/1334 systest<br>**1334** systest1334 | LOCAL已證 |
| STRESS-07 | LOCAL-untested | N/A(qa無NPA) | **1334** systeststatic<br>**1347** systeststatic | NPA+CPA+TLS-key |
| STRESS-08 | LOCAL-untested | **1119** systest | **1334** systest<br>mac:1334 systeststatic | 雙tenant 141已證|
| STRESS-11 | LOCAL-untested | ? | **1331** systest <br>mac:1334 systest| DNS-Security+DSE+web/all+blockDnsTCP=false<br>crash ENG-1180143 |
| STRESS-13 | ? | 未實測 | **1334** systest1334 | CPA-only→用1334 |
| STRESS-26 | LOCAL-only | **1119** systest | 1331/1334 systest<br>**1331** systest1331 | flood塞爆SSH；must use localtest |
| UPGRADE-01 | REG-only(failclose需VM外下config) | 1118/1119 systeststatic| **1334** systeststatic<br>**1331** systeststatic<br>**1334** stg1334up(upgrade專用dc) | 需DSE=FALSE |
| UPGRADE-02 | REG-only(code-gated) | 1118(watchdog=false) | **1334** systest(137 baseline)<br>**1334** stg1334up(upgrade專用dc) | reboot類 |
| FC-03 | REG-only(code-gated) | 1118 | **1334** systest | reboot類 |
| FC-04 | LOCAL-untested | 1118 | 1334或1331 systeststatic(DNS-Security×DSE) | - |
| MU/VDI suites(mu01-08/fc02/vdi01-07) | REG-only(fixture-gated) | 依各suite | 依各case | 需`--vdi`+雙帳號`ssh_username_b`/`ssh_password_b`<br>缺一項整suite靜默skip(`client_fixtures.py:3573-3579`)<br>Windows OpenSSH VM |
| CPA enabled suite | — | — | **1334** stg1334cpa | DSE TRUE+`enableTLSKey=1` |
| CPA disabled suite | — | — | **1331** stg1331cpa | `enableTLSKey=0`（唯一差異） |
| IDP enrollment | — | — | **8243** nscauto5.stg01-mplegacy(無dc,env `mpas_prod.json`) | secure enrollment+SAML IDP<br>1334/1331未開secure enrollment<br>`--domain_name`只填env domain |
| FIPS Win(NPLAN-4241) | — | **1119** qa1119 | — | FIPS feature build+IDP enrollment；test_user=`nsclientautomation+qa1119fips@gmail.com` |

**共同備注**：1331/1334 自 2026-08-18 起皆有 NPA（舊「無 NPA→auto-skip」記錄已過時）。
REG-02 lane 常用 tenant 1118 或 1331 `dc=systest`；watchdog=false；groovy 與 REG
byte-identical，patch 需兩邊同步。


## Jenkins lane ↔ VM ↔ tenant（目前實際用法）

| Lane / job | VM | 常用 tenant/dc | 備注 |
|---|---|---|---|
| DEV/GRS-SYSTEMTEST-REG | SYS-07 `10.136.217.108` | 1331 `dc=systest` | 主 lane |
| DEV/GRS-SYSTEMTEST-REG-02 | AUSTIN-FED-SYSTEST `10.136.211.181` | 1118 或 1331 `dc=systest` | 第二 lane；groovy 與 REG byte-identical |
| DEV/GRS-SYSTEMTEST-**LOCAL1** | SYS-03 `10.136.124.102` | 1119 `dc=systest` | alias `localtest`/`local1` 都通 |
| DEV/GRS-SYSTEMTEST-**LOCAL2** | SYS-04 `10.136.219.35`** | 1119 或 1331 | node `systest-local2`|
| DEV/GRS-SYSTEMTEST-**MAC1** | mac VM `10.56.6.70`（靜態）| **1334** `nsclientauto4.stg` `dc=systeststatic`| SSH mode（非 `--localTest`）<br>`preflight`/`recover` 不支援<br>固定 `vm=10.56.6.70 _force_preflight=1`<br>已證 PASS：STEER-01/05F。見 [[mac_setup]] |
| DEV/GRS-SYSTEMTEST-**MAC2** | main mac agent，node `systest-mac-tar1` | **1334** `dc=systeststatic` | self-runner 直跑（非 SSH<br>已證 PASS<br>見 [[mac_setup]] |




## dc → config 實測 binding 表

**client config 層**（`ou_or_group_name` 精確匹配，不進任何一條 = 落 Default）：

| tenant | 有專屬 client config 的 dc | 落 Default（= 全部互打，不可並行） |
|---|---|---|
| 1334 | `systest`、`stg1334cpa`、`stg1334up` | `systest1334`、`systeststatic1334`、`systeststatic`、其他全部 |
| 1331 | `systest`、`systeststatic`、`stg1331cpa` | `systest1331`、其他全部 |

**steering 層**：`systeststatic`（ou+group）→ 'static'；`systeststatic1334` 實見也綁
'static'（314 nsdiag）；`systest` → 專屬；`systestcloud` → 專屬（1331）。

**不可並行規則（只有兩個家族 — static = non-DSE、non-static = DSE）**：
- **static 家族**（non-DSE）：`systeststatic`、`systeststatic1331`、`systeststatic1334`
  → 共享 'static' steering config → **同家族互不可並行**
- **non-static 家族**（DSE）：`systest`、`systest1331`、`systest1334`、`systestcloud`
  → **同家族互不可並行**
- **跨家族可以並行**（一個 DSE + 一個 non-DSE = 打不同 config）
- 此規則 1331 / 1334 完全同型
- **跨 tenant 永不共享**（owner 2026-08-16）— config 是 tenant 內資源，不同 tenant 隨便並行
- ⚠️ 實測註記（client-config 軸，與家族規則分開看）：1334 上 systest1334/
  systeststatic1334/systeststatic 的 **client config** 都落 Default tenant config
  （無專屬）。家族規則管的是 steering/DSE 軸；若某 case 要做 **client-config**
  push（update_client_config），同 tenant 跨家族也可能撞 Default — 此情境目前
  無實例，碰到再驗

## tenant 103182 — SUPERSEDED 2026-09-11, r142 iter5 已改用 tenant 1347

owner 2026-09-11："we are using new tenant to go iter 5, forget about 103182" — 103182 從未跑過任何
case（見上一版本記錄），該方向已放棄，不要再往這個 tenant 排任何東西。

## tenant 1347（karthik.stg.boomskope.com）— r142 iter5 campaign 現役 tenant（2026-09-11）

owner 給的 dc 家族表（`.env` tenants map 已確認：`karthik` = 1347，見
`golden_regression/test_environment/boomskope_nonprod_stg.json`/`staging.json`）：

| dc（含別名） | AD group | 用途 | 實測 bound steering config（`nsdiag -f`/log） |
|---|---|---|---|
| `sysstatic1347`、`systeststatic1347`、`systeststatic` | systeststatic | non-DSE | `systest static`（default mode `all`） |
| `systest1347`、`systest1347mac`（**正確拼法，見下方 ⚠️**） | systest | DSE | `systest` |
| `up1347`、`up1347mac` | upsystest | upgrade 專用（UPGRADE-01/02） | — |
| `cloud1347`、`cloud1347mac` | cloud | cloud steering（OVLP/STEER-05/**STEER-01** 系列用） | `systestcloud`（已是 cloud mode，build 59 實測） |

**⚠️ `sytest1347` 是打錯字、已修正為 `systest1347`（2026-09-14，owner 抓到）**：這個
chapter 從 2026-09-11 建立起，G2/G3/G5 排程用的就是少一個 `s` 的 `sytest1347`——這串**完全
沒對到 tenant 1347 上任何 OU/config**，client 全部落到 tenant 全域共用的
`Default tenant config`（實測 build 128：`Config:: Default tenant config.`），不是預期
的專屬 `systest` config。正確拼法 `systest1347`（對齊 AD group 名稱 `systest`，跟
`systeststatic1347`/`cloud1347`/`up1347` 同一套命名規則）已實測（REG build 60）：
`Config:: systest.` / `Steering Config:: systest.`，跟 tenant 1457 的 `systest1457`→
`systest` 是同一個 pattern。REG2/LOCAL2 的 G2/G3/G5 已修正並套用（PR 無需要，純 cron
patch）。**MAC2 用的 `sytest1347mac` 大概率同一個 typo，已通知 git44 一併確認。**

**⚠️ STEER-01 只能綁 cloud 家族 dc（2026-09-14，owner）**：STEER-01 的 Cloud Apps
Only 前提（PR 472，`acheng/git-steer01-query-only-fix`）已改成**純查詢**——只讀
`nsdiag -f` 的 `traffic_mode`，不是 cloud 就直接 FAIL，不會再自己動手改+還原。這代表
它**只能在已經是 cloud mode 的 config 上跑**：`cloud1347`（bound `systestcloud`，已驗
證是 cloud）可以；`systeststatic1347`（bound `systest static`，預設 `all`）不行——舊
code 在 static 上會自己把它切成 cloud、teardown 再切回去，但 restore 走
`retry_function`（吞例外回 `None`），失敗時 log 照樣印「已還原」，把共用的 `systest
static` config 卡在 cloud mode（build 57 abort 事故根因）。REG2/LOCAL2 的 G4(static)
排程已把 `test_steer_01` 移到 G1(cloud)，跟 `test_steer_05_fast` 同組；G4 只留
`stress_07`/`stress_08`（兩個都不要求特定 mode）。同一個 tenant 換家族綁的 config
名字不一定一樣（1457 的 `cloud` 家族落在共用的 `Default tenant config`，1347 的
`cloud1347` 卻是專屬的 `systestcloud`）——**不要跨 tenant 假設 config 名字**，每次要
用 `nsdiag -f`/log 實測，別複製這張表的名字去猜別的 tenant。

**Jenkins 現況（2026-09-11 排定，`grs_groovy_lint.py patch-trigger` 直推 live）：**
- **REG2**（`W11-26H1-AUSTIN-SYS-07`）：8 組 cron slot 全部指向 1347/stg，G1/G4/G6→`sytest1347`、
  G5→`systeststatic1347`、G2/G7→`cloud1347`、G3/G8(upgrade_01/02，本次重新加回)→`up1347`。
  `release_info` 非-upgrade 組故意留空（不帶 `--current_release`）— 走 email 安裝路徑,自動裝
  tenant 目前在 STG 的最新 build,owner 2026-09-11 明確要求。
- **REG**（`AUSTIN-FED-SYSTEST`）：原 REG2 那 6 組 tenant **1457**/fed 排程原樣搬過來（`--dc=systest1457`
  等不變,只換 HOSTNAME + branch 改回 `main`）— 1457 iter1 continue 跑,只是從 REG2 換到 REG。
  G3/G8(upgrade)在 1457 上仍缺，因為 1457 沒有 develop-142 build（2026-09-09 owner 已定案，見下方
  1457 章節）。
- **LOCAL2**：無 job 層 default 需要改（default 本來就是 1119/qa，跟 1457 無關）；之後手動觸發
  LOCAL2 一律用 1347（chapter lock `r142-iter5-1347`），不要再手動打 1457。
- **chapter lock**：`grs_jenkins.py chapter show` → `r142-iter5-1347`（tenant 1347, env
  `boomskope_nonprod_stg.json`）。舊 1457 chapter（若有）已由此覆蓋。

尚未實測 G3/G8 在 1347 是否真有 develop-142 build 可供 upgrade 起跳 — 第一次觸發後看結果，別假設。

**2026-09-11 手動 smoke test 發現**：`test_overlap_02_flags_off_parity` 在 `cloud1347`（以及 1457 的
`systest1457cloud`，同一 cloud dc family）上 positive control 失敗 —— 找不到 `clients3.google.com`
的 'Tunneling flow' 行，owner 判定這個 tenant/dc 組合不滿足這個 case 的前提。已從 REG2(1347)/REG(1457)
的 G7 cron 排程 `-k` 清單移除，其餘 5 個 overlap case 不受影響。

**2026-09-12 LOCAL2 加入 iter5**：LOCAL2 排程從 git44 2026-09-10 設的 tenant 1457 process-health（L1-L6，hourly 22-03點）整份換成 iter5/1347（G1-G5+G7/G8，跟 REG2 同組內容，7 個 daily slot，時 0-6 點，刻意跟 REG2 同 dc-family 的時段錯開避免撞車），iterations=5(非-upgrade)/3(upgrade)，release_info pin `--current_release=release-142`。已用 coord.py 通知 git44。

**2026-09-14 test_steer_01 從 G4 搬到 G1（REG2＋LOCAL2 都改）**：見上面 STEER-01 那條 ⚠️。
`grs_groovy_lint.py patch-trigger` 直推 live，POST 200 + read-back 內容核對通過；REG2/
LOCAL2 各自的 verify-build 因為當時 job 正在跑舊排程的 build 排進 queue，等現有 build
跑完會自動起。搭配 code 修復 PR 472。

## chapter r142-iter20-1347（2026-09-14，取代 r142-iter5-1347）

PR 472 merge 後，owner 開新 chapter：非-upgrade 組（G1-G5）`iterations` 5→20；upgrade
組（G7/G8）**維持 iterations=5，不跟著漲**（owner明確要求：25-30min/iter，若也衝
20 會單次跑到 ~14h，超過 8h cron 週期造成排隊）。REG2 的 `timeout=`（manual per-slot
Jenkins timeout，小時）已用 `db.py timeout`（release-142 樣本、median steady-state、
每組只算一次 install overhead、×1.5 headroom）重新算過並套用：
G1=7h、G2=8h、G3=7h、G4=4h、G5=6h、G7=4h、G8=4h。LOCAL2 沒有手動 `timeout` 參數
（self-runner 依 `iterations` 自算 poll timeout，不用改）。`grs_jenkins.py chapter show`
現在只有 `r142-iter20-1347`（global，tenant=1347，env=boomskope_nonprod_stg.json）。

**2026-09-12 overlap 整組移除**：REG2 build #134（bundled）與 #136（isolated 重跑）都在
`test_overlap_06_concurrent_classification` 炸在同一個網域——`clients1.google.com` 在 tenant
1347/`cloud1347` 上完全零 tunnel-first marker（其他 6 個候選網域正常），兩次獨立重現，排除
cross-test cache 理論。根因：`_OVLP06_CANDIDATE_DOMAINS` 這組網域池 2026-09-04 只在 tenant
1334/REG6 上實測過，從未在 1347 上重新驗證。owner 裁決：不只 overlap_06，**整個 overlap 組
(overlap_01/04/05/06/08) 從 REG2(1347)/REG(1457) 排程整組移除**，不再排入未來測試。

## tenant 1457（nscauto7.fed.boomskope.com）— r142 iter1 continuation，現搬到 REG（2026-09-11）

沿用 2026-09-09 已驗證的 6-dc-group 排程（`systest1457`/`systest1457cloud`/`systest1457static`/
`sys1457`），2026-09-11 從 REG2 搬到 REG（`AUSTIN-FED-SYSTEST`），內容不變，只有 VM + branch(回main)。
G3(upgrade_01)/G8(upgrade_02) 仍缺 — 1457 沒有 develop-142 build（只到 141.1.0.2817），等 fed 的
release channel 補上 142 才能重加。

**2026-09-13 排程停用（owner 指示）**：REG 的 cron trigger spec 已清空（只留一行說明），
不再自動跑。手動觸發（`grs_jenkins.py trigger reg ...`）仍可用。REG2(1347/iter5)、
LOCAL2 排程不受影響，繼續照常跑。

## dc 衝突分組（tenant 1334, owner 2026-09-09）

**MSI exit 1603 可能是 dc 衝突**（同一台 VM/tenant 在短時間內用同一個 dc 重複
enroll，或該 dc 當下正被別的 lane/case 佔用）——不是產品安裝 bug 就一定要換 dc
重跑，不要無腦重試同一個 dc。同一組內的 dc 互為替代品（同一 tenant 1334，行為
等價，撞到其中一個就換組內另一個）：

| Group | dc 成員 |
|---|---|
| `systest` | `systest`、`stg1334systemtest`、`sys1334`、`systest1334`、`systest1334mac` |
| `systeststatic` | `systeststatic1334`、`systeststatic1334mac`、`systeststatic` |
| `stg1334cpa` | `stg1334cpa`、`stg1334cpacpa` |
| `stg1334cloud` | `stg1334cloud` |
| `stg1334up` | `stg1334up` |

## dc 選法（owner 規則 2026-07-30）—— 選錯等於測錯東西

| 需要 | 用 |
|---|---|
| **DSE** | **`dc=systest`** |
| **non-DSE** | **`dc=systeststatic`** |

這兩個 dc 是**同一個 tenant 的不同 per-user config**，綁到不同的 steering config。
STRESS-07 用錯燒了 6 輪。**絕不可自行改寫 owner 給的 `--dc` 值**（[[grs_coding]]）。

## DNS Security 開關 API —— 只有一個是對的

`set_dse_steeringconfig` 回 Success、`nsdiag -u` 同步成功，**但 client 仍是
`steer_dns=1`**。與 STRESS-08 的 `update_traffic_steering_mode` vs
`steering_method_none` 是同一種陷阱。

| API | 寫 `dns_enabled` 主開關 | 保留 `dynamic_steering` | 可用 |
|---|---|---|---|
| `set_dse_steeringconfig(dns_*=False)` | ❌ | ✅ | ❌ **關不掉** |
| `update_dns_security(steer_dns_status=0)` | ✅ | ❌ 強制 False | ❌ **會關掉 DSE** |
| **`update_dns_security_on_prem_off_prem`** | ✅ | ✅ | ✅ **正解** |

**順序**：`set_dse_steeringconfig` 會從 server 現況**重建整份 OU config**，所以設
DSE/traffic mode 必須在 DNS push **之前**，否則會把剛關掉的旗標救回來。

**每次 push 後必須 read back** —— build 184 沒驗，量出「DNS-OFF 基線 220ms」（真值
3ms），ON-vs-OFF 比較靜默失效**且測試會綠**（[[feedback_push_success_is_not_applied]]）。

## Latency 基準（1331 / SYS-06，給後人對照）

DNS Security **OFF median 3ms → ON median 221ms**，拆成 **~168ms per-connection
（tunnel 建立）+ ~46ms per-query**。回應大小不影響（10448B 與 1459B 同為 221ms）。
故 gate 訂 **300ms** 而非 spec 的 100ms —— spec 的數字是在沒有 tunnel 的前提下寫的。
量測方法與陷阱見 [[feedback_push_success_is_not_applied]]（假基線 220ms vs 真 3ms）。


**VM/tenant 會漂移**：以 Jenkins console 的 `Target VM found:` + VM 上 `nsdiag -f`
的 Tenant URL 為準，不要信舊記錄（[[feedback_verify_vm_before_touch]]）。

## 新 case 加 row 的欄位

Case | Lane | qa tenant dc | stg tenant dc | 備注 (必要能力，附 build 編號)。

相關：[[reference_cpa_tenant_dse_config]]、[[reference_systemtest_plan]]、
[[reference_failclose_nsdiag_impact]]、[[reference_dns_security_stg1334_absent]]。

(7844 overlap/client-config 知識已移至 reference_nsclient_config_files.md,2026-08-05)

## Tenant ID ↔ name 反查規則（2026-08-05, owner 教訓「you never guess, you learn to find」）
- `--tenant <ID>` 要能在 env json 的 `tenants` map 反查到名字（helper_fixtures.py config fixture line 78 `[0]`，查不到 = IndexError 全掛）。
- **查找範本：`golden_regression/test_environment/boomskope_nonprod_qa.json`**（owner 指定）。例：qa 1093 = nsclientauto1（qa）；同名 tenant 在 stg 是不同 ID（stg nsclientauto1 = 1329）— tenant ID 是 per-stack 的。
- stg 上 ID 1093 不存在於任何 map/history（已查 git log -S）→ 該方向是死路；owner 改派 **stg 1329 = nsclientauto1.stg, dc=stg1329** 給 run 3 flags-OFF。


## UPGRADE 路徑規則（owner 2026-08-15）

141 campaign 的 UPGRADE-01/02 實際跑的是 `--previous_release=release-132`（132→141），
連 proven 綠燈的 REG-02 73-75 也是。Owner 裁決：
- **infra/branch 驗證**：132→141 可接受
- **正式 soak / 真實升級覆蓋**：必須從穩定 build 起跳 = `--previous_release=release-140`（140→141）
- 後續正式 UPGRADE run 前記得改參數；132→141 的既有綠燈不能算升級覆蓋


## UPGRADE-01 clean-base 汙染（REG-02 101 紅 / 105 綠，2026-08-15）

**機制（實證，取代先前「還沒下載第一份 config 所以 Config:: 是空的」那個說法——那是錯的）**：
fresh 裝好的 previous_release client **自己 auto-upgrade 掉了**。nsdebuglog（101, SYS-07）：
服務起來 14s 就 `Check for client upgrade`→`Schedule upgrade check passed`→
`client auto update: msiexec ... /qn started successfully`；MSI 為換 binary 停掉
stAgentSvc（`Received stop event from service control`→`CNSCom2 stopped`），
那 ~48s 內 nsdiag -u/-f 只回 `Failed to connect with Service` → bound config 讀成 ''
→ assert。48s 後服務以 **141.0.0.2769** 重生。

**為什麼會自我升級**：`_ensure_clean_previous` 的 `clientAllowAutoUpdate=0` 用
`search_config=pre_cfg`，而 `pre_cfg` 來自**即將被刪掉的舊 client**。兩種情況會打歪：
(1) VM 無 client → `pre_cfg=''`；(2) 舊 client enroll 在**別的 tenant/dc**（105 實況：
舊 client 綁 1334/`systest`，run 卻 driving 1331/`systeststatic`）。打歪 = 該關的那份沒關。
有舊 client 且 dc 恰好相同時才會生效，所以 73-75 一路綠。

**空 search_config 不是 no-op** — 見 [[sop_config_targeting]]，它會寫進 `data[0]`（任意一份）。

**修法（已驗證，branch `acheng/git-upgrade01-clean-base`）**：`pre_cfg` 空就不推；
install 後有界等到 config 名讀得出來（純為撐過 MSI 換檔窗）→ 用**正確名字**推
auto-update=0 → **重讀版本**比對 previous_release major，漂掉就 uninstall+重裝一次。
⚠️ **絕不可只加 retry 讀 bound config** — 它會等過 MSI 期讀到正確名字然後在 141 的
client 上繼續跑 = 假綠燈。retry 後面必須接版本 gate。

**REG-02 105 實證**（1331/`systeststatic`, 132→141, iter 1, PASS 739.9s）：
bound-config wait attempt 1/12 未達→2/12 SUCCESS；
`baseline drifted to 141.0.0.2769 (wanted 132.x)` → 重裝 → `healthy baseline OK: 132.2.1.2566`；
之後 scenario 完整跑完（FailClose engaged、`upgraded under FailClose: 132.2.1.2566 ->
141.0.0.2769`、delivered ∈ tenant-published、`no leak after upgrade across 4 samples`）。
順帶定案：1331/`systeststatic` 當時確實是 `clientAllowAutoUpdate=1`（105 已把它推成 0，
所以之後同 config 的 run 會走正常路徑、不再觸發 drift 分支）。
