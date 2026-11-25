# 松果能源 V2.1.1 APK 逆向与接口提取报告

> 目标：`松果能源_V2.1.1.apk`（`csg.pinecone`，versionCode 211）
> 目的：仅使用**云端 HTTP 接口**（不含蓝牙）复刻 App 的充电桩通信能力，用于 Home Assistant 集成。
> 工具链：`android-reverse-engineering-skill`（jadx 1.5.1）+ 手工分析 + 在线接口验证。

## 一、应用结构判断

| 项目 | 结论 |
|---|---|
| 包名 | `csg.pinecone` |
| 版本 | 2.1.1（versionCode 211），minSdk 21 / targetSdk 36 |
| 框架 | **uni-app（DCloud）混合应用**，`assets/apps/__UNI__02D3E3E/www/` |
| 业务代码 | `app-service.js`（1.29 MB，压缩混淆后的 Vue/uni-app 运行时 + 业务逻辑） |
| Java 层 | 只有壳与插件，`csg/pinecone` 下仅 5 个类；jadx 反编译出 12005 个 java 文件基本全是第三方库 |
| 原生库 | `libweexcore.so`、`libuts-runtime.so`（uni-app x 运行时）、`lib39285EFA.so`（阿里加固）等，与云端通信无关 |
| 网络栈 | `uni.request`（DCloud 内部封装 OkHttp），**没有 Retrofit/OkHttp 自定义签名拦截器** |
| 蓝牙 | uni 原生插件 + BLE V1/V2 协议（`common/device/*`），本次不涉及 |

因此传统「找 Retrofit 接口」的路径在这里无效，**真正的接口全部在 `app-service.js` 里**，且没有额外的请求签名、加密或时间戳校验（只有 `Authorization` 和 `Phone` 两个请求头）。这也是本方案能够直接复刻的前提。

## 二、运行环境配置

`app-service.js` 中的 `config.js` 模块（压缩后紧跟 `const $={development:...}`）：

```js
development: {
  baseUrl: "http://115.29.143.138:20500/pinecone-mobile/",
  imgURL:  "https://csg-operation-1.oss-cn-shanghai.aliyuncs.com/",
  h5Url:   "http://115.29.143.138/soongoii-h5/"
},
production: {
  baseUrl: "https://sg.csgkeji.com.cn:2408/pinecone-mobile/",
  imgURL:  "https://csg-operation-1.oss-cn-shanghai.aliyuncs.com/",
  h5Url:   "https://sg.csgkeji.com.cn:2408/soongo/"
}
// 当前编译为 production
```

- 生产基址：`https://sg.csgkeji.com.cn:2408/pinecone-mobile/`
- 端口 2408，标准 HTTPS，证书正常，可直接用 aiohttp 访问。

## 三、统一请求封装（`common/api/request.js`）

```js
const Me = e => `${baseUrl}${e}`;                 // 拼 URL
Oe = async (path, data, opts) => {
  headers = { Authorization: token, Phone: userPhone };
  method = opts.method || (data ? "POST" : "GET");
  uni.request({ url: Me(path), data, method, header: headers, timeout: 15000 });
}
```

要点：

1. **GET 请求**参数直接拼在 path 的 query 里；**POST 请求**参数为 JSON body。
2. `Authorization` 直接放 **裸 token**（无 `Bearer` 前缀），同时带 `Phone: 登录手机号`。
3. 登录类接口（`login/phone`、`phone/code/send`、`login/one/click`、`login/third/party/*`）**不带**这两个头。
4. 响应统一 JSON：

```json
{ "success": true, "code": "000000", "msg": "", "data": {...} }
```

5. `code == "030400"` ⇒ 登录失效（`msg` 为「移动端用户未登录」），App 会清空会话并跳转登录页 ⇒ 集成里对应「重新认证」。
6. 部分控制类接口会在 `data` 里再套一层 `{success, data:{operateResult:{success,msg}}}`，`operateResult.success=false` 才是真正失败。

## 四、登录流程

1. `GET phone/code/send?phone={phone}` — 发送短信验证码（返回 `success`）。
2. `POST login/phone`

```json
{ "phone": "13800000000", "phoneCode": "123456", "nickName": "松果果1234" }
```

响应 `data`：

```json
{ "token": "…", "user": {...}, "eligible": ... }
```

