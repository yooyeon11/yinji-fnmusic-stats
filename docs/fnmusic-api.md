# 飞牛音乐 API 研究与 connector 契约

验证日期：2026-10-09。目标为授权内网服务 `http://<NAS地址>:5666/music/`。
本项目从零研究，仅读取目标网页公开 HTML 及它明确引用的 JavaScript 依赖，不读取或复用其他项目。

## 1. 已验证与未验证边界

### 最新：用户现有私有连接的真实 GET 验证

用户已在浏览器完成连接并创建 `runtime/connection.json`。本轮仅读取该私有文件到内存，使用其 token 对目标 NAS 发起 GET；不输出文件内容、token、用户名、用户 GUID 或曲目私有路径，也没有改写连接文件。没有重新调用密码登录、授权码交换或任何 POST/播放接口。

修复后的一轮完整审计共 14 次 GET，全部 HTTP 200：

- `GET /music/api/v1/user/me`：成功解析 `code=0` 用户 dict，字段为 `createdAt,guid,lastAccessedAt,name,role,updatedAt`，不记录值。
- `GET /music/api/v1/play-history/list?page=1&size=100`：成功解析 `code=0`、data `{list,total}`，本轮返回 6 条原始曲目项。
- 这 **6 条样本全部没有** `playedAt,playAt,occurredAt,historyId,historyGUID`；全部有整数 `createdAt/updatedAt`。每条 history.updatedAt 都与同一曲目 metadata.track.updatedAt 相等，因此是曲目元数据时间，**不能作为真实播放时间**。这只是当前服务/样本的实测，不断言其它版本或未来返回永远无播放时间。
- 对全部 6 条曲目执行 `GET /music/api/v1/track/metadata?guid=...`：成功解析 `code=0`，data 精确为 `{track,audioSpec}`；track.album 为 dict，含 `barcode,coverId,createdAt,guid,name,releaseDate,updatedAt`。所有 track.year 为 null，未返回 originalReleaseYear；album.releaseDate 为真实 `YYYY-MM-DD` 发行日期，样本年份为 2023–2025。
- 修复 parse_track 后，全部 6 条的 release_year 与 album.releaseDate 年份一致，history/metadata 规范化年份及 cover_id 一致；duration 使用 audioSpec.duration 原始毫秒，不乘 1000。
- 对全部 6 条执行 `GET /music/api/v1/static/cover?coverId=...`：全部 HTTP 200、`Content-Type:image/webp`，且字节满足 `RIFF...WEBP` 文件头，大小约 39–57 KB。
- 暴露并修复两处真实缺陷：封面先前误用 `/music/static/cover`（HTTP200但返回 HTML SPA，不是图片）；发行年份先前遗漏 album.releaseDate。正确封面路径由公开脚本导入 `E=jO()` 的 `/music/api/v1` 基址重新精确确认，不是猜测目录。

history 顺序在两轮只读请求间发生变化；多次 GET 不构成并发播放时的快照。本轮没有按排列顺序倒推时间，也没有导入/创建听歌历史。

### 较早的未登录 urllib 实际验证（保留失败背景）

- `GET /music/`：HTTP 200，HTML 主入口脚本与下述证据一致。
- `GET /music/api/v1/initialization/state`：HTTP 200，`code=0`，`data={"initialized":true}`。
- `GET /music/api/v1/sys/config`：HTTP 200，`code=0`。`data` 包含 `nasOAuth,serverGUID,serverName,serverVersion,mediasrvVersion,capabilities`；`nasOAuth` 有 `clientId`，没有 `url`。
- 使用授权用户名和密码，按公开前端精确 SHA-256 密码变换与 32 位十六进制 deviceId，`POST /music/api/v1/user/password-login`：HTTP 200，但 `code=120001`，`msg="unauthorized, please login again"`，`data=null`。带公开前端 authx 签名和不带签名均未获得 token。
- 未登录 `GET /music/api/v1/user/me`：HTTP 401。

