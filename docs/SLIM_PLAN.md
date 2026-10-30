# slim-reader-vowifi 精简重构计划

> **工作分支：`slim-reader-vowifi`**（推送实测可用；`claude/nifty-tesla-7e6qmr` 只保存了计划的最初两次提交）
> 状态：计划已确认（2026-10-26），按 §11 分阶段执行；会话中断后从 §11 第一个未勾选的阶段继续
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
| F8 | 线路上限当前是 `config.MAX_SIM_LINES = 10`，由 `tests/test_product_boundaries.py` 强制。 | | 改为可配置、默认 1（D3） |
| F9 | lpac 2.3.0 需要 CMake ≥ 3.31（bullseye 是 3.18）；install.sh 已固定 Kitware 二进制 SHA-256。pcsc-lite 2.3.3 / ccid 1.6.2 用 meson 构建，bullseye 自带 meson 0.56 可能偏旧。 | `install.sh:1794-1800, 89-104` | CI 里用 Kitware cmake + pip 安装的 meson |
| F10 | Debian 11 LTS 已于 2026-08-31 结束，现在（2026-10）bullseye 源可能已迁到 archive.debian.org。 | 公开时间表，需要设备上验证 | install.sh 的"安装运行依赖"可能要处理源；见 Q11 |
| F12 | 设备实测（2026-10-26）：`CONFIG_MEMCG=y`，cgroup2 已启用 memory 控制器 → `MemoryMax` 有效；`CONFIG_NET_NS=y`，`ip netns add` 实测成功；`VETH`/`TUN`/`NF_TABLES`/`NFT_NAT`/`ZRAM` 均为模块（=m）；系统未安装 `nft` 命令；已有 `/dev/zram0` 191 MB swap；349c:0418 是 U 盘类大容量存储设备，不是读卡器（目前只有 1 个 SIM 读卡器）；apt 走阿里云 bullseye 源，可用，security 源被注释掉 | 设备输出 | install.sh：`modprobe` 并写 `/etc/modules-load.d/mdd.conf`；安装 nftables；检测到已有 zram 则跳过；当前硬件只能跑 1 条线路 |
| F13 | 设备在中国大陆网络。浏览器软电话 direct 模式不用 STUN/TURN（`iceServers: []`），不受影响；ePDG 用宿主 DNS 解析（`host/mdd_orchestrator.py:2850`），境内解析器可能返回被污染的或不同的结果；订阅拉取已有"经出口"选项；出口探测 `gstatic.com/generate_204` 经出口访问 | grep | 建议默认开启国家出口、ePDG 经出口解析（Q15）；设备本身不访问 GitHub，发布包在电脑上下载后 scp 到设备 |
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
| config.py | M | 删 SMS/MMS/保号/通知/更新/模块配置项；`MAX_SIM_LINES` → `settings.max_lines`（默认 1，可改 2，D3） |
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
| R2 | 内核特性已全部确认（§10 D-设备）。`nft_fib_inet` 不可用，nftables 为 0.9.8。 | nft 规则禁止 fib 表达式与 0.9.8 之后的语法，加测试检查生成的规则集 |
| R3 | sysmocom Asterisk 在 bullseye（gcc 10、OpenSSL 1.1.1）上从未构建过，原 Dockerfile 用的是 Fedora 44。 | 先单独跑通 build-arm64 的 Asterisk 步骤再做后续 |
| R4 | main.py 拆分时大量测试 patch `main.xxx`，移动后 patch 失效但测试可能"假通过"或失败。 | 每个 router 一步、只改 patch 目标；加路由集合一致性测试 |
| R5 | 删除 SMS 后网络侧行为（F11）。 | Q2 |
| R6 | 一个单元里 4 个进程，任一崩溃的处理沿用 entrypoint 逻辑（swu_ike 内部重启，Asterisk 退出则整个单元退出由 systemd `Restart=on-failure` 拉起）；需要确认 systemd 停止时进程组全部退出（`KillMode=control-group`）。 | 单元测试检查单元文件 + 设备实测 |
| R7 | Debian 11 已停止支持，apt 源可能失效（F10）。 | Q11；install.sh 检测并提示，不擅自改源 |
| R8 | 许可：发布包含 Asterisk/pjproject（GPL-2.0）、lpac（AGPL-3.0）、sing-box（GPL-3.0+）、Xray（MPL-2.0）、pcsc-lite/ccid（BSD/LGPL）二进制。分发二进制需附许可并提供对应源码获取方式。 | 包内带 THIRD_PARTY_LICENSES + 每个组件的上游 URL、固定 commit 和本仓库补丁 |
| R9 | ubuntu-24.04-arm 托管 runner 对公共仓库免费；若 fork 为私有仓库需要付费或不可用。 | Q12 |
| R10 | 现有 Docker 版安装的数据（config.yaml、SQLite）不做迁移。 | Q8 |

---

## 10. 已确认的决定（2026-10-26，以此为准）

- **D1 分支**：`slim-reader-vowifi`。不推 main、不推 tag、不改仓库设置。
- **D2 网络下发短信**：只回 RP-ACK、丢弃内容。从 `mt_rpack_routing` 只保留实现确认所需的最小部分。测试要证明：短信被确认、内容不落盘、不出现在 UI 与 API。
- **D3 线路上限**：默认 1，配置可改为 2；设备实测内存后再定。
- **D4** 保留 `failover.py`（出口节点切换）；删除 `line_offline.py`（离线通知）；删除 `clients.py`（原生客户端令牌）；只保留 direct 媒体模式，删 relay/coturn，`rtp_forward.py` 改写为 nft DNAT 规则生成；`release.yml` 整个删除；Xray 单独打包（`mdd-slim-xray-arm64-<version>.tar.gz`）；不做 Docker 版数据迁移；保留 `resolve_config_docs.py`；控制面非 root + sudo 只放行 `mdd-enginectl`。
- **D5 出口能力**（不要与容器栈的 "socks5 transport" 混淆）：代理库里**订阅、手动粘贴单个节点、手动 SOCKS5** 三种上游都保留（`host/mdd_orchestrator.py` 的 `parse_share_link`/`parse_manual_outbound`/订阅解析）。保留出口 UDP 健康检查（IKE 用 UDP 500/4500）和"出口不通即阻断对应 VoWiFi 线路"。容器栈专用的 SOCKS transport 状态契约不接入原生路径，`outer_transport.py`/`egress_contract.py` 代码保留。
- **D6 中国内地网络**：设备安装与运行不依赖 GitHub/PyPI/npm。sing-box、Xray、lpac、python-build-standalone 3.12 与全部 Python 依赖打进发布包；install.sh 只用 apt 装系统依赖，其余从本地 tarball 离线安装。默认开启国家出口；ePDG 经该国出口解析。install.sh 检查 NTP 同步，未同步时提示（建议 ntp.aliyun.com），不改系统配置。不修改 `/etc/apt/sources.list`。
- **D7 设备**：内核 5.15.0-handsomekernel+；CGROUPS/MEMCG/NET_NS/IP_ADVANCED_ROUTER/IP_MULTIPLE_TABLES=y；NF_TABLES、NFT_NAT、NFT_CT、NFT_MASQ、NFT_REJECT、NF_CONNTRACK、NF_NAT、TUN、VETH、ZRAM 为模块；实测可 modprobe：veth tun nf_tables nft_nat nft_chain_nat nft_ct nft_reject_inet nft_masq；**nft_fib_inet 不可用**；nftables 0.9.8；cgroup2 控制器 cpuset cpu io memory hugetlb pids；已有 /dev/zram0 约 191 MB；读卡器 2ce3:9563 bInterfaceClass 11（CCID），pcscd 显示 "Alcor Link AK9563"，Debian ccid 1.4.34 可识别；卡为 ESTKme Plus 实体 eUICC，最多 6 条逻辑通道；349c:0418 是 U 盘，忽略。
- **D8 由设备带来的要求**：
  - nft 规则不用 fib 表达式、不用 0.9.8 之后的语法。
  - install.sh 预加载上述模块并写 `/etc/modules-load.d/mdd.conf`，uninstall 删除。
  - 已有 zram 则跳过。
  - pcscd：install 时停用并 mask 系统 `pcscd.socket`/`pcscd.service`，卸载 apt 的 pcscd、libccid（保留 pcsc-tools），装固定版本 pcsc-lite 2.3.3 + ccid，由自己的单元启动；uninstall 恢复原状。
  - ccid：确认固定版本源码 Info.plist 含 2ce3:9563，含则不为它加补丁；保留现有 patches/ccid（含 SCR Prime）。
  - eSIM：保留 estkme.py 与 lpac 路径，确认在直接 PC/SC 读卡器模式（不经 vpcd 模块桥）下可用，并补测试。

## 11. 阶段与进度

规则：每阶段结束跑全量测试 + `npm run build`，全部通过后提交、推送，并更新本节。删除前先 grep 引用；`engine/notify.py` 与 `/api/engine/event`（来电、注册状态等）必须保留。遇到会改变已确认架构的问题先停下报告。

- [x] 阶段 0：建分支 `slim-reader-vowifi`、写入已确认决定、记录基线（2015 测试通过、WebUI 构建成功）
- [x] 阶段 1：删除短信/彩信/USSD（保留 D2 的最小 RP-ACK）、余额保号、通知推送、语音留言、联系人、自动更新，连同测试、UI、i18n
- [x] 阶段 2：删除蜂窝模块路径与全部容器部署文件（runtime/、Dockerfile、compose、drivers/、相关 tools 与 workflow）
- [x] 阶段 3：拆分 main.py 为 routers（每拆一块跑一次测试）
- [x] 阶段 4：systemd 替换 Docker 编排——引擎、netns 隔离、出口防泄漏、RTP DNAT，补测试（CI 两个 job 待首次上设备后补）
- [~] 阶段 5：重写 install.sh（install/status/logs/uninstall）与 systemd 单元（已提前完成，待设备验证）
- [~] 阶段 6：CI——ci.yml 在工作分支 push/PR 上跑；新增 build-arm64.yml；删除 release.yml（build-arm64.yml 已提前加入；release.yml 未删）
- [ ] 阶段 7（需要你在设备上执行）：实测内存与功能，调整 MemoryMax 与线路上限

### 进度日志
- 2026-10-26 阶段 0 完成。
- 2026-10-26 阶段 1 完成（提交 b35c9c3 自动更新、bf88ada 语音留言、c6851fd 余额保号、8b6264f 联系人、1af09cf 短信/彩信/USSD、1833903 通知推送与客户端令牌）。全量测试 1298 个通过，WebUI 构建通过。
  - 实际删除顺序与计划不同：先删依赖 notify_push 的功能，最后删 notify_push，避免为马上要删的测试做无用修改。
  - D2 实现：`[volte_ims_msg]` 保留一个只 `Hangup()` 的通配分机（sysmocom 只在消息有去处时才回 200 + RP-ACK）；`mt_rpack_routing.py` 整个保留（三处修正都是确认能送达所必需）；`mo_submit_report`/`mt_concat_udh`/`ussd_body_capture` 删除，已在 sysmocom 源码副本上验证剩余 6 个补丁能顺序打上。测试见 `tests/test_mt_sms_discard.py`。
  - 顺带：`cellular_call.py` 依赖 `cellular_sms`，提前在阶段 1 删除；`clients.py`（D4）在本阶段删除；SMSC 不再是建线路的必填项；store.py 重写为只含 calls 与 line_states（不做旧库迁移，D4）。
  - 待后续阶段处理：Asterisk 的 DEBUG 日志会打印 RP-DATA 十六进制（含短信正文），阶段 4/5 必须保证 Asterisk 默认不开 debug、`pjsip set logger` 关闭；`i18n` 中 "Responsible use notice" 仍写"最多十条线路"，阶段 4 改线路上限时一并修改；软电话页里的"模块通话"UI 和 `api.cellularCall*` 留到阶段 2 随蜂窝路径删除；`sim.py`/卡片信息里仍读取 SMSC（只读、无害）。
