"""科研检索/解析工具示例（无 Key 即可用部分）。

- arxiv_search: 检索 arXiv（无需 API Key）
- extract_pdf_text: 解析 PDF 全文（PyMuPDF，支持本地/URL）

可后续接入 LangGraph 工具节点（@tool / ToolNode），配合 Tavily 联网搜索
（需 TAVILY_API_KEY，见 .env.example）。
"""
from __future__ import annotations

import io
from urllib.request import urlopen


def arxiv_search(query: str, max_results: int = 5) -> list[dict]:
    """按 query 检索 arXiv，返回 [{id, title, authors, published, summary}]。"""
    import arxiv

    client = arxiv.Client(page_size=max_results, delay_seconds=3, num_retries=2)
    results = []
    for r in client.results(arxiv.Search(query=query, max_results=max_results)):
        results.append({
            "id": r.entry_id,
            "title": r.title,
            "authors": [a.name for a in r.authors][:5],
            "published": str(r.published.date()),
            "summary": r.summary.replace("\n", " ")[:400],
        })
    return results


def extract_pdf_text(source: str | bytes, max_pages: int | None = None) -> str:
    """从本地路径或 URL 读取 PDF 并抽取全文文本（PyMuPDF）。"""
    import fitz  # PyMuPDF

    if isinstance(source, bytes):
        doc = fitz.open(stream=source, filetype="pdf")
    elif source.startswith(("http://", "https://")):
        with urlopen(source, timeout=30) as resp:
            doc = fitz.open(stream=resp.read(), filetype="pdf")
    else:
        doc = fitz.open(source)

    pages = list(doc)[:max_pages] if max_pages else list(doc)
    text = "\n\n".join(page.get_text() for page in pages)
    doc.close()
    return text


def download_pdf_bytes(url: str) -> bytes:
    """下载 PDF 到内存字节流。"""
    with urlopen(url, timeout=30) as resp:
        return resp.read()


if __name__ == "__main__":
    import sys

    q = sys.argv[1] if len(sys.argv) > 1 else "large language model agents"
    print(f"=== arXiv 检索: {q} ===")
    for item in arxiv_search(q, max_results=3):
        print(f"- {item['title']}\n  {item['id']} | {', '.join(item['authors'])} | {item['published']}")
        print(f"  {item['summary'][:150]}...")
