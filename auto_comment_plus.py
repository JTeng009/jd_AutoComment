# -*- coding: utf-8 -*-
# @Time : 2022/2/8 20:50
# @Author : @qiu-lzsnmb and @Dimlitter
# @File : auto_comment_plus.py

import argparse
import copy
import io
import logging
import os
import random
import sys
import time
import urllib

import jieba  # just for linting
import jieba.analyse
import requests
import yaml
from lxml import etree

import jdspider

try:
    # 脚本全程 verify=False（京东站点证书校验不稳），屏蔽无意义的告警刷屏
    import urllib3
    from urllib3.exceptions import InsecureRequestWarning

    urllib3.disable_warnings(InsecureRequestWarning)
except Exception:  # pragma: no cover
    pass

# constants
CONFIG_PATH = "./config.yml"
USER_CONFIG_PATH = "./config.user.yml"
ORDINARY_SLEEP_SEC = 10
SUNBW_SLEEP_SEC = 5
REVIEW_SLEEP_SEC = 10
SERVICE_RATING_SLEEP_SEC = 15
# 需要跳过的订单关键词（试用装、赠品、申领类、外卖等）
_SKIP_KEYWORDS = [
    "试用", "试用品", "体验装", "小样", "样品", "赠品",
    "尝鲜", "体验官", "申领", "0元购", "1分钱", "1 分钱", "一分钱",
    "外卖", "京东秒送", "秒送", "达达配送",
]
# 京东风控提示关键词：命中说明请求被限流/拦截，而非接口本身报错
_BUSY_MARKERS = ["活动太火爆", "太火爆", "稍后再试", "请稍后", "访问频繁", "请求过于频繁"]
# 提交失败（被风控）后的重试策略：第 n 次失败后等待 SUBMIT_RETRY_SLEEP_SEC * n 秒
SUBMIT_MAX_RETRY = 3
SUBMIT_RETRY_SLEEP_SEC = 60
# 两次提交之间的最小间隔（秒）与每笔之后的随机休眠区间（秒）
MIN_SUBMIT_INTERVAL_SEC = 30
SUBMIT_SLEEP_RANGE = (30, 60)
# 记录本次运行已用过的句子/文案，避免提交完全重复的内容（重复内容同样会被拦截）
_USED_SENTENCES = set()
_USED_TEXTS = set()
_LAST_SUBMIT_TS = 0.0

# logging with styles
# Reference: https://stackoverflow.com/a/384125/12002560
_COLORS = {
    "black": 0,
    "red": 1,
    "green": 2,
    "yellow": 3,
    "blue": 4,
    "magenta": 5,
    "cyan": 6,
    "white": 7,
}

_RESET_SEQ = "\033[0m"
_COLOR_SEQ = "\033[1;%dm"
_BOLD_SEQ = "\033[1m"
_ITALIC_SEQ = "\033[3m"
_UNDERLINED_SEQ = "\033[4m"

_FORMATTER_COLORS = {
    "DEBUG": _COLORS["blue"],
    "INFO": _COLORS["green"],
    "WARNING": _COLORS["yellow"],
    "ERROR": _COLORS["red"],
    "CRITICAL": _COLORS["red"],
}


def format_style_seqs(msg: str, use_style: bool = True):
    if use_style:
        msg = msg.replace("$RESET", _RESET_SEQ)
        msg = msg.replace("$BOLD", _BOLD_SEQ)
        msg = msg.replace("$ITALIC", _ITALIC_SEQ)
        msg = msg.replace("$UNDERLINED", _UNDERLINED_SEQ)
    else:
        msg = msg.replace("$RESET", "")
        msg = msg.replace("$BOLD", "")
        msg = msg.replace("$ITALIC", "")
        msg = msg.replace("$UNDERLINED", "")


class StyleFormatter(logging.Formatter):
    def __init__(self, fmt=None, datefmt=None, use_style=True):
        logging.Formatter.__init__(self, fmt, datefmt)
        self.use_style = use_style

    def format(self, record):
        rcd = copy.copy(record)
        levelname = rcd.levelname
        if self.use_style and levelname in _FORMATTER_COLORS:
            levelname_with_color = "%s%s%s" % (
                _COLOR_SEQ % (30 + _FORMATTER_COLORS[levelname]),
                levelname,
                _RESET_SEQ,
            )
            rcd.levelname = levelname_with_color
        return logging.Formatter.format(self, rcd)


# 关键词提取干扰词：人群/颜色/尺码/品牌/营销词（避免"这家的女士很好用"之类病句）
_KEYWORD_BLACKLIST = (
    "男", "女", "儿童", "宝宝", "婴儿", "情侣", "亲子",
    "款", "码", "号", "装", "套", "只", "个", "瓶", "包", "盒", "件", "条", "片",
    "白", "黑", "红", "蓝", "绿", "黄", "粉", "灰", "紫", "米白", "透明", "彩色",
    "京东", "京造", "自营", "旗舰店", "官方", "正品", "授权",
    "系列", "认证", "专属", "通用", "专用", "升级", "新款", "整箱", "组合", "味",
)


# 分词增强词库：注册进 jieba 保证品类词不被切碎，同时作为关键词首选来源
_CATEGORY_WORDS = [
    "抽纸", "卷纸", "纸巾", "湿巾", "垃圾袋", "收纳盒", "置物架", "保温杯",
    "洗衣液", "香薰", "花洒", "晾衣架", "拖把", "枕头", "毛巾",
    "面霜", "蜜霜", "精华液", "乳液", "防晒霜", "洗面奶", "身体乳", "爽肤水",
    "眼霜", "口红", "粉底液", "面膜",
    "乳膏", "软膏", "滴剂", "口服液", "胶囊", "咀嚼片", "维生素", "益生菌",
    "鱼油", "叶黄素", "创可贴", "口罩",
    "坚果", "饼干", "酸奶", "巧克力", "薯片", "糖果", "咖啡", "奶粉",
    "雪糕", "冰淇淋", "糯米糍", "干脆面", "方便面", "速食面", "辣条", "豆干",
    "糕点", "粽子", "汤圆", "汽水", "矿泉水", "洗手液", "消毒液",
    "蓝牙耳机", "耳机", "充电器", "数据线", "剃须刀", "电饭煲", "吹风机",
    "扫地机", "净水器", "智能手表", "摄像头",
    "纸尿裤", "拉拉裤", "尿不湿", "奶瓶", "辅食", "安抚奶嘴",
    "休闲裤", "牛仔裤", "羽绒服", "卫衣", "衬衫", "运动鞋",
    "头盔", "安全帽", "电动车", "摩托车", "自行车", "行李箱",
]

# 启动时注册自定义词（高频 + 名词），确保分词不被切碎
for _w in _CATEGORY_WORDS:
    jieba.add_word(_w, freq=1000000, tag="n")


