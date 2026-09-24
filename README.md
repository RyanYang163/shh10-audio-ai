# AI Audio Analyzer（音频 AI 分析器）

> TOS 7 Deb 单包应用 · WebUI 内嵌（iframe）· 版本 **1.0.0**

| 项 | 值 |
|---|---|
| 应用 ID | `shh10-audio-ai` |
| 包类型 | Deb 单包（`application_type: "deb"`） |
| 打开方式 | WebUI 内嵌（`type: "iframe"`，`path: "/shh10-audio-ai/"`） |
| 版本 | 1.0.0 |
| 分类 | `Audio_Video_Entertainment`, `Artificial_Intelligence` |
| 发布者 | shh |
| 开发者仓库 | <https://github.com/RyanYang163/shh10-audio-ai> |
| 隐私政策（公网可访问） | <https://github.com/RyanYang163/shh10-audio-ai/blob/main/PRIVACY.md> |

## 简介

读取音频信息、检测静音段落，并在转写文本与 LRC / SRT / VTT / TXT 字幕之间互转。

Inspect audio files, detect silence, and convert between transcript and subtitle formats (LRC / SRT / VTT / TXT).

## 功能

- WAV / FLAC / MP3 / OGG / M4A 元数据读取（时长、采样率、声道、比特率）
- WAV → PCM 解码，能量 + 过零率静音/有声段落检测
- 波形峰值与静音区间可视化
- 转写文本 ⇄ LRC / SRT / VTT / TXT 双向转换
- 歌词模式：短句自动合并、时间轴平移、批量目录转换
- 全部离线；可选接入本机 faster-whisper 或自配远程接口

全部功能由 **Python 标准库 + 原生 HTML/CSS/JS** 实现，**不含任何第三方代码、第三方
图标字体或前端框架**，因此本仓库的许可证情况极为简单：全部为本项目自有代码，以 MIT 发布。

## 可选增强引擎

本应用的核心功能**不依赖**下列任何一项；它们只是「检测到就用」的增强。
未安装时应用照常启动，只是对应能力在界面上标记为不可用。

| 引擎 | 必需性 | 说明 |
|---|---|---|
| `faster-whisper` | 可选 | 检测到即启用；未检测到时相关功能显示为「不可用」，不影响其它功能 |
| `remote-api` | 可选 | 检测到即启用；未检测到时相关功能显示为「不可用」，不影响其它功能 |

> `.lang` 的 `descript` 字段**只描述离线可用的功能**，不承诺这里的能力，
> 避免触发审核项「描述与实际功能不符」。

## 接口一览

