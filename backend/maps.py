"""高德 Web 服务 POI 2.0。只返回接口事实，不抓取或生成评论。"""
from datetime import datetime, timezone
import math
import json
import logging
import re
from urllib.parse import urlencode
import httpx
import config

DOC_URL = "https://developer.amap.com/api/webservice/guide/api-advanced/newpoisearch"
DEFAULT_CITY = "大庆市"
DEFAULT_SCHOOL = "黑龙江八一农垦大学"
NOTICE = "评分、人均和营业时间来自高德返回值，缺失表示未提供；接口不提供用户评论正文。距离为学校中心点的直线距离，不是步行路程。学校周边不等于校外，校园边界尚未核定，请结合地址核对。"


class _RedactMapKey(logging.Filter):
    """httpx 的 INFO 日志会输出查询串；高德要求 Key 在查询串中传递。"""
    def filter(self, record):
        message = record.getMessage()
        if "restapi.amap.com/" in message:
            record.msg = re.sub(r"([?&]key=)[^&\s\"']+",r"\1[redacted]",message,flags=re.IGNORECASE)
            record.args = ()
        return True


logging.getLogger("httpx").addFilter(_RedactMapKey())


def _text(value):
    return str(value).strip() if isinstance(value, (str, int, float)) and not isinstance(value, bool) else ""


def validate_location(value):
    value = _text(value)
    if not value:
        return ""
    try:
        lon, lat = map(float, value.split(","))
        if not math.isfinite(lon) or not math.isfinite(lat) or not (-180 <= lon <= 180 and -90 <= lat <= 90):
            raise ValueError()
    except (ValueError, TypeError):
        raise ValueError("学校坐标应为有效的高德经度,纬度，例如从高德坐标拾取器复制。") from None
    return f"{lon:.6f},{lat:.6f}"


def public_config(cfg):
    key = _text(cfg.get("amap_key"))
    return {"has_key": bool(key), "masked_key": key[:3]+"****"+key[-3:] if len(key)>8 else ("已设置" if key else ""),
            "city": cfg.get("amap_city", DEFAULT_CITY), "campus_name": cfg.get("amap_campus_name", DEFAULT_SCHOOL),
            "campus_location": cfg.get("amap_campus_location", ""), "radius": cfg.get("amap_radius", 3000)}


def update_config(cfg, changes):
    updated = dict(cfg)
    if changes.get("api_key") is not None:
        key = changes["api_key"].strip()
        if len(key)>200: raise ValueError("高德 Key 长度不正确。")
        updated["amap_key"] = key
    for field in ("city", "campus_name"):
        if changes.get(field) is not None:
            text = changes[field].strip()
            if not 1 <= len(text) <= 60: raise ValueError("请填写城市和学校名称（不超过60字）。")
            updated["amap_"+field] = text
    if changes.get("campus_location") is not None:
        updated["amap_campus_location"] = validate_location(changes["campus_location"])
    if changes.get("radius") is not None:
        radius = int(changes["radius"])
        if not 100 <= radius <= 50000: raise ValueError("搜索半径应为100—50000米。")
        updated["amap_radius"] = radius
    return updated


class MapError(Exception):
    pass