主代理另在真实网页 UI 验证：密码登录提示“用户名或密码错误，请重试”；“使用 NAS 登录”提示“授权登录流程异常，请重试”。该 UI 结论由主代理提供，不伪称 connector 的 urllib 结果。

### 尚未验证与权限边界

- connector 的密码登录成功路径仍只在离线 mock 中验证；本轮使用用户已有 token，不重新尝试 password-login。较早 code120001 不再代表当前连接状态。
- 历史已真实读取但未导入。当前 6 条样本缺少真实播放时间；服务端历史保留范围、是否合并重复播放、其它版本的时间字段和多页并发一致性尚未验证。
- 没有向 `event/report` 发送请求，因为该接口会写入历史，超出仅登录/读取的授权。它的实际成功状态与 `code=0` **未验证**。
- 没有播放、转码、修改歌曲、修改账户或创建历史。
- `runtime/connection.json` 由用户连接创建，本轮仅私有读取，未写入模拟 token、未更改内容。
- 没有将账号、密码或 token 发往外部服务。公开脚本扫描和 connector 都禁用环境 HTTP 代理并禁止自动跟随鉴权请求重定向。

## 2. 精确前端证据

所有脚本均位于目标服务 `/music/static/assets/` 下，由已读取 HTML/脚本中的真实引用发现，不猜测目录或文件名。

| 资产 | 关键证据 |
|---|---|
| `1bc04b5291c26a46d918139138b992d2-D_KTkUJS.js` | HTML 主入口；播放事件构造、sendBeacon；session 开始时间由 `Date.now()` 设置 |
| `5e884898da28047151d0e56f8dc62927-B563oObE.js` | `/music/api/v1` 基址、API 路由和方法、响应 `{code,msg,data}`、SHA-256、authx MD5、music-token cookie、OAuth |
| `bdf49c3c3882102fc017ffb661108c63-B3kwEuEo.js` | 密码登录调用 `passwordLogin({username,password:ee(x),deviceId:m()})`；`ee` 对应核心包导出 `tA as t`，`tA=e=>sha256(e)` |
| `034a7e52c5c9534b709dc1dba4038683-CCjrayJf.js` | recent 页：`playHistory.list({page:1,size:...})`；使用 `data.list/data.total`；危险的时间回退逻辑 |
| `07193df400d460d8dea9e7bcd261e9e4-CPDqqIvG.js` | `TrackPlay='track_play'`；扁平曲目、专辑、artists、duration 规范 |
| `fd1fcd61d83c0244c83762ba7832daad-DHF9K-B9.js` | `track.metadata({guid})`；metadata 响应解构 `{audioSpec,track}`；duration 毫秒 |
| `016b0624f32ee00e1e0de4c879cd3041-ChOD2xpy.js` | `${c()}/static/cover?coverId=...`；c 导入核心包 E=jO，因此是 `/music/api/v1/static/cover`，不是 `/music/static/cover` |

`scripts/research_api.py` 仅做 GET，递归扫描明确引用的 JS 资产；本次共检查 72 个引用资产。示例运行：

```powershell
python scripts/research_api.py --asset 034a7e52c5c9534b709dc1dba4038683-CCjrayJf.js --terms playHistory.list,playedAt
```

## 3. 鉴权与密码算法

### 密码登录

```text
POST /music/api/v1/user/password-login
Content-Type: application/json
{
  "username": "用户名（trim）",
  "password": "SHA256(原密码 UTF-8 字节)的64位小写hex",
  "deviceId": "32位hex设备ID"
}
```

不是 RSA/AES，不是 MD5 密码，不添加盐，不 trim 密码。SHA-256 是摘要，并不提供传输加密；当前 HTTP LAN 仍无 TLS，摘要也必须视作秘密。

前端成功响应预期为 `{code:0,msg:...,data:{userToken:"...",user:{...}}}`，connector 对外重命名为 `{token,user}`。失败响应已经实测为 `code=120001,data=null`。

