# jd_AutoComment

京东批量自动评价工具。基于京东评价中心接口与真实浏览器自动化，自动生成贴合商品品类的评价文案并晒图，支持普通评价与追评。

> 本项目 Fork 自 [Dimlitter/jd_AutoComment](https://github.com/Dimlitter/jd_AutoComment)，在其基础上重构了文案生成、风控对策与提交链路，并新增浏览器自动化版本。上游致谢见文末。

## 功能特性

- **双提交方式**：真实浏览器自动化（`browser_comment.py`，推荐）与 requests 直连（`auto_comment_plus.py`）两套实现
- **自动晒图**：抓取商品其他买家的晒单图，字节微调避免与原图重复后上传京东图床
- **品类化文案**：内置 7 大品类文案池（药品保健/美妆个护/食品饮料/数码家电/母婴/服饰/家居日用），根据商品名自动匹配
- **TF-IDF 关键词提取**：分词 + 统计评分模型从商品名提取核心品类词（如"电动车头盔…M码"提取"头盔"），品牌词/属性词/病症词自动过滤
- **风控对策**：双重编码提交、失败退避重试、失败订单持久化跳过、每单 30~60 秒随机休眠
- **自动过滤**：试用装/赠品/外卖/虚拟权益订单自动跳过
- **追评 UI 模式**：逐字符键入 + 页面控件传图 + 5 星评分，由京东页面自身 JS 完成提交，稳定性最高

## 项目结构

```
jd_AutoComment/
├── browser_comment.py    # 浏览器版（推荐）：普通评价 + 追评，含晒图
├── auto_comment_plus.py  # 本地 requests 版：普通评价 + 追评 + 服务评价
├── jdspider.py           # 评论爬虫（爬同类商品好评作为文案素材）
├── config.example.yml    # 配置模板（复制为 config.yml 填入 Cookie）
├── USAGE.md              # 完整使用说明（参数表、FAQ）
├── requirements.txt
└── .gitignore            # 已排除 Cookie/登录态/运行数据
```

## 快速开始

### 1. 环境要求

- Python 3.8+（推荐 3.10+）
- Node.js 16+（仅浏览器版需要）

### 2. 安装依赖

```bash
pip install -r requirements.txt

# 浏览器版还需要（一次性）：
npm i -g agent-browser
agent-browser install
```

### 3. 配置 Cookie

```bash
copy config.example.yml config.yml
# 编辑 config.yml，填入完整浏览器 Cookie
```

Cookie 获取：电脑 Chrome 登录京东 → 打开 `club.jd.com` 的"我的评价"页 → F12 → Network → 刷新 → 第一个请求 → 复制 `cookie:` 后的整段值。

> 注意：仅 `pt_key` + `pt_pin` 不够，必须是完整 Cookie。Cookie 会过期，失效后重新抓取。

### 4. 运行

```bash
# 先 dry-run 看一遍会评哪些单
python browser_comment.py --dry-run

# 普通评价 1 单（文字+图片），验证后再放量
python browser_comment.py --mode ordinary --limit 1

# 追评 1 单（文字+图片+5星）
python browser_comment.py --mode review --limit 1

# 全量
python browser_comment.py --mode ordinary
python browser_comment.py --mode review
```

本地 requests 版（不需要 agent-browser，含服务评价，但更易触发风控）：

```bash
python auto_comment_plus.py
```

完整参数说明、工作原理、常见问题见 [USAGE.md](USAGE.md)。

## 工作原理（简述）

1. **订单解析**：读取京东评价中心"待评价订单/待追评"列表，按关键词与链接特征过滤无效订单
2. **文案生成**：posseg 分词 + 自定义品类词典 + TF-IDF 特异度评分提取核心品类词，从对应品类文案池随机组句，同轮去重
3. **晒图**：拉取商品晒单图 → 随机位翻转一个字节（避免与原图 MD5 相同）→ 上传京东图床
4. **提交**：
   - 普通评价走页面内 fetch，`content` 双重 percent-编码（与京东页面行为一致，否则存储乱码）
   - 追评走真实页面 UI：逐字符键盘事件键入、页面控件上传图片、点 5 星、发表
5. **风控**：提交最小间隔 30 秒、随机休眠 30~60 秒、失败退避重试（60s/120s ×3）、错误码识别（无资格/风控/需实名）、失败订单记录跳过

## 免责声明

本项目为 Python 学习交流的开源非营利项目，仅作为程序员之间相互学习交流之用。

- 严禁用于商业用途，禁止使用本项目进行任何盈利活动
- 京东对自动化访问有风控机制，不当使用可能导致账号异常，请自行评估风险并控制频率
- 使用者请遵从京东相关政策与法律法规，对一切非法使用所产生的后果，本项目概不负责
- Cookie 属于敏感凭证，请勿提交到 Git 仓库或分享给他人

## 致谢

- [qiu-lzsnmb/jd_lzsnmb](https://github.com/qiu-lzsnmb/jd_lzsnmb) - 自动评价脚本
- [Zhang Jiale/JD_comment_spider](https://github.com/2274900/JD_comment_spider) - 评论爬虫
- [Dimlitter/jd_AutoComment](https://github.com/Dimlitter/jd_AutoComment) - 上游整合项目

## 许可证

[MIT License](LICENSE)
