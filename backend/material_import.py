"""用户提供资料的无损正文提取、带上下文切分和可重复入库。"""
import re
import struct
import hashlib
from datetime import datetime


def word_main_text(word, table):
    """按 MS-DOC FIB/CLX piece table 读取主文档；不扫描已删除文本或执行宏。"""
    u16 = lambda data, pos: struct.unpack_from('<H', data, pos)[0]
    u32 = lambda data, pos: struct.unpack_from('<I', data, pos)[0]
    if u16(word, 0) != 0xa5ec or u16(word, 2) < 0xc1 and u16(word, 2) != 0:
        raise ValueError('仅支持 Word 97 及以后的二进制 DOC')
    if u16(word, 10) & 0x0100:
        raise ValueError('手册已加密，无法直接提取')
    pos = 34 + 2 * u16(word, 32)
    lw = pos + 2
    main_count = u32(word, lw + 12)
    pairs = lw + 4 * u16(word, pos) + 2
    fc, length = struct.unpack_from('<II', word, pairs + 33 * 8)
    clx = table[fc:fc + length]
    pos = 0
    while pos < len(clx) and clx[pos] == 1:
        pos += 3 + u16(clx, pos + 1)
    if pos >= len(clx) or clx[pos] != 2:
        raise ValueError('缺少 DOC 正文片段表')
    size = u32(clx, pos + 1)
    plc = clx[pos + 5:pos + 5 + size]
    if len(plc) != size or (size - 4) % 12:
        raise ValueError('DOC 正文片段表长度无效')
    n = (size - 4) // 12
    cp = struct.unpack_from('<' + 'I' * (n + 1), plc)
    if cp[0] != 0 or cp[-1] < main_count or any(b < a for a, b in zip(cp, cp[1:])):
        raise ValueError('DOC 字符位置不完整')
    parts, covered = [], 0
    for i in range(n):
        if cp[i] >= main_count:
            break
        count = min(cp[i + 1], main_count) - cp[i]
        offset = u32(plc, 4 * (n + 1) + i * 8 + 2)
        compressed = bool(offset & 0x40000000)
        offset &= 0x3fffffff
        start = offset // 2 if compressed else offset
        raw = word[start:start + count * (1 if compressed else 2)]
        if len(raw) != count * (1 if compressed else 2):
            raise ValueError('DOC 正文片段被截断')
        if compressed:
            parts.append(''.join(bytes([b]).decode('cp1252') if b not in (0x81,0x8d,0x8f,0x90,0x9d) else chr(b) for b in raw))
        else:
            parts.append(raw.decode('utf-16le'))
        covered += count
    if covered != main_count:
        raise ValueError('DOC 主文档未完整提取')
    return ''.join(parts), {'main_characters': main_count, 'covered_characters': covered, 'pieces': n}


def clean_word_text(text):
    # Word 域：保留显示结果，丢弃 TOC/HYPERLINK 等域代码；支持嵌套。
    stack, out = [], []
    for ch in text:
        if ch == '\x13': stack.append(False)
        elif ch == '\x14' and stack: stack[-1] = True
        elif ch == '\x15' and stack: stack.pop()
        elif all(stack): out.append(ch)
    text = ''.join(out).replace('\x07', ' | ').replace('\r', '\n').replace('\x0b', '\n').replace('\x0c', '\n')
    text = re.sub(r'[\x00-\x08\x0e-\x1f]', '', text)
    return '\n'.join(line.strip() for line in text.splitlines()).strip()