前端只调这些接口；全部走平台代理（`/v2/proxy/shh10-audio-ai/`），后端同时兼容
`/`、`/<appid>/`、`/v2/proxy/<appid>/` 三种前缀。

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/audio/probe?path=` | 音频元数据（时长 / 采样率 / 声道 / 位深 / 比特率 / 标签） |
| GET | `/api/audio/analyze?path=` | 波形峰值 + 静音区间 + 逐帧 RMS / 过零率（仅 WAV；带缓存，`force=1` 跳过） |
| GET | `/api/audio/subtitles?path=` | 解析字幕文件为 cue 列表（内容识别格式，可用 `format=` 强制） |
| POST | `/api/audio/convert` | 字幕转换（`{inputs\|roots, output_dir, format, mode, offset, merge_short}`），**进队列** |
| POST | `/api/audio/transcribe` | 语音转写（引擎不可用时返回 503 + 指引），**进队列** |
| GET | `/api/audio/engines` | 转写引擎状态（**响应里不含任何凭据**） |
| POST | `/api/audio/engines` | 配置远程转写接口；**启用必须带 `confirm: true`** |
| GET | `/api/audio/files?kind=&q=&limit=` | 已收录文件（`kind=audio` 默认 / `subtitle` / `all`） |
| GET | `/api/audio/summary` | 概览统计 |
| GET | `/api/audio/results?job_id=` | 转换结果记录 |
| GET | `/api/audio/export?format=csv\|json` | 导出已收录清单 |
| GET | `/api/audio/detect?path=` | 快速判断路径能做哪类操作 |
| POST | `/api/jobs` | `{type: "scan"\|"convert"\|"analyze"\|"transcribe", params}` |

任何涉及用户文件的 `path` 都先经 `realpath` 再比对白名单：**白名单外一律 403**，
目录穿越与指向白名单外的软链同样被拒。

## 静音检测的口径与实测数字

口径（都可在「设置」里改）：

| 参数 | 默认值 | 含义 |
|---|---|---|
| 帧长 | 20 ms | 逐帧算 RMS / 过零率的窗口；**静音边界必然量化到帧** |
| 阈值 | -45 dBFS | 帧 RMS 低于它判为静音 |
| 最短静音 | 300 ms | 更短的静音段丢弃（词间换气不算「静音段落」） |
| 同步分析上限 | 120 秒 | 超过就走任务队列（409 + 指引），避免占住一个 HTTP 请求 |

**容忍度 = 一个帧长（20 ms）**：跨越边界的那一帧只要含信号能量（哪怕只占 15%）就被判为
「有声」，所以实测静音段通常比理论值短一点点。合成样本上的实测值（`tests/test_pcm.py`）：

| 样本 | 理论 | 实测 | 说明 |
|---|---|---|---|
| 0.5 s 静音 + 1 s 正弦 + 0.5 s 静音 | [0, 0.500] / [1.500, 2.000] | **[0, 0.500] / [1.500, 2.000]** | 边界正好落在帧边界上，完全一致 |
| 0.523 s 静音 + 0.4 s 正弦 + 0.62 s 静音 | 边界 0.523 / 0.923 | **0.520 / 0.940** | 故意错开帧边界：前者早 3 ms、后者晚 17 ms，都 ≤ 1 帧 |
| 正弦波 RMS（振幅 0.5） | -9.03 dBFS | **-9.03 dBFS** | 与 20·log₁₀(0.5/√2) 吻合 |
| 440 Hz 过零率 | 2×440/44100 = 0.01995 | **0.01995** | 逐帧统计值 |

**吞吐**：本机实测 **5.9 ~ 6.6 M 样本/秒**（多次运行区间；44.1 kHz 立体声 ≈ 每秒可处理
约 70 秒音频 —— 一分钟的歌约 0.9 秒，一小时的录音约 50 秒）。分析是**流式**的：
内存占用与文件长度无关（每块 1 MB，块之间会响应取消/暂停）。

## 权限声明

| 权限 | 用途 | 说明 |
|---|---|---|
| 网络：无宿主端口 | — | iframe 应用经平台代理与后端通信，只监听 Unix socket，不占用任何宿主端口 |
| 文件系统：`/Volume*/@apps/shh10-audio-ai/data` | 运行期数据 | 由应用创建；是**唯一**的写入位置 |
| 文件系统：用户选择加入白名单的目录 | 读取用户文件 | **默认只读**；白名单初始为空，必须由用户显式添加 |
| 系统用户：`shh10-audio-ai` 去连字符 | 隔离运行 | 由平台在安装时创建；**非 root** |
| 共享文件夹 | 不使用 | 不需要额外共享文件夹 |
| 容器 / 特权 | 不使用 | 本应用不是 Docker 应用，不使用任何特权能力 |

资源上限（systemd 单元内声明）：`MemoryMax=1024M`、`CPUQuota=200%`、
`LimitNOFILE=65536`、`LimitNPROC=256`。

## 运行时写入路径清单（指引 12.9.6）

| 路径（除特别注明外，均相对 `/Volume*/@apps/shh10-audio-ai/data/`） | 用途 | 格式 | 创建时机 | 增长上限 / 轮转 | 生命周期 |
|---|---|---|---|---|---|
| `config/runtime.json` | 应用运行配置（含可访问目录白名单、权限级别等） | JSON | 首次启动创建，配置变更时重写 | < 64 KB | 持久，随升级保留 |
| `db/app.db`（及同目录 `-wal` / `-shm`） | 任务队列与应用数据（SQLite） | SQLite | 首次启动建库；任务与扫描结果写入 | 已完成任务只保留最近 200 条，超出自动清理 | 持久，随升级保留；**升级绝不删库**（`PRAGMA user_version` 迁移） |
| `cache/` | 可再生的缓存（扫描索引、图片哈希等） | 二进制 / JSON | 按需生成 | 上限 2 GB，超出按 LRU 清理 | 可再生，可安全删除 |
| `tmp/` | 处理中的临时文件 | 二进制 | 任务开始时创建 | 任务结束即删；**每次启动清空全部残留** | 临时，自动清理 |
| `logs/app.log`（及 `app.log.1` … `.5`） | 应用日志（标准格式，敏感信息已脱敏） | 文本 | 服务运行时持续写入 | 单文件 2 MB，轮转保留 5 个（约 12 MB 上限） | 持久，自动轮转 |
| `output/` | 用户主动生成的结果文件 | 任意 | 用户提交导出 / 提取类任务时创建 | 由用户自行管理 | **属于用户数据，卸载不会删除** |
| `/var/api/shh10-audio-ai.sock`（绝对路径，非 data 下） | 平台代理 Unix socket，mode `0660` | Unix socket | 服务启动时创建 | 不适用 | 每次启动前先删除残留 |
| `/var/lib/shh10-audio-ai/`（绝对路径，systemd `StateDirectory`） | 兼容路径下的应用状态 | 目录 | systemd 创建 | 不适用 | systemd 管理 |
| `/var/log/shh10-audio-ai/`（绝对路径，systemd `LogsDirectory`） | 兼容路径下的日志 | 目录 | systemd 创建 | 不适用 | systemd 管理 |
| `/run/shh10-audio-ai/`（绝对路径，systemd `RuntimeDirectory`） | 运行时目录 | 目录 | systemd 创建 | 不适用 | 服务停止时由 systemd 删除 |

**本应用不写入上表以外的任何路径**，尤其**不写共享的系统 `/tmp`**——临时文件一律
走 `PrivateTmp` 提供的私有 `/tmp` 或上表中的 `data/tmp/`（指引 12.9.6）。

## 隐私政策（审核项 C3 / C4 / C5）

**公网地址（可直接访问）**：<https://github.com/RyanYang163/shh10-audio-ai/blob/main/PRIVACY.md>

包内另有 `PRIVACY.md` 全文，`config.ini` 的 `help` 字段指向同一地址——三条路径都可查。

- **本地优先**：读取音频信息、检测静音段落，并在转写文本与 LRC / SRT / VTT / TXT 字幕之间互转。所有处理都在你的 TNAS 上完成。
- **不采集**：不收集任何使用统计、遥测、设备标识或个人信息。
- **不上传**：不会把你的文件、内容或分析结果发送到任何服务器。

## 构建

```bash
./build.sh            # 默认 x86_64
./build.sh aarch64
```

产物在 `build/output/`：

```
shh10-audio-ai_x86_64.deb
shh10-audio-ai_x86_64.deb.sha256
```

> 包文件名**不含版本号**——版本由 GitHub Release 的 tag 承载（指引 15.1 / 4.2.4）。
> 构建器是 `tools/build_deb.py`，纯 Python 实现，不依赖 `dpkg-deb`，因此在
> Windows 开发机上也能产出真 deb 并做结构自检：
> `python3 tools/build_deb.py --inspect build/output/shh10-audio-ai_x86_64.deb`

## 本地运行（无需 TOS 设备）

```bash
python3 src/__main__.py --tcp 127.0.0.1:18010 --data-dir ./data
# 浏览器打开 http://127.0.0.1:18010/
```

同一份业务代码，只是把 Unix socket 换成 TCP。跑测试：

```bash
python3 -m unittest discover -s tests -v
```

## 安装、升级与卸载验证

```bash
# 安装
sudo dpkg -i shh10-audio-ai_x86_64.deb
sudo systemctl status shh10-audio-ai
sudo journalctl -u shh10-audio-ai -f

