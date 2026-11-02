# -*- coding: utf-8 -*-
"""浏览器版京东批量评价（仿 auto_comment_plus.py 逻辑，提交动作放在真实浏览器里）

流程与 auto_comment_plus.py 一致：
  取待评价/待追评订单 -> 生成文案 -> 取商品晒图 -> 下载 -> 微调字节 -> 上传京东图床 -> 文字+图片一起提交

- 登录态：读取 config.yml / config.user.yml 的 cookie，注入浏览器
- 文案：jieba 关键词 + 内置文案池；加 --spider 则走 auto_comment_plus.generation() 实时爬评论
- 提交：页面内同源 fetch，浏览器自动带完整 Cookie 与请求头
- 风控节奏：复用脚本的 MIN_SUBMIT_INTERVAL_SEC / SUBMIT_SLEEP_RANGE 与重试判断

用法：
  python browser_comment.py --dry-run                 只列订单不提交
  python browser_comment.py --limit 1                 普通评价 1 单（文字+图片）
  python browser_comment.py --mode review --limit 1   追评 1 单（建议先这样测编码）
  python browser_comment.py --encoding gbk            切换提交编码（默认 utf-8）

依赖：agent-browser（npm i -g agent-browser && agent-browser install）
"""

import argparse
import base64
import io
import json
import logging
import os
import random
import re
import subprocess
import sys
import time
import urllib.parse

import jieba.analyse
import requests
import yaml

import auto_comment_plus as acp

_STATE_PATH = "./jd_state.json"
_LIST_URL = "https://club.jd.com/myJdcomments/myJdcomment.action?sort={sort}&page={page}"
_SUBMIT_URL = "https://club.jd.com/myJdcomments/saveProductComment.action"
_REVIEW_URL = "https://club.jd.com/afterComments/saveAfterCommentAndShowOrder.action"
_IMG_LIST_URL = (
    "https://club.jd.com/discussion/getProductPageImageCommentList.action?productId={pid}"
)
_UPLOAD_URL = "https://club.jd.com/myJdcomments/ajaxUploadImage.action"
_FAILED_PATH = "./failed_orders.txt"  # 提交失败订单的 oid 记录，下次运行自动跳过

# 京东提交接口返回码 -> 人话说明（实测与页面 JS 整理）
_RESULT_CODE_MSG = {
    "1": "成功",
    "2": "当前没有评价资格（可能已评价过、已过评价期，或该商品类型不支持评价）",
    "12": "输入内容不符合要求（内容重复/含敏感内容/参数异常，会被风控拦截）",
    "16": "内容不符合要求",
    "25": "操作过于频繁，请稍后再试",
    "28": "操作过于频繁，请稍后再试",
    "42": "需要实名验证",
}
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/114.0.5735.110 Safari/537.36"
)

logger = logging.getLogger("browser_comment")
COOKIE = ""

# 快速模式的文案池（$ 会被替换成从商品名提取的关键词）
_FAST_COMMENTS = [
    "考虑买这个$之前我是有担心过的，因为我不知道$的质量和品质怎么样，但是看了评论后我就放心了。",
    "买这个$之前我是有看过好几家店，最后看到这家店的评价不错就决定在这家店买 ",
    "看了好几家店，也对比了好几家店，最后发现还是这一家的$评价最好。",
    "看来看去最后还是选择了这家。",
    "之前在这家店也买过其他东西，感觉不错，这次又来啦。",
    "这家的$的真是太好用了，用了第一次就还想再用一次。",
    "收到货后我非常的开心，因为$的质量和品质真的非常的好！",
    "拆开包装后惊艳到我了，这就是我想要的$!",
    "快递超快！包装的很好！！很喜欢！！！",
    "包装的很精美！$的质量和品质非常不错！",
    "收到快递后迫不及待的拆了包装。$我真的是非常喜欢",
    "真是一次难忘的购物，这辈子没见过这么好用的东西！！",
    "经过了这次愉快的购物，我决定如果下次我还要买$的话，我一定会再来这家店买的。",
    "不错不错！",
    "我会推荐想买$的朋友也来这家店里买",
    "真是一次愉快的购物！",
    "大大的好评!以后买$再来你们店！(￣▽￣)",
    "第一次在京东买东西，体验挺好的，还会再来的。",
    "大品牌值得信赖，质量过硬，已经推荐给朋友了。",
    "非常满意这次购物体验，京东物流也很快。",
]


# ---------------- agent-browser 封装 ----------------
def ab(*args, session="jd", timeout=120):
    cmd = f'agent-browser --session {session} ' + " ".join(args)
    r = subprocess.run(
        cmd, capture_output=True, text=True, encoding="utf-8",
        errors="replace", shell=True, timeout=timeout,
    )
    return (r.stdout or "").strip()


def eval_js(js, session="jd", raw=False, timeout=120):
    """在页面执行 JS。复杂脚本一律 base64 传入，避免 shell / 编码问题。"""
    b64 = base64.b64encode(js.encode("utf-8")).decode()
    out = ab(f'eval -b "{b64}"', session=session, timeout=timeout)
    if raw:
        return out
    try:
        val = json.loads(out)
    except Exception:
        return out
    if isinstance(val, str):
        try:
            return json.loads(val)
        except Exception:
            return val
    return val