- 2026-10-26 阶段 2 完成（提交 6928864 容器部署文件、b18256e 蜂窝模块路径）。全量测试 976 个通过，WebUI 构建通过。
  - 删除：`host/vpcd_modem_bridge.py`、`host/modem_probe.py`、`control/app/modem_ims.py`、`control/app/modem_voice.py`，模块相关接口（cellular、ims、voice-audio、usb-candidates、modem-profiles），设置里的 `hardware`/`device_defaults`（config.load 会丢弃旧文件里的这两个键），WebUI 的 4G/飞行模式/模块 IMS/通话音频/模块拨号，以及对应测试与 92 条 i18n。
  - orchestrator 只保留出口（sing-box/xray）、路由、时区与服务重启；阶段 4 再按 netns 方案改写。
  - 留给后续阶段：设置页"维护/存储"里仍有 Docker 镜像与构建缓存相关的 UI 和后端字段（阶段 4/5 随 systemd 化删除）；`main.py` 里 `msisdn_source == "modemmanager"` 的旧记录兼容逻辑仍在（无害，读卡器不会产生该来源，阶段 3 拆分时再评估）；`test_operations` 中 host-diagnostics 的测试数据仍是模块字段（只测透传与脱敏），阶段 4 改写 host-diagnostics 时一起更新；MediaModePanel 的 relay 模式留到阶段 4。
