"""
scraper_v4.py  —  ค้นหาข่าวขายทอดตลาดราชการ → Supabase
ออกแบบใหม่ทั้งระบบเพื่อแก้ 3 ปัญหา:
  1. query เดิมเกิน 32 terms → Google ตัด excludes ทิ้ง → เว็บขยะปนมา
  2. TRASH_PROC syntax ผิด "-.oncb.go.th" → ต้อง "-site:oncb.go.th"
  3. ไม่มี site: filter → เว็บใหญ่ครองหน้า 1-5, เว็บเล็กตกหน้า 10+

Budget: 700 credits/วัน (Serper plan ปัจจุบัน)
  A: คำหลัก × 4 domain        = 28 cr
  B: site:.th (dropback)      =  6 cr
  C: จังหวัด × .go.th (77)    = 154 cr
  D: สาธารณสุข                =  8 cr
  E: การศึกษา                 =  8 cr
  F: ท้องถิ่น                  = 10 cr
  G: หน่วยงานกลาง              =  8 cr
  J: catch-all                =  8 cr
  K: qdr:m (dักประกาศ index ช้า) = 5 cr
  L: Facebook                 =  5 cr
  รวม: ~240 credits  เหลือ buffer ~460

การรันด้วยมือบนเครื่อง:
  export SERPER_API_KEY="5f53f80a3c330ef18f42629073dc70f312ae3ade"
  export SUPABASE_URL="https://pfnhxozecazjxjgpfrzu.supabase.co/rest/v1/"
  export SUPABASE_KEY="sb_publishable_9idaI5irCf8jia0qABYyhA_P5VoRRBo"
  python scraper_v4.py

หมายเหตุ dedup:
  content_hash = SHA256(normalize_url(url) + "|" + title.strip())
  ตรงกับ extension content.js ทุกประการ
  hash ซ้ำ → UPDATE snippet + status='update' (ไม่ insert ซ้ำ)
"""

import os, hashlib, time, logging
from datetime import date
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse
import requests
from supabase import create_client

# ══════════════════════════════════════════════════════
# CONFIG
# ══════════════════════════════════════════════════════
SERPER_API_KEY = os.environ["SERPER_API_KEY"]
SUPABASE_URL   = os.environ["SUPABASE_URL"]
SUPABASE_KEY   = os.environ["SUPABASE_KEY"]

TABLE        = "anytime_results"
DOMAIN_TABLE = "crawler_domains_normal"
SLEEP_SEC    = 0.4
TODAY        = date.today()

NOISE_PARAMS = {
    "utm_source","utm_medium","utm_campaign","utm_content","utm_term",
    "fbclid","gclid","sessionid","sid","token","_ga","ref","source",
}

# Python-level filter (domain ที่ใส่ใน query ไม่ได้เพราะ terms เต็ม)
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
# EXCLUDES (แก้ syntax ให้ถูก)
# ══════════════════════════════════════════════════════

# Level 1: CRITICAL (ใส่ทุก query — 4 ops)
TRASH_CRITICAL = (
    '-site:youtube.com -site:facebook.com -site:line.me -site:tiktok.com'
)

# Level 2: PROC (เว็บจัดซื้อที่ scrape แยกอยู่แล้ว — 8 ops ย่อเหลือครึ่ง)
# ใส่เฉพาะ query ที่มี terms buffer เหลือ
TRASH_PROC = (
    '-site:gprocurement.go.th -site:prd.go.th -site:bidding.pea.co.th '
    '-site:doh.go.th -site:egp.rid.go.th -site:customs.go.th '
    '-site:fio.co.th -site:thailandpost.co.th'
)

# Level 3: Keyword exclude (4 ops)
TRASH_KW = '-บังคับคดี -"รอขาย" -"ธนาคารยึด" -"ที่ดิน"'


# ══════════════════════════════════════════════════════
# VERB SETS — 2 ชุด
# VERB_MAIN : ครอบคลุม ใช้ใน A, K (domain แคบอยู่แล้ว)
# VERB_SLIM : ย่อ ใช้ใน B, C, D, E, F, G, L (ต้องเผื่อที่สำหรับ entity filter)
# ══════════════════════════════════════════════════════

VERB_MAIN = (
    '("ขายทอดตลาด" OR "ขายทอดตลาดพัสดุ" OR "ขายทอดตลาดครุภัณฑ์" OR '
    '"ประกาศขายทอดตลาด" OR "ขายโดยวิธีทอดตลาด" OR '
    '"ไม่จำเป็นต้องใช้ในราชการ" OR "พัสดุชำรุดเสื่อมสภาพ" OR '
    '"จำหน่ายพัสดุ" OR "จำหน่ายครุภัณฑ์" OR '
    '"ระบายพัสดุ" OR "จำหน่ายโดยวิธีประมูล")'
)  # = 21 terms