def extract_keyword(pname):
    """从商品名提取核心品类词。

    jieba textrank 按共现权重排序，常把"女士/M码/米白"等干扰词排到第一；
    三层修正：
    1. textrank 候选（过滤黑名单）里出现在标题第一段的，取位置最靠后的
    2. 兜底：第一段分词后最后一个普通名词
    3. 再兜底：textrank 首个非黑名单词 / "宝贝"
    """
    import re as _re
    import jieba.posseg as pseg

    def bad(w):
        return len(w) < 2 or any(b in w for b in _KEYWORD_BLACKLIST)

    # ---- 分词（带位置），候选 = 普通名词/英文/名动词 ----
    try:
        words = pseg.lcut(pname)
    except Exception:
        words = []
    tokens = []  # (word, flag, start) 全量词，用于检查邻居词性
    candidates = []  # (start, word)
    pos = 0
    for w, f in words:
        tokens.append((w, f, pos))
        # vn（名动词，如"干吃"）是动作语义，不是品类名词，排除
        if (f.startswith("n") or f == "eng") and not bad(w):
            candidates.append((pos, w))
        pos += len(w)

    # ---- 品牌段识别：商品名最前方的连续名词合并长度 <=6 且后面还有候选 ----
    brand_end = 0
    for idx, (start, w) in enumerate(candidates):
        if start != brand_end:
            break
        brand_end = start + len(w)
        if idx + 1 < len(candidates) and brand_end > 6:
            break
    has_more = bool(candidates) and candidates[-1][0] >= brand_end
    brand_span = (0, brand_end) if (brand_end and has_more and brand_end <= 6) else None

    # ---- TF-IDF 统计评分 ----
    idf_freq = {}
    try:
        from jieba.analyse import default_tfidf

        idf_freq = default_tfidf.idf_freq
    except Exception:
        pass

    token_index = {t[2]: i for i, t in enumerate(tokens)}
    best, best_score = None, -1.0
    for start, w in candidates:
        score = idf_freq.get(w, 10.0) * (len(w) ** 0.5)
        if w in _CATEGORY_WORDS:
            score *= 2.0
        if brand_span and start < brand_span[1]:
            score *= 0.2
        # 中文商品名前段的英文串基本都是品牌/型号（HOTUN、Face 等）
        if w.isascii() and start * 3 < len(pname):
            score *= 0.2
        # 动词包围的名词是动作宾语（"通[开单]开"），不是商品本体，强惩罚
        i = token_index.get(start)
        if i is not None:
            prev_f = tokens[i - 1][1] if i > 0 else ""
            next_f = tokens[i + 1][1] if i + 1 < len(tokens) else ""
            verb_neighbors = (prev_f == "v") + (next_f == "v")
            if verb_neighbors == 2:
                score *= 0.1
            elif verb_neighbors == 1:
                score *= 0.3
        if score > best_score:
            best, best_score = w, score

    if best:
        return best

    # ---- 兜底路径 ----

    try:
        ranked = jieba.analyse.textrank(pname, topK=10, allowPOS="n") or []
    except Exception:
        ranked = []
    pool = [w for w in ranked if not bad(w)]
    # 不按括号切分：品牌后紧跟的括号会把第一段截得过短（如"虎顿（HOTUN）挂锁..."）
    first_seg = _re.split(r"[\s/，,，【\[|]", pname)[0]

    in_seg = [w for w in pool if w in first_seg]
    if in_seg:
        return max(in_seg, key=lambda w: first_seg.find(w) + len(w))

    try:
        # 兜底：第一段分词取名词，排除开头 4 字内的品牌区词
        nouns = [
            w
            for w, f in pseg.lcut(first_seg)
            if f.startswith("n") and not bad(w) and first_seg.find(w) >= 4
        ]
        if nouns:
            return nouns[-1]
    except Exception:
        pass

    # 兜底：第一段末尾的中文词（"维达超韧抽纸" -> "抽纸"），优先 2 字
    for n in (2, 3):
        m = _re.search(rf"[\u4e00-\u9fa5]{{{n}}}$", first_seg)
        if m and not bad(m.group()):
            return m.group()

    return pool[0] if pool else "宝贝"


# 评价生成
def generation(pname, _class: int = 0, _type: int = 1, opts: object = None):
    result = []
    opts = opts or {}
    items = ["商品名"]
    items.clear()
    items.append(pname)
    opts["logger"].debug("Items: %s", items)
    loop_times = len(items)
    opts["logger"].debug("Total loop times: %d", loop_times)
    for i, item in enumerate(items):
        opts["logger"].debug("Loop: %d / %d", i + 1, loop_times)
        opts["logger"].debug("Current item: %s", item)
        spider = jdspider.JDSpider(item)
        opts["logger"].debug("Successfully created a JDSpider instance")
        # 增加对增值服务的评价鉴别
        if "赠品" in pname or "非实物" in pname or "增值服务" in pname:
            result = [
                "赠品挺好的。",
                "很贴心，能有这样免费赠送的赠品!",
                "正好想着要不要多买一份增值服务，没想到还有这样的赠品。",
                "赠品正合我意。",
                "赠品很好，挺不错的。",
                "本来买了产品以后还有些担心。但是看到赠品以后就放心了。",
                "不论品质如何，至少说明店家对客的态度很好！",
                "我很喜欢这些商品！",
                "我对于商品的附加值很在乎，恰好这些赠品为这件商品提供了这样的的附加值，这令我很满意。"
                "感觉现在的网购环境环境越来越好了，以前网购的时候还没有过么多贴心的赠品和增值服务",
                "第一次用京东，被这种赠品和增值服物的良好态度感动到了。",
                "赠品还行。",
            ]
        else:
            result = spider.getData(4, 3)  # 这里可以自己改
        opts["logger"].debug("Result: %s", result)

    # class 0是评价 1是提取id
    try:
        name = extract_keyword(pname)
        opts["logger"].debug("Name: %s", name)
    except Exception as e:
        opts["logger"].warning(
            'keyword extraction error: %s, name fallback to "宝贝"', e
        )
        name = "宝贝"
    if _class == 1:
        opts["logger"].debug("_class is 1. Directly return name")
        return name
    else:
        num = 0
        if _type == 1:
            num = 6
        elif _type == 0:
            num = 4
        # 优先从未使用过的句子中抽样，降低提交重复内容被拦截的概率
        pool = [c for c in result if c not in _USED_SENTENCES] or list(result)
        num = min(num, len(pool))
        # use `.join()` to improve efficiency
        comments = "".join(random.sample(pool, num))
        text = comments.replace("$", name)
        # 整条文案去重：京东对完全相同的评价内容同样会限流
        tries = 0
        while text in _USED_TEXTS and tries < 5:
            comments = "".join(random.sample(pool, num))
            text = comments.replace("$", name)
            tries += 1
        _USED_SENTENCES.update(comments)
        _USED_TEXTS.add(text)
        opts["logger"].debug("_type: %d", _type)
        opts["logger"].debug("num: %d", num)
        opts["logger"].debug("Raw comments: %s", comments)

        return 5, text