# ---------------- 登录态 ----------------
def load_cookie():
    global COOKIE
    path = (
        "./config.user.yml" if os.path.exists("./config.user.yml") else "./config.yml"
    )
    with open(path, "r", encoding="utf-8") as f:
        COOKIE = yaml.safe_load(f)["user"]["cookie"]
    logger.info("已读取 cookie：%s", path)
    return COOKIE, path


def build_state():
    """把配置文件里的 cookie 转成 agent-browser 的 state 文件（明文 Cookie，勿提交）。"""
    ck, path = load_cookie() if not COOKIE else (COOKIE, "")
    cookies = []
    for part in ck.split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        name, _, value = part.partition("=")
        cookies.append(
            {
                "name": name.strip(),
                "value": value.strip(),
                "domain": ".jd.com",
                "path": "/",
                "expires": -1,
                "httpOnly": False,
                "secure": False,
                "sameSite": "Lax",
            }
        )
    with open(_STATE_PATH, "w", encoding="utf-8") as f:
        json.dump({"cookies": cookies, "origins": []}, f, ensure_ascii=False)
    logger.info("已生成登录态（%d 个 cookie）", len(cookies))
    return _STATE_PATH


# ---------------- 页面数据 ----------------
_JS_STAT = """
(function () {
  var out = {};
  var lis = document.querySelectorAll('#main div div div ul li');
  for (var i = 0; i < lis.length; i++) {
    var a = lis[i].querySelector('a');
    var b = lis[i].querySelector('b');
    if (a) {
      var k = (a.innerText || '').trim();
      if (k) out[k] = b ? (parseInt((b.innerText || '0').trim(), 10) || 0) : 0;
    }
  }
  return JSON.stringify(out);
})()
"""

# 待评价页：tr.tr-th 里有订单号，tr.tr-bd 里有商品链接
_JS_ORDERS = """
(function () {
  var rows = [];
  var trs = document.querySelectorAll('#main tr.tr-bd');
  for (var i = 0; i < trs.length; i++) {
    var r = trs[i];
    var p = r.previousElementSibling;
    var oid = '';
    while (p) {
      if (p.className && p.className.indexOf('tr-th') >= 0) {
        var m = (p.innerText || '').match(/订单号[：:\\s]*([0-9]+)/);
        oid = m ? m[1] : '';
        break;
      }
      p = p.previousElementSibling;
    }
    var a = r.querySelector('td div div div a');
    rows.push({
      oid: oid,
      name: a ? (a.innerText || '').trim() : '',
      href: a ? (a.getAttribute('href') || '') : ''
    });
  }
  return JSON.stringify(rows);
})()
"""

# 追评页：商品名 + 追评链接（含 sku= 与 orderId=）
_JS_REVIEW = """
(function () {
  var rows = [];
  var trs = document.querySelectorAll('#main tr.tr-bd');
  for (var i = 0; i < trs.length; i++) {
    var r = trs[i];
    var a = r.querySelector('td div div div a');
    var name = a ? (a.innerText || '').trim() : '';
    var oid = '', pid = '';
    var as = r.querySelectorAll('a');
    for (var j = 0; j < as.length; j++) {
      var h = as[j].getAttribute('href') || '';
      if (h.indexOf('sku=') >= 0 && h.indexOf('orderId=') >= 0) {
        var m1 = h.match(/sku=([0-9]+)/);
        var m2 = h.match(/orderId=([0-9]+)/);
        if (m1) pid = m1[1];
        if (m2) oid = m2[1];
        break;
      }
    }
    if (!pid && a) {
      var hh = a.getAttribute('href') || '';
      var m3 = hh.match(/item\\.jd\\.com\\/([0-9]+)\\.html/);
      if (m3) pid = m3[1];
    }
    rows.push({ oid: oid, pid: pid, name: name });
  }
  return JSON.stringify(rows);
})()
"""


def fetch_stat(session):
    data = eval_js(_JS_STAT, session=session)
    return data if isinstance(data, dict) else {}


def _open_page(sort, page, session):
    ab(f'open "{_LIST_URL.format(sort=sort, page=page)}"', session=session)
    ab("wait --load networkidle", session=session)