def context_chunks(body, prefix, size=1400):
    """按行/表格行分块，每块重复上下文、表头和本节注释，不拆散价格行。"""
    if len(body.strip()) <= size:
        return [prefix + '\n' + body.strip()]
    lines = body.strip().splitlines()
    notes = '\n'.join(l for l in lines if l.lstrip().startswith('>'))
    prefix = prefix + ('\n' + notes if notes else '')
    chunks, cur, header = [], [], []
    def flush():
        if any(line.strip() for line in cur):
            chunks.append(prefix + '\n' + '\n'.join(cur))
        cur.clear()
    i = 0
    while i < len(lines):
        line = lines[i]
        is_table = line.strip().startswith('|')
        if is_table and i + 1 < len(lines) and re.fullmatch(r'[\s|:\-]+', lines[i+1]) and '-' in lines[i+1]:
            flush()
            header = [line, lines[i+1]]
            cur.extend(header)
            i += 2
            continue
        if not is_table and header:
            flush()
            header = []
        if cur and len('\n'.join(cur)) + len(line) > size:
            flush()
            cur.extend(header)
        cur.append(line)
        i += 1
    flush()
    return chunks


def _document(title, body, source_name, kind, context=''):
    key = hashlib.sha256(title.encode('utf-8')).hexdigest()[:16]
    source = f'用户提供:{source_name}#section-{key}'
    version = ('文件版本标识：20241020（来自文件名，不等于当前有效版本）。请核对后续修订。'
               if kind == '本科生手册' else
               '用户整理的食堂资料，采集日期未提供；价格、份量、营业时段以餐口实际为准。\n'
               '标题数字沿用原文列表序号，未另行核实实际窗口号。并列价格未注明杯型或份量时，具体规格未知。')
    prefix = f'{title}\n来源文件：{source_name}\n{version}'
    if context.strip(): prefix += '\n' + context.strip()
    return {'title': title, 'source': source, 'ktype': kind,
            'content': prefix + '\n\n' + body.strip(), 'chunks': context_chunks(body, prefix)}


def menu_documents(text, source_name):
    stack, sections = [], []
    for line in text.splitlines():
        match = re.match(r'^(#{1,6})\s+(.+)', line)
        bold = re.fullmatch(r'\*\*(.+?)[：:]?\*\*\s*',line.strip())
        if match or bold:
            level = len(match[1]) if match else 4
            heading = match[2].strip() if match else bold[1].strip()
            while stack and stack[-1]['level'] >= level: stack.pop()
            node = {'level':level,'heading':heading,'body':[],'parents':list(stack)}
            sections.append(node); stack.append(node)
        elif stack: stack[-1]['body'].append(line)
    docs=[]
    for node in sections:
        body='\n'.join(node['body']).strip()
        if not body or not any(l.strip() and not l.lstrip().startswith('>') for l in node['body']): continue
        path=[x['heading'] for x in node['parents']]+[node['heading']]
        title='[食堂] '+' / '.join(path)
        inherited='\n'.join('\n'.join(n['body']).strip() for n in node['parents'] if any(l.strip() for l in n['body']))
        docs.append(_document(title,body,source_name,'食堂资料',inherited))
    if not docs: raise ValueError('食堂资料未找到可导入正文')
    return docs