# 查询全部评价
def all_evaluate(opts=None):
    opts = opts or {}
    N = {}
    url = "https://club.jd.com/myJdcomments/myJdcomment.action?"
    opts["logger"].info("URL: %s", url)
    opts["logger"].debug("Fetching website data")
    req = requests.get(url, headers=headers, verify=False)
    opts["logger"].debug(
        "Successfully accepted the response with status code %d", req.status_code
    )
    if not req.ok:
        opts["logger"].debug(
            "Status code of the response is %d, not 200", req.status_code
        )
    req_et = etree.HTML(req.text)
    opts["logger"].debug("Successfully parsed an XML tree")
    evaluate_data = req_et.xpath('//*[@id="main"]/div[2]/div[1]/div/ul/li')
    # print(evaluate)
    loop_times = len(evaluate_data)
    opts["logger"].debug("Total loop times: %d", loop_times)
    for i, ev in enumerate(evaluate_data):
        opts["logger"].debug("Loop: %d / %d", i + 1, loop_times)
        na = ev.xpath("a/text()")[0]
        opts["logger"].debug("na: %s", na)
        try:
            num = ev.xpath("b/text()")[0]
            opts["logger"].debug("num: %s", num)
        except IndexError:
            opts["logger"].info("Can't find num content in XPath, fallback to 0")
            num = 0
        N[na] = int(num)
    return N


def upload_image_to_jd(filename: str, image_data: bytes, opts=None) -> str:
    """
    将图片上传到京东图片服务器，返回可用的图片URL。

    上传接口: https://club.jd.com/myJdcomments/ajaxUploadImage.action
    返回: 协议相对 URL，如 //img30.360buyimg.com/...
    失败时返回空字符串。
    """
    opts = opts or {}
    logger = opts.get("logger")
    url = "https://club.jd.com/myJdcomments/ajaxUploadImage.action"

    upload_headers = {
        "cookie": headers.get("cookie", ""),
        "User-Agent": headers.get("User-Agent", ""),
        "Referer": "https://club.jd.com/myJdcomments/myJdcomment.action",
        "Origin": "https://club.jd.com",
        "X-Requested-With": "XMLHttpRequest",
        "Accept": "*/*",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Connection": "keep-alive",
    }

    buffer = io.BytesIO(image_data)
    files = {
        "name": (None, filename),
        "Filedata": (filename, buffer, "image/jpeg"),
        "upload": (None, "Submit Query"),
    }

    try:
        resp = requests.post(url, headers=upload_headers, files=files, timeout=30, verify=False)
        if resp.status_code == 200 and ".jpg" in resp.text:
            uploaded_url = resp.text.strip()
            if logger:
                logger.info(f"\t\t图片上传成功: {uploaded_url}")
            return uploaded_url
        else:
            if logger:
                logger.warning(f"\t\t图片上传失败: HTTP {resp.status_code}, {resp.text[:200]}")
            return ""
    except Exception as e:
        if logger:
            logger.warning(f"\t\t图片上传异常: {e}")
        return ""


def _safe_json(resp):
    """安全解析 JSON 响应。

    京东风控拦截时接口返回的是纯文本（如 "活动太火爆，请稍微再试"），并非 JSON，
    此时返回空字典，避免调用方抛出 JSONDecodeError。
    """
    try:
        data = resp.json()
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _is_busy_response(text):
    """判断响应是否为京东风控/限流提示。"""
    return any(marker in (text or "") for marker in _BUSY_MARKERS)


def submit_comment(url, data, headers=None, opts=None, referer=None, max_retry=None):
    """提交评价 / 追评 / 服务评价。

    兼容京东风控返回的纯文本响应：
    - 返回 JSON 且 success 为 true  -> 成功
    - 返回 "活动太火爆，请稍微再试" 等风控文案 -> 退避后重试
    - 其它情况（参数错误等）-> 直接判定失败，重试无意义

    无论成功与否都不会抛出异常，保证单笔失败不会中断整轮评价。
    返回 (success: bool, resp_text: str)
    """
    opts = opts or {}
    logger = opts.get("logger")
    global _LAST_SUBMIT_TS
    max_retry = SUBMIT_MAX_RETRY if max_retry is None else max_retry
    req_headers = dict(headers or {})
    if referer:
        req_headers["Referer"] = referer

    resp_text = ""
    for attempt in range(1, max_retry + 1):
        if opts.get("dry_run"):
            if logger:
                logger.info("\t\t[dry-run] 已跳过提交，请求参数: %s", data)
            return True, "dry-run"
        resp = None
        # 保证两次提交之间的最小间隔，避免密集提交触发风控
        gap = time.time() - _LAST_SUBMIT_TS
        if _LAST_SUBMIT_TS and gap < MIN_SUBMIT_INTERVAL_SEC:
            wait = MIN_SUBMIT_INTERVAL_SEC - gap
            if logger:
                logger.info("\t\t距上次提交仅 %.0f 秒，等待 %.0f 秒再提交", gap, wait)
            time.sleep(wait)
        try:
            resp = requests.post(
                url, headers=req_headers, data=data, verify=False, timeout=30
            )
        except Exception as e:
            if logger:
                logger.warning("\t\t提交请求异常: %s", e)
        finally:
            _LAST_SUBMIT_TS = time.time()
        if resp is not None:
            resp_text = (resp.text or "").strip()
            payload = _safe_json(resp)
            if resp.status_code == 200 and payload.get("success"):
                if logger:
                    logger.info(
                        "发送请求后的状态码:%s,text:%s", resp.status_code, resp_text
                    )
                return True, resp_text
            if logger:
                logger.warning(
                    "\t\t提交失败(第%d/%d次) 状态码:%s 返回:%s",
                    attempt,
                    max_retry,
                    resp.status_code,
                    resp_text[:200],
                )
            if not _is_busy_response(resp_text):
                break  # 非风控类失败，重试也不会成功
        if attempt >= max_retry:
            break
        wait = SUBMIT_RETRY_SLEEP_SEC * attempt
        if logger:
            logger.warning(
                "\t\t疑似触发京东风控（%s），%d 秒后重试", resp_text[:60], wait
            )
        time.sleep(wait)
    return False, resp_text