### authx

核心前端为请求设置 `authx: nonce=...&timestamp=...&sign=...`，nonce 为六位数字，timestamp 为 Unix 毫秒。

```text
sign = MD5(prefix + '_' + pathname + '_' + nonce + '_' + timestamp
           + '_' + dataHash + '_' + apiKey)
```

prefix 和 apiKey 是公开前端常量，不是用户秘密。算法及常量在 connector 中按公开脚本实现。

- GET：查询参数按名称排序，重复参数保留最后一个有效值，忽略值字符串 `undefined/null`。URLSearchParams 序列化后把 `+` 改 `%20`，再 decodeURIComponent，然后 MD5。
- 其他方法：对 JSON.stringify 请求对象的紧凑 JSON 文本 MD5；无 body 对空字符串 MD5。
- 所有 MD5 输出是小写 hex。connector 的 `_json` 使用此签名；`proxy` 不重签名、保留浏览器已有 authx。

### Cookie 与代理兼容

- web 会话 cookie 名称为 `music-token`。
- 核心包 `JO()` 仅读取并 decodeURIComponent cookie，路由 bootstrap 用其非空性判断是否调用 `me`。未发现 JWT 解码验证。
- fetch 使用 `credentials:'include'`；未发现将 cookie token 复制到 Authorization 或其它 header。authx 不包含用户 token。
- 仅在服务端注入真实 cookie 不够：浏览器看不到非空 `music-token` 时，前端可能不调用 me 就跳登录。
- 可以给本地 `/music/` 设置 JS 可读的**非敏感哨兵** `music-token=journal-gateway`；真实 token 仍留后端私有存储。connector 丢弃所有传入 Cookie，仅注入实际 `music-token`，防止本地 journal 会话及哨兵发送至远端。
- 本地 cookie 与授权、CSRF、内容安全策略、HTML 注入由 app/server.py 负责，connector 不修改 HTML。
- 本次公开 JS 扫描未发现 WebSocket 使用；上述登录/历史/metadata/封面/event report 都是 HTTP。已有 token 的 me/history/metadata/封面 GET 已实测可用，但没有播放验证，不保证完整 UI 播放不需要其它传输；connector 不实现 WS tunnel。

### NAS OAuth（静态推导，不尝试修复）

前端读取 sys/config 的 nasOAuth。没有 url 时使用当前 origin：

```text
GET /signin?client_id=<公开clientId>&redirect_uri=<origin>/music/oauth/result&app_name=飞牛音乐
POST /music/api/v1/user/auth-login
{"code":"NAS返回的授权码","deviceId":"32位hex"}
```

初始化已完成时，auth-login 预期返回 `{userToken,user}`。仅定位公开调用路径，未交换授权码；密码登录 connector 不自动降级到 NAS OAuth，也不探测其他密码接口。

## 4. 只读 API 与数据结构（已有 token 的成功响应已实测）

| 接口 | 方法与参数 | 已实测 data |
|---|---|---|
| `/music/api/v1/user/me` | GET | 用户对象 |
| `/music/api/v1/play-history/list` | GET `page=1&size=100`，继续 page 分页 | `{list:[原始曲目项],total:数量}` |
| `/music/api/v1/track/metadata` | GET `guid=<GUID>` | `{track:{...},audioSpec:{...}}` |
| `/music/api/v1/static/cover` | GET `coverId=<ID>` | WebP 原始字节与 image/webp Content-Type，不包 JSON |

### history 原始项

当前真实原始项字段为：`album,artists,audioSpec,coverId,createdAt,discNo,duration,genres,guid,isCue,isFavorite,isrc,title,trackNo,updatedAt,year`。album.releaseDate 已返回；本轮没有 playedAt/playAt/occurredAt/historyId/historyGUID。

recent 前端还支持 `album.originalReleaseYear,playedAt,playAt,historyId,historyGUID,source,sourceId,completionRate` 等字段；这只是消费代码支持的字段，不代表当前服务端实际返回所有字段。

