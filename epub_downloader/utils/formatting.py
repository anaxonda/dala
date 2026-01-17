import asyncio
from bs4 import BeautifulSoup
from pygments import highlight
from pygments.lexers import guess_lexer
from typing import List, Dict, Optional

from . .models import log, HN_API_BASE_URL
from . .core.session import fetch_with_retry

def _enrich_comment_tree(roots: List[Dict]) -> List[Dict]:
    """Pre-calculate parent/sibling/root IDs for EPUB navigation buttons."""
    if not roots: return []
    for i in range(len(roots) - 1):
        roots[i]['next_root_id'] = str(roots[i+1].get('id'))
    def recurse(nodes, parent_id, root_id, next_root_id):
        for i, node in enumerate(nodes):
            node['parent_id'] = parent_id
            node['root_id'] = root_id
            node['next_root_id'] = next_root_id
            if i < len(nodes) - 1:
                node['next_sibling_id'] = str(nodes[i+1].get('id'))
            if node.get('children_data'):
                recurse(node['children_data'], str(node.get('id')), root_id or str(node.get('id')), next_root_id)
    for root in roots:
        next_r = root.get('next_root_id')
        if root.get('children_data'):
             recurse(root['children_data'], str(root.get('id')), str(root.get('id')), next_r)
    return roots

async def fetch_comments_recursive(session, comment_ids, fetched_data, max_depth, current_depth=0):
    if not comment_ids or (max_depth is not None and current_depth >= max_depth): return []
    tasks = []
    valid_ids = [cid for cid in comment_ids if cid not in fetched_data]
    for cid in valid_ids:
        url = f"{HN_API_BASE_URL}item/{cid}.json"
        tasks.append(fetch_with_retry(session, url))
    if not tasks: return []
    results = await asyncio.gather(*tasks)
    child_tasks = []
    comments = []
    for i, (data, _) in enumerate(results):
        if not data: continue
        cid = valid_ids[i]
        fetched_data[cid] = data
        if not data.get('deleted') and not data.get('dead'):
            data['children_data'] = []
            data['id'] = str(data.get('id'))
            comments.append(data)
            if data.get('kids'):
                t = fetch_comments_recursive(session, data['kids'], fetched_data, max_depth, current_depth + 1)
                child_tasks.append((data, t))
    if child_tasks:
        res = await asyncio.gather(*(t[1] for t in child_tasks))
        for i, (parent, _) in enumerate(child_tasks):
            parent['children_data'] = res[i]
    return comments

def format_comment_html(comment_data, formatter, depth=0):
    auth = comment_data.get('by', '[deleted]')
    text = comment_data.get('text', '')
    cid = comment_data.get('id')
    pid = comment_data.get('parent_id')
    nsid = comment_data.get('next_sibling_id')
    rid = comment_data.get('root_id')
    nrid = comment_data.get('next_root_id')

    def make_btn(target_id, symbol, title):
        if target_id: return f'<a href="#c_{target_id}" class="nav-btn" title="{title}">{symbol}</a>'
        else: return f'<span class="nav-btn ghost">{symbol}</span>'

    btns = [make_btn(pid, "↑", "Parent"), make_btn(nsid, "→", "Next Sibling"), make_btn(rid if depth > 1 else None, "⏮", "Thread Root"), make_btn(nrid, "⏭", "Next Thread")]
    nav_bar = f'<div class="nav-bar">{"".join(btns)}</div>'

    if '<pre>' in text:
        soup = BeautifulSoup(text, 'html.parser')
        for pre in soup.find_all('pre'):
            try:
                code = pre.get_text()
                lexer = guess_lexer(code)
                hl = highlight(code, lexer, formatter)
                pre.replace_with(BeautifulSoup(hl, 'html.parser'))
            except: pass
        text = str(soup)

    capped_depth = min(depth, 5)
    margin = capped_depth * 10
    border_style = f"border-left: 2px solid #ccc;" if depth > 0 else ""
    padding = 10 if depth < 6 else 2
    style = f"{border_style} padding-left: {padding}px; margin-left: {margin}px; margin-bottom: 15px;"
    if depth == 0: style = "margin-bottom: 20px;"

    header = f'<div class="comment-header"><div class="comment-author"><div class="comment-author-inner">{auth}</div></div><div class="nav-bar">{nav_bar}</div></div>'
    html = f'<div id="c_{cid}" style="{style}">{header}<div class="comment-body">{text}</div>'
    if comment_data.get('children_data'):
        for child in comment_data['children_data']:
            html += format_comment_html(child, formatter, depth + 1)
    html += '</div>'
    return html