# 普通评价
def ordinary(N, opts=None):
    opts = opts or {}
    Order_data = []
    req_et = []
    loop_times = N["待评价订单"] // 20
    opts["logger"].debug("Fetching website data")
    opts["logger"].debug("Total loop times: %d", loop_times)
    for i in range(loop_times + 1):
        url = (
            f"https://club.jd.com/myJdcomments/myJdcomment.action?sort=0&"
            f"page={i + 1}"
        )
        opts["logger"].debug("URL: %s", url)
        req = requests.get(url, headers=headers, verify=False)
        opts["logger"].debug(
            "Successfully accepted the response with status code %d", req.status_code
        )
        if not req.ok:
            opts["logger"].warning(
                "Status code of the response is %d, not 200", req.status_code
            )
        req_et.append(etree.HTML(req.text))
        opts["logger"].debug("Successfully parsed an XML tree")
    opts["logger"].debug("Fetching data from XML trees")
    opts["logger"].debug("Total loop times: %d", loop_times)
    for idx, i in enumerate(req_et):
        opts["logger"].debug("Loop: %d / %d", idx + 1, loop_times)
        opts["logger"].debug("Fetching order data in the default XPath")
        elems = i.xpath('//*[@id="main"]/div[2]/div[2]/table/tbody')
        opts["logger"].debug("Count of fetched order data: %d", len(elems))
        Order_data.extend(elems)
    if len(Order_data) != N["待评价订单"]:
        opts["logger"].debug(
            'Count of fetched order data doesn\'t equal N["待评价订单"]'
        )
        opts["logger"].debug("Clear the list Order_data")
        Order_data = []
        opts["logger"].debug("Total loop times: %d", loop_times)
        for idx, i in enumerate(req_et):
            opts["logger"].debug("Loop: %d / %d", idx + 1, loop_times)
            opts["logger"].debug("Fetching order data in another XPath")
            elems = i.xpath('//*[@id="main"]/div[2]/div[2]/table')
            opts["logger"].debug("Count of fetched order data: %d", len(elems))
            Order_data.extend(elems)

    opts["logger"].info(f"当前共有{N['待评价订单']}个评价。")
    opts["logger"].debug("Commenting on items")
    for i, Order in enumerate(Order_data):
        try:
            oid = Order.xpath('tr[@class="tr-th"]/td/span[3]/a/text()')[0]
            opts["logger"].debug("oid: %s", oid)
            oname_data = Order.xpath(
                'tr[@class="tr-bd"]/td[1]/div[1]/div[2]/div/a/text()'
            )
            opts["logger"].debug("oname_data: %s", oname_data)
            pid_data = Order.xpath('tr[@class="tr-bd"]/td[1]/div[1]/div[2]/div/a/@href')
            opts["logger"].debug("pid_data: %s", pid_data)

        except IndexError:
            opts["logger"].warning(f"第{i + 1}个订单未查找到商品，跳过。")
            continue
        loop_times1 = min(len(oname_data), len(pid_data))
        opts["logger"].debug("Commenting on orders")
        opts["logger"].debug("Total loop times: %d", loop_times1)
        idx = 0
        for oname, pid in zip(oname_data, pid_data):
            opts["logger"].debug("Loop: %d / %d", idx + 1, loop_times1)
            pid = pid.replace("//item.jd.com/", "").replace(".html", "")
            opts["logger"].debug("pid: %s", pid)
            if "javascript" in pid:
                opts["logger"].warning(
                    "\t\t订单 %s pid=%s 包含javascript，可能是京东外卖订单，跳过"
                    % (oname, pid),
                )
                continue
            # 跳过试用装/赠品订单
            if any(kw in oname for kw in _SKIP_KEYWORDS):
                opts["logger"].info(f"\t\t订单 '{oname}' 含试用/赠品关键词，跳过评价")
                continue
            opts["logger"].info(f"\t{i}.开始评价订单\t{oname}[{oid}]并晒图")
            url2 = "https://club.jd.com/myJdcomments/saveProductComment.action"
            opts["logger"].debug("URL: %s", url2)
            xing, Str = generation(oname, opts=opts)
            opts["logger"].info(f"\t\t评价内容,星级{xing}：" + Str)

            # 获取图片
            opts["logger"].info(f"\t\t开始获取图片")
            url1 = (
                f"https://club.jd.com/discussion/getProductPageImageCommentList"
                f".action?productId={pid}"
            )
            opts["logger"].debug("Fetching images using the default URL")
            opts["logger"].debug("URL: %s", url1)
            req1 = requests.get(url1, headers=headers, verify=False)
            opts["logger"].debug(
                "Successfully accepted the response with status code %d",
                req1.status_code,
            )
            if not req.ok:
                opts["logger"].warning(
                    "Status code of the response is %d, not 200", req1.status_code
                )
            opts["logger"].info("imgdata_url:" + url1)
            imgdata = req1.json()
            opts["logger"].debug("Image data: %s", imgdata)
            if imgdata["imgComments"]["imgCommentCount"] == 0:
                opts["logger"].debug("Count of fetched image comments is 0")
                opts["logger"].debug("Fetching images using another URL")
                url1 = (
                    "https://club.jd.com/discussion/getProductPageImage"
                    "CommentList.action?productId=1190881"
                )
                opts["logger"].debug("URL: %s", url1)
                req1 = requests.get(url1, headers=headers, verify=False)
                opts["logger"].debug(
                    "Successfully accepted the response with status code %d",
                    req1.status_code,
                )
                if not req.ok:
                    opts["logger"].warning(
                        "Status code of the response is %d, not 200", req1.status_code
                    )
                imgdata = req1.json()
                opts["logger"].debug("Image data: %s", imgdata)
            # 收集图片 URL（来自其他用户晒单，作为下载来源）
            img_sources = []
            img_list = imgdata.get("imgComments", {}).get("imgList", [])
            for img_item in img_list[:2]:  # 最多取前2张
                url = img_item.get("imageUrl", "").strip()
                if url:
                    img_sources.append(url)
            if not img_sources:
                opts["logger"].warning("\t\t该商品暂无配图，跳过图片部分")
            else:
                opts["logger"].info(f"\t\t图片来源url={','.join(img_sources)}")

            # 下载 → 微调 → 上传 → 收集上传后的URL
            uploaded_imgs = []
            for img_idx, img_url in enumerate(img_sources):
                img_url = img_url.strip()
                if not img_url:
                    continue
                # 处理协议相对 URL (//xxx → https://xxx)
                if img_url.startswith("//"):
                    img_url = "https:" + img_url
                try:
                    img_resp = requests.get(img_url, headers=headers, verify=False, timeout=15)
                    if img_resp.status_code == 200:
                        ext = ".jpg"
                        if "?" in img_url:
                            base_url = img_url.split("?")[0]
                        else:
                            base_url = img_url
                        if "." in base_url:
                            ext = "." + base_url.rsplit(".", 1)[-1].lower()
                            if len(ext) > 5:
                                ext = ".jpg"
                        filename = f"{pid}_{img_idx}{ext}"
                        # 微调图片数据，避免 MD5 与原始图片相同
                        img_data = bytearray(img_resp.content)
                        if len(img_data) > 200:
                            pos = len(img_data) // 2
                            img_data[pos] ^= 1
                        # 上传到京东图片服务器
                        uploaded_url = upload_image_to_jd(filename, bytes(img_data), opts=opts)
                        if uploaded_url:
                            uploaded_imgs.append(uploaded_url)
                        else:
                            opts["logger"].warning(f"\t\t图片上传失败，跳过该图: {filename}")
                    else:
                        opts["logger"].warning(f"\t\t图片下载失败: {img_url}, 状态码: {img_resp.status_code}")
                except Exception as e:
                    opts["logger"].warning(f"\t\t图片处理异常: {img_url}, 错误: {e}")

            # 组装 imgs 参数：优先用上传后的URL，失败则回退到原URL
            if uploaded_imgs:
                imgs_param = ",".join(uploaded_imgs)
            else:
                imgs_param = ""  # 无图则只提交文字
                opts["logger"].info("\t\t没有成功上传的图片，仅提交文字评价")

            Str: str = urllib.parse.quote(Str, safe="/", encoding=None, errors=None)
            data2 = {
                "orderId": oid,
                "productId": pid,  # 商品id
                "score": str(xing),  # 商品几星
                "content": Str,  # 评价内容
                "saveStatus": "1",
                "anonymousFlag": "1",  # 是否匿名
                "imgs": imgs_param,  # 图片url（已上传到京东）
            }
            opts["logger"].debug("Data: %s", data2)
            success, resp_text = submit_comment(
                url2,
                data2,
                headers2,
                opts=opts,
                referer=(
                    "https://club.jd.com/myJdcomments/myJdcomment.action?sort=0&page=1"
                ),
            )
            if success:
                # 当发送后的状态码 200，并且返回值里的 success 是 true 才是晒图成功，此外所有状态均为晒图失败
                opts["logger"].info(f"\t{i}.评价订单\t{oname}[{oid}]并晒图成功")
            else:
                opts["logger"].warning(
                    f"\t{i}.评价订单\t{oname}[{oid}]并晒图失败，返回:{(resp_text or '')[:120]}"
                )
            sleep_sec = random.uniform(*SUBMIT_SLEEP_RANGE)
            opts["logger"].debug("Sleep time (s): %.1f", sleep_sec)
            time.sleep(sleep_sec)
            idx += 1
    N["待评价订单"] -= 1
    return N