3. 之后所有请求带 `Authorization: <token>`、`Phone: <phone>`。
4. `GET login/logout` 退出登录。

> App 里还硬编码了 3 个测试手机号（`17312111694` / `18856296286` / `19952255579`），这些号码点「发送验证码」不会真的发短信——仅供开发者调试，普通账号不受影响。

## 五、充电桩相关接口清单

约定：`gunCode = pileCode + "01"`（单枪桩固定 01）。

### 5.1 账号与设备

| 方法 | 路径 | 参数 | 说明 |
|---|---|---|---|
| GET | `user/info` | - | 用户信息、余额、收益 |
| GET | `user/pile/relation/query` | - | **已绑定充电桩列表**（`data` 为数组） |
| POST | `user/pile/relation/add` | `{phone,pileCode,password}` | 绑定桩（password 为 6 位绑定密钥，可空） |
| POST | `user/pile/relation/remove` | `{phone,pileCode,...}` | 解绑 |
| GET | `user/pile/relation/need/password?pileCode=` | - | 是否需要绑定密钥 |
| POST | `user/pile/relation/update/pile/name` | `{id?,phone,pileCode,pileName}` | 重命名 |
| POST | `user/pile/relation/update/pile/none` | `{id?,phone,pileCode,none}` | 主设备切换（`none=1` 设为主桩） |

设备列表项常用字段：`pileCode`、`pileName`、`pileType`（10=交流，13=直流）、`relationPileType`（1=被分享）、`none`（主桩标记）、`sharePileName`、`shareStatus`。

### 5.2 状态与实时数据

| 方法 | 路径 | 参数 | 说明 |
|---|---|---|---|
| GET | `pile/available/gun?gunCode={pile}01` | - | **枪状态**（`data.gunStatus.code` / `status.code` / `statusCode`） |
| GET | `pile/query/one?pileCode=` | - | 桩详情（`pileName`、`pileType`、`initPower`、`presentPower`、`silentMode`、`selfRecoveryStatus`、`authStatus`、`imageUrl`…） |
| GET | `pile/query/current/pile?gunCode={pile}01` | - | **当前充电数据**：`energyKwh/energy`、`length/chargeDuration`、`currentPower/chargingPower`、`soc`、`voltage`、`current`、`orderCode` |
| GET | `pile/query/signal?pileCode=` | - | 4G 信号档位 0~4（`signalBars/signalLevel/signal/grade`） |
| GET | `pile/charging/data/echarts?gunCode=&orderCode=` | - | 单次充电曲线 |
| GET | `pile/locate/get?pileCode=` | - | 桩位置信息 |
| GET | `pile/upgrade/query?pileCode=` / `pile/upgrade/check?pileCode=` | - | 固件升级状态（未纳入 HA） |

**枪状态码（4G/云端口径）**，见 `common/index.js` 的 `or()`：

| code | 状态 | 备注 |
|---|---|---|
| 0 | 空闲中 | 请连接车辆 |
| 1 | 已连接 | 可启动充电 |
| 2 | 充电中 | |
| 3 | 预约充电中 | 已设置计划充电 |
| 4 | 故障 | 联系售后 |
| 5 | **已离线** | 设备不在线 |
| 6 | 等待充电中 | 启动过程中 |
| 7 | 充电完成 | 请将枪归位 |
| 9 | 已充满 | |
| 10 | 充电暂停 | |
| 11 | 车辆预约中 | |
| 13 | 升级中 | |
| 14 | 待插枪 | |
| 15 | 充电模组已取下 | |

（BLE 口径另有 0~9 的独立映射，与云端不同，本集成只用云端码。）

### 5.3 充电控制

| 方法 | 路径 | 参数 | 说明 |
|---|---|---|---|
| POST | `private/start/charge` | `{pileCode}` | **启动充电**；返回 `operateResult`，可含 `orderCode` |
| GET | `private/netty/stop?pileCode=&orderCode=` | - | **停止充电**，必须先有订单号 |
| GET | `private/gun/unlock?pileCode=&gunCode=` | - | **解锁充电枪**（电子锁） |
| POST | `pile/share` | `{pileCode,shared}` | 公共桩分享开关；业务码 `030467~030469` 表示资料未完善 |
| POST | `panel/query-config` → `panel/query/config?pileCode=`(GET) | - | 自定义面板卡片配置 |
| POST | `panel/save/config` | `{pileCode,selectedCodes:[...]}` | 保存面板卡片 |