class AmapClient:
    def __init__(self, cfg, transport=None):
        self.cfg = cfg
        self.transport = transport

    def _request(self, endpoint, params):
        if not self.cfg.get("amap_key"):
            raise MapError("尚未配置高德 Web 服务 API Key，请联系管理员完成服务端配置后再查询地图。")
        params = {**params, "key": self.cfg["amap_key"], "output": "json", "show_fields": "business,navi"}
        try:
            # No arbitrary base URL, redirects, raw responses or exception URLs are exposed.
            with httpx.Client(timeout=8.0, transport=self.transport, follow_redirects=False) as client:
                response = client.get("https://restapi.amap.com/v5/place/" + endpoint, params=params)
                response.raise_for_status()
                data = response.json()
            if not isinstance(data, dict): raise ValueError()
        except (httpx.HTTPError, ValueError):
            raise MapError("高德请求未完成，请检查网络后重试；若持续失败，请检查高德控制台的服务权限与额度。") from None
        if str(data.get("status")) != "1":
            code = _text(data.get("infocode"))
            tips = {"10001": "Key 无效或已过期，请重新填写。", "10002": "当前 Key 没有该服务权限，请检查地点搜索服务权限。",
                    "10003": "日调用额度已用完，请到高德控制台核对额度。", "10004": "请求过于频繁，请稍后重试。",
                    "10005": "出口 IP 不在白名单中，请检查高德控制台。", "10009": "Key 平台类型不匹配，请使用 Web 服务类型的 Key。",
                    "10012": "服务权限不足，请到高德控制台检查。", "10044": "账号日调用量已达上限，请核对额度。"}
            safe_code = code if re.fullmatch(r"\d{5}",code) else "未知"
            raise MapError(f"高德接口错误（{safe_code}）："+tips.get(code,"请检查高德控制台的 Key、服务权限和额度。"))
        return [row for row in data.get("pois", []) if isinstance(row, dict)] if isinstance(data.get("pois"), list) else []

    def resolve_campus(self):
        location = validate_location(self.cfg.get("amap_campus_location", ""))
        name = self.cfg.get("amap_campus_name", DEFAULT_SCHOOL)
        if location: return {"name":name, "location":location, "source":"手动配置的学校中心点"}
        rows = self._request("text", {"keywords":name, "region":self.cfg.get("amap_city",DEFAULT_CITY), "city_limit":"true","page_size":10})
        matches = []
        for row in rows:
            if _text(row.get("name")) != name: continue
            try: coordinate = validate_location(row.get("location"))
            except ValueError: continue
            if coordinate and coordinate not in {m["location"] for m in matches}:
                matches.append({"id":_text(row.get("id")), "name":name, "location":coordinate, "source":"高德学校名称精确匹配"})
        if len(matches) != 1:
            raise MapError("学校中心点未能唯一确认，请联系管理员核对城市、学校名称或学校中心坐标。")
        return matches[0]

    def _result(self, rows=None, origin=None, **extra):
        return {"available":True, "provider":"高德地图", "fetched_at":datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "notice":NOTICE, "origin":origin, "results":rows or [], **extra}

    def _failure(self, error):
        return self._result(available=False, message=str(error), config_needed=not bool(self.cfg.get("amap_key")))

    def _normalize(self, row, origin=None):
        business = row.get("business") if isinstance(row.get("business"), dict) else {}
        name, pid = _text(row.get("name")), _text(row.get("id"))
        try: location = validate_location(row.get("location"))
        except ValueError: location = ""
        def number(field):
            raw = _text(business.get(field))
            try:
                value = float(raw)
                return raw if math.isfinite(value) and value >= 0 else None
            except (TypeError, ValueError): return None
        detail_url = "https://uri.amap.com/poidetail?" + urlencode({"poiid":pid,"src":"campus_agent","callnative":0}) if pid else ""
        map_url = "https://uri.amap.com/marker?" + urlencode({"position":location,"name":name,"coordinate":"gaode","src":"campus_agent"}) if location else detail_url
        nav_url = ""
        if origin and origin.get("location") and location:
            nav_url = "https://uri.amap.com/navigation?" + urlencode({"from":origin["location"]+","+origin["name"],"to":location+","+name,"mode":"walk","coordinate":"gaode","src":"campus_agent","callnative":0})
        return {"id":pid,"name":name,"address":_text(row.get("address")),"city":_text(row.get("cityname")),
                "district":_text(row.get("adname")),"category":_text(row.get("type")),"location":location,
                "distance_m":_text(row.get("distance")) or None, "rating":number("rating"), "cost":number("cost"),
                "phone":_text(business.get("tel")),"opening_hours":_text(business.get("opentime_today")) or _text(business.get("opentime_week")),
                "tags":_text(business.get("tag")),"review_status":"not_provided",
                "source":"高德地图","source_url":detail_url,"map_url":map_url,"navigation_url":nav_url}

    def search(self, query, scope="nearby", radius=None, sort="distance", limit=5, origin_query=""):
        query = _text(query)
        if not 1 <= len(query) <= 80: raise ValueError("地点关键词应为1—80字。")
        if scope not in ("campus","nearby","city"): raise ValueError("地点范围只能是campus、nearby或city。")
        if sort not in ("distance","rating"): raise ValueError("不支持的地点排序。")
        radius = int(radius if radius is not None else self.cfg.get("amap_radius",3000))
        if not 100 <= radius <= 50000: raise ValueError("搜索半径应为100—50000米。")
        limit = max(1,min(10,int(limit)))
        school = self.cfg.get("amap_campus_name",DEFAULT_SCHOOL)
        params = {"keywords":query,"region":self.cfg.get("amap_city",DEFAULT_CITY),"city_limit":"true","page_size":25,"page_num":1}
        origin = None
        try:
            if origin_query:
                origin_text = _text(origin_query)
                if origin_text in ("学校", "校园", "学校中心", "校门"):
                    origin = self.resolve_campus()
                else:
                    origin_rows = self._request("text", {"keywords":origin_text, "region":self.cfg.get("amap_city",DEFAULT_CITY), "city_limit":"true", "page_size":10})
                    for row in origin_rows:
                        name = _text(row.get("name")); address = _text(row.get("address"))
                        try: location = validate_location(row.get("location"))
                        except ValueError: location = ""
                        if location and (origin_text in name or origin_text in address):
                            origin = {"name":name or origin_text,"location":location,"source":"高德地点搜索"}
                            break
                    if origin is None:
                        origin = self.resolve_campus()
                        origin["requested_name"] = origin_text
                        origin["fallback"] = True
            if scope == "nearby" and origin is None:
                origin = self.resolve_campus()
            if scope == "nearby":
                params.update(location=origin["location"],radius=radius,sortrule="distance")
                rows = self._request("around",params)
            else:
                if scope == "campus":
                    params["keywords"] = query if school in query else school + " " + query
                    if len(params["keywords"])>80: raise ValueError("学校名称与地点关键词合计不能超过80字。")
                rows = self._request("text",params)
                if scope == "campus":
                    rows = [row for row in rows if school in (_text(row.get("name"))+" "+_text(row.get("address")))]
            normalized = [self._normalize(row,origin) for row in rows]
            if sort == "rating": normalized.sort(key=lambda r:float(r["rating"]) if r["rating"] is not None else -1,reverse=True)
            result = self._result(normalized[:limit], origin, scope=scope, query=query, origin_query=_text(origin_query), radius=radius if scope=="nearby" else None, candidate_count=len(normalized))
            if origin and origin.get("fallback"):
                result["origin_note"] = f"未能在高德中确认“{origin.get('requested_name','')}”，已回退到学校中心点；请打开高德导航核对起点。"
            if sort == "rating": result["notice"] += "仅在本次高德返回的候选中按评分排序，不代表全城排名；无评分的地点排在后面。"
            if not normalized: result["message"] = "本次未找到匹配地点；不代表该地点不存在，可以换用全称或核对地图收录情况。"
            return result
        except MapError as error: return self._failure(error)

    def detail(self, poi_id):
        if not re.fullmatch(r"[A-Za-z0-9]{1,40}",_text(poi_id)): raise ValueError("无效的高德地点编号。")
        try:
            rows = self._request("detail",{"id":poi_id})
            return self._result([self._normalize(row) for row in rows],message="" if rows else "该地点暂未返回详情。")
        except MapError as error: return self._failure(error)

    def test_connection(self):
        try:
            origin = self.resolve_campus()
            # A manual coordinate needs no HTTP lookup; still test the actual API credential.
            if self.cfg.get("amap_campus_location"):
                self._request("text",{"keywords":self.cfg.get("amap_campus_name",DEFAULT_SCHOOL),"region":self.cfg.get("amap_city",DEFAULT_CITY),"city_limit":"true","page_size":1})
            return {"ok":True,"message":"高德地点搜索连接成功，已确认学校搜索中心点。","origin":origin}
        except (MapError,ValueError) as error: return {"ok":False,"error":str(error)}


