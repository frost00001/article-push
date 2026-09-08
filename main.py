#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
文献自动订阅器：arXiv + PubMed → 关键词粗筛 → DeepSeek 精筛+中文摘要 → 下载 PDF → 邮件推送

用法：
  python main.py            # 按 config.yaml 的 dry_run 执行（默认只打印+下载，不发邮件）
  python main.py --send     # 忽略 dry_run，真正发邮件
  python main.py --no-llm   # 跳过 DeepSeek，仅关键词筛选（调试用）
"""

import os
import re
import sys
import json
import datetime as dt
from pathlib import Path

import yaml
import requests
import arxiv
from dotenv import load_dotenv
from openai import OpenAI

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")
PDF_DIR = BASE_DIR / "pdfs"
SEEN_FILE = BASE_DIR / "seen.json"

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) lit-digest/0.1"}

# 强制 UTF-8 输出，避免中文在 Windows 控制台乱码
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def log(*a):
    print("[lit-digest]", *a, flush=True)


def load_config():
    with open(BASE_DIR / "config.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_seen():
    try:
        return set(json.loads(SEEN_FILE.read_text(encoding="utf-8")))
    except Exception:
        return set()


def save_seen(seen):
    SEEN_FILE.write_text(json.dumps(sorted(seen)), encoding="utf-8")


def _arxiv_pdf(r):
    """arxiv 4.x 不再提供 pdf_url，改从 links 取，或由 entry_id 构造。"""
    for link in (r.links or []):
        if (link.title or "").lower() == "pdf":
            return link.href
    return r.entry_id.replace("/abs/", "/pdf/")


# ---------------- 抓取 arXiv ----------------
def fetch_arxiv(cfg):
    papers = []
    since = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=cfg["lookback_days"])
    client = arxiv.Client()
    for cat in cfg["arxiv_categories"]:
        try:
            search = arxiv.Search(
                query=f"cat:{cat}",
                max_results=cfg["arxiv_max_per_cat"],
                sort_by=arxiv.SortCriterion.SubmittedDate,
            )
            for r in client.results(search):
                pub = r.published
                if pub.tzinfo is None:
                    pub = pub.replace(tzinfo=dt.timezone.utc)
                if pub < since:
                    continue
                papers.append({
                    "id": r.entry_id,
                    "title": re.sub(r"\s+", " ", r.title).strip(),
                    "abstract": re.sub(r"\s+", " ", r.summary).strip(),
                    "url": r.entry_id,
                    "pdf_url": _arxiv_pdf(r),
                    "doi": (r.doi or "").strip(),
                    "source": "arxiv",
                    "published": pub.isoformat(),
                })
        except Exception as e:
            log(f"arXiv {cat} 抓取失败: {e}")
    return papers


# ---------------- 抓取 PubMed ----------------
def fetch_pubmed(cfg):
    papers = []
    E = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
    try:
        r = requests.get(f"{E}/esearch.fcgi", params={
            "db": "pubmed", "term": cfg["pubmed_query"],
            "reldate": cfg["lookback_days"], "datetype": "pdat",
            "retmax": cfg["pubmed_max"], "retmode": "json", "sort": "date",
        }, timeout=30)
        r.raise_for_status()
        ids = r.json().get("esearchresult", {}).get("idlist", [])
    except Exception as e:
        log(f"PubMed esearch 失败: {e}")
        return papers
    if not ids:
        return papers
    try:
        r2 = requests.get(f"{E}/esummary.fcgi", params={
            "db": "pubmed", "id": ",".join(ids), "retmode": "json",
        }, timeout=30)
        r2.raise_for_status()
        res = r2.json().get("result", {})
    except Exception as e:
        log(f"PubMed esummary 失败: {e}")
        return papers
    for pid in ids:
        it = res.get(pid)
        if not isinstance(it, dict):
            continue
        doi = ""
        for aid in it.get("articleids", []):
            if aid.get("idtype") == "doi":
                doi = aid.get("value", "")
                break
        papers.append({
            "id": f"pmid:{pid}",
            "title": re.sub(r"\s+", " ", it.get("title", "")).strip(),
            "abstract": re.sub(r"\s+", " ", it.get("abstract", "")).strip(),
            "url": f"https://pubmed.ncbi.nlm.nih.gov/{pid}/",
            "pdf_url": "",
            "doi": doi,
            "source": "pubmed",
            "published": "",
        })
    return papers


# ---------------- 关键词粗筛 ----------------
def coarse_filter(papers, cfg):
    kws = [k.lower() for k in cfg["keywords"]]
    return [p for p in papers if any(k in (p["title"] + " " + p["abstract"]).lower() for k in kws)]


# ---------------- DeepSeek 精筛 + 中文摘要 ----------------
def llm_filter_summarize(papers, cfg):
    client = OpenAI(
        api_key=os.environ["DEEPSEEK_API_KEY"],
        base_url=os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
    )
    items = [{"i": i, "title": p["title"], "abstract": p["abstract"][:600]} for i, p in enumerate(papers)]
    sys_prompt = "你是科研助手，只输出合法 JSON，不要输出任何其它文字。"
    user_prompt = (
        "下面是若干论文。请筛选出与「脑科学 / 波动光学 / 光学观测脑（fNIRS、双光子、OCT 等）/ 生物医学 / 生物物理学 / 电磁学」相关的论文，"
        "并为每篇相关论文写一句中文摘要（20~40字，说明这篇在做什么、用了什么方法）。\n"
        "注意：不要选纯 AI / 深度学习分类类的医学影像论文（除非它涉及光学、物理机制或生物物理原理）；偏好实验光学、生物物理、脑成像机制类工作。\n"
        '只输出 JSON 数组：[{"i": 编号, "relevant": true/false, "summary": "中文摘要"}]，编号对应输入列表。\n\n'
        "论文列表：\n" + json.dumps(items, ensure_ascii=False)
    )
    resp = client.chat.completions.create(
        model=cfg.get("llm_model", "deepseek-chat"),
        messages=[{"role": "system", "content": sys_prompt}, {"role": "user", "content": user_prompt}],
        temperature=0.2,
    )
    raw = (resp.choices[0].message.content or "").strip()
    raw = re.sub(r"^```(?:json)?\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)
    try:
        data = json.loads(raw)
    except Exception as e:
        log(f"LLM 输出解析失败，退回空列表: {e}")
        log("原始输出:", raw[:500])
        return []
    out = []
    for d in data:
        if isinstance(d, dict) and d.get("relevant") and 0 <= d.get("i", -1) < len(papers):
            p = papers[d["i"]].copy()
            p["summary_zh"] = d.get("summary", "")
            out.append(p)
    return out


# ---------------- 找开放获取 PDF / 链接 ----------------
def resolve_open_access(p, cfg):
    """返回 (正文链接, 可下载的 PDF 链接或 None)。"""
    if p["source"] == "arxiv" and p["pdf_url"]:
        return p["pdf_url"], p["pdf_url"]
    doi = p["doi"]
    if not doi:
        return p["url"], None
    # Unpaywall：优先直接 PDF（url_for_pdf），否则给落地页链接（不下载）
    try:
        r = requests.get(f"https://api.unpaywall.org/v2/{doi}", params={"email": cfg["email_to"]}, timeout=20, headers=UA)
        if r.status_code == 200:
            j = r.json()
            loc = j.get("best_oa_location") or {}
            pdf = loc.get("url_for_pdf")
            url = pdf or loc.get("url")
            if url:
                return url, pdf
    except Exception:
        pass
    # OpenAlex 兜底
    try:
        r = requests.get(f"https://api.openalex.org/works/doi:{doi}", timeout=20, headers=UA)
        if r.status_code == 200:
            j = r.json()
            loc = j.get("best_oa_location") or {}
            pdf = loc.get("pdf_url")
            if pdf:
                return pdf, pdf
            oa = j.get("open_access") or {}
            if oa.get("oa_url"):
                return oa["oa_url"], None
    except Exception:
        pass
    # 付费：返回 DOI 链接
    return f"https://doi.org/{doi}", None


# ---------------- 下载 PDF ----------------
def download_pdf(url, outdir, safe_name):
    try:
        r = requests.get(url, timeout=60, headers=UA)
        if r.status_code != 200:
            return None
        content = r.content
        ct = (r.headers.get("content-type") or "").lower()
        # 判定是否真是 PDF：content-type 或魔数 %PDF
        if "pdf" not in ct and content[:4] != b"%PDF":
            return None
        fn = outdir / (safe_name + ".pdf")
        fn.write_bytes(content)
        return fn if len(content) > 1000 else None
    except Exception as e:
        log(f"PDF 下载失败 {url}: {e}")
        return None


def safe_filename(title):
    s = re.sub(r'[\\/:*?"<>|]', "_", title)[:60].strip()
    return s or "paper"


# ---------------- 发邮件 ----------------
def send_email(cfg, selected, pdf_paths):
    import smtplib
    from email.mime.multipart import MIMEMultipart
    from email.mime.text import MIMEText
    from email.mime.application import MIMEApplication

    msg = MIMEMultipart()
    msg["From"] = cfg["email_from"]
    msg["To"] = cfg["email_to"]
    msg["Subject"] = f"📚 文献订阅日报 {dt.date.today()}（{len(selected)} 篇）"

    rows = []
    for p in selected:
        link = p.get("final_url") or p.get("url")
        rows.append(
            f'<li><b>{p["title"]}</b><br>'
            f'<span style="color:#555">{p.get("summary_zh", "")}</span><br>'
            f'<a href="{link}">📄 {link}</a> '
            f'<small>({p["source"].upper()})</small></li>'
        )
    body = f'<h3>今日推荐 {len(selected)} 篇（脑科学 / 波动光学 / 光学观测脑 / AI）</h3><ol>{"".join(rows)}</ol>'
    msg.attach(MIMEText(body, "html", "utf-8"))

    max_mb = float(cfg.get("max_attach_mb", 15))
    max_bytes = int(max_mb * 1024 * 1024)
    attached = 0
    for path in pdf_paths:
        if path and Path(path).exists():
            size = Path(path).stat().st_size
            if attached + size > max_bytes:
                log(f"附件超 {max_mb}MB 上限，跳过（正文已有链接）: {Path(path).name}")
                continue
            with open(path, "rb") as f:
                part = MIMEApplication(f.read(), _subtype="pdf")
            part.add_header("Content-Disposition", "attachment", filename=Path(path).name)
            msg.attach(part)
            attached += size

    host = os.environ.get("SMTP_HOST", "smtp.163.com")
    port = int(os.environ.get("SMTP_PORT", "465"))
    user = os.environ.get("SMTP_USER", cfg["email_from"])
    pwd = os.environ.get("SMTP_PASSWORD", "")
    if not pwd:
        log("SMTP_PASSWORD 为空，跳过发信。请在 .env 填入邮箱授权码。")
        return False
    try:
        use_ssl = port in (465, 994)
        if use_ssl:
            s = smtplib.SMTP_SSL(host, port, timeout=60)
        else:
            s = smtplib.SMTP(host, port, timeout=60)
        with s:
            if not use_ssl:
                s.starttls()
            s.login(user, pwd)
            s.sendmail(msg["From"], [msg["To"]], msg.as_string())
    except Exception as e:
        log(f"邮件发送失败: {e}")
        return False
    log(f"邮件已发送到 {cfg['email_to']}")
    return True


# ---------------- 主流程 ----------------
def main():
    send_flag = "--send" in sys.argv
    no_llm = "--no-llm" in sys.argv
    cfg = load_config()
    seen = load_seen()

    log("开始抓取 arXiv ...")
    arxiv_papers = fetch_arxiv(cfg)
    log(f"arXiv 抓到 {len(arxiv_papers)} 篇")
    log("开始抓取 PubMed ...")
    pubmed_papers = fetch_pubmed(cfg)
    log(f"PubMed 抓到 {len(pubmed_papers)} 篇")

    # 合并 + 同一批内去重（同一篇可能出现在多个 arXiv 分类）
    combined = arxiv_papers + pubmed_papers
    _ids = set()
    combined_dedup = []
    for p in combined:
        if p["id"] in _ids:
            continue
        _ids.add(p["id"])
        combined_dedup.append(p)

    all_papers = coarse_filter(combined_dedup, cfg)
    log(f"关键词粗筛后 {len(all_papers)} 篇")

    # 去重：跳过已发过的
    fresh = [p for p in all_papers if p["id"] not in seen]
    log(f"去掉已发 {len(all_papers) - len(fresh)} 篇，剩 {len(fresh)} 篇")

    if fresh and not no_llm:
        log("DeepSeek 精筛 + 摘要中 ...")
        selected = llm_filter_summarize(fresh, cfg)
        log(f"LLM 筛选后 {len(selected)} 篇")
    else:
        selected = list(fresh)
        for p in selected:
            p["summary_zh"] = ""

    selected = selected[: cfg["max_papers"]]

    PDF_DIR.mkdir(exist_ok=True)
    pdf_paths = []
    for i, p in enumerate(selected):
        link, pdf_url = resolve_open_access(p, cfg)
        p["final_url"] = link
        if pdf_url:
            path = download_pdf(pdf_url, PDF_DIR, f"{i + 1:02d}_{safe_filename(p['title'])}")
            pdf_paths.append(path)
            p["pdf_path"] = str(path) if path else ""
            if path:
                log(f"已下载 PDF: {path.name}")
            else:
                log(f"（PDF 下载失败，正文附链接）: {p['title'][:50]}")
        else:
            pdf_paths.append(None)
            p["pdf_path"] = ""
            log(f"（无开放获取 PDF，正文附链接）: {p['title'][:50]}")

    will_send = send_flag or not cfg["dry_run"]

    print("\n" + "=" * 70)
    for i, p in enumerate(selected):
        print(f"{i + 1}. {p['title']}")
        print(f"   [{p['source'].upper()}] {p.get('summary_zh', '')}")
        print(f"   链接: {p['final_url']}")
        if p.get("pdf_path"):
            print(f"   PDF: {p['pdf_path']}")
        print()
    print("=" * 70)

    if will_send:
        ok = send_email(cfg, selected, pdf_paths)
        if ok and selected:
            # 只有发送成功才记录"已发"，失败下次重试
            seen.update(p["id"] for p in selected)
            save_seen(seen)
    else:
        log("dry_run=true，未发送邮件（设 dry_run:false 或加 --send 真正发信）")


if __name__ == "__main__":
    main()