**重要不足：** recent 前端有如下回退：

```text
playedAt || playAt || updatedAt || createdAt || Date.now()
```

其中 updatedAt/createdAt 可为歌曲元数据更新时间/入库时间，Date.now 是读取时刻，都不是可靠历史播放时间。connector **不复制此逻辑**：history 返回原始項，不补 timestamp、不生成 historyId、不去重、不排序或倒推。parse_track 只规范曲目元数据。

当前 6 条真实样本没有 `playedAt/playAt`，且 updatedAt 与曲目 metadata 一致，明确属于“历史时间不足”：不能补出日/月/年听歌统计。未来如返回 playedAt/playAt，应另验证其语义和单位，不能仅依字段名字认定真实播放时间。分页不能恢复远端已被截断/合并/清理的历史，且多页读取并非并发播放下的快照。

### 列表语义实测（2026-10-11，真实账号持续观测）

对 `play-history/list` 的持续观测（每 30 秒轮询的服务端进程 + 只读采样）确认了以下语义，它们直接决定轮询探测的实现：

1. **每曲目一条（按曲目去重）。** 同一曲目的重播不会新增列表条目：重播后该曲目的既有条目前移（或原地更新）。证据：某曲目在 10-10 20:14 被记录为播放，10-11 仍排在更晚播放的曲目之前；全天列表内无重复 GUID。
2. **列表会自行收缩/整理。** 10-10 20:18 观测到并记录过的条目（Rolex），在 10-11 的列表中消失，而更早的条目仍然保留——删除不是简单的 FIFO 裁尾。两次只读请求之间顺序也可能变化（10-09 已观测）。
3. **空闲时顺序稳定。** 无播放期间 2 分钟 8 次采样顺序完全一致；顺序变化与实际播放/整理行为相关。
4. **仍然没有播放时间。** 样本继续缺少 `playedAt/playAt/occurredAt`；`updatedAt/createdAt` 仍与曲目元数据一致，不能当播放时间用。
5. **重播对轮询不可见的部分是结构性的。** 由于无新增条目、无时间字段，「重播已存在的曲目」只能通过条目位置变化推断。轮询探测 1.1.3 起：对账失败时按「首次出现的 GUID」兜底记录；「单一已有条目前移到首位且其余保持相对顺序（允许尾部同时被裁剪）」识别为一次重播；多条目同时变动、中部删除、交换等形状保守跳过。重播识别因此是估算信号，同一轮询间隔内连续切歌会漏记。

### metadata 与 parse_track

`track()` 返回整个原始 data（包括 track 和 audioSpec），不要提前只取嵌套 track。

`parse_track` 接受扁平历史项或 `{track,audioSpec}`，返回且仅返回：

```text
{guid,title,artist,album,duration_ms,genre,cover_id,release_year}
```

- artist：artists.name 用 ` / ` 拼接。
- album：对象取 name；metadata 也兼容字符串 album。
- duration_ms：优先 audioSpec.duration，后取 track.duration，**本来就是毫秒，不乘1000**；未知/无效/负数为 0。
- genre：genres.name 用 `;` 拼接。
- cover_id：曲目 coverId 优先，fallback album.coverId；未知为空字符串。
- release_year：优先有效的 track.year，次选 album.originalReleaseYear，再取 album.releaseDate 的年份（支持有效 YYYY-MM-DD 或 YYYY）；null/0/无效年份不阻断 fallback，无效日期不猜年份，未知 None。releaseDate 是发行元数据，绝不用于播放时间。
- 不产生任何播放时间字段。

## 5. event report（仅静态证据）

```text
POST /music/api/v1/event/report
Content-Type: application/json
{
  "events": [
    {
      "eventType": "track_play",
      "occurredAt": <session.startedAt，Unix毫秒>,
      "payload": {"trackGUID": "曲目GUID"}
    }
  ]
}
```