"""
# 晒单评价
def sunbw(N, opts=None):
    opts = opts or {}
    Order_data = []
    loop_times = N['待晒单'] // 20
    opts['logger'].debug('Fetching website data')
    opts['logger'].debug('Total loop times: %d', loop_times)
    for i in range(loop_times + 1):
        opts['logger'].debug('Loop: %d / %d', i + 1, loop_times)
        url = (f'https://club.jd.com/myJdcomments/myJdcomment.action?sort=1'
               f'&page={i + 1}')
        opts['logger'].debug('URL: %s', url)
        req = requests.get(url, headers=headers, verify=False)
        opts['logger'].debug(
            'Successfully accepted the response with status code %d',
            req.status_code)
        if not req.ok:
            opts['logger'].warning(
                'Status code of the response is %d, not 200', req.status_code)
        req_et = etree.HTML(req.text)
        opts['logger'].debug('Successfully parsed an XML tree')
        opts['logger'].debug('Fetching data from XML trees')
        elems = req_et.xpath(
            '//*[@id="evalu01"]/div[2]/div[1]/div[@class="comt-plist"]/div[1]')
        opts['logger'].debug('Count of fetched order data: %d', len(elems))
        Order_data.extend(elems)
    opts['logger'].info(f"当前共有{N['待晒单']}个需要晒单。")
    opts['logger'].debug('Commenting on items')
    for i, Order in enumerate(Order_data):
        oname = Order.xpath('ul/li[1]/div/div[2]/div[1]/a/text()')[0]
        pid = Order.xpath('@pid')[0]
        oid = Order.xpath('@oid')[0]
        opts['logger'].info(f'\t开始第{i+1}，{oname}')
        opts['logger'].debug('pid: %s', pid)
        opts['logger'].debug('oid: %s', oid)
        # 获取图片
        url1 = (f'https://club.jd.com/discussion/getProductPageImageCommentList'
                f'.action?productId={pid}')
        opts['logger'].debug('Fetching images using the default URL')
        opts['logger'].debug('URL: %s', url1)
        req1 = requests.get(url1, headers=headers, verify=False)
        opts['logger'].debug(
            'Successfully accepted the response with status code %d',
            req1.status_code)
        if not req.ok:
            opts['logger'].warning(
                'Status code of the response is %d, not 200', req1.status_code)
        imgdata = req1.json()
        opts['logger'].debug('Image data: %s', imgdata)
        if imgdata["imgComments"]["imgCommentCount"] == 0:
            opts['logger'].debug('Count of fetched image comments is 0')
            opts['logger'].debug('Fetching images using another URL')
            url1 = ('https://club.jd.com/discussion/getProductPageImage'
                    'CommentList.action?productId=1190881')
            opts['logger'].debug('URL: %s', url1)
            req1 = requests.get(url1, headers=headers, verify=False)
            opts['logger'].debug(
                'Successfully accepted the response with status code %d',
                req1.status_code)
            if not req.ok:
                opts['logger'].warning(
                    'Status code of the response is %d, not 200',
                    req1.status_code)
            imgdata = req1.json()
            opts['logger'].debug('Image data: %s', imgdata)
        imgurl = imgdata["imgComments"]["imgList"][0]["imageUrl"]
        opts['logger'].debug('Image URL: %s', imgurl)

        opts['logger'].info(f'\t\t图片url={imgurl}')
        # 提交晒单
        opts['logger'].debug('Preparing for commenting')
        url2 = "https://club.jd.com/myJdcomments/saveShowOrder.action"
        opts['logger'].debug('URL: %s', url2)
        headers['Referer'] = ('https://club.jd.com/myJdcomments/myJdcomment.'
                              'action?sort=1')
        headers['Origin'] = 'https://club.jd.com'
        headers['Content-Type'] = 'application/x-www-form-urlencoded'
        opts['logger'].debug('New header for this request: %s', headers)
        data = {
            'orderId': oid,
            'productId': pid,
            'imgs': imgurl,
            'saveStatus': 3
        }
        opts['logger'].debug('Data: %s', data)
        if not opts.get('dry_run'):
            opts['logger'].debug('Sending comment request')
            req_url2 = requests.post(url2, data=data, headers=headers, verify=False)
        else:
            opts['logger'].debug('Skipped sending comment request in dry run')
        opts['logger'].info('完成')
        opts['logger'].debug('Sleep time (s): %.1f', SUNBW_SLEEP_SEC)
        time.sleep(SUNBW_SLEEP_SEC)
        N['待晒单'] -= 1
    return N
"""

