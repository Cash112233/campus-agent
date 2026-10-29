"""校园检索的主题约束、别称和来源元数据；不包含学校政策。"""
import re
from urllib.parse import urlsplit

TOPICS = [
    ("校园卡", "一卡通", "饭卡"), ("请假", "销假"), ("图书馆", "借书", "借阅"),
    ("奖学金", "助学金", "资助", "助学贷款"), ("学籍", "休学", "复学", "转专业"),
    ("选课", "退课", "重修", "补考", "缓考"), ("校历",),
    ("中秋", "国庆", "放假", "假期", "调休", "寒假", "暑假", "元旦"),
    ("宿舍", "公寓", "寝室"), ("报修", "维修"), ("食堂", "餐厅", "餐饮"),
    ("就业", "招聘", "双选会", "三方协议"), ("校园网", "上网", "网络故障", "vpn", "门户", "账号登录"),
    ("医保", "医疗保险", "门诊", "住院报销"), ("校车", "班车", "通勤车"),
    ("浴室", "热水", "洗浴"), ("快递", "驿站", "取件"), ("门禁", "出入"),
    ("四六级", "英语四级", "英语六级", "考试报名"), ("普通话", "水平测试"),
    ("综合测评", "综测", "评优", "先进个人"), ("保研", "推免", "考研"),
    ("户籍", "户口", "居住证"), ("实验室", "危化品", "实验安全"), ("邮箱", "邮件系统"),
    ("心理", "心理咨询", "心理健康"), ("安全", "消防", "保卫"), ("失物", "失物招领"),
    ("创新创业", "大创", "竞赛", "创新训练"), ("毕业", "离校", "毕业证", "学位证"),
    ("缴费", "学费", "住宿费", "缴纳"),
]
COLLEGES = ("应用科技学院", "工程学院", "农学院", "动物科技学院", "经济管理学院", "食品学院",
             "人文社会科学学院", "信息与电气工程学院", "生命科学技术学院", "理学院", "园艺园林学院", "土木水利学院")

def compact(text):
    return re.sub(r"[\s*#|：:，,。？?（）()\[\]]", "", str(text or "").lower())

def source_url(source):
    match = re.search(r"https?://[^\s\]）)<>]+", str(source or ""))
    if not match: return ""
    value = match.group().rstrip('。，,;；')
    parsed = urlsplit(value)
    return value if parsed.netloc and not parsed.username and not parsed.password else ""

def source_reference(row):
    return {key: row.get(key) for key in ("doc_id", "title", "source", "source_url", "indexed_at", "published_at", "source_kind")}

def query_terms(query):
    q = compact(query)
    groups = [g for g in TOPICS if any(word in q for word in g)]
    # 只加入每个主题的规范词，避免把大量同义词同时放入 TF-IDF 查询而稀释命中分数；
    # 原始问题仍保留用户自己的别称（如饭卡、VPN）。
    return q, groups, str(query) + " " + " ".join(group[0] for group in groups)


def topic_labels(query):
    """返回命中的主题名，供资料不足时生成可执行的缺口说明。"""
    q = compact(query)
    return [group[0] for group in TOPICS if any(word in q for word in group)]

def is_relevant(q, groups, title, content):
    title, content = compact(title), compact(content)
    text = title + content
    asked_halls = set(re.findall(r'[一二]食堂', q))
    scoped_halls = set(re.findall(r'[一二]食堂', title))
    if asked_halls and scoped_halls and asked_halls.isdisjoint(scoped_halls): return False
    asked_floors = set(re.findall(r'[一二三]楼', q))
    scoped_floors = set(re.findall(r'[一二三]楼', title))
    if asked_halls and asked_floors and scoped_floors and asked_floors.isdisjoint(scoped_floors): return False
    if groups and not any(any(word in text for word in group) for group in groups): return False
    college_pattern = "|".join(re.escape(name) for name in sorted(COLLEGES, key=len, reverse=True))
    asked_colleges = set(re.findall(college_pattern, q))
    scoped_colleges = set(re.findall(college_pattern, title))
    if asked_colleges and scoped_colleges and asked_colleges.isdisjoint(scoped_colleges): return False
    if "本科" in q and "研究生" in title and "本科" not in title: return False
    if any(word in q for word in ("补办", "补卡", "换卡")) and not any(word in text for word in ("补办", "补卡", "换卡")): return False
    if any(word in q for word in ("开放时间", "开门", "开馆时间", "几点关", "营业时间", "闭馆")):
        if not (any(word in text for word in ("开放时间", "开馆时间", "营业时间", "开放时段", "闭馆时间"))
                or (any(word in text for word in ("开馆", "闭馆", "开放")) and re.search(r"\d{1,2}[:：]\d{2}", content))): return False
    if any(word in q for word in ("流程", "怎么办", "如何办理", "怎么申请")) and not any(word in text for word in ("流程", "申请", "办理", "审批", "提交", "登录", "登陆")): return False
    return True

def source_meta(meta, content):
    url = source_url(meta.get("source"))
    host = urlsplit(url).hostname or ""
    date = re.search(r"(?:发布时间|发布日期|(?:^|\n)时间)[：:\s]*(\d{4}-\d{2}-\d{2})", content)
    kind = {'本科生手册': '用户提供的本科生手册', '食堂资料': '用户提供的食堂整理资料'}.get(meta.get('ktype'), '本地整理资料')
    if host and (meta.get('ktype') in ('官网爬取', '官网补充') or str(meta.get('source', '')).startswith('官网:')):
        kind = '学校官网资料'
    return {"source_url": url, "indexed_at": meta.get("indexed_at"), "published_at": date.group(1) if date else None,
            "source_kind": kind}