VERB_SLIM = (
    '("ขายทอดตลาด" OR "ขายทอดตลาดพัสดุ" OR "ขายทอดตลาดครุภัณฑ์" OR '
    '"จำหน่ายพัสดุ" OR "จำหน่ายครุภัณฑ์" OR '
    '"ไม่จำเป็นต้องใช้ในราชการ" OR "พัสดุชำรุดเสื่อมสภาพ")'
)  # = 13 terms


# ══════════════════════════════════════════════════════
# จังหวัดทั้งหมด 77 จังหวัด
# ══════════════════════════════════════════════════════
ALL_PROVINCES = [
    # ภาคเหนือ
    "เชียงใหม่", "เชียงราย", "น่าน", "พะเยา", "แพร่",
    "แม่ฮ่องสอน", "ลำปาง", "ลำพูน", "อุตรดิตถ์",
    # ภาคเหนือตอนล่าง
    "พิษณุโลก", "สุโขทัย", "กำแพงเพชร", "ตาก", "เพชรบูรณ์",
    "พิจิตร", "นครสวรรค์", "อุทัยธานี", "ชัยนาท",
    # ภาคอีสาน
    "กาฬสินธุ์", "ขอนแก่น", "ชัยภูมิ", "นครพนม",
    "นครราชสีมา", "บึงกาฬ", "บุรีรัมย์", "มหาสารคาม",
    "มุกดาหาร", "ยโสธร", "ร้อยเอ็ด", "เลย",
    "ศรีสะเกษ", "สกลนคร", "สุรินทร์", "หนองคาย",
    "หนองบัวลำภู", "อำนาจเจริญ", "อุดรธานี", "อุบลราชธานี",
    # ภาคกลาง
    "กรุงเทพ", "นนทบุรี", "ปทุมธานี", "สมุทรปราการ",
    "พระนครศรีอยุธยา", "นครปฐม", "สมุทรสงคราม",
    "สมุทรสาคร", "สระบุรี", "ลพบุรี", "สิงห์บุรี",
    "สุพรรณบุรี", "อ่างทอง", "นครนายก",
    # ภาคตะวันออก
    "จันทบุรี", "ฉะเชิงเทรา", "ชลบุรี", "ตราด",
    "ปราจีนบุรี", "ระยอง", "สระแก้ว",
    # ภาคตะวันตก
    "กาญจนบุรี", "ประจวบคีรีขันธ์", "เพชรบุรี", "ราชบุรี",
    # ภาคใต้
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

    # ── A: คำหลัก × 4 domain (ดึงลึก 5 หน้า) ─────────
    add("A_go",  f'{VERB_MAIN} site:.go.th {TRASH_KW} {TRASH_CRITICAL}', DW, 5)  # 10 cr
    add("A_ac",  f'{VERB_MAIN} site:.ac.th {TRASH_KW} {TRASH_CRITICAL}', DW, 3)  # 6 cr
    add("A_or",  f'{VERB_MAIN} site:.or.th {TRASH_KW} {TRASH_CRITICAL}', W,  2)  # 2 cr
    add("A_pdf", f'{VERB_SLIM} filetype:pdf {TRASH_KW} {TRASH_PROC}',    DW, 5)  # 10 cr

    # ── B: site:.th catch subdomain พิเศษ ────────────
    add("B_th",  f'{VERB_SLIM} site:.th {TRASH_KW} {TRASH_PROC}',        DW, 3)  # 6 cr

    # ── C: จังหวัดละ query (77 × d+w × 1 page) ──────
    for prov in ALL_PROVINCES:
        add("C_province",
            f'{VERB_SLIM} "{prov}" site:.go.th {TRASH_KW} {TRASH_CRITICAL}',
            DW, 1)  # 77 × 2 = 154 cr

    # ── D: สาธารณสุข ──────────────────────────────────
    add("D_health",
        f'("โรงพยาบาล" OR "สาธารณสุขจังหวัด" OR "สาธารณสุขอำเภอ") '
        f'{VERB_SLIM} site:.go.th {TRASH_KW} {TRASH_CRITICAL}',
        W, 3)  # 3 cr
    add("D_health",
        f'("สำนักงานสาธารณสุข" OR "สสจ" OR "สสอ") '
        f'{VERB_SLIM} site:.go.th {TRASH_KW} {TRASH_CRITICAL}',
        W, 3)  # 3 cr
    add("D_health",
        f'("กรมการแพทย์" OR "ควบคุมโรค") '
        f'{VERB_SLIM} site:.go.th {TRASH_KW}',
        W, 2)  # 2 cr

    # ── E: การศึกษา ───────────────────────────────────
    add("E_edu",
        f'("โรงเรียน" OR "สพป" OR "สพม" OR "เขตพื้นที่การศึกษา") '
        f'{VERB_SLIM} site:.go.th {TRASH_KW}',
        W, 3)  # 3 cr
    add("E_edu",
        f'("มหาวิทยาลัย" OR "ราชภัฏ" OR "ราชมงคล") '
        f'{VERB_SLIM} site:.ac.th {TRASH_KW} {TRASH_CRITICAL}',
        W, 3)  # 3 cr
    add("E_edu",
        f'("วิทยาลัยเทคนิค" OR "วิทยาลัยอาชีวศึกษา" OR "วิทยาลัยการอาชีพ") '
        f'{VERB_SLIM} site:.ac.th {TRASH_KW}',
        W, 2)  # 2 cr

    # ── F: ท้องถิ่น ────────────────────────────────────
    add("F_local",
        f'"องค์การบริหารส่วนตำบล" {VERB_SLIM} site:.go.th {TRASH_KW} {TRASH_CRITICAL}',
        W, 3)  # 3 cr
    add("F_local",
        f'"เทศบาล" {VERB_SLIM} site:.go.th {TRASH_KW} {TRASH_CRITICAL}',
        W, 3)  # 3 cr
    add("F_local",
        f'"องค์การบริหารส่วนจังหวัด" {VERB_SLIM} site:.go.th {TRASH_KW}',
        W, 2)  # 2 cr
    add("F_local",
        f'"ศาล" {VERB_SLIM} site:coj.go.th',
        W, 2)  # 2 cr

    # ── G: หน่วยงานกลาง/จังหวัด ───────────────────────
    add("G_gov",
        f'"สำนักงาน" "จังหวัด" {VERB_SLIM} site:.go.th {TRASH_KW} {TRASH_CRITICAL}',
        W, 3)  # 3 cr
    add("G_gov",
        f'"กรม" {VERB_SLIM} site:.go.th {TRASH_KW} {TRASH_CRITICAL}',
        W, 3)  # 3 cr
    add("G_gov",
        f'("สำนักงานอัยการ" OR "สำนักงานสรรพากร" OR "สำนักงานขนส่ง") '
        f'{VERB_SLIM} site:.go.th {TRASH_KW}',
        W, 2)  # 2 cr

    # ── J: Catch-all บน .go.th ───────────────────────
    add("J_catch",
        f'"ทอดตลาด" site:.go.th {TRASH_KW} {TRASH_CRITICAL}',
        DW, 3)  # 6 cr
    add("J_catch",
        f'"ประกาศขาย" "คุรุภัณฑ์" site:.go.th {TRASH_KW} {TRASH_CRITICAL}',
        W, 2)  # 2 cr

    # ── K: qdr:m (ดักประกาศ index ช้า — ใหม่) ────────
    add("K_month",
        f'{VERB_MAIN} site:.go.th {TRASH_KW} {TRASH_CRITICAL}',
        M, 3)  # 3 cr
    add("K_month",
        f'{VERB_SLIM} filetype:pdf {TRASH_KW} {TRASH_PROC}',
        M, 2)  # 2 cr

    # ── L: Facebook (ใหม่) ───────────────────────────
    add("L_facebook",
        f'{VERB_SLIM} site:facebook.com {TRASH_KW}',
        W, 3)  # 3 cr
    add("L_facebook",
        f'"ขายทอดตลาด" "ราชการ" site:facebook.com',
        W, 2)  # 2 cr

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
    """hash = sha256(normalize_url(url) + "|" + title.strip())
    ตรงกับ extension ทุกประการ"""
    raw = f"{normalize_url(url)}|{title.strip()}"
    return hashlib.sha256(raw.encode()).hexdigest()

def get_hostname(url: str) -> str:
    try:
        h = (urlparse(url).hostname or "").lower()
        return h[4:] if h.startswith("www.") else h
    except Exception:
        return ""

def is_blocked(url: str) -> bool:
    """Python-level filter สำหรับ domain ที่ใส่ใน query ไม่ได้"""
    h = get_hostname(url)
    return any(h == d or h.endswith("." + d) for d in PYTHON_BLOCKED)


# ══════════════════════════════════════════════════════
# SERPER — เพิ่ม location: "Thailand"
# ══════════════════════════════════════════════════════
def serper_search(query: str, tbs: str, max_pages: int) -> list[dict]:
    all_items = []
    for page in range(1, max_pages + 1):
        try:
            resp = requests.post(
                "https://google.serper.dev/search",
                headers={"X-API-KEY": SERPER_API_KEY, "Content-Type": "application/json"},
                json={
                    "q":        query,
                    "gl":       "th",
                    "hl":       "th",
                    "location": "Thailand",   # ← ใหม่ ให้ผลตรงกับคนในไทย
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
def upsert_rows(sb, rows: list[dict]) -> int:
    if not rows:
        return 0

    # ── ขั้น 1: INSERT ทุก row (ซ้ำ hash → ข้าม) ───────────────────────────────
    try:
        sb.table(TABLE).insert(
            rows, returning="minimal"
        ).execute()
    except Exception:
        pass  # handle collision ใน ขั้น 2

    # ── ขั้น 2: PATCH row ที่ hash ซ้ำ → UPDATE snippet + status='update' ───────
    hashes = [r["content_hash"] for r in rows]
    try:
        existing = (
            sb.table(TABLE)
            .select("content_hash")
            .in_("content_hash", hashes)
            .execute()
        )
        existing_hashes = {r["content_hash"] for r in (existing.data or [])}
    except Exception:
        existing_hashes = set()

    new_hashes = {r["content_hash"] for r in rows} - existing_hashes
    updated = 0
    for r in rows:
        if r["content_hash"] in existing_hashes:
            try:
                sb.table(TABLE).update({
                    "snippet":    r.get("snippet", ""),
                    "status":     "update",
                    "updated_at": "now()",
                }).eq("content_hash", r["content_hash"]).neq("status", "trash").execute()
                updated += 1
            except Exception:
                pass

    return len(new_hashes)

def upsert_domains(sb, domain_rows: list[dict]):
    if not domain_rows:
        return
    try:
        sb.table(DOMAIN_TABLE).insert(
            domain_rows, returning="minimal"
        ).execute()
    except Exception as e:
        # code 23505 = unique violation → domain มีแล้ว ไม่ใช่ error จริง
        if '"23505"' not in str(e) and "23505" not in str(e):
            log.warning(f"Supabase crawler_domains_normal error: {e}")


# ══════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════
def load_supabase_blacklist(sb) -> set:
    """โหลด blacklist จาก anytime_domain_blacklist"""
    try:
        resp = sb.table("anytime_domain_blacklist").select("domain").execute()
        domains = {r["domain"] for r in (resp.data or [])}
        log.info(f"   blacklist โหลดแล้ว: {len(domains)} domains")
        return domains
    except Exception as e:
        log.warning(f"   blacklist load error: {e}")
        return set()

def main():
    sb        = create_client(SUPABASE_URL, SUPABASE_KEY)
    today_str = TODAY.isoformat()
    queries   = build_queries()

    # โหลด blacklist จาก Supabase รวมกับ PYTHON_BLOCKED
    sb_blacklist = load_supabase_blacklist(sb)

    est_credits = sum(p for _, _, _, p in queries)
    log.info(f"▶ scraper_v4  วันที่ {today_str}")
    log.info(f"   queries={len(queries)}  ≈{est_credits} credits  (budget 700)")

    total_hits     = 0
    total_saved    = 0
    total_blocked  = 0
    seen: set[str] = set()
    seen_domains: set[str] = set()
    cur_group = ""

    for group, q, tbs, pages in queries:
        if group != cur_group:
            cur_group = group
            log.info(f"══ {group} ══")

        hits = serper_search(q, tbs, pages)
        total_hits += len(hits)
        rows         = []
        domain_rows  = []

        for item in hits:
            url     = (item.get("link")    or "").strip()
            title   = (item.get("title")   or "").strip()
            snippet = (item.get("snippet") or "").strip()
            if not url or not title:
                continue

            # Python-level filter (PYTHON_BLOCKED + Supabase blacklist)
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

            # เก็บ domain .go.th/.ac.th/.or.th ลง pool
            hostname = get_hostname(url)
            if hostname.endswith((".go.th", ".ac.th", ".or.th")):
                if hostname not in seen_domains:
                    seen_domains.add(hostname)
                    parsed = urlparse(url)
                    domain_rows.append({
                        "domain":        hostname,
                        "index_url":     f"{parsed.scheme}://{hostname}/",
                        "url_pattern":   "/",
                        "keyword_found": "serper v4",
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
                "source_type":  "v4",
                "deep_status":  "pending",
            })

        saved = upsert_rows(sb, rows)
        upsert_domains(sb, domain_rows)
        total_saved += saved
        log.info(f"  [{tbs}][{len(hits):3d}h/{saved:3d}new] {q[:70]!r}")
        time.sleep(SLEEP_SEC)

    log.info(
        f"\n✅ เสร็จสิ้น  hits:{total_hits}  ใหม่:{total_saved}  "
        f"blocked:{total_blocked}  วันที่:{today_str}"
    )


if __name__ == "__main__":
    main()