# 追评


def review(N, opts=None):
    opts = opts or {}
    req_et = []
    Order_data = []
    loop_times = N["待追评"] // 20
    opts["logger"].debug("Fetching website data")
    opts["logger"].debug("Total loop times: %d", loop_times)
    for i in range(loop_times + 1):
        opts["logger"].debug("Loop: %d / %d", i + 1, loop_times)
        url = (
            f"https://club.jd.com/myJdcomments/myJdcomment.action?sort=3"
            f"&page={i + 1}"
        )
        opts["logger"].debug("URL: %s", url)
        req = requests.get(url, headers=headers, verify=False)
        opts["logger"].debug(
            "Successfully accepted the response with status code %d", req.status_code
        )
        if not req.ok:
            opts["logger"].warning(
                "Status code of the response is %d, not 200", req.status_code
            )
        req_et.append(etree.HTML(req.text))
        opts["logger"].debug("Successfully parsed an XML tree")
    opts["logger"].debug("Fetching data from XML trees")
    opts["logger"].debug("Total loop times: %d", loop_times)
    for idx, i in enumerate(req_et):
        opts["logger"].debug("Loop: %d / %d", idx + 1, loop_times)
        opts["logger"].debug("Fetching order data in the default XPath")
        elems = i.xpath('//*[@id="main"]/div[2]/div[2]/table/tr[@class="tr-bd"]')
        opts["logger"].debug("Count of fetched order data: %d", len(elems))
        Order_data.extend(elems)
    if len(Order_data) != N["待追评"]:
        opts["logger"].debug('Count of fetched order data doesn\'t equal N["待追评"]')
        # NOTE: Need them?
        # opts['logger'].debug('Clear the list Order_data')
        # Order_data = []
        opts["logger"].debug("Total loop times: %d", loop_times)
        for idx, i in enumerate(req_et):
            opts["logger"].debug("Loop: %d / %d", idx + 1, loop_times)
            opts["logger"].debug("Fetching order data in another XPath")
            elems = i.xpath(
                '//*[@id="main"]/div[2]/div[2]/table/tbody/tr[@class="tr-bd"]'
            )
            opts["logger"].debug("Count of fetched order data: %d", len(elems))
            Order_data.extend(elems)
    opts["logger"].info(f"当前共有 {N['待追评']} 个需要追评。")
    opts["logger"].debug("Commenting on items")
    for i, Order in enumerate(Order_data):
        oname = Order.xpath("td[1]/div/div[2]/div/a/text()")[0]
        _id = Order.xpath("td[3]/div/a/@href")[0]
        opts["logger"].info(f"\t开始追评第{i+1}，{oname}")
        opts["logger"].debug("_id: %s", _id)
        url1 = (
            "https://club.jd.com/afterComments/" "saveAfterCommentAndShowOrder.action"
        )
        opts["logger"].debug("URL: %s", url1)
        pid, oid = _id.replace(
            "http://club.jd.com/afterComments/productPublish.action?sku=", ""
        ).split("&orderId=")
        opts["logger"].debug("pid: %s", pid)
        if "javascript" in pid:
            opts["logger"].warning(
                "\t\t订单 %s pid=%s 包含javascript，可能是京东外卖订单，跳过"
                % (oname, pid),
            )
            continue
        # 跳过试用装/赠品订单
        if any(kw in oname for kw in _SKIP_KEYWORDS):
            opts["logger"].info(f"\t\t订单 '{oname}' 含试用/赠品关键词，跳过追评")
            continue
        opts["logger"].debug("oid: %s", oid)
        _, context = generation(oname, _type=0, opts=opts)
        opts["logger"].info(f"\t\t追评内容：{context}")

        # 获取图片 → 下载 → 微调 → 上传
        opts["logger"].info(f"\t\t开始获取图片")
        url_img = (
            f"https://club.jd.com/discussion/getProductPageImageCommentList"
            f".action?productId={pid}"
        )
        opts["logger"].debug("URL: %s", url_img)
        req_img = requests.get(url_img, headers=headers, verify=False)
        uploaded_imgs = []
        if req_img.status_code == 200:
            imgdata = req_img.json()
            if imgdata.get("imgComments", {}).get("imgCommentCount", 0) > 0:
                img_list = imgdata["imgComments"]["imgList"]
                for img_idx, img_item in enumerate(img_list[:2]):  # 最多取前2张
                    img_url = img_item.get("imageUrl", "").strip()
                    if not img_url:
                        continue
                    # 处理协议相对 URL (//xxx → https://xxx)
                    if img_url.startswith("//"):
                        img_url = "https:" + img_url
                    try:
                        img_resp = requests.get(img_url, headers=headers, verify=False, timeout=15)
                        if img_resp.status_code == 200:
                            ext = ".jpg"
                            if "?" in img_url:
                                base_url = img_url.split("?")[0]
                            else:
                                base_url = img_url
                            if "." in base_url:
                                ext = "." + base_url.rsplit(".", 1)[-1].lower()
                                if len(ext) > 5:
                                    ext = ".jpg"
                            filename = f"{pid}_review_{img_idx}{ext}"
                            img_data = bytearray(img_resp.content)
                            if len(img_data) > 200:
                                pos = len(img_data) // 2
                                img_data[pos] ^= 1
                            # 上传到京东图片服务器
                            uploaded_url = upload_image_to_jd(filename, bytes(img_data), opts=opts)
                            if uploaded_url:
                                uploaded_imgs.append(uploaded_url)
                            else:
                                opts["logger"].warning(f"\t\t追评图片上传失败，跳过: {filename}")
                        else:
                            opts["logger"].warning(f"\t\t追评图片下载失败: {img_url}")
                    except Exception as e:
                        opts["logger"].warning(f"\t\t追评图片处理异常: {img_url}, 错误: {e}")
            else:
                opts["logger"].info("\t\t该商品暂无追评配图")
        else:
            opts["logger"].warning("\t\t获取追评配图失败")

        context = urllib.parse.quote(context, safe="/", encoding=None, errors=None)
        imgs_param = ",".join(uploaded_imgs) if uploaded_imgs else ""
        data1 = {
            "orderId": oid,
            "productId": pid,
            "content": context,
            "anonymousFlag": 1,
            "score": 5,
            "imgs": imgs_param,
        }
        opts["logger"].debug("Data: %s", data1)
        success, resp_text = submit_comment(
            url1,
            data1,
            headers2,
            opts=opts,
            referer=(
                "https://club.jd.com/myJdcomments/myJdcomment.action?sort=3&page=1"
            ),
        )
        if success:
            opts["logger"].info("完成")
        else:
            opts["logger"].warning(f"追评提交失败，返回:{(resp_text or '')[:120]}")
        sleep_sec = random.uniform(*SUBMIT_SLEEP_RANGE)
        opts["logger"].debug("Sleep time (s): %.1f", sleep_sec)
        time.sleep(sleep_sec)
        N["待追评"] -= 1
    return N


