"""
scraper.py  —  ค้นหาข่าวขายทอดตลาดราชการ → Supabase
รัน: python scraper.py

หมายเหตุ dedup:
  ใช้ content_hash = SHA256(normalized_url + "||" + title)
  แทนการ hash แค่ URL เพราะบางเว็บใช้ URL เดิมซ้ำแต่เปลี่ยนเนื้อหาประกาศ
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

NUM_RESULTS = 10      # ต่อหน้า (Serper Free = 10/หน้า)
NUM_PAGES   = 3      # ดึง 3 หน้า = 30 results/query = 3 credits/query
SLEEP_SEC   = 0.6
TABLE       = "anytime_results"

NOISE_PARAMS = {"utm_source","utm_medium","utm_campaign","utm_content","utm_term",
                "fbclid","gclid","sessionid","sid","token","_ga","ref","source"}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

# ══════════════════════════════════════════════════════
# ปี พ.ศ.  —  ปีนี้กับปีที่แล้ว
# ══════════════════════════════════════════════════════
TODAY        = date.today()
THIS_YEAR_BE = TODAY.year + 543 - 1      # ค.ศ. 2026 → พ.ศ. 2568
NEXT_YEAR_BE = THIS_YEAR_BE + 1         # พ.ศ. 2569
YR           = f"({THIS_YEAR_BE} OR {NEXT_YEAR_BE})"

def yq(base: str) -> list[str]:
    """คืน 2 query: แบบไม่มีปี + มีปีนี้+ปีที่แล้ว"""
    return [base, f"{base} {YR}"]

# ══════════════════════════════════════════════════════
# เว็บขยะ — ใส่ใน query ทุกตัวที่ไม่ได้ระบุ site: เฉพาะ
# ══════════════════════════════════════════════════════
TRASH = "-site:led.go.th -site:youtube.com -site:x.com -site:instagram.com -site:tiktok.com -บังคับคดี"

# ══════════════════════════════════════════════════════
# ชุดคำกริยา — ใช้ซ้ำในหลายกลุ่ม
# ══════════════════════════════════════════════════════
# คำเจาะจง (specific) — ค้นทุกเว็บ ไม่จำกัด site
VERB_SPECIFIC = (
    '"ขายทอดตลาด" OR "โดยวิธีขายทอดตลาด" OR "ขายทอดตลาดพัสดุ" OR '
    '"ขายทอดตลาดครุภัณฑ์" OR "ขายทอดตลาดรถยนต์" OR "ขายทอดตลาดอาคาร" OR '
    '"ไม่จำเป็นต้องใช้ในราชการ" OR "พัสดุชำรุดเสื่อมสภาพ"'
)

# คำกลาง (broad) — ใช้กับ site:.th เพื่อกรองให้แคบลง
VERB_BROAD = (
    '"จำหน่ายพัสดุ" OR "จำหน่ายครุภัณฑ์" OR "จำหน่ายพัสดุชำรุด" OR '
    '"จำหน่ายครุภัณฑ์ชำรุด" OR "ระบายพัสดุ" OR "ระบายครุภัณฑ์" OR '
    '"จำหน่ายทรัพย์สิน" OR "ขายทรัพย์สิน" OR "ประมูลขาย" OR "เปิดประมูลขาย" OR '
    '"จำหน่ายของเสื่อมสภาพ" OR "จำหน่ายพัสดุเสื่อมสภาพ" OR '
    '"จำหน่ายครุภัณฑ์เสื่อมสภาพ" OR "จำหน่ายสิ่งของที่ไม่ใช้แล้ว" OR "ระบายทรัพย์สิน"'
)

# คำกลาง full (specific + broad รวมกัน) สำหรับกลุ่มหน่วยงานและจังหวัด
VERB_ALL = (
    '"ขายทอดตลาด" OR "โดยวิธีขายทอดตลาด" OR "ขายทอดตลาดพัสดุ" OR '
    '"ขายทอดตลาดครุภัณฑ์" OR "ขายทอดตลาดรถยนต์" OR "ขายทอดตลาดอาคาร" OR '
    '"จำหน่ายพัสดุ" OR "จำหน่ายครุภัณฑ์" OR "จำหน่ายพัสดุชำรุด" OR '
    '"ระบายพัสดุ" OR "ระบายครุภัณฑ์" OR "จำหน่ายทรัพย์สิน" OR '
    '"ไม่จำเป็นต้องใช้ในราชการ" OR "พัสดุชำรุดเสื่อมสภาพ" OR '
    '"จำหน่ายของเสื่อมสภาพ" OR "จำหน่ายพัสดุเสื่อมสภาพ" OR "ประมูลขาย"'
)

# ── จังหวัด รายภูมิภาค ──
P_NORTH  = "เชียงใหม่ OR เชียงราย OR น่าน OR พะเยา OR แพร่ OR แม่ฮ่องสอน OR ลำปาง OR ลำพูน OR อุตรดิตถ์"
P_NORTH2 = "พิษณุโลก OR สุโขทัย OR กำแพงเพชร OR ตาก OR เพชรบูรณ์ OR พิจิตร OR นครสวรรค์ OR อุทัยธานี OR ชัยนาท"
P_NE     = "กาฬสินธุ์ OR ขอนแก่น OR ชัยภูมิ OR นครพนม OR นครราชสีมา OR โคราช OR บึงกาฬ OR บุรีรัมย์ OR มหาสารคาม OR มุกดาหาร"
P_NE2    = "ยโสธร OR ร้อยเอ็ด OR เลย OR ศรีสะเกษ OR สกลนคร OR สุรินทร์ OR หนองคาย OR หนองบัวลำภู OR อำนาจเจริญ OR อุดรธานี OR อุบลราชธานี"
P_CEN    = "กรุงเทพ OR นนทบุรี OR ปทุมธานี OR สมุทรปราการ OR อยุธยา OR นครปฐม OR สมุทรสงคราม OR สมุทรสาคร OR สระบุรี OR ลพบุรี OR สิงห์บุรี OR สุพรรณบุรี OR อ่างทอง OR นครนายก"
P_EAST   = "จันทบุรี OR ฉะเชิงเทรา OR ชลบุรี OR ตราด OR ปราจีนบุรี OR ระยอง OR สระแก้ว"
P_WEST   = "กาญจนบุรี OR ตาก OR ประจวบคีรีขันธ์ OR เพชรบุรี OR ราชบุรี"
P_SOUTH  = "กระบี่ OR ชุมพร OR ตรัง OR นครศรีธรรมราช OR นราธิวาส OR ปัตตานี OR พังงา OR พัทลุง OR ภูเก็ต OR ระนอง OR สตูล OR สงขลา OR สุราษฎร์ธานี OR ยะลา"

# ══════════════════════════════════════════════════════
# QUERY LIST
# ══════════════════════════════════════════════════════
QUERIES: dict[str, list[str]] = {

    # ─── กลุ่ม 1: คำเจาะจง — ค้นทุกเว็บ (ไม่จำกัด site) ───
    # คำพวกนี้เฉพาะเจาะจงพอ ไม่ต้องระบุ site
    "คำเจาะจง_ทุกเว็บ": [
        *yq(f'({VERB_SPECIFIC}) {TRASH}'),
        # + PDF
        *yq(f'({VERB_SPECIFIC}) filetype:pdf {TRASH}'),
    ],

    # ─── กลุ่ม 2: คำกลาง — จำกัด site:.th ──────────────────
    # คำกว้างต้องจำกัด domain ไม่งั้นได้ขยะเยอะ
    "คำกลาง_site.th": [
        *yq(f'({VERB_BROAD}) site:.th {TRASH}'),
        *yq(f'({VERB_BROAD}) filetype:pdf site:.th {TRASH}'),
    ],

    # ─── กลุ่ม 3: หน่วยงานราชการ + ชุดคำเต็ม ───────────────
    "โรงพยาบาล_สาธารณสุข": [
        *yq(f'({VERB_ALL}) ("โรงพยาบาล" OR "สาธารณสุขจังหวัด" OR "สาธารณสุขอำเภอ" OR "ควบคุมโรค" OR "สำนักงานสาธารณสุข") {TRASH}'),
    ],

    "ท้องถิ่น": [
        *yq(f'({VERB_ALL}) ("อบต" OR "เทศบาลตำบล" OR "เทศบาลเมือง" OR "เทศบาลนคร" OR "อบจ" OR "องค์การบริหารส่วน") {TRASH}'),
    ],

    "การศึกษา": [
        *yq(f'({VERB_ALL}) ("มหาวิทยาลัย" OR "ราชภัฎ" OR "ราชมงคล" OR "วิทยาลัยเทคนิค" OR "วิทยาลัยอาชีวศึกษา" OR "สพป" OR "สพม" OR "เขตพื้นที่การศึกษา" OR "โรงเรียน") {TRASH}'),
        *yq(f'({VERB_ALL}) site:.ac.th {TRASH}'),
    ],

    "ความมั่นคง": [
        *yq(f'({VERB_ALL}) ("ตำรวจภูธร" OR "ตำรวจนครบาล" OR "กองบัญชาการ" OR "กองทัพบก" OR "กองทัพเรือ" OR "กองทัพอากาศ" OR "กรมทหาร" OR "กองพล") {TRASH}'),
        *yq(f'({VERB_ALL}) (site:rta.mi.th OR site:rtaf.mi.th OR site:navy.mi.th)'),
    ],

    "กระทรวง_กรม": [
        *yq(f'({VERB_ALL}) ("เรือนจำ" OR "กรมราชทัณฑ์" OR "สถานพินิจ" OR "คุมประพฤติ" OR "กรมพัฒนาที่ดิน" OR "กรมโยธาธิการ" OR "กรมทางหลวงชนบท" OR "กรมการแพทย์") {TRASH}'),
        *yq(f'({VERB_ALL}) ("สรรพากร" OR "สรรพสามิต" OR "ปปส" OR "ปปง" OR "กรมบัญชีกลาง" OR "กรมพัฒนาธุรกิจ" OR "กรมส่งเสริมการเกษตร") {TRASH}'),
    ],

    # ─── กลุ่ม 4: จังหวัด + ชุดคำเต็ม ──────────────────────
    # ใช้ VERB_ALL เหมือนกลุ่มหน่วยงาน + จำกัด site:.th
    "จังหวัด_เหนือ": [
        *yq(f'({VERB_ALL}) ({P_NORTH}) site:.th {TRASH}'),
        *yq(f'({VERB_ALL}) ({P_NORTH2}) site:.th {TRASH}'),
    ],

    "จังหวัด_อีสาน": [
        *yq(f'({VERB_ALL}) ({P_NE}) site:.th {TRASH}'),
        *yq(f'({VERB_ALL}) ({P_NE2}) site:.th {TRASH}'),
    ],

    "จังหวัด_กลาง_ตะวันออก_ตะวันตก": [
        *yq(f'({VERB_ALL}) ({P_CEN}) site:.th {TRASH}'),
        *yq(f'({VERB_ALL}) ({P_EAST}) site:.th {TRASH}'),
        *yq(f'({VERB_ALL}) ({P_WEST}) site:.th {TRASH}'),
    ],

    "จังหวัด_ใต้": [
        *yq(f'({VERB_ALL}) ({P_SOUTH}) site:.th {TRASH}'),
    ],
}

# ══════════════════════════════════════════════════════
# HELPER: normalize URL + content_hash
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
# SERPER  —  ยิง 1 query, ดึง 100 รายการ
# ══════════════════════════════════════════════════════
def serper_search(query: str) -> list[dict]:
    """ดึงผล 3 หน้า (30 รายการ) ต่อ query — ใช้ 3 credits"""
    all_items = []
    for page in range(1, NUM_PAGES + 1):
        try:
            resp = requests.post(
                "https://google.serper.dev/search",
                headers={"X-API-KEY": SERPER_API_KEY, "Content-Type": "application/json"},
                json={"q": query, "gl": "th", "hl": "th",
                      "num": NUM_RESULTS, "page": page},
                timeout=20,
            )
            resp.raise_for_status()
            items = resp.json().get("organic", [])
            all_items.extend(items)
            if len(items) < NUM_RESULTS:
                break          # หน้าสุดท้ายแล้ว ไม่ต้องดึงต่อ
            time.sleep(0.3)    # หน่วงเล็กน้อยระหว่างหน้า
        except Exception as e:
            log.warning(f"Serper error page={page} [{query[:50]}]: {e}")
            break
    return all_items

# ══════════════════════════════════════════════════════
# SUPABASE  —  upsert (skip ถ้า content_hash ซ้ำ)
# ══════════════════════════════════════════════════════
def upsert_results(sb, rows: list[dict]) -> int:
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
    sb = create_client(SUPABASE_URL, SUPABASE_KEY)
    today_str = TODAY.isoformat()
    log.info(f"▶ เริ่ม scrape วันที่ {today_str}  ปีที่ค้น: {THIS_YEAR_BE}/{NEXT_YEAR_BE}")

    total_saved = 0
    total_hits  = 0
    seen: set[str] = set()

    for group, queries in QUERIES.items():
        log.info(f"══ {group} ({len(queries)} queries) ══")

        for query in queries:
            items = serper_search(query)
            rows  = []

            for item in items:
                url   = (item.get("link") or "").strip()
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
                    "query_used":   query[:500],
                    "search_group": group,
                    "found_date":   today_str,
                    "status":       "new",
                })

            saved = upsert_results(sb, rows)
            total_saved += saved
            total_hits  += len(items)
            log.info(f"  [{len(items):3d} hits / {saved:3d} new]  {query[:90]!r}")
            time.sleep(SLEEP_SEC)

    log.info(f"\n✅ เสร็จสิ้น  hits:{total_hits}  ใหม่:{total_saved}  วันที่:{today_str}")

if __name__ == "__main__":
    main()
