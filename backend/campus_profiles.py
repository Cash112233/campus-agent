"""黑龙江高校通用模板：学校目录、当前学校切换和学校级配置。"""
import copy
import re


def _profile(name, city, website=""):
    return {"id": "", "name": name, "city": city, "website": website,
            "campus_location": "", "radius": 3000}


# 目录用于模板开箱即用；官网、坐标和校内资料由每所学校后续补齐。
_SCHOOL_ROWS = [
    ("黑龙江大学", "哈尔滨市"), ("哈尔滨工业大学", "哈尔滨市"),
    ("哈尔滨理工大学", "哈尔滨市"), ("哈尔滨工程大学", "哈尔滨市"),
    ("黑龙江科技大学", "哈尔滨市"), ("东北石油大学", "大庆市"),
    ("佳木斯大学", "佳木斯市"), ("黑龙江八一农垦大学", "大庆市"),
    ("东北农业大学", "哈尔滨市"), ("东北林业大学", "哈尔滨市"),
    ("哈尔滨医科大学", "哈尔滨市"), ("黑龙江中医药大学", "哈尔滨市"),
    ("牡丹江医学院", "牡丹江市"), ("哈尔滨师范大学", "哈尔滨市"),
    ("齐齐哈尔大学", "齐齐哈尔市"), ("牡丹江师范学院", "牡丹江市"),
    ("哈尔滨学院", "哈尔滨市"), ("大庆师范学院", "大庆市"),
    ("绥化学院", "绥化市"), ("哈尔滨商业大学", "哈尔滨市"),
    ("哈尔滨体育学院", "哈尔滨市"), ("哈尔滨金融学院", "哈尔滨市"),
    ("齐齐哈尔医学院", "齐齐哈尔市"), ("黑龙江工业学院", "鸡西市"),
    ("黑龙江东方学院", "哈尔滨市"), ("哈尔滨信息工程学院", "哈尔滨市"),
    ("黑龙江工程学院", "哈尔滨市"), ("齐齐哈尔工程学院", "齐齐哈尔市"),
    ("黑龙江外国语学院", "哈尔滨市"), ("黑龙江财经学院", "哈尔滨市"),
    ("哈尔滨石油学院", "哈尔滨市"), ("黑龙江工商学院", "哈尔滨市"),
    ("哈尔滨远东理工学院", "哈尔滨市"), ("哈尔滨剑桥学院", "哈尔滨市"),
    ("黑龙江工程学院昆仑旅游学院", "哈尔滨市"), ("哈尔滨广厦学院", "哈尔滨市"),
    ("哈尔滨华德学院", "哈尔滨市"), ("黑河学院", "黑河市"),
    ("哈尔滨音乐学院", "哈尔滨市"), ("哈尔滨职业技术大学", "哈尔滨市"),
    ("齐齐哈尔高等师范专科学校", "齐齐哈尔市"), ("伊春职业学院", "伊春市"),
    ("牡丹江大学", "牡丹江市"), ("黑龙江职业学院", "哈尔滨市"),
    ("黑龙江建筑职业技术学院", "哈尔滨市"), ("黑龙江艺术职业学院", "哈尔滨市"),
    ("大庆职业学院", "大庆市"), ("黑龙江林业职业技术学院", "牡丹江市"),
    ("黑龙江农业职业技术学院", "佳木斯市"), ("黑龙江农业工程职业学院", "哈尔滨市"),
    ("黑龙江农垦职业学院", "哈尔滨市"), ("黑龙江司法警官职业学院", "哈尔滨市"),
    ("鹤岗师范高等专科学校", "鹤岗市"), ("哈尔滨电力职业技术学院", "哈尔滨市"),
    ("哈尔滨铁道职业技术学院", "哈尔滨市"), ("大兴安岭职业学院", "大兴安岭地区"),
    ("黑龙江农业经济职业学院", "牡丹江市"), ("哈尔滨传媒职业学院", "哈尔滨市"),
    ("黑龙江生物科技职业学院", "哈尔滨市"), ("黑龙江商业职业学院", "牡丹江市"),
    ("黑龙江公安警官职业学院", "哈尔滨市"), ("哈尔滨城市职业学院", "哈尔滨市"),
    ("黑龙江农垦科技职业学院", "哈尔滨市"), ("黑龙江旅游职业技术学院", "哈尔滨市"),
    ("黑龙江三江美术职业学院", "佳木斯市"), ("黑龙江生态工程职业学院", "哈尔滨市"),
    ("黑龙江能源职业学院", "双鸭山市"), ("七台河职业学院", "七台河市"),
    ("黑龙江民族职业学院", "哈尔滨市"), ("大庆医学高等专科学校", "大庆市"),
    ("黑龙江交通职业技术学院", "哈尔滨市"), ("哈尔滨应用职业技术学院", "哈尔滨市"),
    ("黑龙江幼儿师范高等专科学校", "牡丹江市"), ("哈尔滨科学技术职业学院", "哈尔滨市"),
    ("佳木斯职业学院", "佳木斯市"), ("黑龙江护理高等专科学校", "哈尔滨市"),
    ("齐齐哈尔理工职业学院", "齐齐哈尔市"), ("哈尔滨幼儿师范高等专科学校", "哈尔滨市"),
    ("黑龙江冰雪体育职业学院", "哈尔滨市"), ("哈尔滨北方航空职业技术学院", "哈尔滨市"),
]


