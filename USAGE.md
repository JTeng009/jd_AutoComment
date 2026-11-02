# 使用说明

本项目提供三种运行方式的京东批量自动评价工具。**推荐使用浏览器版**（`browser_comment.py`）——提交动作在真实浏览器中完成，风控通过率最高，文字与图片均已实测可用。

> ⚠️ 本项目仅供学习交流，严禁用于商业用途或任何盈利活动，使用者需自行承担全部后果。

---

## 一、两个脚本怎么选

| 脚本 | 运行方式 | 文字 | 图片 | 追评 | 服务评价 | 适用场景 |
| --- | --- | --- | --- | --- | --- | --- |
| **`browser_comment.py`** ⭐推荐 | 真实浏览器（agent-browser） | ✅ | ✅ | ✅ | ❌ | 日常使用，风控通过率最高 |
| `auto_comment_plus.py` | requests 直连 | ✅ | ✅ | ✅ | ✅ | 功能最全，但直连提交易触发风控 |

其他文件：

| 文件 | 作用 |
| --- | --- |
| `jdspider.py` | 评论爬虫（爬同类商品好评做文案素材），被本地版引用 |
| `config.yml` / `config.user.yml` | Cookie 配置（两个脚本共用，`config.user.yml` 优先） |
| `requirements.txt` | Python 依赖 |
| `jd_state.json` | 浏览器登录态（脚本自动生成，**含明文 Cookie，已加入 .gitignore，勿提交勿分享**） |
| `failed_orders.txt` | 提交失败订单记录（下次运行自动跳过，可手动清空） |

---

## 二、环境准备

### 2.1 Python 依赖

- Python **3.8+**，推荐 **3.10+**

```bash
pip install -r requirements.txt
```

依赖：`jieba`（关键词提取）、`requests`、`lxml`（页面解析）、`zhon`（中文分句）、`pyyaml`（读配置）。

### 2.2 浏览器版额外需要 agent-browser

```bash
npm i -g agent-browser
agent-browser install    # 下载 Chrome 内核（约 200MB，一次性）
```

要求 Node.js 16+。已验证版本：agent-browser 0.41.1。

### 2.3 获取京东 Cookie

Cookie 是唯一的身份凭证，**必须是完整浏览器 Cookie**（仅 `pt_key`+`pt_pin` 不够）：

1. 电脑 Chrome 登录京东
2. 打开 <https://club.jd.com/myJdcomments/myJdcomment.action>（确认能看到"待评价订单"数字）
3. `F12` → Network → 刷新 → 第一个请求 → Request Headers → 复制 `cookie:` 后的**整段值**

Cookie 会过期（数天到数周），失效后重新抓取即可。**Cookie 等同登录凭证，勿提交 Git、勿发给他人**；`config.yml` 若含真实 Cookie，提交代码前务必清空。

### 2.4 配置 Cookie

推荐使用用户配置（不被仓库更新覆盖）：

```bash
copy config.yml config.user.yml     # Windows
cp config.yml config.user.yml       # Linux / macOS
```

```yml
user:
  cookie: 'pt_key=xxx; pt_pin=xxx; shshshfpa=xxx; ...'
  # 以下可选：调节提交节奏（防风控）
  min_submit_interval: 30   # 两次提交最小间隔（秒），默认 30
  sleep_range: [30, 60]     # 每笔之后的随机休眠区间（秒），默认 30~60
```

> 注意：`config.yml` 必须存在且含 `user.cookie` 字段（`jdspider.py` 导入时直接读取），即使你把 Cookie 填在 `config.user.yml`。

---

## 三、浏览器版（推荐）

### 3.1 工作原理

1. 从 `config.yml` 读取 Cookie 注入浏览器生成登录态（`jd_state.json`）
2. 打开京东评价中心，解析待评价/待追评订单（自动跳过试用装、赠品、外卖/虚拟权益单）
3. **普通评价**：生成文案 → 下载商品晒图 → 微调字节 → 上传京东图床 → 页面内 fetch 提交（双重编码，与京东页面行为一致）
4. **追评**：打开订单专属追评页 → 逐字符键入文案 → 页面控件上传图片 → 点 5 星 → 点发表，由京东页面自己的 JS 完成提交
5. 每单之间随机休眠 30~60 秒（可用参数调整）

### 3.2 常用命令

