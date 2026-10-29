"""基于已入库菜单的随机正餐推荐，不生成菜品、价格或营业状态。"""
import random
import re

from db import get_conn
from retrieval_rules import source_meta


def _active_school_id():
    try:
        from rag import active_school_id
        return active_school_id()
    except Exception:
        return "hlby"


def is_food_request(message):
    q = re.sub(r'\s+', '', str(message or ''))
    if re.search(r'不要推荐|不需要推荐|不用推荐|通知|请假|成绩|课表|吃什么药|吃啥药|术后|过敏|忌口|素食|不吃|不能吃', q):
        return False
    return bool(re.search(r'(?:吃什么|吃点什么|吃啥|吃点啥)|(?:推荐.*(?:吃的|食堂|午饭|晚饭|午餐|晚餐|美食))|(?:(?:午饭|晚饭|午餐|晚餐|食堂).*推荐)', q))


def is_food_followup(message):
    return bool(re.fullmatch(r'(?:再|给我|帮我)?(?:换一批|换几个|换几个推荐|重新推荐|再推荐几个)[吧呀啊！!。？?]*', re.sub(r'\s+', '', str(message or ''))))


_NAME = re.compile(r'菜品|产品|品名|名称|内容|菜名|套餐|计价方式|品种')
_PRICE = re.compile(r'价格|售价|单价|加量|大碗|小碗|大份|小份|标准')
_SIDE = re.compile(r'加料|单加|加品|卤味|小食|饮品|奶茶|水吧|豆浆|早餐|早点|甜品')
_MEAL = re.compile(r'饭|面|粉|饺|馄饨|套餐|汉堡|石锅|米线|焖锅|烤盘|麻辣烫|麻辣拌|轻食|蘸水菜|煎饼|称重餐')


def _location(title):
    parts = title.split(' / ')
    hall = re.search(r'[一二]食堂', title)
    floor = re.search(r'[一二三]楼', title)
    window_index = next((i for i, part in enumerate(parts) if re.match(r'^\d+[.、]', part)), None)
    if not hall or not floor or window_index is None:
        return None
    window = re.sub(r'^\d+[.、]\s*', '', parts[window_index])
    categories = parts[window_index + 1:]
    if any(_SIDE.search(part) for part in categories) or (not categories and _SIDE.search(window)):
        return None
    return hall[0] + floor[0] + ' · ' + window, window


def _menu_rows(content):
    """按原表头映射多组菜名/价格和加量列，保留原单位及并列价格。"""
    headers = []
    for line in content.splitlines():
        if not line.strip().startswith('|'):
            headers = []
            continue
        cells = [c.strip() for c in line.strip().strip('|').split('|')]
        if all(re.fullmatch(r'[:\-\s]*', c) for c in cells):
            continue
        if any(_PRICE.search(c) for c in cells) and any(_NAME.search(c) for c in cells):
            headers = cells
            continue
        if not headers or len(cells) != len(headers):
            continue
        name_cols = [i for i,h in enumerate(headers) if _NAME.search(h) and not _PRICE.search(h)]
        for pos, i in enumerate(name_cols):
            stop = name_cols[pos+1] if pos+1 < len(name_cols) else len(headers)
            prices = [(headers[j], cells[j]) for j in range(i+1, stop) if _PRICE.search(headers[j]) and re.search(r'\d',cells[j])]
            if not cells[i] or not prices:
                continue
            formatted = []
            for label,value in prices:
                value = value if re.search(r'元|¥|￥',value) else value + '元'
                formatted.append((label + '：' if len(prices)>1 else '') + value)
            yield cells[i], '；'.join(formatted), [v for _,v in prices]


def recommend_food(query='', exclude_names=(), rng=None):
    rng = rng or random.SystemRandom()
    q = str(query or '').replace('1食堂','一食堂').replace('2食堂','二食堂').replace('1楼','一楼').replace('2楼','二楼').replace('3楼','三楼')
    halls = set(re.findall(r'[一二]食堂', q))
    floors = set(re.findall(r'[一二三]楼', q))
    budget = re.search(r'(\d+(?:\.\d+)?)\s*元\s*(?:以内|以下|内|左右)|(?:预算|不超过|最多)\s*(\d+(?:\.\d+)?)', q)
    cap = float(next(g for g in budget.groups() if g)) if budget else None
    candidates, seen = [], set()
    conn = get_conn()
    school_id = _active_school_id()
    try:
        menu_rows = conn.execute(
            "SELECT id,title,content,source,ktype,created_at FROM knowledge "
            "WHERE ktype='食堂资料' AND (school_id=? OR school_id='common') ORDER BY id",
            (school_id,),
        )
    except Exception as error:
        if "school_id" not in str(error):
            raise
        menu_rows = conn.execute("SELECT id,title,content,source,ktype,created_at FROM knowledge WHERE ktype='食堂资料' ORDER BY id")
    for row in menu_rows:
        location = _location(row['title'])
        if not location or not row['source']:
            continue
        location, window = location
        if halls and not any(h in location for h in halls): continue
        if floors and not any(f in location for f in floors): continue
        for name, price, values in _menu_rows(row['content']):
            dish = window + '（' + name + '）' if '自选' in window and '自选' in name else name
            if not _MEAL.search(dish) or re.match(r'^(?:单加|加肉|加饭|加面|米饭$|面条$|粉条$)',dish): continue
            if _SIDE.search(dish): continue
            if cap is not None:
                # 称重/按个价格不能被当作一份正餐的预算。
                if any(re.search(r'/|每|两|斤|克|g|个|根',v,re.I) for v in values): continue
                amounts = [float(n) for v in values for n in re.findall(r'\d+(?:\.\d+)?',v)]
                if not amounts or max(amounts)>cap: continue
            key = (location, dish)
            if key in seen: continue
            seen.add(key)
            candidates.append({'dish':dish,'price':price,'location':location,
                               'doc_id':row['id'],'title':row['title'],'source':row['source'],
                               **source_meta({'source':row['source'],'ktype':row['ktype'],'indexed_at':row['created_at']},row['content'])})
    fresh = [c for c in candidates if c['dish'] not in set(exclude_names or ())]
    if len(fresh)>=3: candidates=fresh
    rng.shuffle(candidates)
    target = rng.randint(3,5)
    selected, dishes, windows = [], set(), set()
    # 第一遍分散档口；候选少时再补同档口的不同菜品。
    for distinct_window in (True, False):
        for candidate in candidates:
            if len(selected)>=target: break
            if candidate['dish'] in dishes or distinct_window and candidate['location'] in windows: continue
            selected.append(candidate); dishes.add(candidate['dish']); windows.add(candidate['location'])
    return {'query':str(query or ''),'count':len(selected),'results':selected,
            'note':'来自已收录菜单，价格、份量和营业情况以餐口现场为准。'}


def format_recommendations(result):
    rows=result['results']
    if not rows:
        return '当前没有找到符合条件的食堂正餐菜单，暂时无法推荐。可以放宽食堂、楼层或预算后再试。'
    text=f"这次随机给你挑了 **{len(rows)}个选择**：\n\n"
    for n,row in enumerate(rows,1):
        text+=f"{n}. **{row['dish']}** — {row['price']}\n   {row['location']}（资料#{row['doc_id']}）\n\n"
    if len(rows)<3: text+='符合条件的已收录菜品不足3个，以上为实际找到的选择。\n\n'
    return text+'想换口味就说「换一批」。价格和营业情况以餐口现场为准。'
