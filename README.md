# <img src="custom_components/pinecone_energy/brand/icon@2x.png" width="56" align="top"> 松果能源充电桩 · Home Assistant 集成

基于对官方 App `松果能源 V2.1.1`（`csg.pinecone`）的逆向分析，把充电桩的**云端 HTTP 接口**封装成 Home Assistant 集成。

- ✅ 手机号 + 短信验证码登录，token 自动保存、失效自动提醒重新认证
- ✅ 纯云端通信，**不需要蓝牙**，不需要手机常连桩
- ✅ 启动/停止充电、解锁枪、电流/功率限制、静音模式、智联续充、即插即充、计划充电、实时数据、充电统计
- ✅ 官方 LOGO 图标（从 APK 矢量资源生成，含深色模式版本）
- ✅ 支持 HACS 自定义仓库安装

逆向细节见 [`docs/API提取报告.md`](docs/API提取报告.md)。

## 安装

### 方式 A：HACS（推荐）

1. HACS → 右上角 ⋮ → **自定义仓库（Custom repositories）**
2. 仓库地址填你的 GitHub 仓库，类型选 **Integration**，添加
3. 在 HACS → Integrations 里搜索「松果能源充电桩」→ 下载
4. **重启 Home Assistant**
5. 设置 → 设备与服务 → 添加集成 → 松果能源充电桩

> 想发布成 HACS 仓库：把本目录直接推到 GitHub（根目录已含 `hacs.json`），打一个 `v1.2.1` Release 即可；`manifest.json` 里的 `documentation` / `issue_tracker` 换成你自己的仓库地址。

### 方式 B：手动安装

把 `custom_components/pinecone_energy` 复制到 HA 的 `config/custom_components/` 下，重启 HA，然后添加集成。

```
config/custom_components/pinecone_energy/
├── __init__.py
├── api.py
├── binary_sensor.py
├── brand/                  ← 官方 LOGO（HA 2026.3+ 自动读取）
│   ├── icon.png / icon@2x.png
│   ├── dark_icon.png / dark_icon@2x.png
│   └── logo.png / logo@2x.png (+ dark)
├── button.py
├── config_flow.py
├── const.py
├── coordinator.py
├── entity.py
├── manifest.json
├── number.py
├── plan.py
├── select.py
├── sensor.py
├── strings.json
├── switch.py
├── time.py
└── translations/
```

### 登录流程

输入 App 登录手机号 → 收到 6 位短信验证码 → 输入 → 勾选要接入的充电桩。之后每台桩生成一个设备，实体自动创建。

> 解绑/换绑充电桩后在集成「配置」里重新勾选即可，不用重新登录。

## 关于图标

图标不是自己画的，全部来自 APK 里的官方品牌资源：

| 来源（APK 内路径） | 用途 |
|---|---|
| `static/images/login/brand-songguo.svg` | 横版「松果」LOGO → `logo.png` / `logo@2x.png` |
| `static/images/login/logo-mark.svg` | 松果图形标 → `icon.png` / `icon@2x.png` |
| 品牌色 `#0B5869` | 取自 `brand-songguo-mark.png` 实际像素 |

均为矢量渲染，规格符合 HA brand 规范（icon 256/512 方形、logo 短边 128–256 / 256–512，RGBA 透明底），并额外提供 `dark_*` 深色模式版本。

> HA **2026.3** 起支持自定义集成自带的 `brand/` 目录，图标会直接显示在集成页；更早的版本会显示通用占位图标（不影响功能）。

## 实体一览（每台桩）

| 平台 | 实体 | 说明 |
|---|---|---|
| sensor | 充电状态 | 空闲中 / 已连接 / 充电中 / 充电完成 / 已充满 / 充电暂停 / 已离线 … |
| sensor | 充电功率 (kW) | 实时功率 |
| sensor | 本次充电电量 (kWh) | 当前订单电量（空闲时未知） |
| sensor | 本次充电时长 (min) | |
| sensor | 电池电量 (%) | 车辆 SOC（桩上报值） |
| sensor | 充电电压 (V) / 充电电流 (A) | |
| sensor | 4G 信号 | 0~4 格 |
| sensor | 今日充电电量 / 次数 / 时长 | 云端统计 |
| sensor | 本周充电电量 (kWh) | 本周（周一至今）逐日汇总 |
| sensor | 本月充电电量 (kWh) | `charge/record/stats` MONTH |
| sensor | 今年充电电量 (kWh) | `charge/record/stats` YEAR |
| sensor | 当前订单号 | 默认隐藏 |
| binary_sensor | 在线 / 正在充电 | |
| switch | 开始充电 | 开=下发启动，关=按当前订单号停止 |
| switch | 静音模式 | `private/set/silent/status` |
| switch | 智联续充 | `private/set/self/recovery` |
| switch | 即插即充 | `private/set/auth` |
| switch | 计划充电 | 启用/关闭计划充电 |
| switch | 公共充电桩 | 默认隐藏，`pile/share` |
| number | 电流上限 (A) | 交流桩，6~16/32 A |
| number | 功率上限 (%) | 直流桩，0~100% |
| button | 解锁充电枪 | `private/gun/unlock` |
| button | 刷新数据 | 立即拉取 |
| button | 重新登录（短信验证码） | 随时重新认证；token 失效时依然可用 |
| time | 计划开始 / 结束时间 | |
| select | 计划重复方式 | 每天 / 单次 |

