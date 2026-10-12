# 测试说明

所有测试只使用临时目录、本地回环假 NAS 与模拟数据；**不会写入 `runtime/`，不会向真实飞牛服务写入任何播放，也不会向 Last.fm 或任何外部服务发送数据**。

## 运行

在项目目录：

```sh
python -m unittest discover -s tests -v
```

全部测试仅需标准库（Python 3.10+）。

## 覆盖范围

| 文件 | 内容 |
| --- | --- |
| `test_archive.py` | 事件校验、幂等去重、同曲重播、账号隔离、周/月/年边界、闰年、时区零点、CSV 公式注入防护、搜索转义 |
| `test_report_extras.py` | 星期分布、流派、年代（仅真实发行年）、比例、指纹、快速事实、发现排名、流派流的时间轴与未来污染防护 |
| `test_connector.py` | 公开前端签名（GET/POST）、登录摘要、历史分页、元数据、封面路由与响应、错误净化 |
| `test_poller.py` | 播放探测：历史序列 diff、连续重播、首次只建基线不回填、15 分钟窗口与精确记录对账、对账失败时的兜底（首次出现曲目照记、单条目前移记为重播、多种变动形状不猜测）、账号隔离、`recent()` 真实 HTTP、报告精确/估算拆分 |
| `test_server.py` | 真实 HTTP：会话、401/403、跨站写拒绝、超大请求体、完整导出与导入（合并/恢复）、账号数据隔离、分页与 CSV 公式注入防护 |
| `test_flow.py` | 连接器 → 假 NAS → HTTP 服务 → 临时归档全链路；封面契约；报表分页；CSV；完整导出与合并导入 |

## 浏览器验收（模拟数据）

```sh
python scripts/preview_fixture.py          # 默认 5679
python scripts/preview_fixture.py --port 5680
```

该脚本启动一个**仅本机**的假 NAS 与独立临时数据库（67 条模拟播放），用于检查周/月/年报告、排行榜、收听时钟、年度模块、记录搜索与 CSV。凭据 `fixture / test-only` 是测试专用；日志会打印本次假 NAS 地址。退出即删除临时数据，与正式 `runtime/` 完全隔离。正式服务 `python -m app.server`（默认 5678）不受影响。

## 本地真库注入虚拟播放（全功能手测）

```sh
python scripts/inject_test_data.py             # 往本地 runtime 库注入约一年播放
python scripts/inject_test_data.py --clean     # 清除全部虚拟播放
```

曲目元数据取自**真实账号**（标题/歌手/专辑/封面都是真数据），只有播放事件是合成的，分布在过去一年（工作日/深夜/爆发日等模式）。虚拟行以 `source='synthetic_poll' / 'synthetic_gateway'` 标记，与真实采集（`history_poll`、`fnmusic_event`）可区分，`--clean` 只删虚拟行。注意：只对本机 `runtime/` 使用，**不会向 NAS 写任何数据**；注入会让「首次发现」等统计被虚拟数据占据，正式观察真实习惯前记得 `--clean`。

## 真实账号验证边界

- 已用真实账号验证：登录、`user/me`、播放历史读取、曲目元数据、封面（WebP）。
- 播放历史条目**没有播放时间**，其 `updatedAt` 是元数据更新时间，任何测试与实现都不得把它当作收听时间。
- 播放写入（`event/report` 捕获）与轮询探测以真实使用为准；测试只验证逻辑与本地假服务，不向真实 NAS 发送播放。
- 轮询探测的播放时间按首次发现估算，记录为 `confidence="observed"`；测试断言其与精确记录的区分与对账窗口。
