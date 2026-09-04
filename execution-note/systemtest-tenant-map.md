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
| IPC-01 | LOCAL-only | **1119** systest✅ | **1334** systeststatic<br>**1331** systest1331✅ | tray-icon截圖需真實UI |
| POWER-04 | REG-only(code-gated) | 1118 systest | **1334** systest✅<br>**1334** systest1334✅ | reboot類<br>self-runner不跨重開機 |
| STEER-01 | LOCAL-untested | 1118 systest | **1334** systeststatic✅(141)<br>**1331** systestcloud✅<br>mac:1334 systeststatic | 不需DSE<br>test自備cloud前置✅ |
| STEER-05 | REG-only | 1118 systest | **1334** systeststatic✅<br>mac:1334 systeststatic | LOCAL2本機runner對tenant UI API會斷線→用REG<br>cloud自切<br>✅ |
| STRESS-01 | BOTH✅ | **1119** systest✅ | **1331** systest<br>**1331** systest1331✅(141,LOCAL1 233×20) | REG6兩tenant peak-conns=0是VM問題非tenant |
| STRESS-02 | LOCAL-untested | 1119 systest | **1331** systest1331✅ | - |
| STRESS-03 | LOCAL-only | **1119** systest✅ | **1331** systest✅ | NIC disable切斷SSH；需自帶rescue self-heal task |
| STRESS-04 | BOTH✅ | 1119 systest | **1334** systeststatic<br>**1334** systest1334✅(REG253)<br>**1331** systest1331✅(LOCAL1 231)<br>**1331** systeststatic✅(LOCAL2 78) | 141 三lane皆✅ |
| STRESS-05 | BOTH(設計相容,LOCAL未證) | **1119** systest✅ | 1331/1334 systest<br>**1334** systest1334 | 雙lane刻意設計 |
| STRESS-06 | LOCAL-untested | 1118 systest<br>**1119** systest✅ | 1331/1334 systest<br>**1334** systest1334 | LOCAL已證 |
| STRESS-07 | LOCAL-untested | N/A(qa無NPA) | **1334** systeststatic(首選,2026-08-18起)<br>**1347** systeststatic | NPA+CPA+TLS-key |
| STRESS-08 | LOCAL-untested | **1119** systest✅ | **1334** systest✅(141)<br>mac:1334 systeststatic✅ | 雙tenant 141已證|
| STRESS-11 | LOCAL-untested | ? | **1331** systest✅✅(141.1.0.2802,REG-02 176,DNS markers[1,1,1]PASS) | DNS-Security+DSE+web/all+blockDnsTCP=false<br>crash ENG-1180143<br>mac:1334 systest(非systest1334)✅(MAC2#53,PR437;見dc→config binding表陷阱) |
| STRESS-13 | ? | 未實測 | **1334** systest1334✅(141,REG261×20) | CPA-only→用1334 |
| STRESS-26 | LOCAL-only | **1119** systest✅ | 1331/1334 systest<br>**1331** systest1331✅(141,LOCAL1 235/236,需PR344) | flood塞爆SSH；must use localtest |
| UPGRADE-01 | REG-only(failclose需VM外下config) | 1118/1119 systeststatic| **1334** systeststatic✅<br>**1331** systeststatic✅<br>**1334** stg1334up(upgrade專用dc) | 需DSE=FALSE |
| UPGRADE-02 | REG-only(code-gated) | 1118(watchdog=false) | **1334** systest✅(137 baseline)<br>**1334** stg1334up(upgrade專用dc) | reboot類 |
| FC-03 | REG-only(code-gated) | 1118 | **1334** systest✅ | reboot類 |
| FC-04 | LOCAL-untested | 1118 | 1334或1331 systeststatic✅(DNS-Security×DSE) | - |
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
