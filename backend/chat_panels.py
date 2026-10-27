"""按需在聊天消息里呈现管理操作；持久化事件不含表单或密钥。"""
import re

PANELS = {
    "services": ("校园办事", "可以在这里查办事事项、部门和联系人，也可以直接告诉我你要办什么事。"),
    "knowledge": ("知识库", "这里可以查看、检索和补充知识资料。"),
    "memory": ("长期记忆", "这里可以查看、补充或删除我记住的信息。"),
    "learning": ("学习与审核", "这里可以提炼当前对话，并审核知识；采纳后才会进入知识库。"),
    "settings": ("设置", "请在下方卡片管理学校模板。其他服务配置由系统统一管理。"),
}


def match_panel_request(message):
    text = re.sub(r"[\s，。！!？?：:]", "", message).lower()
    text = re.sub(r"^(请帮我|帮我|请)", "", text)
    aliases = {
        "services": ["办事导航", "查看办事导航", "打开办事导航", "校园办事导航"],
        "knowledge": ["知识库", "查看知识库", "打开知识库", "管理知识库", "添加知识", "上传资料", "知识库状态", "检查知识库状态", "检查一下知识库状态"],
        "memory": ["记忆", "查看记忆", "打开记忆", "管理记忆", "看看你记住了我什么", "你记住了我什么", "整理记忆"],
        "learning": ["学习中心", "审核学习内容", "审核知识", "查看待审核知识", "学习这段对话", "结束并学习", "提炼当前对话"],
        "settings": ["设置", "修改设置", "打开设置", "查看设置", "切换学校", "切换学校模板", "学校模板", "学校设置", "选择学校", "更换学校", "换学校", "添加学校", "管理学校", "配置学校"],
    }
    panel = next((panel for panel, terms in aliases.items() if text in terms), None)
    if panel:
        return panel
    # 支持“我想换学校”“帮我切换学校模板”这类自然说法，同时避免把其他学校的校规咨询误判为设置操作。
    if any(term in text for term in ("切换学校", "更换学校", "换学校", "学校模板", "选择学校", "添加学校")):
        return "settings"
    return None


def open_panel(panel):
    if panel not in PANELS:
        raise ValueError("不支持的对话操作卡片")
    title, message = PANELS[panel]
    return {"ui_panel": panel, "title": title, "message": message}