停止充电的订单号来源：启动返回的 `orderCode`，或 `pile/query/current/pile` 返回的 `orderCode`（App 也是这么兜底的）。

### 5.4 电流/功率限制

| 方法 | 路径 | 参数 | 说明 |
|---|---|---|---|
| POST | `private/query/charge/strategy` | `{pileCode}` | 查询交流桩电流限制（`parameter` = 安培） |
| POST | `private/set/charge/strategy` | `{pileCode,operatorSchema:1,chargingStrategy:{code:4,msg:""},parameter:<A>,isTrue:1}` | 设置交流桩电流上限 |
| POST | `private/power` | `{pileCode,pileCodes:[...],percent,presentPowerStatus:true,startTime,endTime,isTrue:1}` | 直流桩功率上限（百分比）；时间范围 App 取「今天 00:00 ~ +5 年 23:59:59」 |

App 对交流桩的上限推算：`initPower/presentPower` 为 3.5 或 11 kW ⇒ 最大 16 A，其余 ⇒ 32 A；下限 6 A。

### 5.5 功能开关（桩端设置，走云端）

| 方法 | 路径 | 参数 | 说明 |
|---|---|---|---|
| POST | `private/set/silent/status` | `{pileCode,status:0/1,isTrue:1}` | 静音模式 |
| POST | `private/set/self/recovery` | `{pileCode,status:0/1,isTrue:1}` | 智联续充（断电续充） |
| POST | `private/set/auth` | `{pileCode,status:0/1,isTrue:1}` | 即插即充：`status=0` 表示即插即充开启（authStatus=1 才需要鉴权） |

字段语义：`silentMode`/`selfRecoveryStatus`/`authStatus` 取值 `1`（开）、`0`（关）、`255`（未知/不支持）。

### 5.6 计划充电

| 方法 | 路径 | 参数 | 说明 |
|---|---|---|---|
| GET | `private/plan/query?pileCode=` | - | 查询计划（`startTime/endTime/repeatType/operationType/isOpen`） |
| POST | `private/plan/issue` | `{gunCode,pileCode,startTime,endTime,operationType:0/1,isTrue:0,repeatType:"DAILY"\|"ONCE"}` | 下发/取消计划 |

- `operationType=1` 启用，`0` 取消。
- `DAILY`：每天在 `startTime~endTime` 的**时分**充电；`ONCE`：一次性，日期有效。
- 充电中（状态 2）或充电暂停（10）时不能设置计划。

### 5.7 充电记录与统计

| 方法 | 路径 | 参数 | 说明 |
|---|---|---|---|
| POST | `charge/record/day/page` | `{condition:{date:"YYYY-MM-DD",pileCode},pageNum,pageSize}` | 按天分页记录；条目 `startTime,endTime,durationMinutes,elect,orderStatus(3=进行中)`；外层汇总 `totalCount,totalElect,totalDurationMinutes` |
| POST | `charge/record/stats` | `{date,periodType:"DAY"/"WEEK"/"MONTH"/"YEAR",pileCode,weekOfYear?}` | 汇总 + `chartBuckets` |
| POST | `income/details/query/page` | `{pageNum,pageSize,condition:{year,month}}` | 收益明细 |
| POST | `chargeIntegration/count/query?phone=` | - | 累计充电统计 |

## 六、复刻结论（为什么能直接做 HA 插件）

1. 通信全部是普通 JSON over HTTPS，**无签名、无设备指纹、无 TLS Pinning**（App 未做证书绑定），服务端只校验 token + phone。
2. token 有效期未知（未在代码中看到显式过期逻辑，仅按 `030400` 判定失效），因此集成里实现了失效后重新发短信登录（reauth）。
3. 所有充电业务都有云端接口，蓝牙只是「离线/弱网兜底 + 部分 V2 桩的本地控制」，剔除蓝牙后功能完整度约 90%：启动/停止/解锁枪/功率限制/静默/续充/即插即充/计划充电/记录统计/面板配置均可云端完成。
4. 唯一必须在蓝牙侧才能做的是「V2 协议桩的离线本地控制」「固件 DFU 升级」，与本次需求无关。