def parse_place_request(message):
    q = re.sub(r"\s+","",str(message or ""))
    if re.search(r"不要|不需要|不用|申请|办理|报销|规定|政策|通知|借阅|借书|招聘",q): return None
    context = re.search(r"高德|校外|校内|学校附近|学校周边|校园里|校园内|附近|周边",q)
    campus_location_question = re.search(r"图书馆|[一二]食堂|教学楼|宿舍楼|校医院|体育馆",q) and re.search(r"在哪|哪里|位置|怎么走|导航",q)
    route_match = re.search(r"(?:从|由)(.+?)(?:到|去)(.+?)(?:怎么走|如何走|导航|路线|在哪|到达)",q)
    if campus_location_question and not context: context = True
    intent = re.search(r"在哪|哪里|位置|地点|怎么走|导航|找|搜|推荐|评价|评分|口碑|餐馆|饭店|咖啡|奶茶|超市|药店|酒店|宾馆|医院|图书馆|教学楼|食堂|吃什么|有什么",q)
    if route_match:
        context = True
        intent = True
    if not context or not intent: return None
    scope = "campus" if re.search(r"校内|校园里|校园内",q) or (campus_location_question and not re.search(r"校外|附近|周边",q)) else "nearby"
    origin_query = ""
    if route_match:
        origin_query = route_match.group(1).strip("的从由")
        keyword = route_match.group(2).strip("的了呢吧")
    elif re.search(r"这家|那家|这几个|它的",q): keyword = ""
    else:
        keyword = re.sub(r"^(请帮我|帮我|请)?(?:用高德|高德)?(?:查找|查一下|搜索|查|找|搜)?","",q)
        keyword = re.sub(r"学校附近|学校周边|校园里|校园内|校内|校外|附近|周边","",keyword)
        keyword = re.sub(r"\d+(?:\.\d+)?(?:公里|千米|米)(?:以内|内|范围)?","",keyword)
        keyword = re.sub(r"评分高的|评分最高的|口碑好的|推荐一下|推荐|有什么|有哪些|一下|的评价|评价怎么样|怎么样|在哪里|在哪|怎么走|导航|[，。？！!?]","",keyword)
        if re.search(r"吃什么|吃啥|好吃的",keyword): keyword="餐饮"
        keyword = keyword.strip("的了呢吧")
    args = {"query":keyword[:80],"scope":scope,"sort":"rating" if re.search(r"评分|评价|口碑",q) else "distance"}
    if route_match and origin_query:
        args["origin_query"] = origin_query[:80]
    match = re.search(r"(\d+(?:\.\d+)?)(公里|千米|米)",q)
    if match:
        radius = int(float(match[1])*(1000 if match[2]!="米" else 1))
        args["radius"] = radius
    return args