# socket 是否就绪（iframe 应用的关键一项）
ls -l /var/api/shh10-audio-ai.sock
curl --unix-socket /var/api/shh10-audio-ai.sock http://localhost/health

# 启停
sudo systemctl restart shh10-audio-ai

# 卸载（保留数据）
sudo dpkg --remove shh10-audio-ai
# 彻底卸载（仍按设计保留数据盘上的运行数据）
sudo dpkg --purge shh10-audio-ai

# 升级（数据必须保留）
sudo dpkg -i shh10-audio-ai_x86_64.deb
```

**卸载后残留说明**：`dpkg --purge` 会删除 `/usr/local/shh10-audio-ai`、`/var/api/shh10-audio-ai.sock`、
`/var/lib/shh10-audio-ai`、`/var/log/shh10-audio-ai`、systemd 单元与专用用户。
**数据盘上的 `/Volume*/@apps/shh10-audio-ai/data/` 按设计保留**——其中 `output/` 是用户跑出来的
结果文件，属于用户数据（指引 51 / 12.9.7 要求卸载默认保留用户数据）。需要彻底清理时手动执行：

```bash
sudo rm -rf /Volume*/@apps/shh10-audio-ai
```

## 安全设计

- **非 root 运行**：专用系统用户，`User=` / `Group=` 与 `config.ini` 的 `user` 字段一致。
- **systemd 加固**：`NoNewPrivileges`、`ProtectSystem=strict`、`ProtectHome`、
  `PrivateTmp`、`PrivateDevices`、`RestrictSUIDSGID`、`LockPersonality`、
  `RemoveIPC`、`SystemCallArchitectures=native`，可写路径用 `ReadWritePaths` 显式枚举。
- **无 shell 执行入口**：所有命令都是固定映射的具体能力，不存在
  `POST /exec` 这类接受任意命令行的接口。
- **路径白名单**：所有涉及用户文件的操作都先 `realpath` 再比对白名单根，
  目录穿越与指向白名单之外的 symlink 一律拒绝。
- **日志脱敏**：日志写入前对密码、Token、API Key、Cookie、`Bearer` 凭据做兜底打码。
- **生命周期脚本无网络操作**：`preinst` / `postinst` 只做目录、属主与服务注册，
  不含 `apt` / `pip` / `curl`。
- **不写系统目录**：不在 `/etc`、`/usr`、`/boot` 下写任何运行期配置。

## 许可证

本项目自有代码以 **MIT** 发布，全文见 [`LICENSE`](./LICENSE)。
不含任何第三方代码；第三方资源的署名与说明见 [`NOTICE`](./NOTICE) 与
[`THIRD_PARTY_NOTICES.md`](./THIRD_PARTY_NOTICES.md)。

## 真机验证状态（诚实声明）

**本应用目前只在开发机上通过了全部自动化测试与规范门禁，尚未在真机上安装验证。**

同批次的 `shh11-media-audio` 已走通完整链路（上传 → 解析 → 安装 → 启动 → 平台代理 → 前端渲染），
其余应用复用的是同一套打包器、systemd 单元模板与前端框架，但**「同结构」不等于「这个包也验过」** ——
提交前应逐个在真机上装一遍。

### 两点容易被误判的配置，都是实测结论

**1）systemd 单元里刻意没有 `PrivateTmp=true`。**
TOS 上 `/var/api` 与 `/var/log` 都是指向 `/tmp` 的软链（`/tmp` 是 tmpfs）。iframe 应用必须把
Unix socket 建在 `/var/api/<appid>.sock`，而 `PrivateTmp=true` + `ReadWritePaths=/var/api`
会让 systemd 建不出命名空间，服务直接 `226/NAMESPACE` 启动失败；即便命名空间建成，
socket 也会落在**私有** `/tmp` 里，平台 nginx 在宿主上永远看不到它 —— iframe 应用会彻底不可用。
（该项在指引 12.7 中属「Recommended」；其余加固项全部保留。）

**2）单元里必须给 `AmbientCapabilities=CAP_DAC_OVERRIDE`。**
`/var/api` 的权限是 `755 root:root`，非 root 的应用用户既不能在里建文件也不能 unlink
（实测 `touch` 与 `socket.bind()` 都是 `Permission denied`）。没有这个能力时，服务会以
`status=1/FAILURE` 反复重启、socket 永不出现。
`CapabilityBoundingSet=CAP_DAC_OVERRIDE` 把能力集从内核默认的 **41 个收窄到 1 个**，
是净减少；授予的那一个正是「在平台自有的 `/var/api` 里创建自家 socket」所必需的 ——
即指引 12.7「drop all capabilities, add only required ones」的写法。

> 附注：设备上另外几个第三方应用（含已通过审核的同批应用）正是因为第一条而以
> `226/NAMESPACE` 处于 failed 状态。