# 服务评价
def Service_rating(N, opts=None):
    opts = opts or {}
    Order_data = []
    req_et = []
    loop_times = N["服务评价"] // 20
    opts["logger"].debug("Fetching website data")
    opts["logger"].debug("Total loop times: %d", loop_times)
    for i in range(loop_times + 1):
        opts["logger"].debug("Loop: %d / %d", i + 1, loop_times)
        url = (
            f"https://club.jd.com/myJdcomments/myJdcomment.action?sort=4"
            f"&page={i + 1}"
        )
        opts["logger"].debug("URL: %s", url)
        req = requests.get(url, headers=headers, verify=False)
        opts["logger"].debug(
            "Successfully accepted the response with status code %d", req.status_code
        )
        if not req.ok:
            opts["logger"].warning(
                "Status code of the response is %d, not 200", req.status_code
            )
        req_et.append(etree.HTML(req.text))
        opts["logger"].debug("Successfully parsed an XML tree")
    opts["logger"].debug("Fetching data from XML trees")
    opts["logger"].debug("Total loop times: %d", loop_times)
    for idx, i in enumerate(req_et):
        opts["logger"].debug("Loop: %d / %d", idx + 1, loop_times)
        opts["logger"].debug("Fetching order data in the default XPath")
        elems = i.xpath('//*[@id="main"]/div[2]/div[2]/table/tbody/tr[@class="tr-bd"]')
        opts["logger"].debug("Count of fetched order data: %d", len(elems))
        Order_data.extend(elems)
    if len(Order_data) != N["服务评价"]:
        opts["logger"].debug('Count of fetched order data doesn\'t equal N["服务评价"]')
        opts["logger"].debug("Clear the list Order_data")
        Order_data = []
        opts["logger"].debug("Total loop times: %d", loop_times)
        for idx, i in enumerate(req_et):
            opts["logger"].debug("Loop: %d / %d", idx + 1, loop_times)
            opts["logger"].debug("Fetching order data in another XPath")
            elems = i.xpath('//*[@id="main"]/div[2]/div[2]/table/tr[@class="tr-bd"]')
            opts["logger"].debug("Count of fetched order data: %d", len(elems))
            Order_data.extend(elems)
    opts["logger"].info(f"当前共有{N['服务评价']}个需要第一次服务评价。")
    opts["logger"].debug("Commenting on items")
    for i, Order in enumerate(Order_data):
        oname = Order.xpath("td[1]/div[1]/div[2]/div/a/text()")[0]
        # 跳过试用装/赠品订单
        if any(kw in oname for kw in _SKIP_KEYWORDS):
            opts["logger"].info(f"\t\t订单 '{oname}' 含试用/赠品关键词，跳过服务评价")
            continue
        try:
            oid = Order.xpath("td[4]/div/a[1]/@oid")[0]
        except IndexError:
            opts["logger"].warning("Failed to fetch oid")
            continue
        opts["logger"].info(f"\t开始第一次评论，{i+1}，{oname}")
        opts["logger"].debug("oid: %s", oid)
        url1 = (
            f"https://club.jd.com/myJdcomments/insertRestSurvey.action"
            f"?voteid=145&ruleid={oid}"
        )
        opts["logger"].debug("URL: %s", url1)
        data1 = {
            "oid": oid,
            "gid": "32",
            "sid": "186194",
            "stid": "0",
            "tags": "",
            "ro591": f"591A{random.randint(4, 5)}",  # 商品符合度
            "ro592": f"592A{random.randint(4, 5)}",  # 店家服务态度
            "ro593": f"593A{random.randint(4, 5)}",  # 快递配送速度
            "ro899": f"899A{random.randint(4, 5)}",  # 快递员服务
            "ro900": f"900A{random.randint(4, 5)}",  # 快递员服务
        }
        opts["logger"].debug("Data: %s", data1)
        success, resp_text = submit_comment(
            url1,
            data1,
            headers,
            opts=opts,
            referer=(
                "https://club.jd.com/myJdcomments/myJdcomment.action?sort=4&page=1"
            ),
        )
        opts["logger"].info(
            "\t\t " + (resp_text or ("提交成功" if success else "提交失败"))
        )
        sleep_sec = random.uniform(*SUBMIT_SLEEP_RANGE)
        opts["logger"].debug("Sleep time (s): %.1f", sleep_sec)
        time.sleep(sleep_sec)
        N["服务评价"] -= 1
    return N


def No(opts=None):
    opts = opts or {}
    # opts["logger"].info("")
    N = all_evaluate(opts)
    s = "----".join(["{} {}".format(i, N[i]) for i in N])
    opts["logger"].info(s)
    # opts["logger"].info("")
    return N


def main(opts=None):
    opts = opts or {}
    opts["logger"].info("开始京东批量评价！")
    N = No(opts)
    opts["logger"].debug("N value after executing No(): %s", N)
    if not N:
        opts["logger"].error("Ck出现错误，请重新抓取！")
        exit()
    opts["logger"].info(f"已评价：{N['已评价']}个")
    if N["待评价订单"] != 0:
        opts["logger"].info("1.开始普通评价")
        N = ordinary(N, opts)
        opts["logger"].debug("N value after executing ordinary(): %s", N)
        N = No(opts)
        opts["logger"].debug("N value after executing No(): %s", N)
    """ "待晒单" is no longer found in N{} instead of "已评价"
    if N['待晒单'] != 0:
        opts['logger'].info("2.开始晒单评价")
        N = sunbw(N, opts)
        opts['logger'].debug('N value after executing sunbw(): %s', N)
        N = No(opts)
        opts['logger'].debug('N value after executing No(): %s', N)
    """
    if N["待追评"] != 0:
        opts["logger"].info("3.开始批量追评（支持上传图片）")
        N = review(N, opts)
        opts["logger"].debug("N value after executing review(): %s", N)
        N = No(opts)
        opts["logger"].debug("N value after executing No(): %s", N)
    if N["服务评价"] != 0:
        opts["logger"].info("4.开始服务评价")
        N = Service_rating(N, opts)
        opts["logger"].debug("N value after executing Service_rating(): %s", N)
        N = No(opts)
        opts["logger"].debug("N value after executing No(): %s", N)
    opts["logger"].info("全部完成啦！")
    for i in N:
        if N[i] != 0:
            opts["logger"].warning("出现了二次错误，跳过了部分，重新尝试")
            main(opts)


