"""eventmem schema: DDL + idempotent bootstrap.


Notes:

* ``events.vec`` is a raw ``float32`` BLOB (512 dims in production, unconstrained here so tests can
  store whatever they like).
* ``event_projects`` has a *partial* unique index on ``event_id WHERE is_primary = 1``: at most one
  primary project per event, any number of secondary ones.
* The FTS5 table is external-content (``content='events'``) and kept in sync by triggers, so
  ``events`` stays the single source of truth and ``forget`` removes the lexical trace too.
* ``tokenize='trigram'`` is the only tokenizer that gives substring search over Chinese without a
  dictionary: ``unicode61`` treats a whole CJK run as one token, so ``系统盘`` inside
  ``作系统盘，原`` would never match. The price is a 3-character floor (see ``store.fts_search``,
  which falls back to LIKE below it).
* ``artifacts_fts`` mirrors ``artifacts.text`` for the same reason: design §6 has the lexical arm
  cover events *and* artifacts, and a command/error code lives in the artifact, not the event
  sentence.
"""

from __future__ import annotations

import sqlite3

SCHEMA_VERSION = 12

# ``PRAGMA`` statements are deliberately absent: journal_mode / foreign_keys are no-ops inside a
# transaction, and executescript() commits whatever is pending. ensure_schema() sets them first.
DDL = """
CREATE TABLE IF NOT EXISTS meta (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

-- 项目：由人命名，模型只能选不能造
CREATE TABLE IF NOT EXISTS projects (
  id         INTEGER PRIMARY KEY,
  name       TEXT UNIQUE NOT NULL,
  status     TEXT NOT NULL DEFAULT 'active',   -- active / archived
  created_ts REAL NOT NULL
);

-- 会话粘性：说一次项目名，整段会话默认归属
CREATE TABLE IF NOT EXISTS session_project (
  session_id TEXT PRIMARY KEY,
  project_id INTEGER REFERENCES projects(id),
  ts         REAL NOT NULL
);

-- 大块原文：命令、配置、日志、代码，不压缩
CREATE TABLE IF NOT EXISTS artifacts (
  id      INTEGER PRIMARY KEY,
  sha     TEXT UNIQUE NOT NULL,
  kind    TEXT NOT NULL,
  text    TEXT NOT NULL,
  src_msg INTEGER
);

-- 事件
CREATE TABLE IF NOT EXISTS events (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  ts         REAL NOT NULL,
  kind       TEXT NOT NULL,                     -- 决定 / 尝试 / 结果 / 变更 / 教训
  text       TEXT NOT NULL,                     -- ≤120 字，第一人称
  project_id INTEGER REFERENCES projects(id),   -- 主项目，NULL = 未归类
  art_id     INTEGER REFERENCES artifacts(id),
  art_path   TEXT,                              -- artifact 的源文件路径（v7）：同一份文件的多次改动靠它连边
  src_msg    INTEGER,
  session    TEXT,
  agent      TEXT,                              -- 哪个宿主写的（hermes / dsh …），NULL = v11 之前的老事件
  vec        BLOB,                              -- float32 向量，512 维
  edited_ts  REAL                               -- 非 NULL = 人工在可视化界面改过（彩虹高光）
);

-- 多对多：一个事件可以同时属于两个项目
CREATE TABLE IF NOT EXISTS event_projects (
  event_id   INTEGER NOT NULL,
  project_id INTEGER NOT NULL,
  is_primary INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (event_id, project_id)
);

-- 关系：只由 Jev 或写事件时的模型写入；src 是后来的事件，dst 是被关联的旧事件
-- probs：这条边所属那一问的原始概率分布（JSON，axis 4 项 / detail 3 项，含「都不是」），
--        用来以后调 margin 时不必重跑模型（设计 §5，v5）。
CREATE TABLE IF NOT EXISTS edges (
  src        INTEGER NOT NULL,
  dst        INTEGER NOT NULL,
  rel        TEXT NOT NULL,                     -- 改变 / 否定 / 重复 / 因为 / 细化
  conf       REAL NOT NULL,
  decided_by TEXT NOT NULL,                     -- llm_batch / jev / human
  cross      INTEGER NOT NULL DEFAULT 0,
  probs      TEXT,
  edited_ts  REAL,                              -- 非 NULL = 人工在可视化界面改过（彩虹高光）
  backend    TEXT NOT NULL DEFAULT '',          -- v8：哪个决策后端判的（pack 名 / default）
  PRIMARY KEY (src, dst, rel)
);

-- 判定记录：worker 对每一对**实际问过模型**的事件都写一行，不管最后有没有连边。
-- 可视化里「算过但没连上」的灰线就是 pair_evals 里没有对应 edges 的那些对；
-- axis/detail 两问的原始分布都存下来，点开灰线也能看到概率（v6）。
CREATE TABLE IF NOT EXISTS pair_evals (
  src          INTEGER NOT NULL,
  dst          INTEGER NOT NULL,
  axis_probs   TEXT,                            -- 4 项（含「都不是」）
  detail_probs TEXT,                            -- 3 项；「重复」抑制时为 NULL
  axis_label   TEXT,                            -- 过了 margin 的真实标签，否则 NULL
  detail_label TEXT,
  ts           REAL NOT NULL,
  PRIMARY KEY (src, dst)
);

-- Explicit manual edge deletions; missing rows cannot prove legacy intent.
CREATE TABLE IF NOT EXISTS edge_deletions (
  src INTEGER NOT NULL,
  dst INTEGER NOT NULL,
  rel TEXT NOT NULL,
  ts REAL NOT NULL,
  PRIMARY KEY (src, dst, rel)
);

CREATE TRIGGER IF NOT EXISTS edge_deletions_block_jev BEFORE INSERT ON edges
WHEN NEW.decided_by = 'jev' AND EXISTS (
  SELECT 1 FROM edge_deletions WHERE src = NEW.src AND dst = NEW.dst
)
BEGIN
  SELECT RAISE(IGNORE);
END;

-- 被拖动/被拉开的原子位置。格子布局是确定性的，这张表只记住「人把它放到了哪」，
-- 所以没有它图谱照样能画，有它刷新之后才不会立刻弹回格子（v12）。
CREATE TABLE IF NOT EXISTS atom_pos (
  id         INTEGER PRIMARY KEY,
  x          REAL NOT NULL,
  y          REAL NOT NULL,
  z          REAL NOT NULL,
  updated_ts REAL NOT NULL
);

-- 回合末抽取失败（v10）。抽取模型调用失败或输出不可解析时**不写记忆**：一段没有结论的
-- 对话不该变成一条记忆，失败本身才是要让人看见的东西，所以只留一行警告给控制台。
CREATE TABLE IF NOT EXISTS extract_failures (
  id      INTEGER PRIMARY KEY AUTOINCREMENT,
  ts      REAL NOT NULL,
  session TEXT,
  reason  TEXT NOT NULL,                        -- call_failed / unparsable
  detail  TEXT
);

-- 墓碑：只有 id + 内容哈希，永不含文本（设计文档 §8）
CREATE TABLE IF NOT EXISTS tombstones (
  id  INTEGER PRIMARY KEY,
  sha TEXT NOT NULL,
  ts  REAL NOT NULL
);

-- 回合级待补（v3）已废弃（v5 改为回合末 auxiliary LLM 抽取）；老库重开时清掉这张表。
DROP TABLE IF EXISTS pending_turns;

CREATE INDEX IF NOT EXISTS idx_events_ts      ON events(ts);
CREATE INDEX IF NOT EXISTS idx_events_session ON events(session, ts);
CREATE INDEX IF NOT EXISTS idx_events_project ON events(project_id);
CREATE INDEX IF NOT EXISTS idx_events_art     ON events(art_id);
CREATE INDEX IF NOT EXISTS idx_ep_project     ON event_projects(project_id);
CREATE INDEX IF NOT EXISTS idx_edges_dst      ON edges(dst);
CREATE INDEX IF NOT EXISTS idx_edges_src      ON edges(src);
CREATE INDEX IF NOT EXISTS idx_pair_evals_dst ON pair_evals(dst);
CREATE INDEX IF NOT EXISTS idx_extract_failures_ts ON extract_failures(ts);

-- 每个事件最多一个主项目
CREATE UNIQUE INDEX IF NOT EXISTS idx_ep_primary
  ON event_projects(event_id) WHERE is_primary = 1;

-- 词法臂：中文靠 trigram 子串匹配（见模块 docstring）
CREATE VIRTUAL TABLE IF NOT EXISTS events_fts USING fts5(
  text,
  content='events',
  content_rowid='id',
  tokenize='trigram'
);

CREATE TRIGGER IF NOT EXISTS events_fts_ai AFTER INSERT ON events BEGIN
  INSERT INTO events_fts(rowid, text) VALUES (new.id, new.text);
END;

CREATE TRIGGER IF NOT EXISTS events_fts_ad AFTER DELETE ON events BEGIN
  INSERT INTO events_fts(events_fts, rowid, text) VALUES ('delete', old.id, old.text);
END;

CREATE TRIGGER IF NOT EXISTS events_fts_au AFTER UPDATE OF text ON events BEGIN
  INSERT INTO events_fts(events_fts, rowid, text) VALUES ('delete', old.id, old.text);
  INSERT INTO events_fts(rowid, text) VALUES (new.id, new.text);
END;

-- 词法臂第二张表：artifact 全文。命令、配置、日志、报错码只在 artifact 里，事件正文
-- 往往一个字都不含；只索引 events 会让「按命令找那次训练」永远查不到（设计 §6）。
CREATE VIRTUAL TABLE IF NOT EXISTS artifacts_fts USING fts5(
  text,
  content='artifacts',
  content_rowid='id',
  tokenize='trigram'
);

CREATE TRIGGER IF NOT EXISTS artifacts_fts_ai AFTER INSERT ON artifacts BEGIN
  INSERT INTO artifacts_fts(rowid, text) VALUES (new.id, new.text);
END;

CREATE TRIGGER IF NOT EXISTS artifacts_fts_ad AFTER DELETE ON artifacts BEGIN
  INSERT INTO artifacts_fts(artifacts_fts, rowid, text) VALUES ('delete', old.id, old.text);
END;

CREATE TRIGGER IF NOT EXISTS artifacts_fts_au AFTER UPDATE OF text ON artifacts BEGIN
  INSERT INTO artifacts_fts(artifacts_fts, rowid, text) VALUES ('delete', old.id, old.text);
  INSERT INTO artifacts_fts(rowid, text) VALUES (new.id, new.text);
END;
"""


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Create/upgrade the schema. Safe to call on every connection open.

    Sets WAL + foreign_keys (both per-connection), applies the DDL, records the version in
    ``meta``, and rebuilds a lexical index once if it is empty while its content table has
    rows (a database written before that FTS table landed).
    """
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=5000")

    conn.executescript(DDL)

    # v5: ``edges.probs``. ``CREATE TABLE IF NOT EXISTS`` never adds a column to an existing
    # table, so a v4 database needs the explicit ALTER (the column keeps its NULL for old rows).
    edge_cols = {row[1] for row in conn.execute("PRAGMA table_info(edges)")}
    if "probs" not in edge_cols:
        conn.execute("ALTER TABLE edges ADD COLUMN probs TEXT")

    # v6: 人工编辑时间戳（可视化界面）。没有它就只能知道「改过」而不能显示在图上。
    event_cols = {row[1] for row in conn.execute("PRAGMA table_info(events)")}
    if "edited_ts" not in event_cols:
        conn.execute("ALTER TABLE events ADD COLUMN edited_ts REAL")
    if "edited_ts" not in edge_cols:
        conn.execute("ALTER TABLE edges ADD COLUMN edited_ts REAL")

    # v7: artifact 的源文件路径。``sha`` 是内容地址——内容一改 id 就换，同一份配置文件
    # 改三次会是三个 sha；「路径」才是文件的身份，Tier-2 的 artifact 腿靠它才连得上。
    if "art_path" not in event_cols:
        conn.execute("ALTER TABLE events ADD COLUMN art_path TEXT")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_events_art_path ON events(art_path)")

    # v8: 这条边是哪个决策后端判的。换后端（云端 Jev / 本地 4B / LM Studio）后 probs 的
    # 分布形状不同，重算 margin 时必须知道来源；decided_by 仍然只回答「谁算的」。
    if "backend" not in edge_cols:
        conn.execute("ALTER TABLE edges ADD COLUMN backend TEXT NOT NULL DEFAULT ''")

    # v11: 这条事件是哪个宿主写的。两个 agent 共用一个库时，召回/图谱才能回答「这是谁记的」；
    # NULL = 拆分前的老事件（不猜、不回填——同一个 session 也可以来自任何宿主）。
    if "agent" not in event_cols:
        conn.execute("ALTER TABLE events ADD COLUMN agent TEXT")

    conn.execute(
        "INSERT INTO meta(key, value) VALUES('schema_version', ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (str(SCHEMA_VERSION),),
    )

    # ``count(*)`` on an external-content FTS table is answered from the *content* table, so it
    # is never 0 while ``events`` has rows — it cannot detect a missing index. The ``_docsize``
    # shadow table has one row per indexed document and is the honest emptiness probe.
    for table, fts in (("events", "events_fts"), ("artifacts", "artifacts_fts")):
        try:
            n_rows = conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            n_fts = conn.execute(f"SELECT count(*) FROM {fts}_docsize").fetchone()[0]
            if n_rows and not n_fts:
                conn.execute(f"INSERT INTO {fts}({fts}) VALUES('rebuild')")
        except sqlite3.Error:  # a broken FTS index must not stop the plugin from opening
            pass

    conn.commit()