def history_context(tool_events):
    """供模型理解“第一家”等追问；界面消息不混入地点编号。"""
    try: events = json.loads(tool_events or "[]") if isinstance(tool_events,str) else tool_events
    except (ValueError,TypeError): return ""
    facts = []
    for event in events if isinstance(events,list) else []:
        if not isinstance(event,dict) or event.get("name") not in ("search_campus_places","get_campus_place_detail"): continue
        result = event.get("result")
        if not isinstance(result,dict): continue
        rows = result.get("results",[])
        if not isinstance(rows,list): continue
        for row in rows[:10]:
            if not isinstance(row,dict): continue
            facts.append({key:row.get(key) for key in ("id","name","address","city","rating","cost","distance_m","source_url")})
    return "\n【上次地图检索快照，仅供追问定位，信息可能变化；评分不是评论正文】\n"+json.dumps(facts,ensure_ascii=False) if facts else ""


def search_places(query, scope="nearby", radius=None, sort="distance", limit=5, origin_query="", cfg=None, rag=None):
    cfg = config.load_config() if cfg is None else cfg
    if not _text(query):
        result = {"available":False,"results":[],"message":"请告诉我具体店名或地点名称；也可以点击上次结果的“地点详情”核对。","notice":NOTICE}
    else:
        result = AmapClient(cfg).search(query,scope,radius,sort,limit,origin_query=origin_query)
    result["local_references"] = []
    if scope == "campus" and rag is not None:
        result["local_references"] = rag.search(query,top_k=3)
    return result


def format_places(result):
    if not result.get("available"):
        text = result.get("message","暂时无法查询高德地图。")
    elif result.get("results"):
        text = f"本次找到 {len(result['results'])} 个地点，详情见下方高德地点卡片。"
    else:
        text = result.get("message","本次未找到匹配地点。")
    if result.get("local_references"):
        text += "\n\n已找到相关校内资料，可展开参考原文核对。地图未收录时，不推断具体坐标和路线。"
    if result.get("origin_note"):
        text += "\n\n" + str(result["origin_note"])
    return text