主脚本 `Cb`/`wb` 精确构造以上结构，普通 fetch 调用 `event.report`；页面退出也可对同一路径 `navigator.sendBeacon(JSON Blob)`。只发送 trackGUID 和开始时间，并非播放时长、完成率、累计秒数的完整事件。

前端 event.report 是通用 music-envelope API，预期 code0；**本次没有发送，不能标成已验证成功**。connector 不提供 event 写入助手。proxy 作为透明 HTTP 通道具备转发 POST 的能力，是否允许用户后续真实网页操作应由主服务器权限策略控制，不等于本研究授权允许我们主动写入。

## 6. Python 后端契约

`MusicClient(server, token='')` 全标准库 Python 3.10+；server 可为 HTTP(S) origin 或 `/music/` 入口，内部保存规范化 origin。没有文件写入或日志。

| 方法 | 返回 | 说明 |
|---|---|---|
| `login(username,password)` | `{token:str,user:dict}` | SHA256 参数，登录失败清空内存 token；不存密码；不打印 token |
| `me()` | dict | 原始 data |
| `history()` | list | 自动分页、保留原始项/顺序/重复；分页不推进抛错，不静默返回假完整结果 |
| `track(guid)` | dict | 原始 `{track,audioSpec}` data |
| `cover(cover_id)` | `(bytes,content_type)` | 保留字节/MIME |
| `proxy(method,path,body,headers)` | `(status,response_headers,bytes)` | response_headers 为 dict；body 支持 bytes/str/None |
| `parse_track(item)` | 上述8字段 dict | 模块级函数 |

`MusicError` 为 RuntimeError，暴露 `status/status_code/code`。错误文本经过固定格式脱敏，不保留/转发服务器可能回显密码/token的 msg 或 body；服务器可以向用户显示一般错误并读取数字 code。

proxy 规则：

- 只允许 `/music` 或 `/music/...` 绝对路径及 query，拒绝外部 URL、network-path、fragment、控制字符、反斜杠和多重编码 traversal；query 内出现 URL 不会改变请求目标。
- 支持 GET/HEAD/POST/PUT/PATCH/DELETE/OPTIONS；本研究只进行了登录 POST 和 GET。
- HTTP 4xx/5xx/3xx 以原 status/body 返回，不转化为假成功；**不自动跟随任何重定向**，不泄漏真实 token 到其它 origin。
- 不解压/改写 body，不主动生成 authx，保留 Range、Content-Range、Content-Encoding、ETag 等端到端 header。
- 删除 hop-by-hop、传入 Host/Content-Length/Cookie；urllib 重新计算请求 Host/长度，仅发送自身真实 music-token。
- 返回 headers 为 dict，重复 Set-Cookie 无法完整表示；主代理应抑制远端 Set-Cookie 并自行提供本地哨兵。HTML/CSP/Location 的本地兼容改写由主服务器负责。

用户已通过浏览器连接，由私有存储创建 UTF-8 `runtime/connection.json`：`{server,token,user,connected_at}`。本轮 connector 验证仅在内存读取该文件，不打印内容、不改写真实连接；鉴权失败时仍不得生成假连接。

## 7. 测试

32 项 connector 离线 unittest 已通过，使用本地 127.0.0.1 mock HTTP server，无真实账号、无私网写入。覆盖密码摘要和 authx 精确序列化、登录失败脱敏、cookie 替换、原始历史分页、metadata 两种结构、封面 MIME/字节、Range/二进制/错误/重定向透明、路径 traversal 阻断；新增正确 API 封面路径、null/0 年份回退 releaseDate、无效日期与年份优先级回归。样本均为合成数据，只借真实响应结构，不保存私有曲目 GUID/标题/路径。

Python 3.14 实际执行，并用 Python AST 的 3.10 feature_version 检查语法兼容；没有安装第三方依赖。单元测试成功不替代真实验证，本轮另已完成上述 14 次纯 GET 的真实私有连接验证。