```bash
# 建议先 dry-run 看一遍会评哪些单
python browser_comment.py --dry-run

# 普通评价 1 单（文字+图片），验证后再放量
python browser_comment.py --mode ordinary --limit 1

# 追评 1 单（文字+图片+5星）
python browser_comment.py --mode review --limit 1

# 全量
python browser_comment.py --mode ordinary    # 普通评价全部
python browser_comment.py --mode review      # 追评全部
```

### 3.3 全部参数

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `--mode ordinary/review` | `ordinary` | 普通评价 / 追评 |
| `--dry-run` | 关 | 只列订单不提交 |
| `--limit N` | 0（全部） | 最多处理 N 单 |
| `--spider` | 关 | 实时爬同款好评做文案（更贴商品但每单慢 1~2 分钟） |
| `--no-imgs` | 关 | 不带图，仅文字 |
| `--max-imgs N` | 2 | 每单最多带几张图 |
| `--encoding utf-8/gbk` | `utf-8` | 提交编码（一般不用动） |
| `--min-interval` | 30 | 两次提交最小间隔（秒） |
| `--sleep-min / --sleep-max` | 30 / 60 | 每笔之后随机休眠区间（秒） |
| `--session` | `jd` | agent-browser 会话名（多账号可分开） |
| `--keep-open` | 关 | 结束后不关闭浏览器 |
| `--log-file` | `browser_comment.log` | 日志文件（UTF-8） |

### 3.4 文案生成

- 文案池在 `browser_comment.py` 顶部：`_FAST_COMMENTS`（普通评价）/ `_REVIEW_COMMENTS`（追评专用，"使用一段时间后"口吻），可自行增删
- `$` 占位符会替换为从商品名提取的核心词。提取规则：textrank 候选 → 过滤人群/颜色/尺码/品牌黑名单（`_KEYWORD_BLACKLIST`）→ 优先取标题第一段位置靠后的词 → 多级兜底。例："京东京造电动车头盔…男女士安全帽…M码" → `头盔`
- 追评固定 5 星、2~3 句；普通评价 6 句；同一轮内句子与整条文案均去重
- 觉得模板文案太假可加 `--spider` 实时爬同款商品好评

### 3.5 自动跳过的订单

商品名含以下关键词即跳过（词表在 `auto_comment_plus.py` 的 `_SKIP_KEYWORDS`，三个脚本共用）：

- 试用类：`试用 / 试用品 / 体验装 / 小样 / 样品 / 尝鲜 / 体验官 / 申领 / 0元购 / 1分钱 / 一分钱`
- 赠品类：`赠品 / 样品`
- 外卖类：`外卖 / 京东秒送 / 秒送 / 达达配送`
- 另有商品链接无法解析的订单（多为京东外卖/虚拟权益单，如"JD扭蛋商店权益包"）

每条跳过都有日志，dry-run 即可确认。

---

## 四、本地 requests 版（auto_comment_plus.py）

不依赖浏览器，requests 直连提交。功能最全（含服务评价），但直连提交易触发"活动太火爆"风控，建议优先用浏览器版。Cookie 配置见 2.4。

### 4.1 运行

```bash
python auto_comment_plus.py
```

Windows 下如 `python` 不可用，可换用 `py auto_comment_plus.py` 或完整解释器路径。

### 4.2 命令行参数

```text
usage: auto_comment_plus.py [-h] [--dry-run] [-lv LOG_LEVEL] [-o LOG_FILE]
```

| 参数 | 说明 |
| --- | --- |
| `-h`, `--help` | 显示帮助信息 |
| `--dry-run` | 完整跑一遍流程但**不真正提交评论**（提交环节会打印请求参数并跳过） |
| `-lv`, `--log-level` | 日志级别：`DEBUG` / `INFO` / `WARNING` / `ERROR`（默认 `INFO`） |
| `-o`, `--log-file` | 日志文件路径（默认 `log.txt`） |

### 4.3 执行流程

1. **统计**：读取「我的评价」页，统计 `待评价订单 / 待追评 / 服务评价` 数量
2. **普通评价**：5 星 + 自动文案 + 晒图（爬商品晒图 → 微调字节 → 上传京东图床）
3. **追评**：4 句文案 + 晒图
4. **服务评价**：随机 4~5 星
5. 提交失败自动退避重试（60s/120s，最多 3 次），单笔失败不中断整轮
6. 全部完成后若仍有剩余，自动重试一轮