## 七、验证方式

`tools/pinecone_cli.py` 可独立验证每个接口，例如：

```bash
python3 tools/pinecone_cli.py send-code --phone 138xxxxxxxx
python3 tools/pinecone_cli.py login --phone 138xxxxxxxx --code 123456
python3 tools/pinecone_cli.py devices
python3 tools/pinecone_cli.py status --pile <pileCode>
python3 tools/pinecone_cli.py watch --pile <pileCode>
```

未登录直接 GET `user/info` 返回的 `{"code":"030400","msg":"移动端用户未登录","success":false}` 已验证线上可达（HTTP 200，TLS 正常）。

## 八、线上实测记录（2026-11-25，真实账号 + 真实设备）

真机：`348524450034`（「万荟花园」，桩自述名 `银河21kW交流桩8524450034`，21 kW 交流桩，硬件 `CABF01Ver1.10`，固件 `YINHE_T-A00-01.00.01`，协议 V2）。

### 8.1 登录与鉴权

- `phone/code/send` → `login/phone` 全流程成功，返回 `eligible: true` 与 `user.nickName`。
- token 直接生效，`Authorization` + `Phone` 两个头足够，不需要其它头。
- 用伪造 token 请求，稳定返回 `030400 移动端用户未登录` ⇒ 集成的「过期检测 + 重新认证」路径成立。

### 8.2 设备列表（`user/pile/relation/query`）

`data` 是数组，每一项把 `pile` 子对象**平铺**到顶层，关键字段：

```json
{"pileCode":"348524450034","pileName":"万荟花园","pileType":"0","pileCategory":1,
 "pureBluetooth":false,"power":21.0,
 "equipmentType":{"code":2,"msg":"交流设备"},
 "online":{"code":1,"msg":"在线"},"pileStatus":{"code":50,"msg":"正常使用"},
 "silentModeStatus":0,"selfRecoveryStatus":1,"authStatus":1,"plugAndChargeStatus":1,
 "protocolVersion":2,"hardwareVersion":"CABF01Ver1.10",
 "pile":{"pileName":"银河21kW交流桩8524450034","power":21.0,"guns":[...]}}
```

⚠️ 实测修正：**`pileType` 是 `"0"` 字符串**，不是文档常写的 10/13。判断交/直流必须看 `equipmentType.code`（2=交流设备，1=直流设备）或 `guns[0].gunType`（"慢充"/"快充"）。集成已按此实现。

### 8.3 枪状态（`pile/available/gun?gunCode=34852445003401`）

```json
{"success":true,"data":{"success":true,"data":{
  "gunCode":"34852445003401","gunStatus":{"code":0,"msg":"空闲"},
  "power":0.0,"voltage":0.0,"current":0.0,"length":0,"chargingMileage":"0.0","authStatus":1}}}
```

两层的 `data.data` 结构已在客户端做了递归解包；状态文案优先取 `gunStatus.msg`，否则用 code 映射。

### 8.4 实时数据（`pile/query/current/pile`）

空闲时：

```json
{"gunCode":"34852445003401","gunStatus":{"code":0,"msg":"空闲"},
 "power":0.0,"voltage":0.0,"current":0.0,"length":0,
 "chargingMileage":"0.0","pileName":"银河21kW交流桩8524450034","authStatus":1}
```

实测没有 `soc` 字段；桩详情里带一个**上次上报的 SOC**（本次为 32）。集成逻辑：充电数据有 SOC 就用，没有就退回详情里的 SOC。

### 8.5 桩详情（`pile/query/one`）

```json
{"success":true,"data":{
  "pileCode":"348524450034","pileName":"银河21kW交流桩8524450034",
  "equipmentType":{"code":2,"msg":"交流设备"},
  "initPower":21,"soc":32,"authStatus":1,"silentMode":0,
  "selfRecoveryStatus":1,"plugAndChargeStatus":1,"protocolVersion":2,
  "guns":[{"gunCode":"34852445003401","gunStatus":"空闲","gunType":"慢充",
           "power":21.0,"ratedCurrent":32.0,"voltageLowerLimits":176.0,"voltageUpperLimits":264.0}]}}
```

⚠️ 注意：详情返回的字段名和列表接口**不一致**——详情是 `silentMode`，列表是 `silentModeStatus`。集成对两者都做了兼容。