if __name__ == "__main__":
    # parse arguments
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--dry-run",
        help="have a full run without comment submission",
        action="store_true",
    )
    parser.add_argument(
        "-lv",
        "--log-level",
        help="specify logging level (default: info)",
        default="INFO",
    )
    parser.add_argument(
        "-o", "--log-file", help="specify logging file", default="log.txt"
    )
    args = parser.parse_args()
    if args.log_level.upper() not in [
        "DEBUG",
        "WARN",
        "INFO",
        "ERROR",
        "FATAL",
        # NOTE: `WARN` is an alias of `WARNING`. `FATAL` is an alias of
        # `CRITICAL`. Using these aliases is for developers' and users'
        # convenience.
        # NOTE: Now there is no logging on `CRITICAL` level.
    ]:
        args.log_level = "INFO"
    else:
        args.log_level = args.log_level.upper()
    opts = {"dry_run": args.dry_run, "log_level": args.log_level}
    if hasattr(args, "log_file"):
        opts["log_file"] = args.log_file
    else:
        opts["log_file"] = None

    # logging on console
    _logging_level = getattr(logging, opts["log_level"])
    logger = logging.getLogger("comment")
    logger.setLevel(level=_logging_level)
    # NOTE: `%(levelname)s` will be parsed as the original name (`FATAL` ->
    # `CRITICAL`, `WARN` -> `WARNING`).
    # NOTE: The alignment number should set to 19 considering the style
    # controling characters. When it comes to file logger, the number should
    # set to 8.
    formatter = StyleFormatter("%(asctime)s %(levelname)-19s %(message)s")
    rawformatter = StyleFormatter(
        "%(asctime)s %(levelname)-8s %(message)s", use_style=False
    )
    console = logging.StreamHandler()
    console.setLevel(_logging_level)
    console.setFormatter(formatter)
    logger.addHandler(console)
    opts["logger"] = logger
    # It's a hack!!!
    jieba.default_logger = logging.getLogger("jieba")
    jieba.default_logger.setLevel(level=_logging_level)
    jieba.default_logger.addHandler(console)
    # It's another hack!!!
    jdspider.default_logger = logging.getLogger("spider")
    jdspider.default_logger.setLevel(level=_logging_level)
    jdspider.default_logger.addHandler(console)

    logger.debug("Successfully set up console logger")
    logger.debug("CLI arguments: %s", args)
    logger.debug("Opening the log file")
    if opts["log_file"]:
        try:
            handler = logging.FileHandler(opts["log_file"], "w")
        except Exception as e:
            logger.error("Failed to open the file handler")
            logger.error("Error message: %s", e)
            sys.exit(1)
        handler.setLevel(_logging_level)
        handler.setFormatter(rawformatter)
        logger.addHandler(handler)
        jieba.default_logger.addHandler(handler)
        jdspider.default_logger.addHandler(handler)
        logger.debug("Successfully set up file logger")
    logger.debug("Options passed to functions: %s", opts)
    logger.debug("Builtin constants:")
    logger.debug("  CONFIG_PATH: %s", CONFIG_PATH)
    logger.debug("  USER_CONFIG_PATH: %s", USER_CONFIG_PATH)
    logger.debug("  ORDINARY_SLEEP_SEC: %s", ORDINARY_SLEEP_SEC)
    logger.debug("  SUNBW_SLEEP_SEC: %s", SUNBW_SLEEP_SEC)
    logger.debug("  REVIEW_SLEEP_SEC: %s", REVIEW_SLEEP_SEC)
    logger.debug("  SERVICE_RATING_SLEEP_SEC: %s", SERVICE_RATING_SLEEP_SEC)

    # parse configurations
    logger.debug("Reading the configuration file")
    if os.path.exists(USER_CONFIG_PATH):
        logger.debug("User configuration file exists")
        _cfg_path = USER_CONFIG_PATH
    else:
        logger.debug(
            "User configuration file doesn't exist, fallback to the default one"
        )
        _cfg_path = CONFIG_PATH
    with open(_cfg_path, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    logger.debug("Closed the configuration file")
    logger.debug("Configurations in Python-dict format: %s", cfg)
    ck = cfg["user"]["cookie"]
    jdspider.cookie = ck.encode("utf-8")

    # 可选：风控节奏配置，可在 config.yml / config.user.yml 的 user 节点下覆盖
    _user_cfg = cfg.get("user") or {} if isinstance(cfg, dict) else {}
    _min_interval = _user_cfg.get("min_submit_interval")
    if _min_interval is not None:
        try:
            globals()["MIN_SUBMIT_INTERVAL_SEC"] = float(_min_interval)
            logger.info("最小提交间隔设置为 %s 秒", MIN_SUBMIT_INTERVAL_SEC)
        except (TypeError, ValueError):
            logger.warning("min_submit_interval 配置无效，使用默认值")
    _sleep_range = _user_cfg.get("sleep_range")
    if isinstance(_sleep_range, (list, tuple)) and len(_sleep_range) == 2:
        try:
            globals()["SUBMIT_SLEEP_RANGE"] = (
                float(_sleep_range[0]),
                float(_sleep_range[1]),
            )
            logger.info("每笔评价后的休眠区间设置为 %s 秒", SUBMIT_SLEEP_RANGE)
        except (TypeError, ValueError):
            logger.warning("sleep_range 配置无效，使用默认值")

    headers2 = {
        "cookie": ck.encode("utf-8"),
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/114.0.5735.110 Safari/537.36",
        "Connection": "keep-alive",
        "Cache-Control": "max-age=0",
        "X-Requested-With": "XMLHttpRequest",
        "sec-ch-ua": "",
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": "",
        "DNT": "1",
        "Upgrade-Insecure-Requests": "1",
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Sec-Fetch-Site": "same-origin",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-User": "?1",
        "Sec-Fetch-Dest": "empty",
        "Referer": "https://club.jd.com/",
        "Accept-Encoding": "gzip, deflate",
        "Accept-Language": "zh-CN,zh;q=0.9",
        # 'Content-Type':'application/x-www-form-urlencoded'
    }
    headers = {
        "cookie": ck.encode("utf-8"),
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/98.0.4758.82 Safari/537.36",
        "Connection": "keep-alive",
        "Cache-Control": "max-age=0",
        "sec-ch-ua": '" Not A;Brand";v="99", "Chromium";v="98", "Google Chrome";v="98"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
        "DNT": "1",
        "Upgrade-Insecure-Requests": "1",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,"
        "application/signed-exchange;v=b3;q=0.9",
        "Sec-Fetch-Site": "same-site",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-User": "?1",
        "Sec-Fetch-Dest": "document",
        "Referer": "https://order.jd.com/",
        "Accept-Encoding": "gzip, deflate, br",
        "Accept-Language": "zh-CN,zh;q=0.9",
    }
    logger.debug("Builtin HTTP request header: %s", headers)

    logger.debug("Starting main processes")
    try:
        main(opts)
    # NOTE: It needs 3,000 times to raise this exception. Do you really want to
    # do like this?
    except RecursionError:
        logger.error("多次出现未完成情况，程序自动退出")
