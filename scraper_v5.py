"""
scraper_v5.py  —  ค้นหาข่าวขายทอดตลาดราชการ → Supabase
ปรับปรุงจาก v4 ด้วย query structure ใหม่จาก search_terms_v5.txt

ความต่างจาก v4:
  - VERB sets แยกชัดเจน 6 ชุด (HIGH / MID / PROV / ENTITY / LOOSE / VEHICLE)
  - Group ใหม่: A1_go, A2_go_high, A2_go_mid, A3_ac, A4_pdf
  - Group V_vehicle สำหรับยานพาหนะโดยเฉพาะ
  - Group FB_gov ใช้ TRASH_FB แทน TRASH_CRITICAL
  - Group L_or (site:.or.th) แยกออกมาจาก A
  - Pages Policy: cap ต่อ group (2 สำหรับ C_province, 5 สำหรับ FB_gov, 10 ที่เหลือ)
  - Budget: 1,000 credits/วัน (v4 ใช้ 700)

การรันด้วยมือ:
  export SERPER_API_KEY="..."
  export SUPABASE_URL="https://pfnhxozecazjxjgpfrzu.supabase.co"
  export SUPABASE_KEY="..."
  python scraper_v5.py
  python scraper_v5.py --dry-run   # ได้ dry_v5.csv ไม่แตะ Supabase

dedup:
  content_hash = SHA256(normalize_url(url) + "|" + title.strip())
  ตรงกับ extension content.js และ scraper_v4.py ทุกประการ
"""

import os, hashlib, time, logging, argparse, csv, sys
from datetime import date
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse

from dotenv import load_dotenv
load_dotenv()

VERSION = "v5"
TABLE        = "anytime_results"
DOMAIN_TABLE = "crawler_domains_normal"
SLEEP_SEC    = 0.4
TODAY        = date.today()

NOISE_PARAMS = {
    "utm_source","utm_medium","utm_campaign","utm_content","utm_term",
    "fbclid","gclid","sessionid","sid","token","_ga","ref","source",
}

