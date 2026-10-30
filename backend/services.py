"""办事导航：事项分类是产品导航，电话与地址只读取官网号码簿原文。"""
import re
from db import get_conn
from retrieval_rules import source_url, source_meta, COLLEGES


def _active_school_id(school_id=None):
    if school_id:
        return str(school_id)
    try:
        from config import load_config
        return str(load_config().get("current_school_id") or "hlby")
    except Exception:
        return "hlby"

# 仅定义检索入口与咨询方向，不预设审批规则、费用或办理时限。
SERVICES = [
    {"id": "leave", "title": "学生请假", "category": "学生事务", "description": "查找请假申请入口、适用范围和学院咨询方式。", "query": "本科生请假申请流程", "contact_terms": ["本科生管理科"], "missing": "各学院审批权限、所需证明和销假要求需进一步核实。"},
    {"id": "student-status", "title": "学籍与证明", "category": "教务办理", "description": "学籍、休复学、在读证明等事项。", "query": "学籍管理申请办理", "contact_terms": ["学籍科"], "missing": "具体证明材料、办理入口与时限以对应通知为准。"},
    {"id": "course", "title": "选课与考试", "category": "教务办理", "description": "选课、考试、补缓考及重修相关资料。", "query": "选课补考缓考重修申请办理", "contact_terms": ["教务科"], "missing": "个人选课、考试安排尚未接入，申请细则需核对本学期通知。"},
    {"id": "aid", "title": "奖助与资助", "category": "学生事务", "description": "查找奖助政策、申请通知与资助咨询方式。", "query": "本科生奖学金助学金申请", "contact_terms": ["学生资助管理中心"], "missing": "金额、名额、资格与截止日期必须以当年正式通知为准。"},
    {"id": "campus-card", "title": "校园卡补办", "category": "校园生活", "description": "查找校园卡挂失、补卡的正式办理说明。", "query": "校园卡补办", "contact_terms": [], "missing": "补卡地点、工本费和所需证件尚待正式资料核实。"},
    {"id": "library", "title": "图书借阅", "category": "校园生活", "description": "检索图书借阅规定与读者服务联系方式。", "query": "图书馆借阅办理", "contact_terms": ["读者服务部", "咨询服务部"], "missing": "开放时间与借阅额度不能根据电话号码簿推断。"},
    {"id": "repair", "title": "宿舍与报修", "category": "校园生活", "description": "查找公寓服务、维修咨询与正式报修说明。", "query": "宿舍报修办理", "contact_terms": ["公寓服务中心", "维修服务中心"], "missing": "报修入口、受理时间与具体服务范围尚待核实。"},
    {"id": "network", "title": "校园网与账号", "category": "校园生活", "description": "门户登录、校园网使用与故障咨询。", "query": "校园网账号登录办理", "contact_terms": ["网络信息服务", "技术科"], "missing": "账号问题需在学校官方页面处理；助手不收集登录密码。"},
    {"id": "employment", "title": "就业与招聘", "category": "毕业就业", "description": "招聘活动、就业通知与就业指导联系方式。", "query": "就业招聘双选会", "contact_terms": ["就业指导科", "就业市场拓展科"], "missing": "历史招聘公告不代表当前仍可报名，请核对举办与截止日期。"},
]


def list_colleges():
    """返回当前学校模板支持的学院入口，供对话卡片生成可点击列表。"""
    return [{"id": f"college-{index:02d}", "name": name} for index, name in enumerate(COLLEGES, 1)]


def _college_name(value):
    needle = str(value or "").strip()
    for index, name in enumerate(COLLEGES, 1):
        if needle in (name, f"college-{index:02d}"):
            return name
    raise ValueError("未找到该学院")


def college_detail(college, rag, school_id=None):
    """返回学院的已核实联系人和知识库资料，不补造学院简介或联系方式。"""
    name = _college_name(college)
    contacts = list_contacts(name, school_id=school_id) if school_id is not None else list_contacts(name)
    references = []
    if rag:
        rows = rag.search(name, top_k=8, school_id=school_id) if school_id is not None else rag.search(name, top_k=8)
        for row in rows:
            text = f"{row.get('title', '')} {row.get('content', '')}"
            if name in text:
                references.append(row)
    has_information = bool(contacts or references)
    return {
        "id": f"college-{list(COLLEGES).index(name) + 1:02d}",
        "name": name,
        "contacts": contacts[:8],
        "references": references[:8],
        "status": "has_information" if has_information else "needs_sources",
        "missing": "学院简介、专业设置和办理细则需要以该学院官网及最新通知为准；当前仅展示已收录的联系人和资料。",
        "notice": "以下内容来自当前学校的已收录资料，联系人不等于审批人；涉及期限、材料和流程请核对学院官网最新通知。",
    }