跳过规则与浏览器版相同（见 3.5）。

---

## 六、常见问题

**Q1：运行后提示「Ck 出现错误，请重新抓取！」**
Cookie 已失效或格式不对。重新按 2.3 抓取完整 Cookie，确认配置的是 `config.user.yml`（而非 `config.yml`）且缩进正确。

**Q2：提示「请求被重定向到登录页」**
只配置了 `pt_key` + `pt_pin`。`club.jd.com` 必须使用完整浏览器 Cookie。

**Q3：提示「获取到 N 个，尝试第二种 XPath」**
京东页面结构变化，脚本已自动降级用备用 XPath 解析；若仍失败需根据最新页面结构调整 `auto_comment_plus.py` 中的 XPath 表达式。

**Q4：图片上传失败 / 没有成功上传的图片**
图片上传接口 `ajaxUploadImage.action` 有频率限制，或该商品无晒单图。失败时会**自动降级为纯文字评价**，不影响主流程。

**Q5：评价文案里的替换词不合适（如"这家的女士很好用"）**
已修复：关键词由 `extract_keyword()` 提取，会过滤人群/颜色/尺码/品牌黑名单并优先取标题第一段的核心品类词（"京东京造电动车头盔…M码"→"头盔"）。个别长尾商品仍可能取词不准，可在 `_KEYWORD_BLACKLIST` 补充干扰词。

**Q6：想先测试不真实提交**
浏览器版用 `--dry-run`（只列订单）；本地版加 `--dry-run` + `-lv DEBUG` 观察完整流程与请求参数。

**Q7：脚本卡住不动**
每笔评价之间有 30~60 秒随机休眠（模拟真人行为，防风控），属正常现象，请勿中断。

**Q8：日志出现"活动太火爆，请稍微再试"，脚本还会崩溃吗？**
不会再崩溃。新版已做三层处理：

1. **不再解析非 JSON 响应**——风控返回的是纯文本，脚本改为安全解析，失败只记 WARNING 并跳过该单，整轮评价继续跑完；
2. **自动退避重试**——命中风控文案时按 60s / 120s 退避重试，最多 3 次；网络异常同样重试且不抛异常；
3. **主动降频**——两次提交强制最小间隔（默认 30 秒），每笔之后随机休眠 30~60 秒，且同一轮内不重复使用相同句子/整条文案（重复内容同样会被拦截）。

仍然频繁被拦截时，按顺序调整：

- 在 `config.yml` 的 `user` 节点下增大节奏：`min_submit_interval: 90`、`sleep_range: [120, 300]`（默认 `30` / `[30, 60]`）
- 少量分批：一次只跑 3~5 单，分几个时段跑完
- 错开活跃时段：避开大促、京东促销高峰
- 确认 Cookie 新鲜（刚抓取的完整浏览器 Cookie），过期 Cookie 也容易被判为异常流量

**Q9：评价内容变成乱码（çæ¯ 之类）？**
已修复。根因是京东接口的 `content` 需要**双重 percent-编码**（页面 JS 先 `encodeURIComponent`，表单序列化再编一次，服务端解两次）。浏览器版与本地版均已按此实现。若你自行改动提交逻辑，务必保持双重编码。

**Q10：追评提交报「输入内容不符合要求」？**
多为 fetch 直发追评接口导致（参数校验严格）。**追评请使用浏览器版**——它走真实页面 UI 流程（逐字符键入 + 页面控件传图 + 点 5 星 + 发表），由京东页面自己的 JS 构造请求，实测最稳。另外注意：追评必须打分（5 星），否则会被前端校验拦截。

---

## 七、合规与风险提示


1. 本项目为 Python 学习交流的开源非营利项目，仅用于程序员之间相互学习交流，**严禁用于商业用途**。
2. 京东对自动化访问有风控机制，高频请求可能导致账号异常，请合理控制频率，**建议每个账号每天运行一次以内**。
3. 使用者需自行遵守京东的相关服务条款与法律法规，对一切非法使用所产生的后果，本项目概不负责。
4. Cookie 属于敏感凭证，请勿提交到 Git 或分享给他人；若已泄露，请及时在京东「安全设置」中退出登录以使 Cookie 失效。

---

## 八、许可证

[MIT License 2.0](LICENSE)
