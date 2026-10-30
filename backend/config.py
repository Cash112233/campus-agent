"""配置管理：保存在 backend/config.json"""
import json
import os

CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")

DEFAULT_CONFIG = {
    "api_key": "",                        # DeepSeek API Key，由服务端配置
    "current_school_id": "hlby",         # 当前学校模板；知识库和地图按学校隔离
    "custom_profiles": [],                # 用户新增学校配置，不含密钥
    "profile_overrides": {},              # 学校补充的官网/坐标/半径配置
    "amap_key": "",                       # 高德 Web 服务 Key，与大模型密钥分开
    "amap_city": "大庆市",
    "amap_campus_name": "黑龙江八一农垦大学",
    "amap_campus_location": "",            # 留空时用高德校名精确匹配，不预置猜测坐标
    "amap_radius": 3000,
    "volcengine_api_key": "",             # 火山引擎联网搜索 Key，仅服务端使用
    "web_search_default_days": 7,          # 联网搜索默认时间窗口（天）
    "base_url": "https://api.deepseek.com",
    "model": "deepseek-flash",            # deepseek-flash / deepseek-v4-pro
    "temperature": 0.7,
    "max_history": 20,                    # 携带的对话历史条数
    "auto_learn": False,                  # 自主学习：对话轮次达标后自动提炼
    "auto_learn_threshold": 4,            # 自动学习触发轮数
    "host": "127.0.0.1",
    "port": 8300,
    "open_browser": True,
    "access_token": "",                   # 访问口令：非空时所有 API 需携带（公网部署必设）
    "lock_config": False,                 # 部署锁定：禁止通过页面修改 base_url（防密钥外发）
}


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                cfg.update(json.load(f))
        except Exception:
            pass
    # 云端部署：环境变量优先（Render 等平台注入，敏感信息不落盘）
    _env = {
        "api_key": "DEEPSEEK_API_KEY",
        "access_token": "ACCESS_TOKEN",
        "amap_key": "AMAP_KEY",
        "volcengine_api_key": "VOLCENGINE_API_KEY",
        "current_school_id": "SCHOOL_ID",
        "lock_config": "LOCK_CONFIG",
    }
    for key, env in _env.items():
        val = os.environ.get(env)
        if val:
            cfg[key] = val.lower() in ("1", "true", "yes") if key == "lock_config" else val
    return cfg


def save_config(cfg):
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