def list_contacts(query="", school_id=None):
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT id,title,source,content,ktype,created_at FROM knowledge "
            "WHERE (school_id=? OR school_id='common') "
            "AND ((ktype='官网爬取' AND (title LIKE '%电话查询%' OR title LIKE '%办公电话%')) "
            "OR (ktype='电话簿' AND content LIKE '%学校官网%')) ORDER BY id",
            (_active_school_id(school_id),),
        ).fetchall()
    except Exception as error:
        if "school_id" not in str(error):
            raise
        rows = conn.execute("SELECT id,title,source,content,ktype,created_at FROM knowledge WHERE ktype='官网爬取' AND (title LIKE '%电话查询%' OR title LIKE '%办公电话%') ORDER BY id").fetchall()
    contacts, seen = [], set()
    needle = re.sub(r"\s+|电话号码|联系方式|办公地址|电话|地址|请问|帮我查|查一下|的", "", query or "")
    requested_colleges = [name for name in sorted(COLLEGES, key=len, reverse=True) if name in needle]
    requested_colleges += [name for name in ("马克思主义学院", "体育教研部") if name in needle]
    for doc in rows:
        department, pending = "", ""
        for number, line in enumerate(doc["content"].splitlines()):
            heading = re.match(r"^#{2,4}\s*(.+?)\s*$", line.strip())
            if heading:
                pending = heading.group(1).strip()
                continue
            cells = [part.strip().strip("*") for part in line.split("|")]
            if cells and cells[0] == "":
                cells = cells[1:]
            if cells and cells[-1] == "":
                cells = cells[:-1]
            if len(cells) < 3:
                if line.strip() and not re.search(r"号码簿|电话|部门|部 门|附件|https?", line): pending = line.strip()
                continue
            if not re.search(r"\d{7}", cells[-2]): continue
            if len(cells) >= 4:
                department = (pending if cells[-4].startswith("（") else "") + cells[-4]
            elif len(cells) == 3 and pending:
                department = pending
            pending = ""
            role, phone, address = cells[-3:]
            key = (department, role, phone, address)
            if not department or key in seen: continue
            combined = re.sub(r"\s+", "", " ".join(key))
            primary = re.sub(r"[（(][^）)]*[）)]", "", combined)
            simplified = re.sub(r"副处级|正处级|党委|专职|兼", "", combined)
            if requested_colleges:
                college_match = any(
                    department == college or
                    re.search(r"(^|[（(、/ ])" + re.escape(college) + r"($|[）)、/ ])", department)
                    for college in requested_colleges
                )
                if not college_match:
                    continue
            if needle and needle not in combined and needle not in primary and needle not in simplified: continue
            seen.add(key)
            meta = source_meta({"source": doc["source"], "ktype": doc["ktype"] or "官网爬取", "indexed_at": doc["created_at"]}, doc["content"])
            contacts.append({"id": f"{doc['id']}:{number}", "department": department, "role": role,
                             "phone": phone, "address": address, "doc_id": doc["id"], "title": doc["title"],
                             "source": doc["source"], **meta})
    return contacts


def list_services(query=""):
    needle = (query or "").strip()
    return [{k: v for k, v in item.items() if k != "contact_terms"} for item in SERVICES
            if not needle or any(needle in item[key] for key in ("title", "description", "category", "query"))]


def service_detail(service_id, rag, college="", school_id=None):
    item = next((x for x in SERVICES if x["id"] == service_id), None)
    if item is None: return None
    contacts = []
    for term in item["contact_terms"]:
        contacts.extend(list_contacts(term, school_id=school_id))
    references = [row for row in rag.search(item["query"], top_k=4, school_id=school_id)
                  if row.get("ktype") in ("官网爬取", "本科生手册") and not any(word in row["title"] for word in ("电话查询", "教学教辅机构", "党政管理机构"))]
    return {**{k: v for k, v in item.items() if k != "contact_terms"}, "contacts": contacts[:8],
            "references": references, "college_contacts": list_contacts(college, school_id=school_id) if college else [], "status": "has_references" if references else "needs_sources",
            "notice": "以下为已收录的原文片段和咨询联系人；联系人不等于审批人。请核对适用学院、学历层次、学期及后续通知。"}