## 自动化示例

**充满后通知：**

```yaml
automation:
  - alias: 充电桩充满提醒
    trigger:
      - platform: state
        entity_id: sensor.mo_hui_hua_yuan_charging_status
        to: "已充满"
    action:
      - service: notify.mobile_app_xxx
        data:
          message: 车辆已充满，可以拔枪了
```

**低电价时段充电：**

```yaml
  - alias: 每晚 22 点开启计划充电
    trigger:
      - platform: time
        at: "21:55:00"
    action:
      - service: switch.turn_on
        target:
          entity_id: switch.mo_hui_hua_yuan_scheduled_charging
```

**插枪自动充电：**

```yaml
  - alias: 插枪自动充电
    trigger:
      - platform: state
        entity_id: sensor.mo_hui_hua_yuan_charging_status
        to: "已连接"
    action:
      - service: switch.turn_on
        target:
          entity_id: switch.mo_hui_hua_yuan_start_charging
```

## 验证状态（2026-11-25 真机实测）

已在真实账号 + 真实桩（21 kW 交流桩，协议 V2）上跑通：

- ✅ 短信验证码登录、token 复用、失效返回 `030400` 并被识别为需要重新认证
- ✅ 设备列表、桩详情、枪状态（0=空闲中）、4G 信号（4 格）、电流上限（32 A）、计划充电（每天 00:00–08:00）、面板配置、今日统计
- ✅ 本地 HA 2025.1.4 实例接入成功：28 个实体按真实数据创建，配置流程/选项流程正常，重启后 entity_id 稳定
- ✅ 周/月/年电量：`MONTH`/`YEAR` 接口真实返回（`data.totalElect`，附 `chartBuckets` 分桶）；`WEEK` 服务端不支持，改为本周逐日汇总，真机复测 27 个实体全绿
- ✅ token 不是定时过期，而是**单会话**：在 App 或其他地方再登录一次，旧 token 立刻失效；失效后 HA 自动弹出重新认证，也可以随时按「重新登录（短信验证码）」按钮

实测中修正的推断错误：

1. `pileType` 实际是 `"0"` 字符串 → 交直流改按 `equipmentType.code`（2=交流/1=直流）和 `guns[0].gunType`（慢充/快充）判断
2. 桩详情返回 `{success, data:{...}}`，旧解包逻辑会把字段留在嵌套层
3. 列表接口 `silentModeStatus`，详情接口 `silentMode`，命名不一致
4. `charge/record/stats` **不支持 `DAY`/`WEEK` 周期**（只有 MONTH/YEAR 可用）→ 今日数据改用日记录接口的当日汇总
5. `private/set/charge/strategy`、`private/power` 的 `isTrue` 仅蓝牙路径携带，云端不能带

## 调试

`tools/pinecone_cli.py` 可独立验证接口（不依赖 HA）：

```bash
python3 tools/pinecone_cli.py send-code --phone 138xxxxxxxx
python3 tools/pinecone_cli.py login --phone 138xxxxxxxx --code 123456
python3 tools/pinecone_cli.py devices
python3 tools/pinecone_cli.py status --pile 3617xxxxxxxx
python3 tools/pinecone_cli.py watch --pile 3617xxxxxxxx --count 5 --interval 5
```

`tools/pinecone_probe.py` 会按集成同样的映射逻辑打印一份快照，方便核对字段。

## 常见问题

| 现象 | 处理 |
|---|---|
| 添加时提示「账号下没有可用的充电桩」 | 先在 App 里绑定桩，或用同一手机号确认账号正确 |
| 实体变「不可用」 | token 失效（在 APP 里登录过就会这样，单会话机制），HA 会自动弹出「重新认证」，也可以直接按设备上的「重新登录（短信验证码）」按钮 |
| 「本周充电电量」一直是 0 | 周维度服务端不提供，靠本周逐日记录累加；桩在某天没记录就是 0，属正常 |
| 停止充电失败「未获取到充电订单号」 | 设备没有进行中的订单（已停止/已拔枪），属正常保护 |
| 状态显示「已离线」 | 桩本身 4G 掉线，云端查不到，需检查桩的网络 |
| 电流上限最大值不对 | 上限取 `guns[0].ratedCurrent`，取不到再按功率推断（3.5/11 kW → 16 A，其余 → 32 A） |

## 已知边界

- 只实现云端接口。**V2 协议桩的本地蓝牙控制、固件 DFU 升级**不在范围内（按需求排除蓝牙）。
- 轮询默认 30 秒，可在集成「配置」调整（10~300 秒）。App 充电时是 1.5 秒，HA 不需要这么激进。
- 接口来自逆向，官方变更路径/字段可能失效；字段解析已做多别名兜底，路径变更需更新 `api.py`。

## 免责声明

本项目仅用于**个人设备在自有账号下的互操作**（把自己的充电桩接入自己的智能家居）。请遵守松果能源的服务条款与当地法律，不要用于未授权访问、批量控制他人设备或任何商业用途。
