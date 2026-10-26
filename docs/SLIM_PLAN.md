# slim-reader-vowifi 精简重构计划

> 状态：**计划待确认**（阶段 0 未开始，尚未改动任何代码）
> 基线：`761d38e`（MddIdd/mdd-sim-gateway v1.14.0 合并提交）
> 基线测试：`python -m unittest discover -s tests` 在 Python 3.12 下 2015 个测试全部通过；`webui npm run build` 成功（2026-10-26 10:37 CST）

目标设备：OpenStick（MSM8916）Debian 11 arm64，内核 5.15.0-handsomekernel+，内存 382 MB（可用约 286 MB），
swap 191 MB，根分区可用 1.6 GB，读卡器 2ce3:9563（Alcor AK9563，本项目 usbreader.py 已专门处理过的型号）。

---

## 1. 调查结论（会影响方案的事实）

| # | 事实 | 证据 | 影响 |
|---|---|---|---|
| F1 | 引擎容器只用了 4 种隔离：独立网络命名空间（Docker bridge 172.17/16 + NAT）、`/dev/net/tun` + `CAP_NET_ADMIN`、按线路的 bind mount（`/config/instance.json`、`/logs`、`/run/mdd-sim-gateway`、`/run/pcscd`），以及发布到宿主的 RTP UDP 端口段。另有 6 个 IPv6 sysctl（关 RA/autoconf/tempaddr）。 | `control/app/engine.py:436-620` | systemd 用 `NetworkNamespacePath=` + `BindPaths=` + `DeviceAllow=` + `CapabilityBoundingSet=` 可以一一对应 |
| F2 | 原生（非容器栈）模式下的"出口不泄漏"只靠启动前检查：控制面等 orchestrator 报告 `/32 ePDG 路由 → 国家 TUN` 就绪后才启动引擎。sing-box 启动后若退出，TUN 网卡消失，其上的 /32 路由随之消失，IKE 重传会**落回默认路由**。代码里没有 blackhole/unreachable 兜底。 | `host/mdd_orchestrator.py:2878-2980`，`control/app/egress.py:714` | 新方案要比原版更严：每线路独立路由表 + `unreachable default` + nft forward 丢弃，详见 §4.3 |
| F3 | 现有依赖的固定版本在 3.9 上**装不上**：fastapi 0.141.1、uvicorn 0.52.0、websockets 17.1、requests 2.34.2、python-multipart 0.0.32、Pillow 12.3.0 都要求 Python ≥ 3.10。 | `uv pip install --dry-run` on 3.9.25 | 见 §5 Python 决策 |
| F4 | 所有 .py 在 3.9 下**语法**都能编译，但运行期会失败：`engine/outer_transport.py` 的 dataclass 注解 `str \| None` 在 3.9 导入即 `TypeError`（已实测）；`main.py` 有 44 个函数签名用 `X \| None`，FastAPI 会在运行期求值路由参数注解。 | 实测 | 同上 |
| F5 | `control/app/failover.py` **不是**蜂窝故障转移，而是"国家出口节点该不该切换"的判定（HOLD/SWITCH/BACK_OFF）。 | 文件头文档、`main.py:2387-2457` | 建议**保留**（属于国家出口），见待定问题 Q3 |
| F6 | `notify_push` 与引擎回调是两回事：`engine/notify.py`（引擎→控制面 `/api/engine/event`，携带 call_in/registered/tunnel_up 等）必须保留；`control/app/notify_push.py` 只是对外推送，在 main.py 有 39 处调用，可整体移除。 | grep | 符合任务要求 |
| F7 | `media.py` 和 `rtp_forward.py` 都直接依赖 Docker：relay 模式是 coturn 容器 + 内部 Docker 网络；rtp_forward 是为 SOCKS 线路起的转发容器。direct 模式本质只是"Docker 发布 RTP 端口 + Asterisk 把 ICE host 候选改成宿主 LAN 地址"。 | 两个文件头 | 原生下用 nft DNAT 替代端口发布，见 §4.4、Q5 |
| F8 | 线路上限当前是 `config.MAX_SIM_LINES = 10`，由 `tests/test_product_boundaries.py` 强制。 | | 改为可配置、默认 2 |
| F9 | lpac 2.3.0 需要 CMake ≥ 3.31（bullseye 是 3.18）；install.sh 已固定 Kitware 二进制 SHA-256。pcsc-lite 2.3.3 / ccid 1.6.2 用 meson 构建，bullseye 自带 meson 0.56 可能偏旧。 | `install.sh:1794-1800, 89-104` | CI 里用 Kitware cmake + pip 安装的 meson |
| F10 | Debian 11 LTS 已于 2026-08-31 结束，现在（2026-10）bullseye 源可能已迁到 archive.debian.org。 | 公开时间表，需要设备上验证 | install.sh 的"安装运行依赖"可能要处理源；见 Q11 |
| F11 | SMS-over-IMS：网络下发的短信由 `res_pjsip_messaging` 接收，RP-ACK 回送依赖要删的 `mt_rpack_routing` 补丁。删掉后，短信不再只是"不显示"，而是 SMSC 收不到确认，会重发并最终判失败。 | `engine/asterisk-keep-modules.txt`、补丁说明 | 见 Q2 |