def handbook_documents(text, source_name):
    """以手册目录校验全部规章边界，再按章、节和完整条款组织检索条目。"""
    lines=text.splitlines()
    toc=[]
    for i,line in enumerate(lines):
        m=re.match(r'^[一二三四五六七八九十]+、(.+?)\t\d+$',line)
        if m: toc.append((i,m[1]))
    norm=lambda s: re.sub(r'\s+','',s)
    boundaries=[]
    if toc:
        cursor=toc[-1][0]+1
        for _,title in toc:
            found=None
            for i in range(cursor,len(lines)):
                if not lines[i].strip(): continue
                for width in range(1,4):
                    if norm(''.join(lines[i:i+width]))==norm(title):
                        found=(i,width,title);break
                if found: break
            if not found: raise ValueError(f'手册目录未匹配正文：{title}')
            boundaries.append(found);cursor=found[0]+found[1]
    else:
        for i,line in enumerate(lines):
            if len(line)<85 and re.search(r'(办法|规定|细则|制度|指南|须知)(?:（[^）]+）)?$',line.strip()) and not re.match(r'^第.+条',line):
                boundaries.append((i,1,line.strip()))
    if not boundaries: raise ValueError('未识别手册规章标题')
    docs=[]
    front_end=toc[0][0] if toc else boundaries[0][0]
    if front_end:
        body='\n'.join(lines[:front_end]).strip()
        if body: docs.append(_document('[手册20241020] 前言与学校简介',body,source_name,'本科生手册'))
    for idx,(start,width,policy) in enumerate(boundaries):
        end=boundaries[idx+1][0] if idx+1<len(boundaries) else len(lines)
        body='\n'.join(lines[start+width:end]).strip()
        scope='\n'.join(l for l in body.splitlines() if re.search(r'适用于|自.{0,25}(施行|试行)|试行期|有效期',l))
        context='版本与适用范围原文摘录：\n'+scope if scope else ''
        chapter,section,blocks='','',[]
        for line in body.splitlines():
            if re.match(r'^第[一二三四五六七八九十百零]+章\s*',line):
                chapter=line;section=''
            elif re.match(r'^第[一二三四五六七八九十百零]+节\s*',line): section=line
            if not blocks or (chapter,section)!=blocks[-1][0]: blocks.append(((chapter,section),[]))
            blocks[-1][1].append(line)
        for (chapter,section),block_lines in blocks:
            # 条款中的分项紧随本条；避免将前提条件与后半条脱离。
            units=[]
            for line in block_lines:
                if not units or re.match(r'^第[一二三四五六七八九十百零]+条\s*',line): units.append([])
                units[-1].append(line)
            if not any(re.match(r'^第[一二三四五六七八九十百零]+条\s*',line) for line in block_lines):
                units=[[line] for line in block_lines]
            packs,cur=[],[]
            for unit in units:
                unit='\n'.join(unit).strip()
                if not unit: continue
                if cur and sum(len(x) for x in cur)+len(unit)>1500:
                    packs.append('\n'.join(cur));cur=[]
                cur.append(unit)
            if cur: packs.append('\n'.join(cur))
            for n,pack in enumerate(packs):
                title='[手册20241020] '+' / '.join(x for x in (policy,chapter,section) if x)
                if len(packs)>1:
                    articles=re.findall(r'^第[一二三四五六七八九十百零]+条',pack,re.M)
                    title+=' / '+('—'.join(dict.fromkeys([articles[0],articles[-1]])) if articles else f'正文{n+1}')
                d=_document(title,pack,source_name,'本科生手册',context)
                # 完整条款已在条目边界分组，避免通用切块再次截断分项。
                d['chunks']=[d['content']]
                docs.append(d)
    return docs


def upsert_documents(conn, documents):
    """单事务替换相同来源的分块；保留知识编号与点击数，不改其他来源。"""
    from rag import active_school_id
    school_id = active_school_id()
    ids=[]
    with conn:
        for d in documents:
            row=conn.execute('SELECT id FROM knowledge WHERE source=? AND school_id=?',(d['source'],school_id)).fetchone()
            stamp=datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            if row:
                doc_id=row[0]
                conn.execute('UPDATE knowledge SET title=?,content=?,ktype=?,created_at=? WHERE id=?',
                             (d['title'],d['content'],d['ktype'],stamp,doc_id))
                conn.execute('DELETE FROM knowledge_chunks WHERE doc_id=?',(doc_id,))
            else:
                doc_id=conn.execute('INSERT INTO knowledge(title,source,content,ktype,school_id,created_at) VALUES(?,?,?,?,?,?)',
                                    (d['title'],d['source'],d['content'],d['ktype'],school_id,stamp)).lastrowid
            conn.executemany('INSERT INTO knowledge_chunks(doc_id,chunk_index,content) VALUES(?,?,?)',
                             [(doc_id,i,ch) for i,ch in enumerate(d['chunks'])])
            ids.append(doc_id)
    return ids
