"""
scraper_v3.py  —  ค้นหาข่าวขายทอดตลาดราชการ → Supabase
GitHub Actions ready — รันครั้งเดียว ได้ทั้ง qdr:d และ qdr:w

Budget: 500 credits/วัน
  A_specific : 2q × 5p × 2(d+w) = 20 cr   | ดึงลึกสุด คำเจาะจง
  A_announce : 2q × 3p × 2(d+w) = 12 cr   | ประกาศขาย + คุรุภัณฑ์/พัสดุ
  B_broad    : 4q × 3p × 2(d+w) = 24 cr   | จำหน่าย site:.th
  C_province : 77q× 2p × 2(d+w) = 308 cr  | จังหวัดละ query
  D_health   : 3q × 3p × 1(w)   = 9 cr    | สาธารณสุข
  E_education: 4q × 3p × 1(w)   = 12 cr   | การศึกษา
  F_local    : 4q × 3p × 1(w)   = 12 cr   | ท้องถิ่น
  G_gov      : 6q × 3p × 1(w)   = 18 cr   | หน่วยงานกลาง
  J_domain   : 3q × 3p × 1.7    = 15 cr   | .go.th/.ac.th
  รวม: ~430 credits/รัน  เหลือ buffer ~70

หมายเหตุ dedup:
  content_hash = SHA256(normalized_url + "||" + title)
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

TABLE     = "anytime_results"
SLEEP_SEC = 0.4
TODAY     = date.today()

NOISE_PARAMS = {
    "utm_source","utm_medium","utm_campaign","utm_content","utm_term",
    "fbclid","gclid","sessionid","sid","token","_ga","ref","source",
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════
# EXCLUSIONS
# ══════════════════════════════════════════════════════
TRASH_SITE = (
    "-site:led.go.th -site:youtube.com -site:x.com "
    "-site:instagram.com -site:tiktok.com "
    "-site:bidding.pea.co.th -site:gprocurement.go.th -site:prd.go.th "
    "-site:dailynews.co.th -site:mgronline.com -site:naewna.com "
    "-site:line.me -site:auct.co.th -site:bam.co.th"
)
TRASH_KW = ' -บังคับคดี -"รอขาย" -"ธนาคารยึด" -"ที่ดิน"'
TRASH_PROC = (
    " -.oncb.go.th -.oag.go.th -.md.go.th -.radc.go.th"
    " -.dld.go.th -.dede.go.th -.rd.go.th -customs.go.th"
)

EX      = TRASH_SITE + TRASH_KW
EX_PROC = EX + TRASH_PROC


# ══════════════════════════════════════════════════════
# VERB SETS
# ══════════════════════════════════════════════════════
VERB_SPECIFIC = (
    '"ขายทอดตลาด" OR "โดยวิธีขายทอดตลาด" OR "ขายทอดตลาดพัสดุ" OR '
    '"ขายทอดตลาดครุภัณฑ์" OR "ขายทอดตลาดวัสดุ" OR '
    '"ไม่จำเป็นต้องใช้ในราชการ" OR "พัสดุชำรุดเสื่อมสภาพ" OR '
    '"ประกาศขายทอดตลาด" OR "ประมูลขายทอดตลาด"'
)

VERB_ANNOUNCE = '"ประกาศขาย" OR "ประกาศจำหน่าย"'

VERB_BROAD = (
    '"จำหน่ายพัสดุ" OR "จำหน่ายครุภัณฑ์" OR '
    '"จำหน่ายพัสดุชำรุด" OR "จำหน่ายครุภัณฑ์ชำรุด" OR '
    '"จำหน่ายของเสื่อมสภาพ" OR "จำหน่ายพัสดุเสื่อมสภาพ" OR '
    '"ระบายพัสดุ" OR "ระบายครุภัณฑ์" OR "จำหน่ายทรัพย์สิน"'
)

# ใช้กับกลุ่ม C (จังหวัด) — สั้นลงเพื่อไม่ให้ query เกิน char limit
# ไม่ซ้ำกับ A เพราะ A ไม่ได้ filter จังหวัด
VERB_PROV = (
    '"ขายทอดตลาด" OR "จำหน่ายพัสดุ" OR '
    '"จำหน่ายครุภัณฑ์" OR "ไม่จำเป็นต้องใช้ในราชการ" OR '
    '"พัสดุชำรุดเสื่อมสภาพ"'
)

# ใช้กับกลุ่มหน่วยงาน D/E/F/G
VERB_SHORT = (
    '"ขายทอดตลาด" OR "ขายทอดตลาดพัสดุ" OR "ขายทอดตลาดครุภัณฑ์" OR '
    '"จำหน่ายพัสดุ" OR "จำหน่ายครุภัณฑ์" OR '
    '"ไม่จำเป็นต้องใช้ในราชการ" OR "พัสดุชำรุดเสื่อมสภาพ"'
)


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
# (group, query, tbs, pages)
# ══════════════════════════════════════════════════════
def build_queries() -> list[tuple[str, str, str, int]]:
    rows: list[tuple[str, str, str, int]] = []

    def add(group: str, q: str, tbs_list: list[str], pages: int):
        for t in tbs_list:
            rows.append((group, q, t, pages))

    DW = ["qdr:d", "qdr:w"]
    W  = ["qdr:w"]

    # ── A: คำเจาะจง ทุกเว็บ (ดึงลึกสุด 5 หน้า) ─────
    add("A_specific", f'({VERB_SPECIFIC}) {EX}',              DW, 5)
    add("A_specific", f'({VERB_SPECIFIC}) filetype:pdf {EX}', DW, 5)
    add("A_announce", f'({VERB_ANNOUNCE}) +"คุรุภัณฑ์" {EX}', DW, 3)
    add("A_announce", f'({VERB_ANNOUNCE}) +"พัสดุ" {EX}',     DW, 3)

    # ── B: คำกลาง + site:.th (3 หน้า) ───────────────
    add("B_broad", f'({VERB_BROAD}) site:.th {EX}',              DW, 3)
    add("B_broad", f'({VERB_BROAD}) filetype:pdf site:.th {EX}', DW, 3)
    add("B_broad", f'({VERB_BROAD}) site:.go.th {EX}',           DW, 3)
    add("B_broad", f'({VERB_BROAD}) site:.ac.th {EX}',           DW, 3)

    # ── C: จังหวัดละ query × 2 pages (d+w) ──────────
    # 77 จว. × 2p × 2(d+w) = 308 credits
    # ผลหน้า 3+ ซ้ำกับกลุ่ม A มาก จึงหยุดที่ 2
    for prov in ALL_PROVINCES:
        add("C_province", f'({VERB_PROV}) "{prov}" {EX}', DW, 2)

    # ── D: สาธารณสุข (3 หน้า) ────────────────────────
    add("D_health", f'({VERB_SHORT}) ("โรงพยาบาล" OR "สาธารณสุขจังหวัด" OR "สาธารณสุขอำเภอ") {EX}', W, 3)
    add("D_health", f'({VERB_SHORT}) ("สำนักงานสาธารณสุข" OR "สสจ" OR "สสอ") {EX}',                  W, 3)
    add("D_health", f'({VERB_SHORT}) ("กรมการแพทย์" OR "ควบคุมโรค") {EX}',                            W, 3)

    # ── E: การศึกษา (3 หน้า) ─────────────────────────
    add("E_education", f'({VERB_SHORT}) ("โรงเรียน" OR "สพป" OR "สพม" OR "เขตพื้นที่การศึกษา") {EX}',            W, 3)
    add("E_education", f'({VERB_SHORT}) ("มหาวิทยาลัย" OR "ราชภัฏ" OR "ราชมงคล") {EX}',                         W, 3)
    add("E_education", f'({VERB_SHORT}) ("วิทยาลัยเทคนิค" OR "วิทยาลัยอาชีวศึกษา" OR "วิทยาลัยการอาชีพ") {EX}', W, 3)
    add("E_education", f'({VERB_SHORT}) site:.ac.th {EX}',                                                         W, 3)

    # ── F: ท้องถิ่น (3 หน้า) ─────────────────────────
    add("F_local", f'+"องค์การบริหารส่วนตำบล" +"ขายทอดตลาด" {EX_PROC}',    W, 3)
    add("F_local", f'+"เทศบาล" +"ขายทอดตลาด" {EX_PROC}',                   W, 3)
    add("F_local", f'+"องค์การบริหารส่วนจังหวัด" +"ขายทอดตลาด" {EX_PROC}', W, 3)
    add("F_local", f'+"ศาล" +"ขายทอดตลาด" site:coj.go.th {EX_PROC}',       W, 3)

    # ── G: หน่วยงานกลาง/จังหวัด (3 หน้า) ────────────
    add("G_gov", f'+"สำนักงาน" +"จังหวัด" +"ขายทอดตลาด" {EX}',            W, 3)
    add("G_gov", f'+"สำนักงาน" +"จังหวัด" +"จำหน่ายพัสดุ" site:.th {EX}', W, 3)
    add("G_gov", f'+"สำนักงานอัยการ" +"ขายทอดตลาด" {EX}',                 W, 3)
    add("G_gov", f'+"สำนักงานสรรพากร" +"ขายทอดตลาด" {EX}',                W, 3)
    add("G_gov", f'+"สำนักงานขนส่ง" +"จังหวัด" +"ขายทอดตลาด" {EX}',       W, 3)
    add("G_gov", f'+"สำนักงานสาธารณสุขจังหวัด" +"ขายทอดตลาด" {EX}',       W, 3)

    # ── J: domain เฉพาะ (3 หน้า) ─────────────────────
    add("J_domain", f'"ทอดตลาด" site:.go.th {EX}',      DW, 3)
    add("J_domain", f'"ขายทอดตลาด" site:.ac.th {EX}',   DW, 3)
    add("J_domain", f'"ขายทอดตลาด" site:.or.th {EX}',   W,  3)

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
    raw = f"{normalize_url(url)}||{title.strip()}"
    return hashlib.sha256(raw.encode()).hexdigest()


# ══════════════════════════════════════════════════════
# SERPER
# ══════════════════════════════════════════════════════
def serper_search(query: str, tbs: str, max_pages: int) -> list[dict]:
    all_items = []
    for page in range(1, max_pages + 1):
        try:
            resp = requests.post(
                "https://google.serper.dev/search",
                headers={"X-API-KEY": SERPER_API_KEY, "Content-Type": "application/json"},
                json={"q": query, "gl": "th", "hl": "th",
                      "num": 10, "page": page, "tbs": tbs},
                timeout=20,
            )
            resp.raise_for_status()
            items = resp.json().get("organic", [])
            all_items.extend(items)
            if len(items) < 10:
                break          # ผลน้อยกว่า 10 = หมดแล้ว หยุดก่อนครบ pages
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
    sb.table(TABLE).upsert(
        rows, on_conflict="content_hash", ignore_duplicates=True
    ).execute()
    return len(rows)


# ══════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════
def main():
    sb        = create_client(SUPABASE_URL, SUPABASE_KEY)
    today_str = TODAY.isoformat()
    queries   = build_queries()

    est_credits = sum(p for _, _, _, p in queries)
    log.info(f"▶ scraper_v3  วันที่ {today_str}")
    log.info(f"   queries={len(queries)}  ≈{est_credits} credits  (budget 500)")

    total_hits  = 0
    total_saved = 0
    seen: set[str] = set()
    cur_group = ""

    for group, q, tbs, pages in queries:
        if group != cur_group:
            cur_group = group
            log.info(f"══ {group} ══")

        hits = serper_search(q, tbs, pages)
        total_hits += len(hits)
        rows = []

        for item in hits:
            url   = (item.get("link")  or "").strip()
            title = (item.get("title") or "").strip()
            if not url or not title:
                continue
            chash = make_content_hash(url, title)
            if chash in seen:
                continue
            seen.add(chash)
            rows.append({
                "content_hash": chash,
                "title":        title[:500],
                "url":          url,
                "snippet":      (item.get("snippet") or "")[:1000],
                "query_used":   q[:500],
                "search_group": group,
                "tbs_used":     tbs,
                "found_date":   today_str,
                "status":       "new",
            })

        saved = upsert_rows(sb, rows)
        total_saved += saved
        log.info(f"  [{tbs}][{len(hits):3d}hits/{saved:3d}new] {q[:75]!r}")
        time.sleep(SLEEP_SEC)

    log.info(
        f"\n✅ เสร็จสิ้น  hits:{total_hits}  ใหม่:{total_saved}  "
        f"วันที่:{today_str}"
    )


if __name__ == "__main__":
    main()