---

## 2. 文件处置清单

记号：**K** 保留不动；**M** 修改；**D** 删除；**N** 新增。

### 2.1 control/app

| 文件 | 处置 | 原因 |
|---|---|---|
| main.py (8759 行) | M → 拆分 | 先删功能再拆成 routers（§3） |
| auth.py, authz.py, gate.py | K/M | 基础鉴权保留；authz/gate 中 SMS/MMS/联系人权限项删除 |
| clients.py | 待定 Q4 | 原生客户端 Bearer 令牌（非浏览器） |
| config.py | M | 删 SMS/MMS/保号/通知/更新/模块配置项；`MAX_SIM_LINES` → `settings.max_lines`（默认 2） |
| store.py (3014 行) | M | 只保留 calls、line_states、legacy_history_imports 等通话/线路表；删 messages/sms*/mms*/allowance*/keepalive*/voicemails/contacts* |
| status.py, carrier_id.py, vowifi_support.py, version.py, ami.py, card.py | K/M | status.py 去掉 docker 引用 |
| sim.py, usbreader.py | K | PC/SC 读卡器检测与 PIN |
| engine.py | M（重写后端） | Docker SDK → systemd（经特权助手，§4） |
| runtime.py | M | Docker 事件缓存 → `systemctl show` 轮询缓存 |
| egress.py, egress_contract.py, failover.py | K/M | 国家出口；egress 改为读新 orchestrator 的状态 |
| media.py | M | 只留 direct 模式；relay(coturn) 删除（Q5） |
| rtp_forward.py | M | 改为生成 nft DNAT 规则（不再起容器）（Q5） |
| softphone_ws.py | M | 引擎地址从容器 IP 换成线路 netns 的 veth 地址 |
| lpa.py, estkme.py | K | eSIM |
| operations.py | M | 保留备份/支持包脱敏；删 docker、MMS、容器栈逻辑 |
| sysinfo.py | M | 删 docker 镜像统计，保留 procfs/sysfs 主机健康 |
| line_offline.py | 待定 Q3 | 只为离线通知服务，倾向删除 |
| device_state.py | M | 删模块/4G/飞行模式，只保留读卡器设备视图 |
| cellular_call.py, cellular_sms.py, modem_ims.py, modem_voice.py | D | 蜂窝模块路径 |
| sms_pdu.py, ussd.py, mms.py, mms_convert.py, mms_media.py, mms_pdu.py, mms_staging.py, mms_transport.py, mms_workers.py | D | 短信/彩信/USSD |
| allowance.py | D | 余额与保号 |
| notify_push.py | D | 对外推送 |
| contacts.py | D | 联系人（store.py 对它的引用一并删） |
| update_check.py | D | 自动更新 |
| data/carrier_list.textpb, mcc_country.json | K | carrier_id / 国家出口 |
| control/requirements.txt | M | 删 docker、pyserial（仅 mms_transport 用）、phonenumberslite（仅 contacts 用）、Pillow、pi-heif；`uvicorn[standard]` 视内存改为 `uvicorn + httptools`（不装 uvloop/watchfiles）；补 pycryptodome、card（引擎用） |
| control/run.py | M | 去掉 Docker `/data` 路径翻译；数据目录默认 `/var/lib/mdd` |
| control/Dockerfile | D | |

### 2.2 engine