def fetch_orders(mode, session, total=0):
    """抓取订单列表。ordinary: sort=0；review: sort=3"""
    sort = 3 if mode == "review" else 0
    js = _JS_REVIEW if mode == "review" else _JS_ORDERS
    pages = max(1, total // 20 + (1 if total % 20 else 0))
    orders, seen = [], set()
    js_skipped = 0
    for p in range(1, pages + 1):
        _open_page(sort, p, session)
        rows = eval_js(js, session=session)
        rows = rows if isinstance(rows, list) else []
        logger.info("第 %d 页抓到 %d 条", p, len(rows))
        for r in rows:
            if mode == "review":
                pid = (r.get("pid") or "").strip()
                oid = (r.get("oid") or "").strip()
            else:
                pid = parse_pid(r.get("href", ""))
                oid = (r.get("oid") or "").strip()
            if not pid or "javascript" in pid or not oid:
                # 链接无法解析的多为京东外卖/虚拟权益单
                js_skipped += 1
                logger.info(
                    "跳过外卖/虚拟订单（无有效商品链接）：%s [oid=%s]",
                    r.get("name", ""),
                    oid,
                )
                continue
            key = (oid, pid)
            if key in seen:
                continue
            seen.add(key)
            orders.append({"oid": oid, "pid": pid, "name": r.get("name", "")})
    if js_skipped:
        logger.info("共跳过 %d 个外卖/虚拟订单（无有效商品链接）", js_skipped)
    return orders


def parse_pid(href):
    if not href:
        return ""
    return href.replace("//item.jd.com/", "").replace(".html", "").strip()


# 追评专用文案池（使用一段时间后的补充反馈口吻，$ 会被替换成商品核心词）
_REVIEW_COMMENTS = [
    "用了几天来追评，$的整体表现比预期还好，值得推荐。",
    "追加评价：$使用了一段时间没有任何问题，质量依旧在线。",
    "特意使用一周后回来追评，$依然很好用，家人都很满意。",
    "补充一下使用感受：$的做工细节到位，耐用度也不错。",
    "使用半个月后来追评，$没有出现任何质量问题，好评！",
    "用了几天才来评价，$确实物有所值，比实体店划算多了。",
    "追评：物流快，包装好，$上手体验很棒，值得入手。",
    "用了这么久来补充评价，$的性价比真的很高，回购首选。",
    "追加：$和描述完全一致，没有色差，尺寸也合适。",
    "使用一段时间后发现$越用越顺手，之前担心的问题都没有出现。",
    "来追评啦，$用了很久依然完好如初，这个价钱很值。",
    "补充追评：家人用了都说好，$已经成为家里的常备好物了。",
    "追评一下：期间联系过客服，响应很快，售后给力，$也没让我失望。",
    "用了几天来追评，$整体很满意，希望商家多做活动，还会再来的。",
    "用了一阵子专门回来追加，$稳定耐用，当初没选错。",
    "补充评价：$天天都在用，体验一直不错，推荐给同事了。",
]

# 分品类文案池：商品名命中关键词时优先使用，不足部分由通用池补足
# （$ 会被替换成商品核心词；药品类注意合规口吻，不宣传疗效）
_CATEGORY_COMMENTS = {
    "药品保健": (
        ("药", "膏", "口服液", "滴剂", "胶囊", "维生素", "钙", "铁", "锌",
         "益生菌", "鱼油", "保健品", "软糖", "冲剂", "凝胶", "喷雾", "创可贴",
         "体温计", "口罩", "医用", "叶黄素", "泡腾"),
        [
            "$收到了，包装完好，生产日期新鲜，客服咨询也很耐心。",
            "发货很快，包装严实。$按说明使用了几天，感觉温和不错，用完会回购。",
            "第一次在网上买$，比药店划算，包装专业，日期也新。",
            "$的包装很仔细，还附了说明卡，客服讲解清楚，放心。",
            "收到$就按说明使用了，很温和，没有不适感。",
            "家里常备的$，一直在他家买，正品放心，物流也快。",
            "给长辈买的$，客服耐心解答了用法，包装完好，好评。",
            "$外包装严实，日期新鲜，官方授权店买得放心。",
            "用完一疗程了来回购$，我用着感觉不错，因人而异仅供参考。",
            "$收到啦，包装无破损，日期近，物流快，满意。",
        ],
    ),
    "美妆个护": (
        ("霜", "精华", "乳液", "面膜", "口红", "粉底", "香水", "洗发", "护发",
         "沐浴", "牙膏", "护肤", "爽肤水", "眼霜", "防晒", "洗面奶", "身体乳",
         "彩妆", "睫毛", "面膜"),
        [
            "$的质地很好推开，上脸不闷，包装也很有质感。",
            "收到$就迫不及待试了，味道很好闻，包装精致，喜欢！",
            "用过一周来评价，$很温和，敏感肌用着也没有不适。",
            "$的外包装很精美，送人拿得出手，自己用也合适。",
            "质地和描述一致，$上手效果不错，会回购。",
            "旗舰店买的$，正品放心，日期新鲜，物流也快。",
            "$很滋润，吸收也快，用了几天皮肤状态有变好。",
            "对比了好几家，最后选了这家$，事实证明没选错。",
            "$的香味很淡雅，不刺鼻，日常用刚刚好。",
            "包装严实还有防伪标识，$是正品无疑，满意。",
        ],
    ),
    "食品饮料": (
        ("食品", "零食", "饮料", "果汁", "牛奶", "酸奶", "饼干", "面包", "蛋糕",
         "坚果", "糖果", "茶", "咖啡", "大米", "面粉", "香油", "酱油", "醋",
         "肠", "鲜", "饼", "巧克力", "薯片", "干果", "水果"),
        [
            "$的包装很严实，收到时完好无损，日期新鲜。",
            "味道很好，$打开就能闻到香味，家里人都爱吃。",
            "$的分量很足，性价比高，是正品味道，会回购。",
            "物流超快，第二天就到了，$包装完好，日期新鲜。",
            "给家里囤的$，孩子很喜欢吃，已经推荐给同事了。",
            "$的口感很好，不甜腻，比超市买的划算。",
            "回购好几次了，$的品质一直很稳定，值得信赖。",
            "$收到啦，包装仔细还有防撞泡沫，很贴心。",
            "味道正，$是小时候的那个味道，吃完再囤。",
            "早餐配着$很方便，家里人都说不错，回购。",
        ],
    ),
    "数码家电": (
        ("手机", "电脑", "笔记本", "平板", "耳机", "音响", "音箱", "电视",
         "冰箱", "洗衣机", "空调", "电饭煲", "路由", "充电", "数据线", "鼠标",
         "键盘", "硬盘", "摄像头", "手表", "灯", "电器", "剃须", "吹风", "扫地",
         "净水", "豆浆", "微波"),
        [
            "$的做工很精致，细节都对得上，是正品。",
            "收到$第一时间开机测试，各项功能都正常，很满意。",
            "$的包装很严实，多层保护，机器完好无损。",
            "用了几天来评价，$运行流畅，性能符合预期。",
            "客服很专业，收到$后还耐心指导了使用方法，好评。",
            "$性价比很高，同价位里配置很能打。",
            "给父母买的$，操作简单，他们很快就上手了。",
            "京东物流就是快，上午下单$下午就到了，安装师傅也专业。",
            "续航/功率和描述一致，$没有虚标，实测达到预期。",
            "外观比图片还好看，$的质感超出这个价位的水平。",
        ],
    ),
    "母婴用品": (
        ("婴儿", "宝宝", "婴幼儿", "纸尿裤", "拉拉裤", "奶粉", "奶瓶", "辅食",
         "幼儿", "童装", "儿童", "宝妈", "尿不湿"),
        [
            "$的材质很放心，没有异味，宝宝用着很合适。",
            "给宝宝买的$，包装完好，日期新鲜，用着很安心。",
            "$收到啦，做工精细没有毛刺，宝宝很喜欢。",
            "第二次回购$了，宝宝用着一直很好，信赖这个牌子。",
            "$的细节做得很到位，宝妈群推荐的果然没错。",
            "客服很耐心，按宝宝月龄推荐了合适的$，很专业。",
            "$很柔软亲肤，宝宝皮肤敏感也没有红印，满意。",
            "当了妈妈后买东西特别谨慎，$的资质报告齐全，放心。",
        ],
    ),
    "服饰鞋包": (
        ("裤", "衬衫", "T恤", "上衣", "外套", "裙", "鞋", "袜", "帽", "内衣",
         "羽绒服", "卫衣", "夹克", "背包", "皮包", "裤衩", "衫"),
        [
            "$的版型很正，面料舒服，尺码标准，按平时穿的码买就行。",
            "收到$试穿了，大小合适，做工也不错，没有色差。",
            "$上身效果很好，同事都问链接，值得入手。",
            "包装很仔细，$没有褶皱没有线头，满意。",
            "给家人买的$，很合身，说穿着很舒服。",
            "$质量对得起价格，缝线整齐，面料手感不错。",
            "洗过一次$也没有变形褪色，做工扎实，好评。",
            "按客服推荐的尺码买的$，刚刚好，很专业。",
        ],
    ),
    "家居日用": (
        ("纸", "巾", "洗衣", "清洁", "收纳", "垃圾袋", "杯子", "锅", "碗",
         "筷子", "床品", "枕头", "被子", "毛巾", "拖把", "湿巾", "收纳盒",
         "置物架", "挂钩", "香薰", "花洒", "晾衣"),
        [
            "$收到啦，包装完好，做工扎实，安装也很简单。",
            "$和描述一致，材质厚实，没有廉价感，满意。",
            "家里正需要$，收到就装上了，很实用，推荐。",
            "$的质量超出预期，细节处理得很好，好评。",
            "用过几天来评价，$很实用，家里人都说买得值。",
            "这次囤货很成功，$价格比超市便宜，品质不打折。",
            "$的设计很合理，用过就回不去了，回购。",
            "生活必需品，$一直在他家买，正品放心。",
        ],
    ),
}

# ---------------- 文案 ----------------
# 关键词提取时的干扰词：人群/颜色/尺码/品牌/营销词（这些词替换 $ 会造成"这家的女士很好用"之类病句）
_KEYWORD_BLACKLIST = (
    "男", "女", "儿童", "宝宝", "婴儿", "情侣", "亲子",
    "款", "码", "号", "装", "套", "只", "个", "瓶", "包", "盒", "件", "条", "片",
    "白", "黑", "红", "蓝", "绿", "黄", "粉", "灰", "紫", "米白", "透明", "彩色",
    "京东", "京造", "自营", "旗舰店", "官方", "正品", "授权", " Flag",
    "系列", "认证", "专属", "通用", "专用", "升级", "新款", "整箱", "组合", "味",
    # 属性/功效词（常见于商品名尾部，不适合做品类词）
    "透气", "超薄", "宽松", "直筒", "修身", "修护", "保湿", "滋润", "补水",
    "控油", "舒缓", "紧致", "去屑", "柔顺", "抗菌", "防臭", "便携", "大容量",
    "加厚", "加固", "防水", "防滑", "耐磨", "防摔", "轻奢", "高颜值",
    "祛痘", "淡化", "美白", "抗皱", "去黄", "疤痕",
    # 病症/适应症词（药品名尾部常见，如"皮炎湿疹"）
    "湿疹", "皮炎", "瘙痒", "感冒", "咳嗽", "上火", "失眠", "脱发", "过敏",
    "腹泻", "疼痛", "炎症",
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
    """从商品名提取核心品类词（分词 + TF-IDF 统计评分模型）。

    score = IDF(词) x sqrt(词长) x 品牌惩罚 x 品类词典加成
    - IDF：词的特异度。"速食/食品"这类泛化词 IDF 低，"干脆面/雪糕"这类具体词 IDF 高
    - 品牌惩罚：商品名开头的短名词段（<=6字，如"中街冰点"）视为品牌区，权重 x0.2
    - 品类词典加成：命中 _CATEGORY_WORDS 的词 x2（兼作分词辅助）
    取最高分词；无候选时走统计兜底链（textrank 等）。
    """
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
    first_seg = re.split(r"[\s/，,，【\[|]", pname)[0]

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
        m = re.search(rf"[\u4e00-\u9fa5]{{{n}}}$", first_seg)
        if m and not bad(m.group()):
            return m.group()

    return pool[0] if pool else "宝贝"


def pick_pool(name):
    """按商品名匹配品类文案池，未命中返回空列表（由通用池兜底）。"""
    for _cat, (keys, comments) in _CATEGORY_COMMENTS.items():
        if any(k in name for k in keys):
            return comments
    return []


def gen_text(name, use_spider=False, sentences=6, pool=None, max_prefixed=None):
    if use_spider:
        return acp.generation(name, opts={"logger": logger})
    keyword = extract_keyword(name)
    prefixes = ("追评", "追加", "补充")
    is_prefixed = lambda c: any(p in c[:12] for p in prefixes)
    fallback_pool = pool or (pick_pool(name) + _FAST_COMMENTS) or _FAST_COMMENTS

    def sample_once():
        """取一批句子：显式池/追评池直接用；否则品类池优先、通用池补足"""
        if pool:
            avail = [c for c in pool if c not in acp._USED_SENTENCES] or list(pool)
            return random.sample(avail, min(sentences, len(avail)))
        cat_avail = [c for c in pick_pool(name) if c not in acp._USED_SENTENCES]
        gen_avail = [c for c in _FAST_COMMENTS if c not in acp._USED_SENTENCES]
        picked = random.sample(cat_avail, min(sentences, len(cat_avail)))
        if len(picked) < sentences and gen_avail:
            picked += random.sample(
                gen_avail, min(sentences - len(picked), len(gen_avail))
            )
        if not picked:
            picked = random.sample(
                _FAST_COMMENTS, min(sentences, len(_FAST_COMMENTS))
            )
        return picked

    def limit_prefixed(items):
        """限制前缀句数量：超出部分用普通句替换"""
        if max_prefixed is None:
            return items
        prefixed = [c for c in items if is_prefixed(c)]
        excess = len(prefixed) - max_prefixed
        if excess > 0:
            plain = [c for c in fallback_pool if not is_prefixed(c) and c not in items]
            random.shuffle(plain)
            for old, new in zip(prefixed[:excess], plain):
                items[items.index(old)] = new
        return items

    text = ""
    picked = []
    for _ in range(6):
        picked = limit_prefixed(sample_once())
        text = "".join(picked).replace("$", keyword)
        if text not in acp._USED_TEXTS:
            break
    acp._USED_SENTENCES.update(picked)
    acp._USED_TEXTS.add(text)
    return 5, text


# ---------------- 图片（仿 auto_comment_plus.upload_image_to_jd）----------------
def _req_headers(extra=None):
    h = {
        "cookie": COOKIE,
        "User-Agent": _UA,
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Connection": "keep-alive",
    }
    if extra:
        h.update(extra)
    return h


def fetch_upload(pid, max_img=2):
    """取商品晒图 -> 下载 -> 微调字节 -> 上传京东图床，返回上传后的 URL 列表。"""
    urls = []
    try:
        r = requests.get(
            _IMG_LIST_URL.format(pid=pid), headers=_req_headers(), timeout=20, verify=False
        )
        data = r.json()
        for item in (data.get("imgComments", {}).get("imgList") or [])[:max_img]:
            u = (item.get("imageUrl") or "").strip()
            if u:
                urls.append(u)
    except Exception as e:
        logger.warning("获取商品 %s 晒图失败：%s", pid, e)
    if not urls:
        logger.info("商品 %s 暂无晒图，仅提交文字", pid)
        return []

    uploaded = []
    for idx, u in enumerate(urls):
        try:
            if u.startswith("//"):
                u = "https:" + u
            ir = requests.get(u, headers=_req_headers(), timeout=20, verify=False)
            if ir.status_code != 200:
                logger.warning("图片下载失败：%s", u)
                continue
            ext = ".jpg"
            base = u.split("?")[0]
            if "." in base:
                e = "." + base.rsplit(".", 1)[-1].lower()
                if len(e) <= 5:
                    ext = e
            filename = f"{pid}_{idx}{ext}"
            data = bytearray(ir.content)
            if len(data) > 200:  # 微调一个字节，避免与原图 MD5 相同
                data[len(data) // 2] ^= 1
            url = upload_image(filename, bytes(data))
            if url:
                uploaded.append(url)
        except Exception as e:
            logger.warning("图片处理异常：%s", e)
    logger.info("商品 %s 上传成功 %d 张图", pid, len(uploaded))
    return uploaded


def upload_image(filename, image_data):
    """上传到京东图床，返回协议相对 URL（如 //img30.360buyimg.com/...），失败返回 ''。"""
    headers = _req_headers(
        {
            "Referer": "https://club.jd.com/myJdcomments/myJdcomment.action",
            "Origin": "https://club.jd.com",
            "X-Requested-With": "XMLHttpRequest",
            "Accept": "*/*",
        }
    )
    files = {
        "name": (None, filename),
        "Filedata": (filename, io.BytesIO(image_data), "image/jpeg"),
        "upload": (None, "Submit Query"),
    }
    try:
        resp = requests.post(
            _UPLOAD_URL, headers=headers, files=files, timeout=30, verify=False
        )
        if resp.status_code == 200 and ".jpg" in resp.text:
            raw = resp.text.strip()
            # 提交评价时 imgs 需要完整 URL（抓包确认）：
            # //img30.360buyimg.com/shaidan/jfs/...，而非裸 jfs/ 相对路径
            if raw.startswith("//") or raw.startswith("http"):
                return raw
            return "//img30.360buyimg.com/shaidan/" + raw.lstrip("/")
        logger.warning("图片上传失败：HTTP %s %s", resp.status_code, resp.text[:120])
    except Exception as e:
        logger.warning("图片上传异常：%s", e)
    return ""


# ---------------- 提交 ----------------
def download_images(pid, max_img=2):
    """下载商品晒图到本地（微调字节避免与原图一致），供页面控件上传。"""
    urls = []
    try:
        r = requests.get(
            _IMG_LIST_URL.format(pid=pid), headers=_req_headers(), timeout=20, verify=False
        )
        for item in (r.json().get("imgComments", {}).get("imgList") or [])[:max_img]:
            u = (item.get("imageUrl") or "").strip()
            if u:
                urls.append(u)
    except Exception as e:
        logger.warning("获取商品 %s 晒图失败：%s", pid, e)
    files = []
    for idx, u in enumerate(urls):
        try:
            if u.startswith("//"):
                u = "https:" + u
            ir = requests.get(u, headers=_req_headers(), timeout=20, verify=False)
            if ir.status_code != 200:
                continue
            data = bytearray(ir.content)
            if len(data) > 200:
                data[len(data) // 2] ^= 1
            path = os.path.join("img", f"_review_{pid}_{idx}.jpg")
            with open(path, "wb") as f:
                f.write(bytes(data))
            files.append(path)
        except Exception as e:
            logger.warning("图片下载异常：%s", e)
    if files:
        logger.info("商品 %s 已下载 %d 张晒图", pid, len(files))
    else:
        logger.info("商品 %s 暂无可用晒图，仅提交文字", pid)
    return files


def review_via_ui(oid, pid, text, img_files, session):
    """UI 方式追评（京东页面自己的 JS 构造请求，最稳）。

    关键点（均为实测结论）：
    - 内容必须逐字符派发 keydown/keypress/input/keyup，页面 JS 才能感知
    - 必须点星级评分（span.star5），否则校验拦截
    - 图片通过页面上传控件（input[type=file]）上传，京东自己调上传接口
    - 成功标志：页面跳转 saveAfterCommentSuccess.action
    """
    url = (
        "https://club.jd.com/afterComments/productPublish.action"
        f"?sku={pid}&orderId={oid}"
    )
    ab(f'open "{url}"', session=session)
    ab("wait --load networkidle", session=session)
    time.sleep(2)

    # 1) 逐字符填入内容
    payload = base64.b64encode(text.encode("utf-8")).decode()
    type_js = (
        "(function(){"
        "var el=document.querySelector('textarea');"
        "el.focus(); el.value='';"
        "var text=decodeURIComponent(escape(atob('" + payload + "')));"
        "var opts={bubbles:true,cancelable:true};"
        "for(var i=0;i<text.length;i++){"
        "  var ch=text[i];"
        "  el.dispatchEvent(new KeyboardEvent('keydown',Object.assign({key:ch},opts)));"
        "  el.value+=ch;"
        "  el.dispatchEvent(new KeyboardEvent('keypress',Object.assign({key:ch},opts)));"
        "  el.dispatchEvent(new Event('input',{bubbles:true}));"
        "  el.dispatchEvent(new KeyboardEvent('keyup',Object.assign({key:ch},opts)));"
        "}"
        "return 'typed:'+el.value.length;"
        "})()"
    )
    r = eval_js(type_js, session=session, raw=True)
    logger.info("填入内容：%s", r)
    if "typed:0" in r:
        return False, "fill failed"

    # 2) 上传图片（页面控件）
    if img_files:
        snap = ab("snapshot -i", session=session)
        file_ref = ""
        for line in snap.splitlines():
            if "选择文件" in line and "ref=" in line:
                file_ref = line.split("[ref=")[1].split("]")[0]
                break
        if file_ref:
            for p in img_files:
                ab(f'upload {file_ref} "{p}"', session=session)
                time.sleep(2)
            time.sleep(3)
        else:
            logger.warning("未找到上传控件，本次仅文字")

    # 3) 点 5 星
    r = eval_js(
        "(function(){var s=document.querySelector('.commstar .star5');"
        "if(!s)return 'NO_STAR';s.click();"
        "return 'score='+(document.querySelector('.star-info')||{}).innerText;})()",
        session=session,
        raw=True,
    )
    logger.info("评分：%s", r)

    # 4) 发表
    snap = ab("snapshot -i", session=session)
    post_ref = ""
    for line in snap.splitlines():
        if "发表" in line and "ref=" in line:
            post_ref = line.split("[ref=")[1].split("]")[0]
            break
    if not post_ref:
        return False, "post button not found"
    ab(f"click {post_ref}", session=session)

    # 5) 轮询跳转结果
    for _ in range(15):
        time.sleep(2)
        cur = ab("get url", session=session)
        if "saveAfterCommentSuccess" in cur:
            logger.info("追评成功（已跳转成功页）")
            return True, cur
        if "productPublish" not in cur:
            break
    return False, "no redirect to success page"


def submit_in_browser(mode, oid, pid, text, imgs, session, encoding="utf-8", max_retry=3):
    """在浏览器页面内提交，返回 (success, resp_text)。

    content 必须在 Python 侧预编码成纯 ASCII 后提交（JS 端 URLSearchParams 会编码错）。
    """
    url = _REVIEW_URL if mode == "review" else _SUBMIT_URL
    # 关键：京东页面提交的 content 是【双重 percent-编码】（encodeURIComponent 后
    # 表单序列化又编一次，服务端解两次）。与抓包行为保持一致，否则存储乱码。
    inner = urllib.parse.quote(text, safe="", encoding=encoding, errors="ignore")
    content = urllib.parse.quote(inner, safe="")
    # imgs 与原 requests 版一致：单层编码，保留 / 与 ,
    imgs_str = urllib.parse.quote(",".join(imgs), safe="/,")
    if mode == "review":
        body = (
            f"orderId={oid}&productId={pid}&content={content}"
            f"&imgs={imgs_str}&anonymousFlag=1&score=5"
        )
    else:
        body = (
            f"orderId={oid}&productId={pid}&score=5&content={content}"
            f"&saveStatus=1&anonymousFlag=1&imgs={imgs_str}"
        )
    # 追评的 Referer 用订单专属页（与抓包一致）
    if mode == "review":
        referer = (
            "https://club.jd.com/afterComments/productPublish.action"
            f"?sku={pid}&orderId={oid}"
        )
    else:
        referer = _LIST_URL.format(sort=0, page=1)
    js = (
        "window.__jd_submit = null; "
        "fetch('%s', {method:'POST', credentials:'include', headers:{"
        "'Content-Type':'application/x-www-form-urlencoded; charset=%s',"
        "'X-Requested-With':'XMLHttpRequest',"
        "'Accept':'application/json, text/javascript, */*; q=0.01',"
        "'Referer':'%s'}, body: %s})"
        ".then(function(r){return r.arrayBuffer();})"
        ".then(function(buf){"
        "  try { window.__jd_submit = new TextDecoder('gbk').decode(buf); }"
        "  catch (e) { window.__jd_submit = new TextDecoder('utf-8').decode(buf); }"
        "})"
        ".catch(function(e){window.__jd_submit = 'ERR:' + e;}); 'sent'"
        % (url, encoding.upper(), referer, json.dumps(body))
    )
    resp_text = ""
    for attempt in range(1, max_retry + 1):
        eval_js(js, session=session)
        for _ in range(10):
            time.sleep(2)
            raw = eval_js("window.__jd_submit", session=session, raw=True)
            try:
                val = json.loads(raw)
            except Exception:
                val = raw
            if val:
                resp_text = val if isinstance(val, str) else json.dumps(val)
                break
        try:
            payload = json.loads(resp_text)
        except Exception:
            payload = None
        ok = bool(payload.get("success")) if isinstance(payload, dict) else (
            '"success":true' in resp_text.replace(" ", "")
        )
        if ok:
            logger.info("提交成功：%s", resp_text[:120])
            return True, resp_text
        code = str(payload.get("resultCode", "")) if isinstance(payload, dict) else ""
        reason = _RESULT_CODE_MSG.get(code)
        logger.warning(
            "提交失败(第%d/%d次)：%s%s",
            attempt,
            max_retry,
            resp_text[:120],
            f"  <-- {reason}" if reason else "",
        )
        if code in ("2", "42"):
            # 无评价资格 / 需要实名：重试无意义，直接放弃
            break
        if not acp._is_busy_response(resp_text) or attempt >= max_retry:
            break
        wait = acp.SUBMIT_RETRY_SLEEP_SEC * attempt
        logger.warning("疑似触发京东风控，%d 秒后重试", wait)
        time.sleep(wait)
    return False, resp_text or ""


# ---------------- 主流程 ----------------
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="只列订单不提交")
    parser.add_argument("--limit", type=int, default=0, help="最多评价几单（0=全部）")
    parser.add_argument(
        "--mode", default="ordinary", choices=["ordinary", "review"], help="普通评价/追评"
    )
    parser.add_argument("--spider", action="store_true", help="用爬虫实时生成文案（慢）")
    parser.add_argument("--no-imgs", action="store_true", help="不取图，只提交文字")
    parser.add_argument("--max-imgs", type=int, default=2, help="每单最多带几张图")
    parser.add_argument(
        "--encoding", default="utf-8", choices=["utf-8", "gbk"], help="提交内容编码"
    )
    parser.add_argument("--session", default="jd", help="agent-browser 会话名")
    parser.add_argument("--min-interval", type=float, default=acp.MIN_SUBMIT_INTERVAL_SEC)
    parser.add_argument("--sleep-min", type=float, default=acp.SUBMIT_SLEEP_RANGE[0])
    parser.add_argument("--sleep-max", type=float, default=acp.SUBMIT_SLEEP_RANGE[1])
    parser.add_argument("--keep-open", action="store_true", help="结束后不关闭浏览器")
    parser.add_argument("--log-file", default="browser_comment.log")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        stream=sys.stdout,
    )
    if args.log_file:
        fh = logging.FileHandler(args.log_file, "w", encoding="utf-8")
        fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)-8s %(message)s"))
        logging.getLogger().addHandler(fh)

    state = build_state()
    sort = 3 if args.mode == "review" else 0
    ab(f'--state {state} open "{_LIST_URL.format(sort=sort, page=1)}"', session=args.session)
    ab("wait --load networkidle", session=args.session)

    stat = fetch_stat(args.session)
    logger.info("统计：%s", stat)
    key = "待追评" if args.mode == "review" else "待评价订单"
    total = stat.get(key, 0)
    if not total:
        logger.warning("没有%s，结束", key)
        return

    orders = fetch_orders(args.mode, args.session, total)
    skipped = [
        o for o in orders if any(kw in o["name"] for kw in acp._SKIP_KEYWORDS)
    ]
    for o in skipped:
        logger.info("跳过试用/赠品订单：%s [oid=%s]", o["name"], o["oid"])
    orders = [o for o in orders if o not in skipped]
    # 历史失败订单自动跳过（如"无评价资格"的订单，重试也不会成功）
    failed_oids = set()
    if os.path.exists(_FAILED_PATH):
        with open(_FAILED_PATH, "r", encoding="utf-8") as f:
            failed_oids = {line.strip() for line in f if line.strip()}
    if failed_oids:
        before = len(orders)
        orders = [o for o in orders if o["oid"] not in failed_oids]
        logger.info(
            "按失败记录跳过 %d 个订单（记录见 %s）", before - len(orders), _FAILED_PATH
        )
    logger.info(
        "共 %d 个待%s商品（已过滤 %d 个试用/赠品）", len(orders), args.mode, len(skipped)
    )
    for i, o in enumerate(orders[:10]):
        logger.info("  %d. %s [oid=%s pid=%s]", i, o["name"], o["oid"], o["pid"])
    if len(orders) > 10:
        logger.info("  ... 其余 %d 条已省略", len(orders) - 10)

    if args.dry_run:
        logger.info("dry-run：不提交，结束")
        return

    todo = orders[: args.limit] if args.limit else orders
    ok_count = fail_count = 0
    new_failed = set()
    for i, o in enumerate(todo):
        if args.mode == "review":
            # 追评：使用一段时间后的补充反馈口吻，2~3 句更自然
            score, text = gen_text(
                o["name"],
                use_spider=args.spider,
                sentences=random.randint(2, 3),
                pool=_REVIEW_COMMENTS,
                max_prefixed=1,
            )
        else:
            score, text = gen_text(o["name"], use_spider=args.spider)
        logger.info("[%d/%d] %s 评价内容：%s", i + 1, len(todo), o["name"], text)
        if args.mode == "review":
            # 追评走 UI 流程（京东页面自己构造请求，实测最稳）
            img_files = [] if args.no_imgs else download_images(o["pid"], args.max_imgs)
            success, _ = review_via_ui(o["oid"], o["pid"], text, img_files, args.session)
        else:
            imgs = [] if args.no_imgs else fetch_upload(o["pid"], args.max_imgs)
            success, _ = submit_in_browser(
                args.mode, o["oid"], o["pid"], text, imgs, args.session, args.encoding
            )
        ok_count += 1 if success else 0
        fail_count += 0 if success else 1
        if not success:
            new_failed.add(o["oid"])
        if i < len(todo) - 1:
            wait = max(args.min_interval, random.uniform(args.sleep_min, args.sleep_max))
            logger.info("休眠 %.0f 秒", wait)
            time.sleep(wait)

    logger.info("完成：成功 %d，失败 %d", ok_count, fail_count)
    if new_failed:
        with open(_FAILED_PATH, "a", encoding="utf-8") as f:
            for oid in sorted(new_failed):
                f.write(oid + "\n")
        logger.info(
            "失败订单已记录到 %s（下次运行自动跳过；若已解决可手动清空该文件）",
            _FAILED_PATH,
        )
    if not args.keep_open:
        ab("close", session=args.session)


if __name__ == "__main__":
    main()