PYTHON_BLOCKED = {
    "x.com", "instagram.com", "threads.net",
    "mgronline.com", "naewna.com", "dailynews.co.th",
    "auct.co.th", "sia.co.th", "bam.co.th",
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════
# EXCLUDES
# ══════════════════════════════════════════════════════

# CRITICAL: ใส่ทุก query ยกเว้น FB_gov
TRASH_CRITICAL = (
    '-site:youtube.com -site:facebook.com -site:line.me -site:tiktok.com'
)

# PROC: เว็บจัดซื้อกลาง — ใส่เฉพาะ query ที่ terms เหลือพอ
TRASH_PROC = (
    '-site:gprocurement.go.th -site:prd.go.th -site:bidding.pea.co.th '
    '-site:doh.go.th -site:egp.rid.go.th -site:customs.go.th '
    '-site:fio.co.th -site:thailandpost.co.th'
)

# KW: ใส่ทุก query (ยกเว้น FB_gov ใช้ inline)
TRASH_KW = '-บังคับคดี -"รอขาย" -"ธนาคารยึด" -"ที่ดิน"'

# FB: ใช้แทน TRASH_CRITICAL ใน FB_gov (เก็บ facebook.com ไว้)
TRASH_FB = '-site:youtube.com -site:line.me -site:tiktok.com'


# ══════════════════════════════════════════════════════
# VERB SETS  (จาก search_terms_v5.txt)
# ══════════════════════════════════════════════════════

# HIGH (9 terms) — แม่นยำสูง ใช้ domain ใดก็ได้
VERB_HIGH = (
    '("ขายทอดตลาด" OR "ขายทอดตลาดพัสดุ" OR "ขายทอดตลาดครุภัณฑ์" OR '
    '"ประกาศขายทอดตลาด" OR "ประกาศขายพัสดุ" OR "ประกาศขายครุภัณฑ์" OR '
    '"ไม่จำเป็นต้องใช้ในราชการ" OR "หมดความจำเป็นต้องใช้ในราชการ" OR '
    '"พัสดุชำรุดเสื่อมสภาพ")'
)

# MID (6 terms) — คำกว้าง ต้องบังคับ site: เสมอ
VERB_MID = (
    '("จำหน่ายพัสดุ" OR "จำหน่ายครุภัณฑ์" OR '
    '"จำหน่ายพัสดุชำรุด" OR "จำหน่ายครุภัณฑ์ชำรุด" OR '
    '"ระบายพัสดุ" OR "ระบายครุภัณฑ์")'
)

# PROV (9 terms) — สำหรับ C_province โดยเฉพาะ
VERB_PROV = (
    '("ขายทอดตลาด" OR "ขายทอดตลาดพัสดุ" OR "ขายทอดตลาดครุภัณฑ์" OR '
    '"ประกาศขายทอดตลาด" OR "ประกาศขายพัสดุ" OR "ประกาศขายครุภัณฑ์" OR '
    '"ไม่จำเป็นต้องใช้ในราชการ" OR "จำหน่ายพัสดุ" OR "จำหน่ายครุภัณฑ์")'
)

# ENTITY (7 terms) — สำหรับ D/E/F/G
VERB_ENTITY = (
    '("ขายทอดตลาด" OR "ขายทอดตลาดพัสดุ" OR "ขายทอดตลาดครุภัณฑ์" OR '
    '"ประกาศขายทอดตลาด" OR "จำหน่ายพัสดุ" OR '
    '"ไม่จำเป็นต้องใช้ในราชการ" OR "พัสดุชำรุดเสื่อมสภาพ")'
)

# LOOSE (6 terms) — ดักตกหล่น ต้องคู่ site: เสมอ
VERB_LOOSE = (
    '("ทอดตลาด" OR "ประกาศขาย" OR "จำหน่ายครุภัณฑ์" OR '
    '"ระบายพัสดุ" OR "ชำรุดเสื่อมสภาพ" OR "เสื่อมสภาพ")'
)

# VEHICLE (5 terms) — ยานพาหนะราชการ
VERB_VEHICLE = (
    '("ขายทอดตลาดครุภัณฑ์ยานพาหนะ" OR "ขายทอดตลาดรถยนต์" OR '
    '"ขายทอดตลาดรถ" OR "หมดความจำเป็นต้องใช้ในราชการ" OR '
    '"ครุภัณฑ์ยานพาหนะและขนส่ง")'
)


# ══════════════════════════════════════════════════════
# จังหวัด 77
# ══════════════════════════════════════════════════════
ALL_PROVINCES = [
    "เชียงใหม่", "เชียงราย", "น่าน", "พะเยา", "แพร่",
    "แม่ฮ่องสอน", "ลำปาง", "ลำพูน", "อุตรดิตถ์",
    "พิษณุโลก", "สุโขทัย", "กำแพงเพชร", "ตาก", "เพชรบูรณ์",
    "พิจิตร", "นครสวรรค์", "อุทัยธานี", "ชัยนาท",
    "กาฬสินธุ์", "ขอนแก่น", "ชัยภูมิ", "นครพนม",
    "นครราชสีมา", "บึงกาฬ", "บุรีรัมย์", "มหาสารคาม",
    "มุกดาหาร", "ยโสธร", "ร้อยเอ็ด", "เลย",
    "ศรีสะเกษ", "สกลนคร", "สุรินทร์", "หนองคาย",
    "หนองบัวลำภู", "อำนาจเจริญ", "อุดรธานี", "อุบลราชธานี",
    "กรุงเทพ", "นนทบุรี", "ปทุมธานี", "สมุทรปราการ",
    "พระนครศรีอยุธยา", "นครปฐม", "สมุทรสงคราม",
    "สมุทรสาคร", "สระบุรี", "ลพบุรี", "สิงห์บุรี",
    "สุพรรณบุรี", "อ่างทอง", "นครนายก",
    "จันทบุรี", "ฉะเชิงเทรา", "ชลบุรี", "ตราด",
    "ปราจีนบุรี", "ระยอง", "สระแก้ว",
    "กาญจนบุรี", "ประจวบคีรีขันธ์", "เพชรบุรี", "ราชบุรี",
    "กระบี่", "ชุมพร", "ตรัง", "นครศรีธรรมราช",
    "นราธิวาส", "ปัตตานี", "พังงา", "พัทลุง",
    "ภูเก็ต", "ระนอง", "สตูล", "สงขลา",
    "สุราษฎร์ธานี", "ยะลา",
]  # 77 จังหวัด


# ══════════════════════════════════════════════════════
# BUILD QUERIES
# รูปแบบ: (group, query, tbs, pages)
# ══════════════════════════════════════════════════════
def build_queries() -> list[tuple[str, str, str, int]]:
    rows: list[tuple[str, str, str, int]] = []

    def add(group: str, q: str, tbs_list: list[str], pages: int):
        for t in tbs_list:
            rows.append((group, q, t, pages))

    DW = ["qdr:d", "qdr:w"]
    W  = ["qdr:w"]
    M  = ["qdr:m"]

    # ── A1: HIGH × .go.th (ไม่ block PROC เพราะ terms ครบ) ──
    add("A1_go",
        f'{VERB_HIGH} site:.go.th {TRASH_KW} {TRASH_CRITICAL}',
        DW, 10)  # 20 cr

    # ── A2_go_high: HIGH × .go.th + block PROC เพิ่ม ────────
    add("A2_go_high",
        f'{VERB_HIGH} site:.go.th {TRASH_KW} {TRASH_PROC}',
        DW, 10)  # 20 cr

    # ── A2_go_mid: MID × .go.th (คำกว้าง + CRITICAL) ────────
    add("A2_go_mid",
        f'{VERB_MID} site:.go.th {TRASH_KW} {TRASH_CRITICAL}',
        DW, 10)  # 20 cr

    # ── A3: HIGH × .ac.th ─────────────────────────────────
    add("A3_ac",
        f'{VERB_HIGH} site:.ac.th {TRASH_KW} {TRASH_CRITICAL}',
        DW, 10)  # 20 cr

    # ── A4: HIGH × filetype:pdf ────────────────────────────
    add("A4_pdf",
        f'{VERB_HIGH} filetype:pdf {TRASH_KW} {TRASH_PROC}',
        DW, 10)  # 20 cr

    # ── B: MID × site:.th (catch subdomain พิเศษ) ─────────
    add("B_th",
        f'{VERB_MID} site:.th {TRASH_KW} {TRASH_PROC}',
        DW, 10)  # 20 cr

    # ── C: 77 จังหวัด × .go.th (cap 2 กันงบทะลัก) ────────
    for prov in ALL_PROVINCES:
        add("C_province",
            f'{VERB_PROV} "{prov}" site:.go.th {TRASH_KW}',
            DW, 2)  # 77 × 2tbs × 2p = 308 cr worst-case

    # ── D: สาธารณสุข ───────────────────────────────────────
    add("D_health",
        f'("โรงพยาบาล" OR "สาธารณสุข" OR "สสจ" OR "สสอ") '
        f'{VERB_ENTITY} site:.go.th {TRASH_KW}',
        W, 10)  # 10 cr
    add("D_health",
        f'("กรมการแพทย์" OR "ควบคุมโรค") '
        f'{VERB_ENTITY} site:.go.th {TRASH_KW}',
        W, 10)  # 10 cr

    # ── E: การศึกษา ────────────────────────────────────────
    add("E_edu",
        f'("โรงเรียน" OR "สพป" OR "สพม" OR "วิทยาลัย") '
        f'{VERB_ENTITY} site:.go.th {TRASH_KW}',
        W, 10)  # 10 cr
    add("E_edu",
        f'("มหาวิทยาลัย" OR "ราชภัฏ" OR "ราชมงคล") '
        f'{VERB_ENTITY} site:.ac.th {TRASH_KW}',
        W, 10)  # 10 cr

    # ── F: ท้องถิ่น ─────────────────────────────────────────
    add("F_local",
        f'("อบต" OR "เทศบาล" OR "อบจ" OR "องค์การบริหารส่วน") '
        f'{VERB_ENTITY} site:.go.th {TRASH_KW}',
        W, 10)  # 10 cr
    add("F_local",
        f'"ศาล" {VERB_ENTITY} site:coj.go.th',
        W, 10)  # 10 cr

    # ── G: หน่วยงานกลาง ────────────────────────────────────
    add("G_gov",
        f'("กรม" OR "สำนักงาน" OR "ศูนย์" OR "องค์การ") '
        f'{VERB_ENTITY} site:.go.th {TRASH_KW}',
        W, 10)  # 10 cr

    # ── V: ยานพาหนะราชการ (ใหม่) ───────────────────────────
    add("V_vehicle",
        f'{VERB_VEHICLE} site:.go.th {TRASH_KW}',
        DW, 10)  # 20 cr

    # ── J: Catch-all ─────────────────────────────────────
    add("J_catch",
        f'{VERB_LOOSE} site:.go.th {TRASH_KW} {TRASH_CRITICAL}',
        DW, 10)  # 20 cr
    add("J_catch",
        f'{VERB_MID} site:.go.th {TRASH_KW} {TRASH_CRITICAL}',
        DW, 10)  # 20 cr

    # ── K: qdr:m (ดักประกาศ index ช้า) ───────────────────
    add("K_month",
        f'{VERB_HIGH} site:.go.th {TRASH_KW}',
        M, 10)  # 10 cr

    # ── L: site:.or.th ─────────────────────────────────────
    add("L_or",
        f'{VERB_HIGH} site:.or.th {TRASH_KW}',
        W, 10)  # 10 cr

    # ── FB_gov: Facebook (ใช้ TRASH_FB แทน TRASH_CRITICAL) ─
    add("FB_gov",
        f'("ขายทอดตลาด" OR "ขายทอดตลาดพัสดุ" OR "ขายทอดตลาดครุภัณฑ์") '
        f'site:facebook.com '
        f'-บังคับคดี -"รอขาย" -"ธนาคารยึด" -"ที่ดิน" {TRASH_FB}',
        DW, 5)  # 10 cr

    return rows


# ══════════════════════════════════════════════════════
# HELPER
# ══════════════════════════════════════════════════════
def normalize_url(url: str) -> str:
    try:
        parsed = urlparse(url)
        qs = {k: v for k, v in parse_qs(parsed.query).items()
              if k.lower() not in NOISE_PARAMS}
        return urlunparse(parsed._replace(query=urlencode(qs, doseq=True), fragment=""))
    except Exception:
        return url

def make_content_hash(url: str, title: str) -> str:
    raw = f"{normalize_url(url)}|{title.strip()}"
    return hashlib.sha256(raw.encode()).hexdigest()

def get_hostname(url: str) -> str:
    try:
        h = (urlparse(url).hostname or "").lower()
        return h[4:] if h.startswith("www.") else h
    except Exception:
        return ""

def is_blocked(url: str) -> bool:
    h = get_hostname(url)
    return any(h == d or h.endswith("." + d) for d in PYTHON_BLOCKED)


# ══════════════════════════════════════════════════════
# SERPER
# ══════════════════════════════════════════════════════
def serper_search(query: str, tbs: str, max_pages: int, api_key: str) -> list[dict]:
    import requests
    all_items = []
    for page in range(1, max_pages + 1):
        try:
            resp = requests.post(
                "https://google.serper.dev/search",
                headers={"X-API-KEY": api_key, "Content-Type": "application/json"},
                json={
                    "q":        query,
                    "gl":       "th",
                    "hl":       "th",
                    "location": "Thailand",
                    "num":      10,
                    "page":     page,
                    "tbs":      tbs,
                },
                timeout=20,
            )
            resp.raise_for_status()
            items = resp.json().get("organic", [])
            all_items.extend(items)
            if len(items) < 10:
                break
            time.sleep(0.3)
        except Exception as e:
            log.warning(f"Serper error p={page} [{query[:55]}]: {e}")
            break
    return all_items


# ══════════════════════════════════════════════════════
# SUPABASE
# ══════════════════════════════════════════════════════
def upsert_rows(sb, rows: list[dict]) -> tuple[int, int]:
    if not rows:
        return 0, 0

    hashes = [r["content_hash"] for r in rows]

    def norm(s):
        return " ".join((s or "").split()).strip()

    try:
        existing = (
            sb.table(TABLE)
            .select("content_hash, snippet, status")
            .in_("content_hash", hashes)
            .execute()
        )
        existing_map = {
            r["content_hash"]: {"snippet": r["snippet"], "status": r["status"]}
            for r in (existing.data or [])
        }
    except Exception:
        existing_map = {}

    new_rows = [r for r in rows if r["content_hash"] not in existing_map]
    upd_rows = [r for r in rows
                if r["content_hash"] in existing_map
                and norm(r.get("snippet", "")) != norm(existing_map[r["content_hash"]]["snippet"])]

    if new_rows:
        try:
            sb.table(TABLE).insert(new_rows, returning="minimal").execute()
        except Exception:
            pass

    for r in upd_rows:
        try:
            old_status = existing_map[r["content_hash"]]["status"]
            new_status = "update" if old_status == "read" else old_status
            sb.table(TABLE).update({
                "snippet":     r.get("snippet", ""),
                "status":      new_status,
                "deep_status": "pending",
            }).eq("content_hash", r["content_hash"]).neq("status", "trash").execute()
        except Exception as e:
            log.warning(f"PATCH update error: {e}")

    return len(new_rows), len(upd_rows)

def upsert_domains(sb, domain_rows: list[dict]):
    if not domain_rows:
        return
    try:
        sb.table(DOMAIN_TABLE).insert(
            domain_rows, returning="minimal"
        ).execute()
    except Exception as e:
        if '"23505"' not in str(e) and "23505" not in str(e):
            log.warning(f"Supabase crawler_domains_normal error: {e}")

def load_supabase_blacklist(sb) -> set:
    try:
        resp = sb.table("anytime_domain_blacklist").select("domain").execute()
        domains = {r["domain"] for r in (resp.data or [])}
        log.info(f"   blacklist โหลดแล้ว: {len(domains)} domains")
        return domains
    except Exception as e:
        log.warning(f"   blacklist load error: {e}")
        return set()


# ══════════════════════════════════════════════════════
# DRY-RUN CSV
# ══════════════════════════════════════════════════════
DRY_CSV = f"dry_{VERSION}.csv"

def write_dry_csv(rows: list[dict], tbs: str):
    """เขียน URL ลง CSV แทนการ upsert (dry-run mode)"""
    with open(DRY_CSV, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        for r in rows:
            w.writerow([r["url"], r["title"][:60], r["search_group"], tbs])


# ══════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════
def main():
    parser = argparse.ArgumentParser(description="scraper_v5 — ค้นหาประกาศขายทอดตลาด")
    parser.add_argument("--dry-run", action="store_true",
                        help="เขียน CSV เท่านั้น ไม่แตะ Supabase (ใช้สำหรับ A/B test)")
    args = parser.parse_args()
    DRY_RUN = args.dry_run

    SERPER_API_KEY = os.environ.get("SERPER_API_KEY", "")
    if not SERPER_API_KEY:
        log.error("กรุณาตั้ง SERPER_API_KEY ใน environment")
        sys.exit(1)

    today_str = TODAY.isoformat()
    queries   = build_queries()
    est_credits = sum(p for _, _, _, p in queries)

    log.info(f"▶ scraper_{VERSION}  วันที่ {today_str}  dry_run={DRY_RUN}")
    log.info(f"   queries={len(queries)}  ≈{est_credits} credits worst-case  (budget 1,000)")

    if DRY_RUN:
        # ล้างไฟล์เก่า
        with open(DRY_CSV, "w", newline="", encoding="utf-8") as f:
            csv.writer(f).writerow(["url", "title", "search_group", "tbs"])
        log.info(f"   [DRY-RUN] จะเขียนผลลง {DRY_CSV}")
        sb = None
        sb_blacklist: set = set()
        seen_domains: set = set()
    else:
        from supabase import create_client
        SUPABASE_URL = os.environ["SUPABASE_URL"]
        SUPABASE_KEY = os.environ["SUPABASE_KEY"]
        sb = create_client(SUPABASE_URL, SUPABASE_KEY)
        sb_blacklist = load_supabase_blacklist(sb)
        try:
            _dr = sb.table(DOMAIN_TABLE).select("domain").execute()
            seen_domains: set = {r["domain"] for r in (_dr.data or [])}
            log.info(f"   known domains: {len(seen_domains)}")
        except Exception:
            seen_domains: set = set()

    total_hits    = 0
    total_saved   = 0
    total_blocked = 0
    seen: set[str] = set()
    cur_group = ""

    for group, q, tbs, pages in queries:
        if group != cur_group:
            cur_group = group
            log.info(f"══ {group} ══")

        hits = serper_search(q, tbs, pages, SERPER_API_KEY)
        total_hits += len(hits)
        rows        = []
        domain_rows = []

        for item in hits:
            url     = (item.get("link")    or "").strip()
            title   = (item.get("title")   or "").strip()
            snippet = (item.get("snippet") or "").strip()
            if not url or not title:
                continue

            if is_blocked(url):
                total_blocked += 1
                continue
            h = get_hostname(url)
            if any(h == d or h.endswith("." + d) for d in sb_blacklist):
                total_blocked += 1
                continue

            chash = make_content_hash(url, title)
            if chash in seen:
                continue
            seen.add(chash)

            hostname = get_hostname(url)
            if not DRY_RUN and hostname.endswith((".go.th", ".ac.th", ".or.th")):
                if hostname not in seen_domains:
                    seen_domains.add(hostname)
                    parsed = urlparse(url)
                    domain_rows.append({
                        "domain":        hostname,
                        "index_url":     f"{parsed.scheme}://{hostname}/",
                        "url_pattern":   "/",
                        "keyword_found": f"serper {VERSION}",
                        "first_seen":    today_str,
                        "page_count":    0,
                        "priority":      4,
                        "monitor_type":  "B",
                        "status":        "active"
                    })

            rows.append({
                "content_hash": chash,
                "title":        title[:500],
                "url":          url,
                "snippet":      snippet[:1000],
                "query_used":   q[:500],
                "search_group": group,
                "found_date":   today_str,
                "status":       "new",
                "source_type":  VERSION,
                "deep_status":  "pending",
            })

        if DRY_RUN:
            write_dry_csv(rows, tbs)
            log.info(f"  [{tbs}][{len(hits):3d}h/{len(rows):3d}rows] {q[:70]!r}")
        else:
            saved, updated = upsert_rows(sb, rows)
            upsert_domains(sb, domain_rows)
            total_saved += saved
            log.info(f"  [{tbs}][{len(hits):3d}h/{saved:3d}new/{updated:3d}upd] {q[:70]!r}")

        time.sleep(SLEEP_SEC)

    if DRY_RUN:
        import pandas as pd
        try:
            df = pd.read_csv(DRY_CSV)
            unique_urls = df["url"].nunique()
            log.info(f"\n✅ [DRY-RUN] hits:{total_hits}  unique_urls:{unique_urls}  "
                     f"blocked:{total_blocked}  ไฟล์: {DRY_CSV}")
        except Exception:
            log.info(f"\n✅ [DRY-RUN] hits:{total_hits}  blocked:{total_blocked}  "
                     f"ไฟล์: {DRY_CSV}")
    else:
        log.info(
            f"\n✅ เสร็จสิ้น  hits:{total_hits}  ใหม่:{total_saved}  "
            f"blocked:{total_blocked}  วันที่:{today_str}"
        )


if __name__ == "__main__":
    main()