| 文件 | 处置 | 原因 |
|---|---|---|
| swu_ike.py, pin_keeper.py, ami_usim.py, render.py, outer_transport.py, log_capture.py, notify.py | K/M | render.py 删 SMS/语音留言/USSD 相关渲染；notify.py 的 MANAGER_URL 改为 veth 网关地址 |
| entrypoint.sh | M → `mdd-engine-run` | 作为 systemd 单元的 ExecStart，保留原有"PIN→SWu→P-CSCF→ami_usim→Asterisk"顺序与 supervisor 记录；删 relay 防火墙段 |
| asterisk-keep-modules.txt, templates/modules.conf.j2 | M | 去掉 SMS/USSD/语音留言/MWI/多余编解码与格式（§6） |
| templates/extensions.conf.j2 | M | 删 `[mdd_voicemail]`、`[volte_ims_msg]`、`[msg-from-local]`、`[ussd-report]` 及其引用 |
| 其他 templates/*.j2 | K/M | sounds/vm-beep.wav 删（语音留言） |
| patches/asterisk/ims_public_identity_log.py, reauth_missing_security_server.py, volte_registration_expiry_from_contact.py, volte_sim_timeout_retry.py | K | 任务要求 |
| patches/asterisk/resolve_config_docs.py | K | 任务清单未列；它只把 sysmocom resolve 字段改用 nodoc API，消除启动 ERROR，与保留功能相关（Q7 确认） |
| patches/asterisk/mo_submit_report.py, mt_concat_udh.py, mt_rpack_routing.py, ussd_body_capture.py | D | 任务要求（注意 F11 / Q2） |
| Dockerfile, Dockerfile.overlay | D | 构建配方迁到 build-arm64.yml 的脚本后再删 |

### 2.3 host / tools / 其他

| 路径 | 处置 | 原因 |
|---|---|---|
| host/mdd_orchestrator.py (约 3550 行) | M（大幅删减重写） | 保留：订阅解析、sing-box/Xray 配置生成与进程管理、节点排名/重选/停滞上报、时区；删除：ModemManager/NetworkManager、vpcd 桥、更新请求、服务重启请求、容器相关；新增：线路 netns/策略路由/nft 管理 |
| host/vpcd_modem_bridge.py, modem_probe.py, mdd_update.py, mdd_container_update.py, mdd_image_cleanup.py | D | |
| install.sh (101 KB) | M → 重写 | 只剩 install/status/logs/uninstall（§7） |
| update-policy.json | D | |
| runtime/, drivers/, tools/container-runtime/, tools/drivers/ | D | 容器部署 |
| tools/pve-bind-ec25-modems.sh, tools/pve-usb-diagnose.sh, tools/validate_nas_catalog.py | D | 蜂窝/PVE/NAS |
| tools/engine-fingerprint.sh | D | 只服务于 Docker 镜像复用判断 |
| tools/check-subscriber-identifiers.sh, tools/update_aosp_carrier_data.py, hooks/pre-push | K | 隐私扫描、carrier 数据 |
| patches/ccid/*, patches/lpac/* | K | |
| .github/workflows/issue-triage.yml, drivers.yml, .github/scripts/, .github/codex/ | D | 任务要求 |
| .github/workflows/ci.yml | M | §8 |
| .github/workflows/release.yml | M（实际删空，Q9） | |
| .github/workflows/build-arm64.yml | N | §8 |
| .github/ISSUE_TEMPLATE/nas-compatibility.yml | D | NAS 相关 |
| .github/release-notes/* | K | 历史记录，不影响运行 |
| .dockerignore | D | |
| docs/CONTAINER_DEPLOYMENT*.md, docs/design/container-release-plan.md, issue-108-container-runtime.md, unknown-modem-onboarding.md | D | |
| docs/INSTALL.md, ARCHITECTURE.md, TROUBLESHOOTING.md, DEVELOPMENT.md, RELEASE_CHECKLIST.md, README*.md | M | 按精简版重写/删节 |
| screenshots/keepalive-*, messages-*, sms-* | D | 对应功能已删 |
| LICENSE, NOTICE, THIRD_PARTY_LICENSES.md | K/M | 发布包要附带第三方许可与源码获取说明（§9 风险 R8） |

### 2.4 systemd 与新文件（N）

- `packaging/systemd/mdd-control.service`、`mdd-orchestrator.service`、`mdd-pcscd.service`(+`.socket`)、`mdd-netns@.service`、`mdd-engine@.service`、`mdd-singbox.service`（可能由 orchestrator 直接托管，见 §4）
- `packaging/bin/mdd-enginectl`（唯一的特权入口，见 §4.2）
- `packaging/sudoers.d/mdd`
- `host/mdd_netns.py`（纯函数生成 ip/nft 命令 + 执行器，便于测试）
- `build/`：CI 里用的构建脚本（asterisk、pcsc-lite、ccid、lpac、python、打包）

### 2.5 webui

| 文件 | 处置 |
|---|---|
| views/Softphone.jsx, GlobalSoftphone.jsx, softphone.js, Esim.jsx, VowifiHistory.jsx, Logs.jsx, SimConfig.jsx, SimSelector.jsx | K/M（删联系人名解析、短信入口） |
| views/UnifiedPages.jsx (130 KB) | M：删 CapabilitySwitch 的 4G/飞行模式、ModemVoiceAudioPanel、ModemImsPanel、HardwarePanel 的模块部分、MediaModePanel 的 relay、NotificationsPage、SystemPage 的更新部分 |
| App.jsx | M：删 Messages/Keepalive/Contacts/Notifications 入口与 UpdateModal |
| views/Messages.jsx, MmsSettings.jsx, Keepalive.jsx, AllowancePanel.jsx, Contacts.jsx, contactNames.js, mmsAttachments.js, mmsFormats.js | D |
| tests/mms-*.test.mjs | D；vowifi-history.test.mjs K |
| i18n.jsx | M：删上述功能词条（有 `test_i18n_coverage` 兜底） |
| assets/fonts/Twemoji.Mozilla.ttf (1.4 MB) | 待定：只为短信 emoji 渲染则删 |
| release-dist.SHA256SUMS | D 或重新生成（CI 构建即可，不再需要） |

### 2.6 tests（125 个文件）

- **删除**（对应功能删除）：allowance、cellular_call、cellular_sms、cellular_sms_imsi_fallback、contacts、container_compose、container_update、container_update_space、device_migration、esim_modem_identity、esim_profile_switch（模块 SIM 桥重启）、hardware_runtime、image_cleanup、install_media、install_mms_rule、install_requirements、install_tun_mtu、issue_triage、keepalive、line_offline（若 Q3 删）、media_relay、message_identity、message_reads、mms_*（7 个）、modem_backend、modem_failed_recovery、modem_identity_metadata、modem_ims、modem_probe、modem_voice、nas_catalog、notify_push、pve_bind_script、relay_image_updates、runtime_image_imports、sms_binary、sms_concat、sms_inbound_dialplan、sms_transport、synology_driver_pack、update_apply、update_check、ussd、voicemail、ui_notification_templates、engine_image_refresh、engine_image_layout、container_egress（若 Q6 不保留 SOCKS 状态契约）、run_paths
- **修改**：line_lifecycle、device_state（只剩读卡器）、auto_provision、operations、sysinfo、gate、authz、client_signin、i18n_coverage、engine_manager_url、engine_paths、media_engine、media_control、rtp_forward、softphone_ws、country_egress、stalled_exit、product_boundaries（上限 2、可配置）、ui_initial_loading、ui_softphone_media_preflight、service_codes（只留拨号模式部分）、test_engine_log_hardening 等引用了被删模板段的
- **保留**：swu_ike/pin_keeper/ami_usim/render 相关（dito_vowifi_compat、eap_identity_request、epdg_rekey_selectors、iccid_probe_timeout、rekey_policy、swu_proxy_transport、swu_source_address、engine_reader_binding、engine_restart_evidence、invite_uri_params、line_user_agent、port_allocation、volte_* 补丁测试）、auth、carrier_id、config_cache、esim_*（409/run_coroutine/container_identity/lpa_packaged_binary）、line_history、line_naming、log_capture、notify_urllib_fallback、pin_preflight_message、pre_push_privacy_hook、proxy_node_display、reader_apdu_compat、reader_line_rebind、sim_identity、status_registration、store、vowifi_*、webui_caching、wrong_card_attribution 等
- **新增**：netns/策略路由/nft 规则生成测试、出口断开不泄漏测试（单元 + CI 特权集成）、systemd 单元渲染测试、enginectl 参数校验测试、新 install.sh 的 `sh -n` 与关键函数测试、线路上限配置测试

每删一个功能，先 `grep` 确认其他保留模块不再引用，再删测试；保留功能的测试不允许删、不允许跳过。

---

## 3. main.py 拆分方案

**顺序：先删功能（阶段 2–3），再拆分（阶段 4）。** 删完预计 main.py 降到 4000–4500 行，拆分时移动的代码少一半。

目标结构：

```
control/app/
  main.py            # 只剩 FastAPI 创建、中间件(gate)、静态文件、include_router、lifespan
  state.py           # hub（AMI 客户端、websocket 广播、runtime registry、锁）与共享辅助函数
  tasks.py           # 后台任务：status_poller、card_monitor、host_health_poller、media_supervisor
  routers/
    auth.py          # /api/auth/*（及 clients，若保留）
    lines.py         # /api/instances CRUD、start/stop/status/register/reprovision/pin/country、
                     # /api/provision、/api/ports/suggest、/api/cards、/api/readers、/api/sim/*、
                     # /api/devices（读卡器视图）
    engine.py        # /api/engine/event（引擎回调）、健康/恢复（apply_health 等）
    softphone.py     # /api/instances/{iid}/softphone(+/ws)、call、hangup、calls、/api/media
    esim.py          # /api/esim/*
    egress.py        # /api/egress/*
    logs.py          # /api/instances/{iid}/logs、/api/diagnostics/support-bundle、VoWiFi 历史
    system.py        # /api/settings、/api/system/status|backups|maintenance|host-alerts
```

做法与约束：

1. 先建 `state.py`，把 `hub` 和被多个分组共享的辅助函数移进去，解决循环导入；routers 只依赖 `state` 与业务模块，不依赖 `main`。
2. 每次只移一个 router：代码原样搬移 → `app.include_router` → 修正测试里 `patch.object(main, ...)` 的目标为新模块 → 跑全量 pytest。测试大量 patch `main.xxx`，这是拆分的主要工作量，也是唯一允许为拆分而改测试的地方（只改 patch 目标，不改断言）。
3. URL、方法、请求/响应结构保持不变，WebUI 不需要因拆分改动。
4. 路由总数用一个测试固定（拆分前后 `app.routes` 的 (path, methods) 集合一致）。

---

## 4. systemd 架构

### 4.1 进程与单元

| 单元 | 身份 | 内容 | 内存上限（初值，实测后调整） |
|---|---|---|---|
| `mdd-pcscd.service` | root | 源码构建的 pcsc-lite 2.3.3 + 带补丁 ccid；屏蔽发行版 pcscd | 16M |
| `mdd-orchestrator.service` | root | 精简后的 orchestrator：sing-box / 可选 Xray 进程、订阅、节点选择、线路 netns 与 nft/策略路由 | 主进程 48M；sing-box 子进程单独计入，整体 `MemoryMax=128M` |
| `mdd-control.service` | `mdd` 用户（非 root） | uvicorn HTTPS :8443，WebUI + API | 112M |
| `mdd-netns@<line>.service` | root，oneshot | 创建 `/run/netns/mdd-<line>`、veth、地址、sysctl | — |
| `mdd-engine@<line>.service` | root（能力受限） | 一个单元里跑 pin_keeper + swu_ike + ami_usim + asterisk（沿用 entrypoint 的顺序与监督） | 160M |

全局：`MALLOC_ARENA_MAX=2`、`PYTHONDONTWRITEBYTECODE` 不设（字节码预编译进包）、`LogLevelMax=info`。

### 4.2 控制面怎么启停引擎

推荐：控制面以非 root 运行，只能通过 `sudo -n /opt/mdd/bin/mdd-enginectl {start|stop|restart|status} <line>` 操作；sudoers 只放行这一个文件。`mdd-enginectl` 校验线路 ID（`^[0-9]{1,3}$`）且线路在配置内，再调用 `systemctl`。

理由：bullseye 的 polkit 是 0.105，只支持 `.pkla`，不能按单元名限制权限；直接给 `mdd` 用户 systemctl 权限等于给 root。面向浏览器的进程不以 root 运行，是这个方案最主要的安全收益。代价：多一个 root 常驻的 orchestrator（约 25–30 MB）。D-Bus 方案需要在 venv 里装 dbus 绑定（要编译），收益不明显，不采用。

状态读取：`runtime.py` 改为每 3 s 一次 `systemctl show -p ActiveState,SubState,ExecMainStatus,NRestarts,ActiveEnterTimestampMonotonic mdd-engine@*`（只读，不需要 sudo），替代 Docker 事件流。

### 4.3 网络隔离（替代 Docker bridge）与"出口不通时不泄漏"

每条线路 N（1..max_lines）：

```
netns mdd-N:   eth0 10.88.N.2/30  default via 10.88.N.1
               ipsec0 (swu_ike 在 netns 内自建的 TUN，IMS 内层流量)
host:          mdd-hN 10.88.N.1/30
               ip rule  pref 1000+N  iif mdd-hN            lookup 1000+N
               ip rule  pref 900+N   iif mdd-hN fwmark 0x8N lookup main   # 只放行 DNAT 进来的 RTP 回包
               table 1000+N:
                 代理模式: default dev tun-<cc>           (tun 消失时该路由随之消失)
                           unreachable default metric 4000 (兜底，永远存在)
                 直连模式: default via <上游网关> dev <上游>
nft table inet mdd:
  forward: iifname mdd-hN ct mark 0x8N accept                # RTP 回包
           iifname mdd-hN oifname tun-<cc> accept           # 代理模式唯一出口
           iifname mdd-hN drop                               # 其余一律丢弃（第二道闸）
  postrouting: 直连模式 iifname mdd-hN oifname <上游> masquerade；代理模式不做 masquerade
  prerouting: udp dport <线路 N 的 RTP 段> dnat to 10.88.N.2, ct mark set 0x8N
```

- 两道闸：路由层（专用表里 `unreachable` 兜底）+ 过滤层（forward 只放行到本国 TUN）。任何一层失效，另一层仍然阻断。
- 引擎不做外部 DNS：ePDG 由 orchestrator 解析（与原版相同，走宿主 DNS），解析结果写入线路的渲染参数，引擎直接连 IP；netns 内不配置 resolv.conf 上游。
- 控制面 → 引擎（AMI 5038、Asterisk WS 8088）走 `10.88.N.2`，属于宿主本地发起的连接，回包在 netns 内是直连路由，不受上述策略影响。
- 线路的 IPv6：沿用容器里的 6 个 sysctl；veth 不配置 IPv6 全局地址，IPv6 只存在于 ipsec0 内层。
- 引擎单元：`NetworkNamespacePath=/run/netns/mdd-%i`、`BindsTo=mdd-netns@%i.service`、`DeviceAllow=/dev/net/tun rw`、`CapabilityBoundingSet=CAP_NET_ADMIN CAP_NET_RAW CAP_NET_BIND_SERVICE CAP_SYS_NICE CAP_SETUID CAP_SETGID`、`ProtectSystem=strict`、`ProtectHome=yes`、`PrivateTmp=yes`。
- 文件布局沿用容器内路径，减少 render.py/entrypoint 改动：`BindPaths=/var/lib/mdd/instances/%i/run:/run/mdd-sim-gateway`、`.../logs:/logs`、`.../instance.json:/config/instance.json`（只读）、`TemporaryFileSystem=/etc/asterisk /var/spool/asterisk`、`.../astdb:/var/lib/asterisk/astdb-dir`，每条线路的 Asterisk 运行目录互不相干。
- 测试：
  1. 单元测试：`host/mdd_netns.py` 生成的命令/规则集合（代理模式必须有 unreachable 兜底、forward drop、无 masquerade；直连模式有 masquerade；规则对线路 N 互不交叉）。
  2. 特权集成测试（CI 单独 job，`sudo`，GitHub runner 支持 netns）：建 netns + veth + 用 `ip tuntap` 模拟国家 TUN + dummy 上游网卡；删除 TUN 后从 netns 发 UDP 500/4500，断言 `ip route get` 返回 unreachable、上游网卡 nft 计数为 0、包没有从上游离开。非 root 环境自动跳过（只在这个 job 里跑）。

### 4.4 媒体（浏览器软电话）

只保留 direct 模式：每条线路的 RTP 段由 nft DNAT 到 netns，Asterisk 的 ICE host 候选仍改写为宿主 LAN 地址（rtp.conf.j2 现有逻辑）。relay(coturn) 模式与转发容器删除；`rtp_forward.py` 改为生成上述 DNAT 规则（Q5）。

---

## 5. Python 版本决策：打包 python-build-standalone 3.12

结论：在 CI 里把 python-build-standalone 3.12（aarch64-unknown-linux-gnu，install_only_stripped）解压到 `/opt/mdd/python`，并在 **同一路径** 建 `/opt/mdd/venv` 后安装依赖，一起打进发布包；系统 Python 3.9 不碰。

理由（均已在本仓库实测）：

1. 当前固定版本的 fastapi/uvicorn/websockets/requests/python-multipart 都要求 ≥ 3.10（F3）。留在 3.9 意味着把 Web 栈整体降级到旧版本，其中包括有安全修复的版本，且 API 有差异。
2. 运行期不兼容：`outer_transport.py` 在 3.9 导入即失败；FastAPI 在运行期求值 44 处 `X | None` 签名（F4）。逐个改写可以做到，但 swu_ike.py（36 万字节）等核心路径此前一直在 3.12+ 上运行和测试，回退到 3.9 的行为差异没有测试覆盖。
3. 3.9 已于 2025-10 结束上游支持，Debian 11 本身也已停止 LTS。

代价：发布包增加约 45–60 MB（解压后，删 test/idlelib/tkinter/ensurepip 后估算），内存无差别。glibc：PBS 的 aarch64 构建目标 glibc 2.17，bullseye 为 2.31，兼容。pyscard、cryptography 等在 bullseye 容器内构建/安装，和设备 glibc 一致。

---

## 6. Asterisk 精简

- 编译期：保持 sysmocom 参考构建的 configure 参数，`menuselect --disable-all` 后只 `--enable` keep 列表里的模块（这一点偏离"与参考构建逐字节一致"的原则，换来更短的编译与更小的产物；构建后仍按 keep 列表数量校验，缺一个即失败）。
- keep 列表删除候选：`res_pjsip_messaging`（取决于 Q2）、`func_base64`、`func_cut`（仅 SMS）、`func_channel`（仅 USSD 钩子，需再确认挂断处理是否依赖）、`app_record`、`app_playback`（仅语音留言，需确认无其他提示音）、`res_pjsip_send_to_voicemail`、`res_pjsip_mwi*`、`res_mwi_devstate`、`res_pjsip_one_touch_record_info`、多余编解码（adpcm/g726/gsm/ilbc/lpc10/speex）与 format_*（只留 wav/pcm/sln），以及 pidf/xpidf/dialog_info/publish 系列（需确认软电话 JsSIP 不依赖 presence）。每删一个都在模板里 grep 引用，并在 CI 构建后用 `asterisk -rx "module show"` 校验加载。
- `modules.conf.j2` 改为 `autoload=no` + 显式 `load =`，与 keep 列表由同一个测试保证一致。
- 日志：`logger.conf.j2` 默认 console/messages 为 `notice,warning,error`（INFO 以下不写）；`verbose=0`。

---

## 7. 新 install.sh

子命令：`install`、`status`、`logs [control|orchestrator|engine <N>|pcscd]`、`uninstall [--purge]`。

`install <mdd-slim-arm64-x.y.z.tar.gz>`：

1. 检查 `uname -m = aarch64`、`/dev/net/tun` 为字符设备、内核支持 netns/veth/nftables（`ip netns add` 试建后删除、`nft list ruleset`）、systemd ≥ 247。
2. 安装运行依赖（apt，只装运行库：libjansson4、libxml2、libsqlite3-0、libedit2、libuuid1、libsrtp2-1、libopencore-amr*、libvo-amrwbenc0、libspeex*（若保留）、libunbound8、libcurl4、libusb-1.0-0、libudev1、nftables、iproute2、ca-certificates、sudo）；源不可用时给出明确提示（F10，Q11）。
3. 校验包内 SHA256SUMS 后解压到 `/opt/mdd/<version>`，`/opt/mdd/current` 软链接切换。
4. 创建 `mdd` 系统用户、`/var/lib/mdd`（0700，属主 `mdd`）。
5. 屏蔽发行版 `pcscd.service/pcscd.socket`，安装 sudoers、systemd 单元，`daemon-reload`，启用 pcscd/orchestrator/control。
6. 没有 zram 时启用 zram（优先用 `zram-tools`/zramswap；没有该包时写一个 oneshot 单元 `modprobe zram` + `zramctl` + `mkswap/swapon`，大小 = 50% 内存、lz4/lzo-rle）。已有 swap 不删。
7. 打印 `https://<LAN IP>:8443` 和首次管理员设置提示。

`uninstall` 默认保留 `/var/lib/mdd`，`--purge` 才删除；恢复发行版 pcscd 屏蔽状态。

---

## 8. CI

- `ci.yml`：触发 `push`/`pull_request` 于 `slim-reader-vowifi`（另保留 `workflow_dispatch`）；job：python（3.12，pytest/unittest 全量 + py_compile + `sh -n`）、privacy（订阅者标识扫描）、webui（npm ci + build + audit）、netns-leak（sudo 集成测试）。删除 control-image/engine-image 两个 Docker job。
- `build-arm64.yml`（workflow_dispatch，输入 version，默认读 VERSION）：`runs-on: ubuntu-24.04-arm`，`container: debian:bullseye`；步骤：
  1. pcsc-lite 2.3.3（SHA-256 校验）、ccid 1.6.2 + patches/ccid（SHA-256 校验），meson 用 pip 固定版本
  2. sysmocom pjproject + Asterisk（固定 commit，同现 Dockerfile）+ 保留的 5 个补丁 + 精简 menuselect，strip
  3. lpac 2.3.0（固定 commit）+ patches/lpac，Kitware CMake（SHA-256 校验）
  4. python-build-standalone 3.12（SHA-256 校验）→ `/opt/mdd/venv` 安装固定依赖
  5. 下载 sing-box 1.13.15、Xray 26.3.27 arm64 并校验 SHA-256（沿用 install.sh 里的固定值）
  6. WebUI 构建（容器内装 Node 22 官方二进制并校验，或在 job 前一步于宿主 runner 构建后传入）
  7. 冒烟：`asterisk -V`、按 keep 列表数模块、`ldd` 无 not found、`python -c "import ..."`、`lpac version`
  8. 打包 `mdd-slim-arm64-<version>.tar.gz` + `SHA256SUMS` + 第三方许可，`actions/upload-artifact`
  不发 Release、不推 GHCR。
- `release.yml`：其中所有 job（引擎/控制/运行时镜像、驱动包）都是推 GHCR 或发 Release，删完不剩内容，建议整个文件删除（Q9）。

---

## 9. 风险

| # | 风险 | 应对 |
|---|---|---|
| R1 | 内存：估算（未实测）pcscd 3 + sing-box 30–40 + orchestrator 25–30 + control 60–80 + 每线路（asterisk 25–40、swu_ike 25–35、ami_usim 约 20、pin_keeper 约 15）≈ 90–110 MB。1 条线路约 210–260 MB，2 条约 300–370 MB，超过当前可用 286 MB。 | zram + MemoryMax + MALLOC_ARENA_MAX；第一次构建后在设备上实测 RSS，再决定默认值是否需要从 2 降到 1 或合并 pin_keeper/ami_usim 进程 |
| R2 | 内核特性未确认：handsomekernel 是否开启 NET_NS、VETH、NF_TABLES/NFT_NAT/NFT_MASQ、IP_MULTIPLE_TABLES、MEMCG、ZRAM。缺 MEMCG 则 MemoryMax 无效；缺 netns/veth 则 §4.3 整体不可行。 | 需要设备输出（Q10） |
| R3 | sysmocom Asterisk 在 bullseye（gcc 10、OpenSSL 1.1.1）上从未构建过，原 Dockerfile 用的是 Fedora 44。 | 先单独跑通 build-arm64 的 Asterisk 步骤再做后续 |
| R4 | main.py 拆分时大量测试 patch `main.xxx`，移动后 patch 失效但测试可能"假通过"或失败。 | 每个 router 一步、只改 patch 目标；加路由集合一致性测试 |
| R5 | 删除 SMS 后网络侧行为（F11）。 | Q2 |
| R6 | 一个单元里 4 个进程，任一崩溃的处理沿用 entrypoint 逻辑（swu_ike 内部重启，Asterisk 退出则整个单元退出由 systemd `Restart=on-failure` 拉起）；需要确认 systemd 停止时进程组全部退出（`KillMode=control-group`）。 | 单元测试检查单元文件 + 设备实测 |
| R7 | Debian 11 已停止支持，apt 源可能失效（F10）。 | Q11；install.sh 检测并提示，不擅自改源 |
| R8 | 许可：发布包含 Asterisk/pjproject（GPL-2.0）、lpac（AGPL-3.0）、sing-box（GPL-3.0+）、Xray（MPL-2.0）、pcsc-lite/ccid（BSD/LGPL）二进制。分发二进制需附许可并提供对应源码获取方式。 | 包内带 THIRD_PARTY_LICENSES + 每个组件的上游 URL、固定 commit 和本仓库补丁 |
| R9 | ubuntu-24.04-arm 托管 runner 对公共仓库免费；若 fork 为私有仓库需要付费或不可用。 | Q12 |
| R10 | 现有 Docker 版安装的数据（config.yaml、SQLite）不做迁移。 | Q8 |

---

## 10. 待定问题

- **Q1 分支**：会话设定的开发分支是 `claude/nifty-tesla-7e6qmr`，你要求的是 `slim-reader-vowifi`。我的理解是以你为准：从 `761d38e` 新建 `slim-reader-vowifi` 并推送到它。请确认；如果推送被代理拒绝（只允许 `claude/` 前缀），退而在 `claude/nifty-tesla-7e6qmr` 上开发，CI 触发分支相应调整。
- **Q2 短信删除后的网络侧处理**：(a) 完全删除 `res_pjsip_messaging`，网络下发的短信会被拒收，SMSC 重发直至失败；(b) 保留模块和一条最小拨号计划，只回 200/RP-ACK、丢弃内容、不入库（需要保留 `mt_rpack_routing` 补丁或等价逻辑）。我倾向 (b)，代价是多保留一个补丁。
- **Q3** `failover.py` 属于国家出口节点切换，建议保留；`line_offline.py` 只服务于离线通知，建议随 notify_push 删除。是否同意？
- **Q4** `clients.py`（原生客户端 Bearer 令牌登录）保留还是删除？只用浏览器的话可删。
- **Q5** 媒体只保留 direct 模式，删除 coturn relay 模式；`rtp_forward.py` 改写为 nft DNAT 规则生成（不再是转发容器）。是否同意？
- **Q6** 国家出口在原生下只实现 TUN 模式（§4.3）。SOCKS5 传输（`outer_transport.py` + `egress_contract.py`）代码保留但不接入，还是也做成可选模式？我倾向保留代码、暂不接入。
- **Q7** `patches/asterisk/resolve_config_docs.py` 不在你的清单里，我建议保留（理由见 §2.2）。
- **Q8** 不做从 Docker 版的数据迁移，只支持全新安装。是否同意？
- **Q9** `release.yml` 删完推 GHCR/发 Release 后不剩任何 job，是否整个文件删除？
- **Q10** 请在设备上执行并贴回：
  ```
  zcat /proc/config.gz 2>/dev/null | grep -E 'CONFIG_(NET_NS|VETH|TUN|NF_TABLES|NFT_NAT|NFT_MASQ|NFT_CT|NF_TABLES_INET|IP_MULTIPLE_TABLES|IP_ADVANCED_ROUTER|MEMCG|ZRAM|ZSMALLOC|CRYPTO_LZ4|CRYPTO_LZO|XFRM_USER)=' || ls /boot/config-*
  cat /sys/fs/cgroup/cgroup.controllers; stat -fc %T /sys/fs/cgroup
  cat /proc/swaps; lsmod | grep -E 'zram|veth|nf_tables'
  systemctl --version | head -1
  ip netns add t && ip link add t0 type veth peer name t1 && ip link del t0; ip netns del t; nft list ruleset | head
  lsusb -v -d 2ce3:9563 2>/dev/null | grep -E 'bInterfaceClass|iProduct'
  lsusb -v -d 349c:0418 2>/dev/null | grep -E 'bInterfaceClass|iProduct|iManufacturer'
  dpkg -l | grep -E 'pcscd|libccid|libpcsclite' ; cat /etc/apt/sources.list
  ```
- **Q11** `apt-get update` 在设备上是否还能正常工作？如已切到 archive.debian.org，install.sh 只检测并提示，不自动改源，是否可以？
- **Q12** fork 仓库是公开还是私有（决定 ubuntu-24.04-arm runner 能否使用）？
- **Q13** Xray 作为可选组件：打进主包但默认不启用，还是单独出一个 `mdd-slim-xray-arm64-<version>.tar.gz`？我倾向单独打包（主包省约 30 MB）。
- **Q14** 控制面以非 root `mdd` 用户运行 + sudo 只放行 `mdd-enginectl`（§4.2），多一个约 25–30 MB 的 root orchestrator。若内存紧张，可合并为一个 root 进程。先按分离方案做，是否同意？

---

## 11. 阶段与进度

每个阶段结束：全量测试 + `npm run build` 通过后提交；失败先修再进入下一阶段。

- [ ] 阶段 0：建分支、提交本计划、记录基线
- [ ] 阶段 1：删外围（workflows：issue-triage/drivers、.github/scripts、.github/codex、runtime/、drivers/、tools/container-runtime、tools/drivers、tools/pve-*、update-policy.json、NAS 模板、相关测试/文档）
- [ ] 阶段 2：删功能——通知推送、自动更新、余额/保号、联系人、语音留言（control + webui + i18n + 测试）
- [ ] 阶段 3：删功能——短信/彩信/USSD（含 Asterisk 补丁、模板、store 表）
- [ ] 阶段 4：删蜂窝模块路径（cellular_*、modem_*、device_state 模块逻辑、host/vpcd_modem_bridge、host/modem_probe、orchestrator 的 MM/NM 部分、4G/飞行模式 UI）
- [ ] 阶段 5：拆分 main.py（state.py → 逐个 router）
- [ ] 阶段 6：线路上限可配置（默认 2）
- [ ] 阶段 7：systemd 后端——mdd_netns.py + 泄漏测试、engine.py/runtime.py/media/rtp_forward/softphone_ws 改造、orchestrator 去 Docker/去模块、enginectl、单元文件、engine 运行脚本
- [ ] 阶段 8：Asterisk 模块/模板精简、日志级别
- [ ] 阶段 9：新 install.sh（含 zram）
- [ ] 阶段 10：CI——ci.yml、build-arm64.yml、release.yml 处理；删 Dockerfile/compose
- [ ] 阶段 11：文档（README、INSTALL、ARCHITECTURE、TROUBLESHOOTING）
- [ ] 阶段 12：在设备上实测（需要你执行），按实测调整 MemoryMax 和默认线路数
