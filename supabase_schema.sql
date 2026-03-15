-- ══════════════════════════════════════════════════════════
-- Supabase Schema — ระบบข่าวขายทอดตลาดราชการ (anytime search)
-- รัน SQL นี้ใน Supabase → SQL Editor
--
-- ใช้ prefix "anytime_" เพื่อไม่ชนกับตารางโปรเจคเดิม
-- (action / global_excludes / province_groups / search_settings)
-- ══════════════════════════════════════════════════════════

-- ── ตาราง: ผลการค้นหา ────────────────────────────────────
CREATE TABLE IF NOT EXISTS anytime_results (
    id             BIGSERIAL PRIMARY KEY,

    -- dedup key = SHA256(normalized_url + "||" + title)
    -- ใช้ทั้ง url + title เพราะบางเว็บใช้ URL เดิมซ้ำแต่เปลี่ยนเนื้อหา
    content_hash   TEXT UNIQUE NOT NULL,

    title          TEXT NOT NULL,
    url            TEXT NOT NULL,
    snippet        TEXT,
    query_used     TEXT,           -- query string ที่ใช้ค้น
    search_group   TEXT,           -- กลุ่ม เช่น ท้องถิ่น / การศึกษา
    found_date     DATE NOT NULL DEFAULT CURRENT_DATE,
    created_at     TIMESTAMPTZ DEFAULT NOW(),

    -- new = ยังไม่อ่าน | read = อ่านแล้ว
    status         TEXT DEFAULT 'new'
                   CHECK (status IN ('new', 'read')),
    read_at        TIMESTAMPTZ,    -- NULL = ยังไม่อ่าน
    updated_at     TIMESTAMPTZ DEFAULT NOW()
);

-- ── Index ─────────────────────────────────────────────────
CREATE INDEX IF NOT EXISTS idx_anytime_found_date  ON anytime_results(found_date DESC);
CREATE INDEX IF NOT EXISTS idx_anytime_status      ON anytime_results(status);
CREATE INDEX IF NOT EXISTS idx_anytime_group       ON anytime_results(search_group);
CREATE INDEX IF NOT EXISTS idx_anytime_read_at     ON anytime_results(read_at DESC NULLS LAST);
CREATE INDEX IF NOT EXISTS idx_anytime_created_at  ON anytime_results(created_at DESC);

-- ── Trigger: auto-update updated_at ──────────────────────
-- ตรวจสอบก่อนว่า function ชื่อนี้มีอยู่แล้วหรือไม่
-- ถ้าโปรเจคเดิมสร้าง update_updated_at() ไว้แล้วก็ใช้ร่วมกันได้เลย
CREATE OR REPLACE FUNCTION update_updated_at()
RETURNS TRIGGER AS $$
BEGIN NEW.updated_at = NOW(); RETURN NEW; END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_anytime_updated_at
BEFORE UPDATE ON anytime_results
FOR EACH ROW EXECUTE FUNCTION update_updated_at();

-- ── Row Level Security ────────────────────────────────────
ALTER TABLE anytime_results ENABLE ROW LEVEL SECURITY;

CREATE POLICY "anytime_allow_select" ON anytime_results FOR SELECT USING (true);
CREATE POLICY "anytime_allow_insert" ON anytime_results FOR INSERT WITH CHECK (true);
CREATE POLICY "anytime_allow_update" ON anytime_results FOR UPDATE USING (true);

-- ── Helper view: นับยังไม่อ่าน ───────────────────────────
CREATE OR REPLACE VIEW anytime_unread_count AS
SELECT COUNT(*) AS total FROM anytime_results WHERE status = 'new';