def _slug(name, index):
    if name == "黑龙江八一农垦大学":
        return "hlby"
    return f"hlj-{index:03d}"


DEFAULT_PROFILES = []
for _i, (_name, _city) in enumerate(_SCHOOL_ROWS, 1):
    _p = _profile(_name, _city)
    _p["id"] = _slug(_name, _i)
    DEFAULT_PROFILES.append(_p)


def _clean_custom(item):
    if not isinstance(item, dict):
        raise ValueError("学校配置必须是对象")
    name = str(item.get("name") or "").strip()
    city = str(item.get("city") or "").strip()
    if len(name) < 2 or len(name) > 80 or not city:
        raise ValueError("学校名称和城市不能为空")
    school_id = str(item.get("id") or "").strip().lower()
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{1,48}", school_id):
        school_id = "custom-" + re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:35]
    return {
        "id": school_id, "name": name, "city": city,
        "website": str(item.get("website") or "").strip()[:300],
        "campus_location": str(item.get("campus_location") or "").strip(),
        "radius": max(100, min(50000, int(item.get("radius", 3000)))),
    }


def list_profiles(cfg=None):
    profiles = [copy.deepcopy(p) for p in DEFAULT_PROFILES]
    for raw in (cfg or {}).get("custom_profiles", []) if isinstance(cfg, dict) else []:
        try:
            custom = _clean_custom(raw)
        except (TypeError, ValueError):
            continue
        profiles = [p for p in profiles if p["id"] != custom["id"]]
        profiles.append(custom)
    overrides = (cfg or {}).get("profile_overrides", {}) if isinstance(cfg, dict) else {}
    if isinstance(overrides, dict):
        for profile in profiles:
            patch = overrides.get(profile["id"])
            if isinstance(patch, dict):
                for key in ("city", "name", "website", "campus_location", "radius"):
                    if key in patch and patch[key] not in (None, ""):
                        profile[key] = patch[key]
    return profiles


def get_profile(school_id, cfg=None):
    sid = str(school_id or "").strip()
    return next((p for p in list_profiles(cfg) if p["id"] == sid), None)


def apply_profile(cfg, school_id):
    updated = dict(cfg or {})
    profile = get_profile(school_id, updated)
    if profile is None:
        raise ValueError("未找到这所学校，请先添加学校配置")
    updated["current_school_id"] = profile["id"]
    updated["amap_city"] = profile["city"]
    updated["amap_campus_name"] = profile["name"]
    updated["amap_campus_location"] = profile.get("campus_location", "")
    updated["amap_radius"] = profile.get("radius", 3000)
    return updated


def add_custom_profile(cfg, data):
    updated = dict(cfg or {})
    profile = _clean_custom(data)
    existing = [p for p in updated.get("custom_profiles", []) if isinstance(p, dict) and p.get("id") != profile["id"]]
    existing.append(profile)
    updated["custom_profiles"] = existing
    return updated, profile


def update_profile_override(cfg, school_id, changes):
    updated = dict(cfg or {})
    profile = get_profile(school_id, updated)
    if profile is None:
        raise ValueError("未找到当前学校模板")
    overrides = dict(updated.get("profile_overrides") or {})
    patch = dict(overrides.get(profile["id"]) or {})
    for key in ("city", "name", "website", "campus_location", "radius"):
        if key in changes and changes[key] not in (None, ""):
            patch[key] = changes[key]
    overrides[profile["id"]] = patch
    updated["profile_overrides"] = overrides
    return updated


def public_profile(profile, current_id):
    out = {k: profile.get(k, "") for k in ("id", "name", "city", "website", "campus_location", "radius")}
    out["current"] = profile.get("id") == current_id
    return out