电流上限：`guns[0].ratedCurrent = 32` ⇒ 集成把交流桩上限取 32 A，而不是靠功率猜。

### 8.6 电流限制（`private/query/charge/strategy`）

```json
{"success":true,"data":[{"pileCode":"348524450034","parameter":32,
 "strategy":4,"socProperty":0,"socValue":50,"pattern":0,"phone":"18938836025"}]}
```

`data` 是**数组**（按手机号/操作者分行），取第一条的 `parameter`；集成已按数组处理。

### 8.7 计划充电（`private/plan/query`）

```json
{"success":true,"data":{"pileCode":"348524450034","operationType":1,
 "repeatType":"DAILY","startTime":"2026-02-20 00:00:00",
 "endTime":"2031-02-20 08:00:00","isTrue":0,"chargePlanValidation":"NO_VALIDATION"}}
```

✅ `DAILY` 计划的结束时间确实是「开始 + 5 年」同日同时分，和 `Ip()` 的算法一致。`operationType=1` 即启用。

### 8.8 统计与记录

| 请求 | 结果 |
|---|---|
| `charge/record/stats` `periodType=DAY` | ❌ `参数缺失或请求格式异常,请输入完整 888888` |
| `periodType=WEEK`（带/不带 `weekOfYear`） | ❌ 同上（服务端就是没有 WEEK 分支） |
| `periodType=MONTH` | ✅ `data.totalElect/totalCount/totalDurationMinutes` + `chartBuckets`（按日，30 个） |
| `periodType=YEAR` | ✅ 同结构 + `chartBuckets`（按月，12 个） |

MONTH 的真实响应（2026-11，该桩无充电记录故全 0）：

```json
{"code":"200","data":{
  "totalElect":0.0,"totalCount":0,"totalDurationMinutes":0,
  "chartBuckets":[{"bucketKey":"2026-11-01","count":0,"durationMinutes":0,"elect":0.0,"label":"01日"}, "..."]
}}
```

即：**周期总计在 `data` 顶层**（`totalElect`/`totalCount`/`totalDurationMinutes`），`chartBuckets` 只是画图表用的分桶序列，桶内字段是 `elect`/`count`/`durationMinutes`。
| `charge/record/day/page` | ✅ 返回 `{page:{resultSet...},totalCount,totalElect,totalDurationMinutes}` |

⚠️ 实测修正：**服务端并不支持 DAY 周期的 stats**（App 里统计页也只用了 WEEK/MONTH）。集成因此改用 `charge/record/day/page` 的当日汇总拿「今日电量/次数/时长」。

实测该桩所有日期统计均为 0（设备 2026-10-27 之后没有充电记录），因此「单条记录明细」的字段只能依据 JS 代码推断（`startTime/endTime/durationMinutes/elect/orderStatus`），未做真机校验。

### 8.9 面板配置（`panel/query/config`）

```json
{"data":{"pileCode":"348524450034","equipmentType":2,"selectedCodes":[],
 "cards":[{"code":"PUBLIC_PILE","name":"公共充电桩","selected":false,"sort":1},
          {"code":"PRIVATE_SHARE","name":"私桩分享","selected":false,"sort":2},
          {"code":"POWER_RECOVERY","name":"智联续充","selected":false,"sort":3},
          {"code":"PLUG_AND_CHARGE","name":"即插即充","selected":false,"sort":4}]}}
```

### 8.10 未验证项（本次没有插车）

| 项 | 原因 |
|---|---|
| `private/start/charge` / `private/netty/stop` | 未插枪，按要求不做充电测试；停止还依赖订单号 |
| `private/gun/unlock` | 属于机械动作，未执行 |
| 充电中的 `energyKwh`/`soc` 字段名 | 空闲态接口不返回，代码里已做 `energyKwh/energy/elect` 等别名兜底 |

### 8.11 即插即充的口径

App 代码里 `plugInCharge = !authStatus`（`Hm = e => e ? 0 : 1`），但平台数据里同时又给了 `plugAndChargeStatus: 1`。实测该桩 `authStatus=1`、`plugAndChargeStatus=1` 并存，两者语义相反。集成按 **App UI 的算法**（`!authStatus` ⇒ 当前显示为「关」）实现；如果 App 里显示是开，改 `switch.py` 的 `is_on` 即可。