- 2026-10-26 插入的两个单独提交：6f912d4 ci.yml 的 push/PR 触发加上 `slim-reader-vowifi`；9fcbe5c WebUI 的 vite 固定为 6.4.4。原因：CI 的 npm audit 报 vite <=6.4.3 的 high 级公告 GHSA-587h-7wmj-h5pm 等。vite 是直接的 devDependency，lock 文件与上游 761d38e 相同，不是本分支引入的。lock 只改了 vite 一项。
- 2026-10-26 阶段 3 进行中（main.py 5002 → 2391 行），每个 router 单独提交，提交前全量测试通过（978 个）：
  - 已拆出：
    - e1b8942 auth：同时加入 `tests/test_route_set.py`，固定拆分前的 (path, method) 集合；加入 `tests/route_listing.py`。FastAPI 0.141 的 `app.routes` 里，被 include 的 router 只占一项、不展开，所以 authz、路由集合、无短信接口这三个测试要遍历进去。
    - a879084 esim，并新建 state.py
    - 9b47915 egress
    - 43824f8 logs
    - cc04b7f system
    - f6ef110 softphone
    - 85107f4 engine
  - 还剩，按顺序：
    1. lines：/api/instances、/api/provision、/api/cards、/api/readers、/api/sim/*、/api/ports/suggest、/api/devices
    2. tasks.py：card_monitor、status_poller、host_health_poller、media_supervisor 及只被它们用的辅助函数，例如 msisdn 学习、线路状态记录、媒体收敛
    3. 收尾：main.py 只留应用创建、gate 中间件、审计中间件、include_router、lifespan、静态 WebUI；删除 tools/slim/；在 SLIM_PLAN 中标记阶段 3 完成。
  - 拆分方法：
    - 用 `tools/slim/` 里的临时生成器（说明见其 README），每一步都从 `tools/slim/main_source.py` 重新生成全部模块。
    - 跨模块引用一律写成 `app_state.名字`、`<router>_routes.名字`，所以 patch 定义它的模块就能作用到所有调用方，main.py 里不留兼容别名。
    - state 模块用别名 `app_state` 导入，因为很多函数里有名为 `state` 的局部变量。
    - 每个模块各自定义 `log = logging.getLogger("vowifi.main")`，日志名不变。
  - 唯一的代码改动：maintenance 的 refresh_egress 原来直接调用 egress 的路由函数。现在把函数体提到 `state.refresh_egress()`，两个路由都调用它，避免 router 之间互相导入。
  - state.py 现在放了什么（1275 行）、为什么放在那里：
    - 规则：被两个及以上模块（router、或仍留在 main 的路由/后台任务）使用的函数，连同它依赖的东西，都放进 state.py。router 只依赖 state 和业务模块。
    - 具体内容：
      - Hub 类和 hub 实例
      - 卡片草稿与热插拔自动建线：_ensure_card_draft、_auto_promote_card_draft、_auto_start_hotplugged_line、_line_auto_start_allowed、_draft_missing、_next_instance_id、_match_instance_by_iccid、_find_running_by_reader、_start_engine_checked
      - 读卡器与 IMEI 绑定：_live_reader_binding_for_instance、_device_for_card、_hardware_imei_for_card、_apply_current_hardware_imei
      - 生命周期记录：_record_lifecycle
      - 健康与恢复：apply_health、_auto_recover_instance、_judge_exit_failure、_local_card_fault、_peer_line_registered、出口账本、push_status、_with_status_activity。计划 §3 原本写把 apply_health 放进 engine router，但状态轮询任务也调用它，所以只能放在 state。
      - 主机告警状态文件
      - refresh_egress
      - 运营商标识辅助函数
  - 测试 patch 目标的进度：
    - 已改到 app_state 或新 router 模块的测试：test_auto_provision、test_device_state、test_engine_restart_evidence、test_esim_409_structured、test_esim_run_coroutine_close、test_line_lifecycle、test_line_naming、test_media_control、test_mt_sms_discard、test_operations、test_pin_preflight_message、test_reader_line_rebind、test_vowifi_support、test_wrong_card_attribution、test_service_codes、test_softphone_ws、test_gate、test_authz。
    - test_wrong_card_attribution 中对 egress/failover/engine 的重绑定是手工改到 app_state 的。
    - 读源码的测试（test_stalled_exit、test_i18n_coverage 的 reason 扫描、test_ui_rekey_labels）改为通过 `tests/control_source.py` 读整个控制面。已核对 reason 集合与原 main.py 相同。
    - 还在通过 main 访问的（都属于 lines 或后台任务，会在后面两步由 retarget 自动改写）：
      - test_auto_provision、test_device_state、test_line_lifecycle、test_line_naming、test_media_control、test_pin_preflight_message：涉及 _unified_devices、extract_msisdn、_refresh_card_matches、_reader_index/port_for_instance、api_instance_*、api_device_*、_media_retry、_sustained_alerts、_outage_detail、_poll_instance_status、_CARD_REREAD_DELAYS、MSISDN_VERIFY_INTERVAL_SECONDS 等。
      - test_webui_caching 用的 main._HashedAssets 会留在 main，不需要改。
      - 测试里的 `main.cfg`、`main.engine` 这类业务模块引用，在 main 不再导入它们时由 retarget 改成直接导入。
  - 下一步的第一条具体动作（在仓库根目录、用 3.12 venv）：`timeout 120 python tools/slim/split_gen.py tools/slim/main_source.py auth,esim,egress,logs,system,softphone,engine,lines --list`，然后运行 pyflakes，再运行 `python tools/slim/retarget.py`，并处理它输出的 MANUAL 行，最后跑全量测试、提交 lines。

- 2026-10-26 阶段 3 完成（e1b8942 auth、a879084 esim、9b47915 egress、43824f8 logs、cc04b7f system、f6ef110 softphone、85107f4 engine、f589b51 lines、13d246b tasks）。main.py 从 5002 行变为 201 行，只留应用创建、gate 中间件、审计中间件、include_router、lifespan 和静态 WebUI。
  - 临时生成器 tools/slim/ 已删除。从现在起 control/app/*.py 和 routers/*.py 就是正式源码，直接编辑它们。
  - Codespace 里 tests/test_country_egress.py 的 IdleBackoffTests.test_waiting_ends_early_when_an_input_document_changes 会偶发失败（对时间敏感）。拆分前的 8fa4ab9 上同样如此，GitHub CI 上通过。
  - 遗留项 1（下一步先做）：路由防漏测试。以拆分前 9fcbe5c 的 /api/ 路由 (method, path) 集合和 WebSocket 路径集合为固定基线，断言 tests/route_listing.api_routes(app) 与基线一致；再用 TestClient 在未登录状态下请求每个基线接口，除白名单（登录、首次设置等）外都必须返回 401 或 403。
  - 遗留项 2：设置页的维护/存储里仍有 Docker 镜像与构建缓存相关的 UI 和后端字段，留给阶段 4。
  - 遗留项 3：i18n 里仍写最多十条线路，阶段 4 改线路上限时一并修改。
- 2026-10-28 遗留项 1 完成：路由防漏测试（本提交）。全量测试 998 个通过，WebUI 构建通过。
  - `tests/test_route_set.py`：基线改为从 9fcbe5c 实际导出（在该提交的工作树里遍历 app.routes 并展开 include 的 router），66 个 /api/ 的 (method, path) 加 2 个 WebSocket 路径；与原 EXPECTED 逐项相同。非 APIRoute/APIWebSocketRoute 的路由（普通 Starlette Route、Mount）也会被检出，避免绕过比较。
  - `tests/test_route_auth.py`：用 TestClient 走完整应用（gate、审计中间件、routers），未登录请求每个基线接口，分"未设置管理员"和"已设置管理员"两种状态各跑一遍；也测了带无效 cookie 的情况。除白名单外都必须是 401/403；WebSocket 必须以 4401/4403 关闭（gate 对 WebSocket 的对应码）。
  - 白名单（来自 gate.PUBLIC_PATHS 与 routers/auth.py）：`GET /api/auth/status`（登录页判断显示"设置"还是"登录"，未登录时 csrf 为空）、`POST /api/auth/setup`（auth.configured() 后返回 409）、`POST /api/auth/login`（按来源限速，错误密码 401）。`POST /api/engine/event` 不在白名单：它要求引擎令牌，未带或带错都返回 401，单独有测试。
  - 变异验证：把 /api/settings 加进 PUBLIC_PATHS、把 WebSocket 来源改成不要求会话，测试都会失败（WebSocket 等待第一条消息有 5 秒上限，被错误放行的 socket 不会让测试挂住）。
  - TestClient 依赖 httpx，它不是运行依赖。新增 `tests/requirements.txt`（固定 httpx==0.28.1），ci.yml 的 python job 额外安装它；设备上的发布包不包含它。starlette 1.10 对 httpx 打印弃用警告，不影响结果。
  - 观察到但未改动：`/docs`、`/docs/oauth2-redirect`、`/openapi.json`、`/redoc` 在 9fcbe5c 和现在都不经登录即可访问（不在 /api/ 下，gate 视为静态资源）。会暴露完整接口清单，但不暴露数据。是否关闭（`FastAPI(docs_url=None, redoc_url=None, openapi_url=None)`）列为阶段 4 待定问题 P-1。
- 2026-10-28 步骤 1：阶段 4 设计写入 §12（文件清单、两个 systemd 单元、netns/veth、每线路路由表、nft 0.9.8 规则、sudo + mdd-enginectl、阻断逻辑与测试方案、风险、12 个待定问题 P-1～P-12）。**未写代码，等待确认。** 下一步：按你对 P-1～P-12 的答复修订 §12，然后从 §12.11 第 1 项开始实现。
- 2026-10-28 确认：P-1～P-12 全部采用 §12.10 括号里的推荐做法，§12 设计按原文生效，不再逐条讨论。用户要求尽快在棒子上跑起来：按 §12.11 第 1–6 步顺序实现（每步仍跑全量测试 + `npm run build`），第 6 步完成后先出 arm64 包装到设备上验证，第 7 步 CI（`netns-leak`、`nft-bullseye`）可以在首次上设备之后补。下一步：§12.11 第 1 项。
- 2026-10-28 §12.11 第 1 项完成（0528af7 之后的提交）：`host/mdd_netns.py` + `tests/test_netns_rules.py`（46 个）+ `tests/test_netns_leak.py`（特权集成，9 个场景，root 且 `MDD_NETNS_IT=1` 才运行）。集成测试在沙箱里用 nftables 1.0.9 和 Debian 11 的 nftables 0.9.8 用户态各跑一遍，全部通过。
  - 对 §12 的一处补充：每条线路的路由表里加宿主直连私网的 `throw` 路由。原因：内核对转发包做反向路径检查时，把出接口 `mddvN` 当作入接口查策略路由，命中线路自己的表、查到 TUN，严格 `rp_filter` 下会丢掉浏览器回给 Asterisk ICE 检查的包（集成测试 g 复现过，`IPReversePathFilter` 计数加 1）。forward 链仍只放行带标记的 RTP，其余到局域网的包照样丢弃。
  - 沙箱内核没有 IPv6，`up` 改为：IPv6 的 sysctl 失败只告警，rp_filter 等其余步骤失败就退出。
- 2026-10-28 §12.11 第 2 项完成：`packaging/systemd/mdd-netns@.service`、`mdd-engine@.service`、`packaging/sbin/mdd-enginectl`、`packaging/sudoers.d/mdd` + `tests/test_packaging.py`（15 个；含 `visudo -cf` 与 `systemd-analyze verify`，工具不存在时跳过）。与 §12.3 的差异：引擎单元去掉了 `/var/run/asterisk`、`/var/log/asterisk` 两个临时挂载（Asterisk 的控制套接字和日志按 P-4 放在线路自己的 run/logs 目录），增加 `MDD_AST_LOGDIR`。
- 2026-10-28 §12.11 第 3 项完成：orchestrator 每轮调用 `mdd_netns.reconcile()`（代理关闭时也调用，线路按直连处理），删除 /32 ePDG 路由、ePDG 解析与地址保留（`EpdgAddressRetentionTests` 5 个测试随功能删除）；国家 SOCKS 默认监听 127.0.0.1；sing-box/Xray 自行退出时立即醒来重建；服务重启改用 `mdd-control.service`/`mdd-orchestrator.service`（`services` 范围用 `systemd-run` 临时单元执行 `systemctl restart`，不再调用 install.sh）。`proxy-status.json` 的 `lines[N]` 增加 `route`/`netns`/`netns_present`，去掉 `addresses`/`routed_addresses`。新增 `tests/test_orchestrator_lines.py`（12 个）。
- 2026-10-28 §12.11 第 4 项完成：控制面改为 systemd 后端。
  - `engine.py`：`sudo -n mdd-enginectl`（start 前先 stop→清运行文件→写 instance.json→reset-failed→start，出口或 ePDG 解析失败时不动正在运行的引擎）；`systemctl show` 读状态，`InvocationID` 作为"代"（运行时字典的 `container_id` 改名 `generation`）；`journalctl` 读日志；`asterisk -r -s <线路 run 目录>/asterisk/asterisk.ctl -x` 执行 CLI；删除线路前 `enginectl down`。
  - `runtime.py`：每 3 秒一次 `systemctl show` 轮询代替 Docker 事件流。
  - `egress.resolve_epdg()`：TUN 线路经本国 SOCKS（127.0.0.1）解析，直连线路用宿主解析；`desired.json` 增加每线路 `rtp` 与 `control_port`。
  - `config.py`：`PRODUCT_MAX_LINES = 2`（源码硬上限）、`settings.max_lines` 默认 1；`manager_url` 默认 `https://10.88.<线路>.1:<端口>`，忽略旧的 `host.docker.internal`；数据目录 0750、线路目录 0770、instance.json 0640（引擎无 `CAP_DAC_OVERRIDE`，经组 mdd 访问；config.yaml、auth.json 仍是 0600）。原来经容器环境变量传给引擎的 `SWU_LIVENESS_PERIOD`、`SWU_PCSCF_APPLY_MODE` 改为写进 instance.json，由 render.py 写入 engine.env（默认值不变）。
  - 删除：`rtp_forward.py`、media relay 全部代码（`media.py` 只剩 direct）、`media_supervisor` 后台任务、requirements 里的 `docker`；docker 依赖去掉后，operations/sysinfo/system 路由里的镜像清理、构建缓存、容器重启执行器、Docker 存储统计也在本项一起删除（WebUI 部分留在第 6 项）。设置接口校验 `max_lines` ∈ {1,2}，调小后立即停止超出的线路。
  - P-1：关闭 `/docs`、`/redoc`、`/openapi.json`，加测试。
  - 测试：删除 `test_media_relay`、`test_rtp_forward`；Docker 机制相关的用例随功能删除；新增 `tests/test_engine_systemd.py`（27 个）；`test_runtime_registry`、`test_media_control`、`test_engine_manager_url`、`test_product_boundaries` 按新行为重写。全量 1009 个通过。
- 2026-10-28 §12.11 第 5 项完成：引擎侧。
  - `entrypoint.sh`：删除 relay 防火墙段；脚本、日志、ami_usim.ini 路径改用 `MDD_ENGINE_DIR`/`MDD_LOGDIR`/`MDD_ENGINE_ETC`（默认仍是容器路径）；创建 `$MDD_RUNDIR/asterisk`；core dump 默认关闭（P-7）。
  - `render.py`：读 `MDD_CONFIG`；`MDD_RUNDIR`/`MDD_LOGDIR`/`MDD_AST_LOGDIR`/`MDD_ENGINE_ETC`/`MDD_AST_ETC`；删除 relay 与 `engine_subnet` 分支（软电话监听固定在 veth 地址）。`notify.py` 同样改为读环境变量。
  - 模板：`asterisk.conf` 的 `astlogdir`/`astrundir` 指向线路目录，`astctlpermissions = 0660`（控制面经组 mdd 用 `asterisk -r -s`）；拨号计划里 `calls.txt` 与 `notify.py` 走线路路径，`notify.py` 用 Python 解释器显式调用（不依赖可执行位）；rtp/pjsip/http 模板去掉 relay 分支。
  - **修正一处违反硬约束的默认行为**：swu_ike 每次应用 P-CSCF 都会执行 `core set debug 3` 90 秒（`SWU_PCSCF_DEBUG_SECONDS` 默认 90），DEBUG 输出写进持久的 `reload_debug` 日志，期间收到的短信 RP-DATA 十六进制会落盘。改为：swu_ike 默认 0；render.py 只在该线路开启 Asterisk 调试时写 90；`logger.conf` 的 `reload_debug` 通道也只在调试时出现。
  - 测试：`tests/test_media_engine.py` 重写为 direct 渲染、线路路径、默认无 debug（含"任何地方都不打开 pjsip logger"）、engine.env 注入（`$(...)`、反引号、换行、引号等被 `set -a; .` 读入后原样保留且不执行）。全量 1004 个通过（relay 渲染与入口防火墙相关用例随功能删除）。
- 2026-10-28 §12.11 第 6 项完成：WebUI。设置页"维护"删除两个 Docker 清理按钮与说明，"主机信息/存储"删除 Docker 镜像、构建缓存、容器可写层四行，删除"容器模式不能重启主机"的提示；"通话与 VoWiFi"删除媒体模式面板（只剩 direct），增加"SIM 线路数量：最多 1/2 条"选择与内存提示；软电话删除 relay 相关逻辑与提示（`iceServers` 恒为空）；i18n 删除 28 条 Docker/relay 词条，合规提示改为"最多 2 条 SIM 线路"/"at most two SIM lines"。新增 `tests/test_ui_slim.py`。全量 1008 个通过，WebUI 构建与 node 测试通过。后端的 Docker 字段已在第 4 项删除。
- 2026-10-28 arm64 发布包（为尽快上设备，提前做了阶段 5 的 install.sh/单元和阶段 6 的 build-arm64.yml）。
  - `build/build-arm64.sh`：在 debian:bullseye arm64 里构建，全部下载固定版本并校验 sha256/提交号。产物 `mdd-slim-arm64-<版本>.tar.gz`（`install.sh` + `payload/` + `SHA256SUMS`），Xray 单独打成 `mdd-slim-xray-arm64-<版本>.tar.gz`（只在 REALITY/XHTTP 节点需要）。payload 内含 python-build-standalone 3.12 与全部 Python 依赖、Asterisk（sysmocom 分支 + 本仓库补丁，关闭 BUILD_NATIVE、无声音包、按白名单裁剪模块）、pcsc-lite + ccid（带本仓库补丁）、lpac、sing-box、WebUI dist；`apt-packages.txt` 由 ldd + dpkg -S 生成，设备上只用 apt 装这些系统库。
  - `.github/workflows/build-arm64.yml`：手动触发，或提交信息含 `[arm64]` 的 push 触发；ubuntu-24.04-arm 上跑 debian:bullseye 容器；结果只作为 workflow artifact（保留 30 天），不建 release、不打 tag。
  - `install.sh` 重写：`install [--xray 包]`、`status`、`logs [control|orchestrator|pcscd|engine N] [行数]`、`reset-admin`、`uninstall [--purge]`。安装前先 `sha256sum -c`，再做环境检查（aarch64、systemd ≥ 247、tun/veth/nf_tables、netns、10.88/16 冲突、旧 Docker 版残留）。不改 apt 源；发行版的 pcscd/libccid 会被停用并卸载（记入 install-state，uninstall 时恢复）；没有 zram 交换时建 `mdd-zram.service`（内存一半，lzo-rle）。
  - 新单元：`mdd-pcscd.service`、`mdd-orchestrator.service`（root）、`mdd-control.service`（用户 mdd，`systemd-journal` 组）。`requirements-runtime.txt` 固定发布包里的 Python 依赖版本。
  - 代码改动：orchestrator 以 root 写入数据目录的文件改为属主与数据目录一致（`hand_to_data_owner`），否则控制面（mdd）读不到 proxy-status.json；asterisk.conf 增加 `astdatadir` 指向包内 `var/lib/asterisk`，因为单元把 `/var/lib/asterisk` 挂成空 tmpfs，Asterisk 缺 documentation/ 会退出；`SWU_LIVENESS_PERIOD`/`SWU_PCSCF_APPLY_MODE` 原来是 Docker 环境变量，现在写进 instance.json 再由 render.py 写入 engine.env；P-CSCF 调试窗口默认 0 秒，只有 `debug_asterisk` 时为 90 秒。
  - 测试：删除依赖旧 install.sh/Docker 镜像的 `test_install_media`、`test_install_tun_mtu`、`test_install_requirements`、`test_engine_image_refresh`；新增 `tests/test_install_script.py`（15 个）。全量 993 个通过，WebUI 构建与 node 测试通过。
  - 未在 arm64 上实测的点：sysmocom Asterisk 在 gcc 10 下能否编过（R3）、pcsc-lite 2.x 的 meson 选项、各组件在 Debian 11 glibc 2.31 上的运行，均由 build-arm64.yml 的冒烟测试先检查一遍。
- 2026-10-28 build-arm64 首次运行（run 40640355602，1225304）失败在容器的第一次 apt：bullseye-security 的包在 deb.debian.org/debian-security 上 404（LTS 结束后已挪到 archive.debian.org；那里的 Release 日期 2026-09-12，没有 Valid-Until，openssl 等包可下载）。修正：workflow 在 apt 之前把容器里的 `deb.debian.org/debian-security` 改写为 `archive.debian.org/debian-security`（.list 与 .sources 都改），build-arm64.sh 开头重复一次（幂等）。只改构建容器。
  - 待定：设备上 install.sh 的 apt 会遇到同样问题。是自动改写安全源，还是只检测并提示手动修改，等设备上 `grep -rn security /etc/apt/sources.list /etc/apt/sources.list.d/` 的输出再决定；在此之前 install.sh 不改 apt 源。
- 2026-10-28 build-arm64 第二次运行（run 40641838007，3ace8e0）：换源已生效，apt 通过；失败在编译。
  - pcsc-lite 2.3.3 链接 `libpcsclite.so.1` 时报 `undefined reference to pthread_once`（src/libredirect.c:182）：glibc 2.31 的 pthread 函数还在 libpthread 里，pcsc-lite 的 meson 没给这个库链接 threads，又带 `-Wl,--no-undefined`。修正：meson setup 加 `-Dc_link_args=-pthread`。
  - 脚本问题：`ninja -C build && ninja -C build install` 在 `set -e` 下，左边失败不会退出，所以 pcsc-lite 的错误被跳过，报错位置变成了后面 ccid 找不到 libpcsclite。修正：pcsc-lite 与 ccid 两处都拆成两行。其余 `&&` 已检查：重试循环里的条件判断、子 shell 内的 `cd && 命令`（子 shell 的退出码仍受 `set -e` 约束），不需要改。
- 2026-10-28 build-arm64 第三次运行（run 40644627821，bef589b）：pcsc-lite 与 ccid 已编过；失败在 Asterisk 补丁。`ims_public_identity_log.py` 找不到 `/home/asterisk-build/asterisk/...`：补丁脚本的默认路径是原 Docker 镜像里的目录。4 个脚本读环境变量 `AST_SRC`，`mt_rpack_routing.py`、`resolve_config_docs.py` 写死路径。修正：build-arm64.sh 在应用补丁前 `export AST_SRC="$WORK/asterisk"`；那两个脚本也改为读 `AST_SRC`（默认值不变）；`test_mt_sms_discard` 改为通过 `AST_SRC` 运行补丁；`test_install_script` 新增检查：每个补丁脚本都读 `AST_SRC`，并且在空目录上只因缺源文件退出，不出现 Python 错误。
  - 本地复现手段：沙箱里起 dockerd，用 amd64 `debian:bullseye`（gcc 10.2.1、glibc 2.31，与 CI 容器同版本）跑 build-arm64.sh 中 Python 之前的步骤。沙箱代理对 GitHub 源码归档返回 403，所以本地用 git 取 pcsc-lite/ccid 同一标签代替（CI 仍用固定 sha256 的归档）。本地已复现第三次的失败，修正后 6 个补丁都能打上，configure 通过；Asterisk 编译结果见下一条。
- 2026-10-28 build-arm64 第四次运行（run 40645261213，563357b）：6 个补丁都打上了，Asterisk 在 arm64 上编译成功（R3 消除）；失败在模块白名单：`res_pjsip_outbound_registration.so` 没有生成，本地 x86 复现结果相同。之后用本地 x86 环境把整个脚本跑通（只为本地替换了 x86 的 Python/sing-box/Xray 下载与校验值），途中又修了四处：
  - `res_pjsip_outbound_registration` 的 MODULEINFO 要求 `libmnl`（sysmocom 的 netlink/xfrm 代码），menuselect 因此跳过了它。原 Fedora Dockerfile 装了 `libmnl-devel`，构建依赖里补上 `libmnl-dev`。其余白名单模块的外部依赖（speex、ogg、vorbis、unbound、srtp、openssl、gsm）都已在列表里。
  - lpac 源码写的是 `#include <PCSC/winscard.h>`，固定版本的 pcsc-lite 头文件装在 `$PREFIX/include/PCSC`。cmake 加 `-DCMAKE_C_FLAGS="-I$PREFIX/include"`；同时去掉这个 lpac 版本不认识的 `STANDALONE_MODE`、`LPAC_WITH_APDU_UQMI`。
  - pyscard 没有可用的预编译 wheel，需要现场编译。python-build-standalone 的 sysconfig 指定编译器为 clang，容器里没有，所以 pip 这一步设 `CC=gcc LDSHARED="gcc -shared"`。
  - 冒烟测试的脚本错误：`ldd | grep 'not found'` 在一切正常时返回 1，pipefail 让 `unresolved=$(...)` 直接终止脚本；`pcscd --version | head -n1` 和 `sing-box version | head -n1` 有 SIGPIPE 风险；`sh -n a b` 只检查第一个文件。已全部改掉；`find | head -n1` 改为 `find -print -quit`。
  - `test_install_script` 新增 `test_bullseye_build_fixes_stay`，固定以上修正，并检查构建命令不再用 `a && b` 的写法。
  - 本地 x86 结果：全流程通过（各组件冒烟测试、Python 导入、控制面导入、打包）。主包 75 MB（解压后约 225 MB，其中 Python 约 150 MB：解释器与 libpython 各约 30 MB、site-packages 49 MB），Xray 包 14 MB。apt-packages.txt 列出 66 个运行库及 nftables/iproute2/procps/sudo/ca-certificates/kmod，都来自 bullseye main。瘦身（tcl/tk、idlelib、ensurepip 等）留到首次上设备之后再看。
- 2026-10-28 build-arm64 第五次运行（run 40647531386，3c65c2a）通过，CI 也通过。产物 `mdd-slim-arm64-1.14.0-slim.5`（artifact 12798113421，保留到 2026-11-27）：主包 68 MB，Xray 包 12 MB，各带 .sha256。arm64 冒烟测试：Asterisk 20.7.0、pcsc-lite 2.3.3、sing-box 1.13.15、Python 导入与控制面导入全部通过；apt-packages.txt 与本地 x86 结果相同。下一步：设备上先只读检查 apt 源（待定的第 2 项），再安装。
- 2026-10-28 设备首装反馈（1.14.0-slim.5，openstick）。Bug 1：设备页白屏，`UnrecognizedUsbDevices is not defined`。
  - 原因：这个组件是蜂窝模块的"未识别 USB 设备"探测（`api.usbCandidates`/`probeUsbCandidate`），阶段 2（b18256e）删除模块路径时删掉了组件与接口，但设备页里两处引用没删。vite 只转译不检查，所以构建能通过。slim 只支持 USB 读卡器，这个组件不该存在，修正为删除引用（349c:0418 不是读卡器，显示出来也没有可用的操作）。
  - 防再犯：前端是纯 JS（无 TypeScript），所以用 ESLint 而不是 tsc：`no-undef` 和 `react/jsx-no-undef`，不开风格规则。`npm run build` 改为 `eslint . && vite build`，CI 的 webui job 随之检查；CI 另加 `node --test tests/*.test.mjs`。`webui/tests/lint-guard.test.mjs` 确认引用未定义组件或标识符会报错、build 脚本先跑 lint。手动验证过：故意插入 `<NoSuchPanel />` 时 `npm run build` 退出码为 1。新增的 devDependencies 都固定版本（eslint 9.39.5、eslint-plugin-react 7.37.5、eslint-plugin-react-hooks 5.2.0（只注册，让已有的 disable 注释能解析）、globals 17.13.0），原有包版本不变，只用于构建。
  - 全仓排查：ESLint 在前端只报出这两处；pyflakes 在 control/engine/host 里没有未定义名称；control/app 的所有模块都能导入；`api.js` 调用的 63 个路径后端全部存在，前端没有绕过 api.js 的 `/api/` 调用。
- 2026-10-28 Bug 3：测试分享链接时返回 500，报 `int('20450/')`。
  - 原因：vless/trojan/hysteria2 已经用 `urlsplit` 解析；出错的是 ss 分支，它手工切分，只去掉了 `#` 和 `?`，所以 `host:port/?...` 里的 `/` 进了端口。同一分支对 `[IPv6]:port` 也会把方括号留在主机名里；vmess 的 JSON 端口没有校验。
  - 修正：新增 `link_port()`（只接受 1–65535 的十进制数，否则报可读的 ValueError）和 `_split_link()`（`urlsplit` + `.hostname`/`.port`，并把 urlsplit 自己的端口异常换成可读信息）。ss 分支改为先确定 userinfo（最后一个 `@` 之前，标准 base64 里可能有 `/`，先百分号编码），再交给 `urlsplit`，SIP002 base64、2022 明文、整段 base64 三种写法都支持。vmess 校验 host 与端口；`clash_outbound` 也改用 `link_port()`（订阅节点同样受益）。
  - 400：`egress.EgressInputError`（EgressError 的子类）表示输入无法解析；`test_proxy_profile` 把所有读取链接的步骤（包括 Xray 出站的构建）放进同一个 try，路由对它返回 400 `{message, parsed}`，前端已经读取 `detail.message`。缺少 sing-box 等网关自身问题仍为 503。
  - 测试：`tests/test_share_link_ports.py`（11 个，含子用例）覆盖 vless、trojan、hysteria2/hy2、ss 三种写法、vmess 与 `host:port/?q`、`host:port?q`、`host:port/`、`#备注`、`[IPv6]:port`、无端口、非法端口等组合，以及路由的 400/503。确认过这些测试在修改前的代码上会失败（包括设备上那种链接格式）。
- 2026-10-28 Bug 2：线路上限挡住了 SIM，页面却只提示"插入可读取的 SIM"。
  - 匹配逻辑分析：插卡时按 ICCID 匹配（`state._match_instance_by_iccid`），不按读卡器槽位；读卡器槽位只在"已有运行中的引擎占着这张卡"时用来对应。拔卡或拔读卡器不占用、也不释放任何名额。"SIM line limit reached; ignoring…" 只会在 `_ensure_card_draft` 里打出，而走到这里的前提是读到的 ICCID 不属于任何已保存线路。因此设备上 6697 这次读到的 ICCID 与线路 1 保存的不同：要么是另一张卡，要么是 eUICC 切换了配置（ICCID 随配置变化），要么线路 1 保存的 ICCID 与读到的不一致。代码里没有找到"同一 ICCID 重插后匹配不上"的路径，需要设备上的数据确认（只看尾号，见交付说明）。
  - 修改：上限挡住时在卡片状态里记下 `not_enabled = {reason: line_limit, max_lines, iccid_tail}`；`/api/devices` 对这种卡返回 `provisioning.state = "line_limit"` 和 `line_limit` 字段，VoWiFi 原因改为"这张 SIM 没有对应线路：已达到 SIM 线路上限"；设备页显示"尾号 xxxx 的 SIM 未启用：已达到上限（N 条）"和处理办法。卡片监控每轮检查被挡住的卡，一旦删掉线路或调大上限，就自动为它建线路并尝试启动，不需要重新插卡。
  - 测试：`tests/test_line_limit_card.py`（7 个）：同一张卡拔读卡器再插、只拔卡再插都回到原线路，不尝试新建；重插时读卡失败不占名额；上限挡住时日志、卡片状态、`/api/devices`、`/api/cards` 都带原因；删线路、调大上限、卡片监控自动重试三种解除方式。
- 2026-10-28 softphone WebSocket 反复 403：判断为预期行为，不是鉴权 bug。
  - 依据：同一时间 `/ws?auth_close=1` 用同一 cookie 和 Origin 能连上，说明会话和 Origin 校验都通过；Origin 被拒时 gate 会打 "refused WebSocket … from origin" 日志。403 来自 `ws_softphone` 自己：线路不存在或关了软电话（1008）、读不到引擎状态或引擎没运行（1013）、引擎连不上（1011），都是在 accept 之前 close，uvicorn 把这种情况记为 HTTP 403，浏览器只看到 1006。`GlobalSoftphone` 会给每条开着软电话的线路建 UA，不看线路是否在运行，JsSIP 按 2–30 秒的退避一直重连，所以线路 1 没运行时就每隔几秒一个 403（与 Bug 2 的线路状态吻合）。
  - 修改（按"预期行为"处理）：relay 对这些情况先以 sip 子协议 accept，再用明确的关闭码和原因关闭：4404 线路不存在、4410 软电话已关、4409 线路未运行、4503 读不到状态、4502 引擎无响应；不支持 sip 子协议的客户端仍在握手阶段拒绝（1008）。JsSIP 每次连上都会把退避计数清零，所以前端不能依赖它：`Softphone` 收到这些码时停掉 JsSIP 的重连，自己按 30 秒起翻倍、最长 10 分钟重试，注册成功后清零；软电话页显示原因和下次重试时间，其他页面不提示。
  - 测试：`test_softphone_ws` 按新行为改写（accept 后带码和原因关闭，原因不超过 123 字节，前后端的码集合一致）；`webui/tests/softphone-refusal.test.mjs` 检查退避序列、拒绝后停掉 UA 并按时重启、`stop()` 取消待重试。
- 2026-10-28 新功能：蓝牙音频页面。权限模型按 2026-10-28 的确认：控制面仍以 mdd 运行，不加入 bluetooth/pulse-access 组、不改 PulseAudio 认证，所有蓝牙与音量操作都是一次 `sudo -n /usr/local/sbin/mdd-btctl <动作> [参数]`。
  - `packaging/sbin/mdd-btctl`（root，0755，由 install.sh 安装、uninstall 删除）：用发布包自带的解释器（`#!/opt/mdd/current/venv/bin/python3 -I`；/opt/mdd 归 root，mdd 不可写），不读 mdd 可写的任何文件。动作白名单 status/scan/pair/connect/disconnect/remove/volume/mute/autoconnect，其余一律退出码 64；MAC、0–100 音量、0/1 静音、MAC 或 none 在脚本里再校验一遍；固定 PATH 与环境（`LC_ALL=C`、`PULSE_SERVER=unix:/run/pulse/native`），参数列表调用，不经过 shell；输出 JSON；修改类动作用 flock `/run/mdd-btctl.lock` 串行；autoconnect 先写临时文件再 rename。
  - 与确认内容的一处差异：`status` 不取锁。配对会持锁约 40 秒，而页面扫描时每秒轮询状态；status 只读，修改类动作仍全部串行。
  - 配对在同一个 bluetoothctl 会话里完成（stdin 依次写 agent、scan on、等设备出现、pair、trust、connect），即设备上实测成功的做法；扫描用后台进程，30 秒，状态写在 `/run/mdd-btctl.scan`。名字里的零宽字符会清掉；未命名设备（名字就是地址）标为 named=false；音量取 `pactl list sinks` 里第一条 `Volume:`（不是 `Base Volume:`），多声道取平均。
  - sudoers 增加一行 `mdd ALL=(root) NOPASSWD: /usr/local/sbin/mdd-btctl`，`visudo -cf` 通过。mdd-control 单元没有 ProtectSystem/NoNewPrivileges，sudo 起的 root 进程能写 /var/lib/bt-speaker、访问系统 D-Bus 和 /run/pulse，单元不用改。
  - 控制面 `routers/bluetooth.py`：GET status、POST scan、POST devices/{mac}/{action}、PUT volume、PUT autoconnect；只解析脚本的 JSON；pair 超时 45 秒，其余 20 秒；sudo 或脚本缺失、sudoers 未装时 status 返回 available=false 和原因；修改类操作另有 asyncio.Lock。鉴权沿用 gate + authz（只有管理员会话）。`test_route_set` 增加 `ADDED_HTTP`（基线之后有意新增的路由），`test_route_auth` 对它们同样做未登录拒绝检查。
  - 前端 `views/Bluetooth.jsx`，导航"蓝牙音频"：当前音箱、音量滑块（停止拖动 200 ms 后提交）、静音；扫描按钮带 30 秒倒计时，扫描时每秒轮询，其他时间页面可见时每 3 秒；列表按已配对、音频设备、其他排序，未命名设备默认隐藏；每行有配对/连接/断开、开机自动连接、删除配对（二次确认）；设了自动连接的音箱点断开时提示约 30 秒会重连，可选"断开并取消自动连接"；配对时提示最长 40 秒，失败显示后端信息和"进入配对模式并断开手机连接"。用无头浏览器加模拟 API 在 390 px 宽度下渲染过：无运行时错误、无横向滚动。
  - 测试：`tests/test_bluetooth.py`（26 个 + 1 个需要特权的集成测试）：用设备上的真实输出测解析；29 种非法参数（含 shell 元字符）都退出 64 且不执行命令；用假的 bluetoothctl/pactl 测各动作的参数列表、文件和 JSON；路由的 400/502/503/504。集成测试 `MDD_SUDO_IT=1`：以 mdd 身份 `sudo -n mdd-btctl status` 成功，非法参数 64，`id`、`sh -c`、`python3` 都被 sudo 拒绝，已在沙箱里用临时用户跑过并清理。`webui/tests/bluetooth-page.test.mjs` 测排序和隐藏。
  - 尚未在设备上实测：扫描、配对、连接、音量、静音、断开、删除配对、重启后自动连接。
- 2026-10-28 确认与结案：
  - 蓝牙 `status` 不取 flock 锁：已确认，按现状保留。
  - Bug 2 第 2 点关闭，不改匹配逻辑。设备侧时间线：18:05 eSIM（…6697）因线路上限被忽略 → 18:06:58 `POST /api/instances` 把线路 1 改绑到 6697（所以 18:08:31 拔出时 matched=1）→ 18:09 插入另一张卡（…5920），线路 1 被 6697 占着，被忽略 → 18:11:46 删除线路 1 → 18:12:28 重插后自动建立线路 1 并匹配。每次匹配变化都对应插卡事件或用户写操作。按 1.14.0-slim.10，18:11 删掉线路后卡片监控会自动补建，不用再重插卡。
  - 设备上的临时补丁（`index-BQK4vjq9.js` 首行插入的组件桩及其 .bak）随旧版本目录 /opt/mdd/1.14.0-slim.5 一起被 install.sh 清掉（安装新版本后删除 /opt/mdd 下其他版本目录）。
- 2026-10-28 设备反馈：英国出口报 `'str' object has no attribute 'get'`。订阅（Sub Store 导出）返回的是 base64 编码的分享链接列表，不是 Clash YAML，YAML 把它读成字符串，`subscription()` 随后对字符串调用 `.get("proxies")`。修正：新增 `parse_subscription()`，接受 Clash YAML（只保留映射类型的节点）以及明文或 base64 的分享链接列表（每行用 `parse_share_link` 转换，名称取 `#` 后的备注、vmess 取 `ps`；本网关不支持的链接跳过）；没有可用节点时报出说明两种格式的错误。测试 `tests/test_subscription_formats.py`。另：局域网网页慢查明是棒子 Wi-Fi 链路丢包（ping 平均 186 ms、大包丢失 50–75%），与 mdd 无关。
- 2026-10-29 用户确认：线路上限从"默认 1、可配置为 2"改为"默认 1、可配置 1–10"（恢复 slim 之前的上限）。`PRODUCT_MAX_LINES = 10`；合规提示改回"最多 10 条 SIM 线路 / at most ten SIM lines"；设置页的内存提示改为说明只有插着卡的线路才运行。引擎侧（mdd-enginectl、netns 10.88.N、地址段）本来就覆盖 1–10，不用改。测试 `test_product_boundaries`、`test_ui_slim` 同步更新。
- 2026-10-29 eSIM 页显示"未安装 lpac"（1.14.0-slim.13）。设备上以 mdd 身份直接运行 `/opt/mdd/current/lpac/lpac chip info` 和 `profile list` 都成功（EID、6 个配置文件都读到了），所以 lpac 本身和读卡路径没有问题。原因是 `lpa.lpac_bin()` 只认设置里保存的路径、旧容器的 `/usr/local/bin/lpac` 和数据目录下的默认路径，不看 mdd-control.service 设置的 `MDD_LPAC_BIN`（slim 打包时只在 `config.default_lpac_bin()` 里读了它，而这个函数没有被 `lpac_bin()` 调用）。修正：`lpac_bin()` 在回退前先检查 `MDD_LPAC_BIN`；设置里显式写的自定义路径仍然优先。同时把两处过时的提示（"运行 install.sh build-lpac"）改为"重新执行 install.sh install"。测试 `test_lpa_packaged_binary` 增加一个用例。
- 2026-10-30 设备反馈：网络出口页 `POST /api/egress/refresh` 返回 500，英国/德国出口提示 "no subscription node matched country keywords"。订阅地址先前输入有误（`doenload`），用户已改正。500 的栈只拿到了开头，推断原因（未在设备上确认）：orchestrator 以 root 创建了 `orchestrator/subscriptions/`，控制面（mdd）刷新时删除缓存文件得到 PermissionError，`refresh_egress()` 未处理。修正：orchestrator 写完订阅缓存后用 `hand_to_data_owner` 交还属主；`refresh_egress()` 删不掉的缓存文件只记警告并在结果里返回 `kept`，仍然重新发布出口，不再 500。测试 `tests/test_refresh_egress.py`。已存在的 root 属主目录需要一次 `chown`（见交付说明）。我之前让用户查看订阅缓存时用错了目录（`/var/lib/mdd/subscriptions`，正确的是 `/var/lib/mdd/orchestrator/subscriptions`），空输出不能说明什么。

---

## 12. 阶段 4 设计（2026-10-28 起草；2026-10-28 已确认：P-1～P-12 全部按推荐执行，可以开始写代码）

本节只细化 §4 的落地方式，不改动 §10 的已确认决定。凡是与 §4 原文措辞不同的地方，都在 §12.10 列为待定问题，附推荐做法，由你决定。

### 12.0 范围

- 阶段 4 做：引擎从 Docker 容器改为 `mdd-engine@N.service`；线路 netns、veth、每线路路由表；nft（出口防泄漏、RTP DNAT）；控制面经 `sudo` 调 `mdd-enginectl`；运行状态改读 systemd；删除 relay/coturn 与 RTP 转发容器；设置页"维护/存储"的 Docker 部分；i18n "最多十条线路"；线路上限可配置（默认 1，最大 2）。
- 留给阶段 5：`install.sh` 重写，`mdd-control`/`mdd-orchestrator`/`mdd-pcscd` 三个常驻单元，安装时创建用户、目录、sudoers、模块加载与 sysctl。阶段 4 只把单元文件、sudoers 片段、enginectl 放进 `packaging/` 并用测试固定内容。
- 结果：阶段 4 结束到阶段 5 结束之间，仓库里的 `install.sh` 仍是旧的 Docker 版，**不能在设备上安装**。

### 12.1 总体结构与信任边界

```
浏览器 ──HTTPS──> mdd-control（用户 mdd）
                    │ 写 desired.json / instances/N/instance.json（0640, 组 mdd）
                    │ sudo -n /usr/local/sbin/mdd-enginectl {start|stop|restart|down|reset-failed} N
                    │ systemctl show（只读，不需要 sudo）
                    │ AMI 10.88.N.2:5038、WS 10.88.N.2:8088（宿主主动发起）
                    ▼
mdd-orchestrator（root）── sing-box/Xray（子进程）── TUN mdd-<cc>
   │ 读 desired.json，校验后：每线路默认路由（TUN / 直连 / 不放行）、nft 两张表
   ▼
mdd-netns@N（root, oneshot）：netns mdd-N、veth mddvN<->eth0、地址、sysctl、
                              ip rule 与路由表 1000+N 里的 unreachable 兜底
mdd-engine@N（root, Group=mdd, 能力受限）：entrypoint → pin_keeper → swu_ike → ami_usim → asterisk
```

- 信任边界与现在相同：控制面写的文件由 root 进程读取并校验（orchestrator 读 desired.json；引擎读 instance.json）。新增一处：引擎原来是容器内 root，现在是宿主上的受限 root，所以 instance.json → engine.env 的转义必须可靠（render.py 已用 `shlex.quote`，阶段 4 加注入测试，见 §12.8）。
- 两道闸（§4.3）的归属：**路由闸**由 `mdd-netns@N` 建立，orchestrator 不运行也存在；**过滤闸**（nft forward）由 orchestrator 整表原子写入。

### 12.2 文件改动清单

| 文件 | 改动 |
|---|---|
| `host/mdd_netns.py`（新） | 纯函数：线路编号 → 名称/地址/表号/标记；`line_route_state()` 判定 TUN/直连/不放行；生成 `ip` 命令列表与完整 nft 规则文本；带一个可替换的执行器。命令行入口 `up N`/`down N` 供 `mdd-netns@.service` 调用。 |
| `host/mdd_orchestrator.py` | 删除 /32 ePDG 路由管理（`resolve`、`retained_epdg_addresses`、`current_managed_routes`、`apply_routes`、`MANAGED_ROUTE_PROTO`），因为每线路路由表已把 netns 的全部出站流量导向本国 TUN；新增 `reconcile_lines()`：按 `line_route_state()` 设置表 1000+N 的默认路由、直连规则、TUN 与 veth 的 `rp_filter=2`，并用 `nft -f -` 原子替换两张表；顺序固定为"先收紧后放开"（先删旧路由，再换 nft，再加新路由）。`COUNTRY_PROXY_LISTEN` 默认值 172.17.0.1 → 127.0.0.1。`_sleep_for_work` 在 sing-box/Xray 进程退出时立即醒来。服务重启请求里的单元名改为 `mdd-control`/`mdd-orchestrator`。proxy-status.json 的 `lines[iid]` 去掉 `addresses`/`routed_addresses`，增加 `route`（`tun`/`direct`/`blocked`）与 `netns`。 |
| `control/app/engine.py` | 删除 Docker SDK。`start()`：取线路出口状态（`egress.ensure_line`，仍是启动前 fail-closed）→ 解析 ePDG（代理模式经本国 SOCKS 用现有 `resolve_ipv4_via_socks`，直连模式用宿主 DNS）→ 写 instance.json（含解析后的 IP）→ 若在运行先 `capture_diagnostics` → `enginectl restart N`。`stop()`/`capture_and_stop()`：`enginectl stop N`，以 systemd `InvocationID` 代替容器 ID 做"同一代"判断。`container_runtime()` → `unit_runtime()`：一次 `systemctl show` 读 `ActiveState,SubState,MainPID,NRestarts,InvocationID,ActiveEnterTimestamp,ExecMainCode,ExecMainStatus`，地址固定为 `10.88.N.2`。`logs()`：`journalctl -u mdd-engine@N -o short-iso`。`exec_cli()`/`registration_state()`：`asterisk -r -s <instances/N/run/asterisk/asterisk.ctl> -x <命令>`，5 秒超时。删除 `ensure_image`、`reconcile_rtp_forward`、`media_mode_of`、`_format_docker_logs`。`delete_instance_data` 前先 `enginectl down N`。 |
| `control/app/runtime.py` | Docker 事件流 → 每 3 秒一次 `systemctl show mdd-engine@1..max`（一个子进程），对比 `ActiveState`/`InvocationID` 变化后调用原来的 `on_change(iid, runtime, action)`；`get/invalidate/start/close` 接口不变。 |
| `control/app/media.py` | 只留 direct：`DIRECT`、`mode()` 恒为 direct、`provisioning()` 返回空 ICE 服务器。删除 relay、coturn、镜像、探测、`main()`。`GET /api/media` 仍在，返回 `{"mode": "direct"}`（路由基线不变）。 |
| `control/app/rtp_forward.py` | 删除。DNAT 规则改由 `host/mdd_netns.py` 生成、orchestrator 应用（见 P-5）。 |
| `control/app/egress.py` | `desired_document()` 每条线路增加 `rtp: [start, end]`；文档增加 `control_port`。新增 `resolve_epdg(inst, line_state)`。容器栈 SOCKS 状态契约代码按 D5 保留，不再被引擎调用。 |
| `control/app/config.py` | `MAX_SIM_LINES` → `max_lines()`：读 `settings.max_lines`，默认 1，只接受 1 或 2。`_engine_manager_url()` 默认值改为 `https://10.88.<id>.1:<http_port>`（显式设置仍优先）。 |
| `control/app/operations.py`、`sysinfo.py`、`routers/system.py` | 删除镜像清理、构建缓存清理、容器重启执行器、Docker 存储统计；`/api/system/maintenance` 遇到 `prune_*` 返回 400；`/api/system/status` 去掉 `container_stack`，`host_restart_available` 恒为 true。 |
| `control/app/tasks.py`、`main.py` | 删除 `media_supervisor`（只服务 relay 与按媒体模式重建）；lifespan 去掉容器重启收尾与 `engine.close_client`。 |
| `control/app/routers/softphone.py` | 去掉 `docker.errors`；软电话 WS 中继地址来自 `unit_runtime()`。 |
| `control/requirements.txt` | 删除 `docker`。 |
| `engine/entrypoint.sh` | 删除 1b 节 relay 防火墙；脚本路径改用 `${MDD_ENGINE_DIR:-/usr/local/bin}`；Asterisk 用 `-C` 指向渲染出的 asterisk.conf。文件名保持不变，由单元直接执行（§2.2 原写改名为 `mdd-engine-run`，为减少改动保留原名，测试与 CI 的 `sh -n` 不变）。 |
| `engine/render.py`、`templates/asterisk.conf.j2`、`logger.conf.j2` | 删除 relay 与 `engine_subnet` 分支；路径从环境变量取（见 P-4）；asterisk.conf 增加 `astrundir=<run>/asterisk`、`astctlpermissions=0660`；`logger.conf` 的 `reload_debug => debug` 改为只在 `debug_asterisk` 为真时写出；默认 `debug=0`、不开 pjsip logger（加测试固定）。 |
| `packaging/systemd/mdd-netns@.service`、`mdd-engine@.service`（新） | 见 §12.3。 |
| `packaging/sbin/mdd-enginectl`、`packaging/sudoers.d/mdd`（新） | 见 §12.7。 |
| `webui/src/views/UnifiedPages.jsx`、`i18n.jsx`、`softphone.js`、`Softphone.jsx`、`GlobalSoftphone.jsx`、`api.js` | 删除存储页的 Docker 行与两个清理按钮、MediaModePanel 的 relay 显示、relay 相关提示；"Responsible use notice" 改为"最多两条线路"；系统设置加"线路上限 1/2"（见 P-6）。 |
| `.github/workflows/ci.yml` | 新增 `netns-leak` 与 `nft-bullseye` 两个 job（§12.8）。 |
| 测试 | 删除：`test_media_relay`、`test_rtp_forward`、`test_engine_image_refresh`。重写：`test_runtime_registry`、`test_media_engine`、`test_media_control`、`test_engine_paths`、`test_engine_manager_url`、`test_country_egress` 中 /32 路由部分、`test_operations`/`test_sysinfo` 中 Docker 部分、`test_product_boundaries`（上限）、`test_ui_softphone_media_preflight`。新增：§12.8 列出的测试。`test_install_media`、`test_install_tun_mtu`、`test_run_paths` 依赖旧 install.sh，留到阶段 5。 |

### 12.3 systemd 单元

`packaging/systemd/mdd-netns@.service`：

```ini
[Unit]
Description=MDD line %i network namespace
Before=mdd-engine@%i.service

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/opt/mdd/current/venv/bin/python -I /opt/mdd/current/host/mdd_netns.py up %i
ExecStop=/opt/mdd/current/venv/bin/python -I /opt/mdd/current/host/mdd_netns.py down %i
```

`packaging/systemd/mdd-engine@.service`（按 P-4 推荐写法）：

```ini
[Unit]
Description=MDD VoWiFi engine, line %i
BindsTo=mdd-netns@%i.service
After=mdd-netns@%i.service mdd-pcscd.service mdd-orchestrator.service
Wants=mdd-pcscd.service
ConditionPathExists=/var/lib/mdd/instances/%i/instance.json
StartLimitIntervalSec=600
StartLimitBurst=10

[Service]
Type=simple
User=root
Group=mdd
UMask=0007
ExecStart=/opt/mdd/current/engine/entrypoint.sh
Restart=always
RestartSec=5
TimeoutStopSec=20
KillMode=control-group
NetworkNamespacePath=/run/netns/mdd-%i
RuntimeDirectory=mdd-engine-%i
Environment=MDD_ID=%i
Environment=MDD_CONFIG=/var/lib/mdd/instances/%i/instance.json
Environment=MDD_RUNDIR=/var/lib/mdd/instances/%i/run
Environment=MDD_LOGDIR=/var/lib/mdd/instances/%i/logs
Environment=MDD_ENGINE_DIR=/opt/mdd/current/engine
Environment=MDD_TPL=/opt/mdd/current/engine/templates
Environment=MDD_ENGINE_ETC=/run/mdd-engine-%i
Environment=MDD_ASTERISK_CORE_DUMP=0
Environment=PATH=/opt/mdd/current/venv/bin:/opt/mdd/current/asterisk/sbin:/usr/sbin:/usr/bin:/sbin:/bin
Environment=LD_LIBRARY_PATH=/opt/mdd/current/lib
Environment=PYTHONDONTWRITEBYTECODE=1 MALLOC_ARENA_MAX=2 SWU_TUN_MTU=1400
ReadWritePaths=/var/lib/mdd/instances/%i/run /var/lib/mdd/instances/%i/logs /run/pcscd
TemporaryFileSystem=/etc/asterisk:mode=0750 /var/lib/asterisk:mode=0750 /var/spool/asterisk:mode=0750 /var/log/asterisk:mode=0750 /var/run/asterisk:mode=0750
DevicePolicy=closed
DeviceAllow=/dev/net/tun rw
CapabilityBoundingSet=CAP_NET_ADMIN CAP_NET_RAW CAP_NET_BIND_SERVICE
AmbientCapabilities=
NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome=yes
PrivateTmp=yes
ProtectKernelModules=yes
ProtectControlGroups=yes
RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6 AF_NETLINK
LockPersonality=yes
LimitCORE=0
MemoryMax=160M
```

说明：

- 不设 `ProtectKernelTunables`：swu_ike 在 netns 内对 `ipsec0` 执行 `sysctl -w net.ipv6.conf.ipsec0.*`（`engine/swu_ike.py` 的 `set_routes`），需要可写的 `/proc/sys/net`（该路径按 netns 隔离，不影响宿主）。
- 能力集比 §4.3 少了 `CAP_SYS_NICE CAP_SETUID CAP_SETGID`：Asterisk 以 root 运行、不带 `-U/-p`，按代码阅读用不到。设备实测若缺能力再补，阶段 7 记录。不给 `CAP_DAC_OVERRIDE`，所以 instance 目录与文件必须是组 `mdd` 可读写（控制面写 0640/0770；引擎 `Group=mdd`、`UMask=0007`，它创建的文件控制面也能读和删）。
- `TemporaryFileSystem=` 让每条线路的 Asterisk 有自己的 `/etc/asterisk` 等目录（与容器一样每次启动都是空的）。挂载点需要宿主上存在空目录，阶段 5 由 install.sh 创建、uninstall 删除。
- `Restart=always` 对应原来的 `unless-stopped`；`NRestarts` 对应 `RestartCount`。
- `LimitCORE=0` 与 `MDD_ASTERISK_CORE_DUMP=0` 见 P-7。
- `MemoryMax=160M` 是 §4.1 的初值，阶段 7 实测后调整。

### 12.4 netns 与 veth（`mdd_netns.py up N`，N 为线路 ID，1–10）

| 项 | 值 |
|---|---|
| netns | `mdd-N`（`/run/netns/mdd-N`） |
| veth | 宿主侧 `mddvN`（不用 §4.3 的 `mdd-hN`：`mdd-h1` 会被匹配 TUN 的 `mdd-*` 通配到）；netns 侧 `eth0` |
| 地址 | 宿主 `10.88.N.1/30`，netns `10.88.N.2/30`，netns 默认路由 via `10.88.N.1` |
| 路由表 / 规则优先级 | 表 `1000+N`；`pref 1000+N iif mddvN lookup 1000+N`；`pref 900+N iif mddvN fwmark M lookup main`；直连时另加 `pref 950+N iif mddvN lookup main` |
| 标记 M | `0x6d640000 + N` |

`up` 的步骤（幂等：已存在且一致就跳过，不一致就先 `down` 再建）：

```sh
ip netns add mdd-N
ip link add mddvN type veth peer name eth0 netns mdd-N
ip addr add 10.88.N.1/30 dev mddvN
sysctl -qw net.ipv6.conf.mddvN.disable_ipv6=1 net.ipv4.conf.mddvN.rp_filter=2
ip link set mddvN up
ip -n mdd-N link set lo up
ip -n mdd-N addr add 10.88.N.2/30 dev eth0
ip -n mdd-N link set eth0 up
ip -n mdd-N route add default via 10.88.N.1
ip netns exec mdd-N sysctl -qw \
    net.ipv6.conf.all.accept_ra=0 net.ipv6.conf.default.accept_ra=0 \
    net.ipv6.conf.all.autoconf=0 net.ipv6.conf.default.autoconf=0 \
    net.ipv6.conf.all.use_tempaddr=0 net.ipv6.conf.default.use_tempaddr=0 \
    net.ipv6.conf.all.disable_ipv6=0 net.ipv6.conf.eth0.disable_ipv6=1
# 路由闸：从这一刻起，netns 发往外部的流量默认不可达
ip route replace unreachable default metric 4000 table 1000+N
ip rule add pref 1000+N iif mddvN lookup 1000+N
ip rule add pref 900+N  iif mddvN fwmark M lookup main
```

`down`：删除上述 3 条规则、`ip route flush table 1000+N`、`ip link del mddvN`、`ip netns del mdd-N`。

- netns 内不放 resolv.conf：引擎不做外部 DNS，ePDG 由控制面解析成 IP 写进 instance.json（§4.3）；代理模式经本国出口解析（D6）。
- 前 6 个 IPv6 sysctl 与容器相同；`ipsec0` 需要 IPv6，所以 `all.disable_ipv6=0`，只关掉 `eth0` 的 IPv6。
- 宿主全局需要 `net.ipv4.ip_forward=1`（原来由 Docker 打开）。阶段 4 由 orchestrator 启动时检查并打开，阶段 5 由 install.sh 写入 `/etc/sysctl.d/`。

### 12.5 每线路路由表

表 `1000+N` 里永远有 `unreachable default metric 4000`。orchestrator 只增删一条更优先的默认路由，或者加一条直连规则：

| `line_route_state()` 结果 | 条件 | 表 1000+N | pref 950+N 直连规则 |
|---|---|---|---|
| `tun` | 国家出口已启用，`proxy-status` 中该国 `ready=true`，且 TUN 网卡存在 | `default dev mdd-<cc> metric 100` + unreachable | 无 |
| `direct` | 全局未开代理（`proxy.enabled=false`），或该国出口显式 `mode=direct` | 只有 unreachable | `iif mddvN lookup main` |
| `blocked` | 其余情况：出口未就绪、出口出错、没有为该国配置出口、国家无法判断、线路停用 | 只有 unreachable | 无 |

- sing-box 退出时 TUN 网卡消失，内核立即删掉 `dev mdd-<cc>` 路由，落到 unreachable；orchestrator 在下一轮（sing-box 退出会立即唤醒）重启 sing-box 并在 TUN 出现后加回路由。
- 线路换国家：先删旧默认路由（落到 unreachable），再原子替换 nft，最后加新路由，中间不会经过另一国家的 TUN。
- `fwmark` 规则只服务两类包（标记由 nft 打，见 §12.6）：被 DNAT 进来的 RTP 连接的回包；引擎从自己的 RTP 端口发往宿主直连私网的 UDP（ICE 连通性检查，见 P-3）。

### 12.6 nft 规则（nftables 0.9.8 兼容）

约束：不用 `fib`；不用 `inet` 族 NAT（NAT 放在 `ip` 族表里）；链优先级写数字（-150/-100/0/100）不写名字；不用 `typeof`、flowtable 等新语法。整表替换用 `table X` + `delete table X` + 定义，放在同一个 `nft -f` 事务里（表不存在时也不会失败）。"本机地址"不能用 `fib daddr type local`，改为 orchestrator 每轮从 `ip -4 -o addr` 生成 `local4` 集合（排除 10.88.0.0/16 与 TUN 地址）；"宿主直连私网"`lan4` 来自 `ip -4 route show table main scope link`，只取 RFC 1918 网段并排除 10.88.0.0/16。

示例：线路 1 走美国出口（`mdd-us`，RTP 10000–10011），线路 2 直连（RTP 10100–10111），控制面端口 8443，本机 LAN 地址 192.168.1.50/24：

```nft
table inet mdd
delete table inet mdd
table inet mdd {
  set lan4 { type ipv4_addr; flags interval; elements = { 192.168.1.0/24 } }

  chain mangle_pre {
    type filter hook prerouting priority -150; policy accept;
    iifname "mddv1" ct mark 0x6d640001 meta mark set 0x6d640001
    iifname "mddv1" ip daddr @lan4 udp sport 10000-10011 meta mark set 0x6d640001
    iifname "mddv2" ct mark 0x6d640002 meta mark set 0x6d640002
    iifname "mddv2" ip daddr @lan4 udp sport 10100-10111 meta mark set 0x6d640002
  }

  chain forward {
    type filter hook forward priority 0; policy accept;
    iifname "mddv1" meta mark 0x6d640001 accept
    iifname "mddv1" oifname "mdd-us" accept        # 仅当线路 1 的状态是 tun
    oifname "mddv1" ct mark 0x6d640001 accept
    oifname "mddv1" ct state established,related accept
    iifname "mddv2" meta mark 0x6d640002 accept
    iifname "mddv2" accept                          # 仅当线路 2 的状态是 direct
    oifname "mddv2" ct mark 0x6d640002 accept
    oifname "mddv2" ct state established,related accept
    iifname "mddv*" counter drop
    oifname "mddv*" counter drop
  }

  chain input {
    type filter hook input priority 0; policy accept;
    iifname "mddv*" ct state established,related accept
    iifname "mddv*" tcp dport 8443 accept           # 引擎回调 /api/engine/event
    iifname "mddv*" counter drop
  }
}

table ip mdd_nat
delete table ip mdd_nat
table ip mdd_nat {
  set local4 { type ipv4_addr; elements = { 192.168.1.50 } }
  set lan4 { type ipv4_addr; flags interval; elements = { 192.168.1.0/24 } }

  chain pre {
    type nat hook prerouting priority -100; policy accept;
    iifname != "mddv*" iifname != "mdd-*" ip daddr @local4 udp dport 10000-10011 ct mark set 0x6d640001 dnat to 10.88.1.2
    iifname != "mddv*" iifname != "mdd-*" ip daddr @local4 udp dport 10100-10111 ct mark set 0x6d640002 dnat to 10.88.2.2
  }

  chain post {
    type nat hook postrouting priority 100; policy accept;
    ip saddr 10.88.1.2 ip daddr @lan4 udp sport 10000-10011 masquerade
    ip saddr 10.88.1.2 oifname "mdd-us" masquerade  # P-2，推荐保留
    ip saddr 10.88.2.2 ip daddr @lan4 udp sport 10100-10111 masquerade
    ip saddr 10.88.2.2 oifname != "mddv2" masquerade # 仅直连
  }
}
```

- 线路状态是 `blocked` 时，生成器不输出该线路的 `oifname "mdd-<cc>" accept` / `iifname "mddvN" accept`，也不输出 TUN 的 masquerade，包会落到 `iifname "mddv*" drop`。
- IPv6：`inet` 族 forward 链里的两条 `drop` 同样作用于 IPv6；veth 两端都关了 IPv6。
- `input` 链让 netns 只能访问控制面端口；控制面主动连 AMI/WS 的回包靠 `established` 放行。
- 端口段由 orchestrator 校验：整数、1024–65535、宽度 ≤ 60、线路之间不重叠、不含控制面端口；不合格的线路按 `blocked` 处理并在 `proxy-status` 里写明原因。

### 12.7 控制面调用

- `/usr/local/sbin/mdd-enginectl`（root 所有，0755，POSIX sh，不读取任何 `mdd` 可写的文件）：

```sh
#!/bin/sh
# The only command the control plane may run as root (see /etc/sudoers.d/mdd).
set -eu
PATH=/usr/sbin:/usr/bin:/sbin:/bin
usage() { echo "usage: mdd-enginectl {start|stop|restart|down|reset-failed} <line 1-10>" >&2; exit 64; }
[ "$#" -eq 2 ] || usage
case "$2" in [1-9]|10) line=$2 ;; *) usage ;; esac
case "$1" in
  start|stop|restart|reset-failed) exec systemctl "$1" "mdd-engine@$line.service" ;;
  down) exec systemctl stop "mdd-engine@$line.service" "mdd-netns@$line.service" ;;
  *) usage ;;
esac
```

- `/etc/sudoers.d/mdd`（安装时用 `visudo -cf` 校验）：

```
Defaults:mdd !requiretty, !lecture, env_reset, timestamp_timeout=0
mdd ALL=(root) NOPASSWD: /usr/local/sbin/mdd-enginectl
```

  bullseye 的 sudo 是 1.9.5，不支持参数正则，所以参数校验放在脚本里，测试覆盖。
- 控制面：`subprocess.run(["sudo", "-n", ENGINECTL, action, iid], timeout=60)`；`ENGINECTL` 可由环境变量覆盖，测试里换成假的脚本。
- 只读操作都不经 sudo：`systemctl show`、`journalctl -u mdd-engine@N`（`mdd` 加入 `systemd-journal` 组，阶段 5 安装时做，见 P-12）、`asterisk -r -s` 控制套接字（套接字在 `instances/N/run/asterisk/`，组 `mdd`，0660）。
- 控制面单元（阶段 5）不能设 `NoNewPrivileges=yes` 或收窄 `CapabilityBoundingSet`，否则 sudo 无法提权；这是"用 sudo 不用 polkit"（D4）的代价，见 R-4。

### 12.8 出口断开时的阻断与测试方案

阻断分三层，任何一层单独都不会让 IKE 走默认路由：

1. **启动前**（沿用）：`egress.ensure_line()` 等到 `proxy-status.lines[N].ready`，否则 `EgressError`、不启动；代理模式下 ePDG 经本国 SOCKS 解析，失败也不启动。
2. **路由闸**（新增）：表 1000+N 只有在 `tun` 状态才有可用默认路由；TUN 消失时内核自动删路由。
3. **过滤闸**（新增）：forward 链只放行到本国 TUN（或显式直连），其余 `drop`。

线路已在运行时出口断开：不主动停止引擎，swu_ike 的重传在宿主上被 unreachable 拒绝（不出网卡），控制面现有健康策略照常判定（冻结 → 重选节点 / 退避）。出口恢复后 orchestrator 加回路由，swu_ike 下一次重连即恢复。

测试：

1. 单元测试（`tests/test_netns_rules.py`，普通 CI job 里跑）
   - `line_route_state()` 真值表：出口 ready → tun；未就绪 / 出错 / 未配置 / 国家未知 / 线路停用 → blocked；全局关代理或显式 direct → direct；TUN 网卡不存在 → blocked。
   - 生成的规则：任何状态下 `up` 命令都含 unreachable 兜底与 `iif` 规则；blocked 线路没有任何 accept 到 TUN 或上游的规则、没有 TUN 的 masquerade；每条线路都被 `mddv*` drop 覆盖；线路 1 的规则不出现 `mddv2`/`10.88.2.`；DNAT 只匹配 `@local4` 和本线路端口段。
   - 0.9.8 兼容的词法检查：生成文本里不出现 `fib`、`typeof`、`flowtable`、`priority dstnat|srcnat|filter|mangle` 等；NAT 只在 `ip` 族表。
   - orchestrator 状态迁移（假执行器记录命令）：ready → 未就绪时，同一轮里先删默认路由再替换 nft；换国家时顺序为"删旧路由 → nft → 加新路由"；sing-box 进程退出时 `_sleep_for_work` 立即返回。
   - `mdd-enginectl`：用假的 `systemctl` 放在 PATH 前面，验证合法参数被原样转发，`0`、`11`、`01`、`1;id`、`../1`、多余参数等全部以 64 退出且不调用 systemctl。
   - 单元文件：固定 `NetworkNamespacePath`、`BindsTo`、`NoNewPrivileges=yes`、`ProtectSystem=strict`、能力集、`DeviceAllow`、`LimitCORE=0`；不存在来自 `/var/lib/mdd` 的 `EnvironmentFile`。
   - engine.env 注入：instance.json 的 PIN/IMEI/APN 等字段含 `$(touch x)`、反引号、换行、单双引号时，`set -a; . engine.env` 后值原样保留，且没有命令被执行。
   - 日志：默认渲染的 logger.conf/asterisk.conf 不含 `debug` 通道、`debug=0`；模板里没有 `pjsip set logger on`。
2. 特权集成测试（新 job `netns-leak`，`ubuntu-latest` + `sudo`；`tests/integration/test_netns_leak.py`，非 root 或未设 `MDD_NETNS_IT=1` 时跳过）。为了不碰 runner 自己的默认路由，整个拓扑放在一个模拟宿主的 netns 里：模拟宿主 `it-host`、模拟上游 `it-wan`（nft 计数器统计到达的包）、模拟 LAN 浏览器 `it-lan`、线路 netns `mdd-1`、模拟国家 TUN（`ip tuntap` + 一个读包计数的小进程，代替 sing-box）。规则用 `mdd_netns.py` 生成并在 `it-host` 里执行。场景：
   - a. 出口就绪：`mdd-1` 发 UDP 500/4500 到 198.51.100.10 → TUN 计数增加，`it-wan` 为 0。
   - b. 停掉读包进程并删除 TUN（模拟 sing-box 退出）：`ip route get 198.51.100.10 from 10.88.1.2 iif mddv1` 返回 unreachable，发包后 `it-wan` 为 0。
   - c. TUN 仍在、状态改为未就绪：路由被删，同上。
   - d. 只删 nft 表（模拟过滤闸丢失），TUN 不在：路由闸仍挡住，`it-wan` 为 0。
   - e. 只删 ip 规则（模拟路由闸丢失），nft 在、TUN 不在：包按主表走向 `it-wan`，被 forward drop，`it-wan` 为 0，drop 计数增加。
   - f. 直连：包到达 `it-wan`，源地址是 masquerade 后的上游地址。
   - g. RTP：`it-lan` 发 UDP 到宿主 LAN 地址的 10000 端口 → 到达 `mdd-1` 的 10.88.1.2；回包回到 `it-lan`，TUN 计数不变；`mdd-1` 主动从 10000 发往 `it-lan` → 到达且源地址是宿主 LAN 地址。
   - h. IPv6：`mdd-1` 发 IPv6 包 → 不出 `it-host`。
3. 0.9.8 语法检查（新 job `nft-bullseye`）：在 GitHub runner 上 `docker run --privileged debian:bullseye`，apt 安装 nftables 0.9.8，对生成器输出的若干组配置执行 `nft -c -f`（只检查不加载）。Docker 只用于 CI，不进发布包，见 P-9。

### 12.9 风险

| # | 风险 | 应对 |
|---|---|---|
| R-1 | 不做 masquerade 时 sing-box 收到源地址为 10.88.N.2 的包；现在的 Docker 路径里，容器流量进 TUN 前被 Docker 的 `MASQUERADE ! -o docker0` 改成了 TUN 地址，这种情况从没跑过。 | P-2 推荐保留 TUN masquerade，与现状一致；集成测试 a 覆盖转发路径；阶段 7 设备实测。 |
| R-2 | 去掉 /32 ePDG 路由后，从 TUN 回来的包的源地址（ePDG）在主表里指向上游网卡，严格 `rp_filter` 会丢包。 | orchestrator 对 TUN 与 `mddvN` 设 `rp_filter=2`（内核取 `all` 与接口值的较大者，2 即宽松）；集成测试在 `it-host` 里把 `all.rp_filter` 设为 1 来跑。 |
| R-3 | ePDG 改为启动时解析一次的 IP；运营商轮换 DNS 记录后，旧 IP 停用前线路不受影响，停用后要等健康策略重启引擎（重启时重新解析）。原生 Docker 版是引擎每次重连自己解析。 | 记录到阶段 7 观察；需要时再让 swu_ike 重连前请求控制面重新解析。 |
| R-4 | 控制面要能用 sudo，单元里不能用 `NoNewPrivileges` 和能力集收窄；`ProtectSystem=strict` 下 sudo 能否正常写时间戳/日志需实测。 | sudoers 已设 `timestamp_timeout=0`；阶段 5 在设备上验证，不行就给 `/run/sudo` 加 `ReadWritePaths`。 |
| R-5 | `settings.bind` 若被设成某个 LAN 地址，引擎回调 `10.88.N.1` 连不上。 | 控制面启动时检查，bind 不是 `0.0.0.0` 时在系统状态里告警；阶段 4 加测试。 |
| R-6 | 10.88.0.0/16 与用户 LAN 冲突。 | 阶段 5 install.sh 检查并拒绝安装；orchestrator 发现冲突时所有线路 `blocked` 并报原因（P-11）。 |
| R-7 | `ip_forward=1` 是全局设置，宿主的其他网卡之间也会转发。forward 链对非 `mddv*` 流量保持 `accept`，不改变用户已有行为。 | 在 INSTALL 文档写明。 |
| R-8 | systemd 247 对 `NRestarts` 在手动启动时是否清零、`TemporaryFileSystem` 挂载点是否自动创建，按文档理解，没在设备上验证。 | 阶段 7 验证；单元文件改动不影响其余设计。 |
| R-9 | 能力集收窄后 Asterisk 或 swu_ike 可能缺少某个能力（例如 `CAP_NET_RAW` 是否需要）。 | 设备上按日志补，阶段 7 记录。 |

### 12.10 待定问题（2026-10-28 已答复：全部按括号里的推荐执行）

- **P-1** `/docs`、`/openapi.json`、`/redoc` 现在不登录即可访问（拆分前就如此）。是否在阶段 4 关闭？（推荐关闭：`FastAPI(docs_url=None, redoc_url=None, openapi_url=None)`，不影响 /api/ 基线。）
- **P-2** 代理模式下是否对进入 TUN 的流量做 masquerade？§4.3 原写"不做"。（推荐做：与现在的 Docker 路径一致，sing-box 只看到本机地址；它不影响防泄漏，因为 masquerade 只作用于已被放行到 TUN 的包。）
- **P-3** 是否允许引擎从自己的 RTP 端口主动发往宿主直连私网（浏览器）的 UDP 走主表并 masquerade？§4.3 只放行了 DNAT 回包。（推荐允许：Asterisk 做完整 ICE，会先向浏览器发连通性检查；只放行回包时，这些包会被导向 TUN 或不可达；按 conntrack 的规则推断，它们还会占住同一个 5 元组，使浏览器随后的入站包 DNAT 冲突，可能造成通话无声（推断，未实测，集成测试 g 会覆盖）。Docker 版允许这种流量。限制为：源端口在本线路 RTP 段、目的在 RFC 1918 直连网段、仅 UDP。）
- **P-4** 引擎路径：§4.3 写"沿用容器内路径（`/config`、`/logs`、`/run/mdd-sim-gateway`）用 BindPaths 映射"。这需要在宿主根目录建 `/config`、`/logs` 挂载点。（推荐改为环境变量 `MDD_CONFIG`、`MDD_LOGDIR`、`MDD_RUNDIR` 直接指向 `/var/lib/mdd/instances/N/...`，默认值保持容器路径，约 15 处代码改动；Asterisk 自己的目录仍用 `TemporaryFileSystem`。）
- **P-5** D4 写"`rtp_forward.py` 改写为 nft DNAT 规则生成"。规则要由 root 的 orchestrator 应用，所以生成器放在 `host/mdd_netns.py`，`control/app/rtp_forward.py` 删除。可以吗？（推荐可以；只是位置变化。）
- **P-6** 线路上限 2 的开关放在哪里？（推荐：WebUI 系统设置里 1/2 二选一，改小时自动停止超出的线路，沿用现有 `line_allowed` 逻辑；也可以只允许改 config.yaml。）
- **P-7** Asterisk core dump 是否默认关闭？现在容器里是开启的（`-g` + `ulimit core unlimited`），设备根分区只有 1.6 GB。（推荐关闭，排障时再在单元 drop-in 里打开。）
- **P-8** 出口 UDP 健康检查：现在是手动测试（`/api/egress/{country}/test`、节点测试）加线路健康策略触发的节点重选，没有周期性 UDP 探测。阶段 4 是否保持不变？（推荐保持，只把 SOCKS 测试入口从 172.17.0.1 改到 127.0.0.1；新增周期探测会引入误判导致的线路闪断。）
- **P-9** CI 的 nft 0.9.8 语法检查用 `docker run --privileged debian:bullseye`。Docker 只在 CI 出现。可以吗？（推荐可以；GitHub runner 自带 nftables 1.x，无法检查 0.9.8 语法。）
- **P-10** `mdd-enginectl` 的线路号上限：固定 1–10（与可能存在的旧配置 ID 兼容），还是读取 `max_lines`？（推荐固定 1–10：root 脚本不解析 `mdd` 可写的配置；超过 `max_lines` 的线路由控制面拒绝启动。）
- **P-11** 10.88.0.0/16 固定还是可配置？（推荐固定，冲突时安装拒绝并提示。）
- **P-12** 引擎日志用 journald（`mdd` 进 `systemd-journal` 组，可读全部系统日志），还是让 entrypoint 把输出写进 `instances/N/logs/engine.log`（复用 `log_capture.py` 轮转）？（推荐 journald：改动最小、日志有时间戳和大小上限；如果你介意 `mdd` 能读整个系统日志，就选文件方案。）

### 12.11 实施顺序（确认后按此提交，每步全量测试 + `npm run build`）

1. `host/mdd_netns.py` + `tests/test_netns_rules.py`（纯函数，不接入任何调用方）
2. `packaging/`：两个单元文件、`mdd-enginectl`、sudoers 片段 + 测试
3. orchestrator 接入 `reconcile_lines()`，删除 /32 路由，SOCKS 改监听 127.0.0.1
4. 控制面 `engine.py`/`runtime.py` 改为 systemd 后端，ePDG 解析，`max_lines`，manager_url，删除 `rtp_forward.py`、media relay、`media_supervisor`、requirements 里的 docker
5. 引擎：entrypoint 去 relay、路径环境变量、asterisk.conf 控制套接字、logger 默认无 debug
6. operations/sysinfo/system 路由的 Docker 部分 + WebUI 存储页、媒体面板、i18n、线路上限设置
7. CI：`netns-leak`、`nft-bullseye`