### 8.12 Home Assistant 运行时验证

在 docker 无关的本地 HA（2025.1.4 + Python 3.12）里用真实 token 起了一个测试实例，结果：

- 集成 setup 成功，coordinator 30 s 轮询稳定 `success: True`（真实 HTTP 往返 0.35 s）
- 自动创建 **27 个实体**（1 台桩），状态与云端一致：`空闲中`、功率 0.0 kW、SOC 32%、信号 4 格、在线 on、充电 off、电流上限 32 A、计划 00:00–08:00 / DAILY、智联续充 on、静音 off、即插即充 off
- 配置流程渲染出 `phone` 表单，选项流程渲染出 `piles + scan_interval` 表单，均无报错
- 实体注册表持久化正常，重启后 entity_id 不变

顺带修正了两个平台/蓝牙口径差异：

- `private/set/charge/strategy` 与 `private/power` 的 `isTrue` 字段**只有蓝牙路径才带**（`iBle=1`），云端路径不带；集成按云端路径发送。
- `private/plan/issue` 的 `isTrue` 云端固定 0，蓝牙才 1；集成本来就发 0，正确。
- `private/set/silent/status`、`/self/recovery`、`/auth` 三个开关接口的 `isTrue` 恒为 1，集成保持一致。

### 8.13 周/月/年电量传感器（v1.2.0）

- **月、年**：直接 `charge/record/stats`（`periodType=MONTH` / `YEAR`），读 `data.totalElect`
- **周**：服务端不支持 `WEEK`，改为把当前 ISO 周（周一→今天）的 `charge/record/day/page` 逐日汇总 `totalElect`；首次失败后进程内记住，之后不再空跑 stats
- 三者均做 15 分钟缓存、按自然周/月/年切换自动失效；HA 侧 `device_class=energy`、`state_class=total`
- 真机复测：27 个实体全部创建，本周/本月/今年电量均为 `0.0`（与云端一致——该桩自 2026-10-27 起没有充电记录），`stats WEEK failed ... 参数缺失` 仅在首次探测出现一次

### 8.14 token 生命周期（修正）

**不是 1 小时过期，而是「一个账号只有一个有效会话」**：任何一次新的登录（APP 里重新登录、另一台手机、我们自己的 CLI）都会让之前发的 token 立即失效。

早上那次“看起来 1 小时过期”，实际是我用同一账号重新登录做别的验证，把 HA 的 token 踢掉了。APP 侧没有 refresh token 逻辑，收到 `030400` 一律 `reLaunch` 回登录页；所以集成不做自动续期，只在失效时走重新认证。

因此：
- 只要不在别处重新登录，HA 的 token 可以一直用（实测跨多个小时的多轮轮询都正常）
- 一旦在 APP 里登录过，下一次轮询就会收到 `移动端用户未登录`
- 短信验证码 **5 分钟有效、一次性**，短时间连续请求会限流（`短信发送过于频繁`，1–2 分钟后恢复）

### 8.15 重新登录入口（v1.2.1，已实测）

失效后有三个入口，全部接到同一个 reauth 流程（发短信 → 收码 → 写回 token → 重载集成）：

| 入口 | 触发方式 | 实测结果 |
|---|---|---|
| 自动 | coordinator 收到 `030400` → `ConfigEntryAuthFailed` + `entry.async_start_reauth()` | 运行时把 client.token 改成假值，下一轮即出现 `登录状态已失效` 警告并弹出 reauth 流程 |
| 集成卡片 | HA 设置 → 设备与服务 → 松果能源 → 「重新认证」 | 走同一流程（设置阶段 token 无效时也会直接进入） |
| 按钮实体 | 每台桩新增 `button.<桩>_重新登录`（短信验证码），`available` 恒为 True | `button.press` 调用后 6 秒内出现 `reauth_confirm` 流程 |

修复前的坑：`PineconeAuthError` 只是被当成普通 `UpdateFailed`，HA 只会“重试”，不会弹重新认证，也没有入口——这就是之前“失效后没有登录入口”的原因。另外 `DataUpdateCoordinator` 必须传 `config_entry=entry`，否则它拿不到要重认证的条目（HA 源码里 `self.config_entry.async_start_reauth` 依赖它）。

