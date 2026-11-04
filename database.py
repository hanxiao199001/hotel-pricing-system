"""
数据库模块 - 存储历史价格和分析数据
"""
import os
import sqlite3
from datetime import datetime
from typing import List, Dict, Optional


class DatabasePathError(RuntimeError):
    """数据库文件无法创建/打开（路径错误、无写权限、目录不可用）。"""


def _prepare_db_path(db_path: str) -> str:
    """规范化数据库路径并确保其父目录存在。"""
    if not db_path or not str(db_path).strip():
        raise DatabasePathError("数据库路径为空，请检查 PRICING_DB_PATH 配置。")

    path = os.path.abspath(os.path.expanduser(str(db_path)))
    parent = os.path.dirname(path)

    if not os.path.isdir(parent):
        try:
            os.makedirs(parent, exist_ok=True)
        except OSError as exc:
            raise DatabasePathError(
                f"数据库目录不存在且无法创建: {parent}\n"
                f"    当前 PRICING_DB_PATH = {db_path}\n"
                f"    提示: 留空该变量即可使用项目目录下的 pricing.db，"
                f"或填一个已存在的目录（Windows 用 D:/xxx/pricing.db 这种写法）。"
            ) from exc

    if os.path.isdir(path):
        raise DatabasePathError(
            f"PRICING_DB_PATH 指向的是一个目录，不是数据库文件: {path}"
        )

    return path


class PricingDatabase:
    def __init__(self, db_path: str):
        self.db_path = _prepare_db_path(db_path)
        self.init_database()

    def init_database(self):
        """初始化数据库表"""
        try:
            conn = sqlite3.connect(self.db_path)
        except sqlite3.OperationalError as exc:
            raise DatabasePathError(
                f"无法打开数据库文件: {self.db_path}\n"
                f"    系统报错: {exc}\n"
                f"    提示: 请确认该路径所在磁盘存在且有写权限。"
            ) from exc
        cursor = conn.cursor()
        
        # 自己酒店价格历史
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS my_hotel_prices (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date DATE NOT NULL,
            price DECIMAL(10,2) NOT NULL,
            occupancy_rate DECIMAL(5,2),
            rooms_available INTEGER,
            rooms_total INTEGER,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """)
        
        # 竞品酒店价格
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS competitor_prices (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date DATE NOT NULL,
            hotel_name TEXT NOT NULL,
            hotel_id TEXT,
            price DECIMAL(10,2) NOT NULL,
            distance_km DECIMAL(5,2),
            rating DECIMAL(3,1),
            reviews_count INTEGER,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """)
        
        # 调价记录
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS price_adjustments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date DATE NOT NULL,
            old_price DECIMAL(10,2) NOT NULL,
            new_price DECIMAL(10,2) NOT NULL,
            reason TEXT,
            occupancy_rate DECIMAL(5,2),
            competitor_avg DECIMAL(10,2),
            auto_adjusted BOOLEAN DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """)
        
        # 调价建议
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS price_suggestions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date DATE NOT NULL,
            current_price DECIMAL(10,2) NOT NULL,
            suggested_price DECIMAL(10,2) NOT NULL,
            reason TEXT,
            occupancy_rate DECIMAL(5,2),
            competitor_min DECIMAL(10,2),
            competitor_avg DECIMAL(10,2),
            competitor_max DECIMAL(10,2),
            status TEXT DEFAULT 'pending',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """)
        
        # 差评回复草稿
        #
        # review_key 是「日期+评分+正文」的哈希，不用列表下标：
        # 重新采集后点评顺序会变，下标一错就会把「已采用」标到别的点评上。
        # (review_key, source) 唯一——同一条点评的模板稿和模型稿各存一份，可对比。
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS reply_drafts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            review_key TEXT NOT NULL,
            review_date DATE,
            rating DECIMAL(3,1),
            room TEXT,
            themes TEXT,
            draft TEXT NOT NULL,
            source TEXT NOT NULL DEFAULT 'template',
            status TEXT NOT NULL DEFAULT 'draft',
            edited INTEGER NOT NULL DEFAULT 0,
            note TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP
        )
        """)
        cursor.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS ux_reply_drafts_key_source "
            "ON reply_drafts(review_key, source)"
        )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS ix_reply_drafts_status ON reply_drafts(status)"
        )

        # 每日自动采集的执行记录（判重的唯一事实源，不靠进程内存）
        #   (collect_type, date) 唯一：价格/点评各一行。
        #   status: done = 当天已采到数据；failed = 尝试过但失败
        #   attempts 只统计「自动」尝试；手动「立即采集」成功同样写 done，
        #   让当天自动任务跳过，但不占用自动重试的次数额度。
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS daily_collects (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            collect_type TEXT NOT NULL,
            date DATE NOT NULL,
            status TEXT NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 0,
            job_id TEXT,
            detail TEXT,
            updated_at TIMESTAMP
        )
        """)
        cursor.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS ux_daily_collects_type_date "
            "ON daily_collects(collect_type, date)"
        )

        self._dedupe_and_add_unique_indexes(cursor, conn)
        self._ensure_decision_columns(cursor, conn)

        conn.commit()
        conn.close()
        print("✅ 数据库初始化完成")

    # 唯一键定义：表名 -> (GROUP BY 子句, 列名元组)
    _UNIQUE_KEYS = (
        ("my_hotel_prices", "date", ("date",)),
        ("competitor_prices", "date, hotel_name", ("date", "hotel_name")),
        ("price_suggestions", "date", ("date",)),
    )

    def _dedupe_and_add_unique_indexes(self, cursor, conn):
        """保证「同一天同一对象只有一条」。

        原实现是纯 INSERT，同一天跑多次就会叠加：实测一天累积出 93 条竞品、
        10 条自家价，历史分析（COUNT/AVG）直接失真。这里先去重保留最新一条，
        再建唯一索引，把约束交给数据库而不是靠调用方自觉。
        """
        removed = 0
        for table, group_by, _cols in self._UNIQUE_KEYS:
            before = cursor.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            cursor.execute(
                f"DELETE FROM {table} WHERE id NOT IN "
                f"(SELECT MAX(id) FROM {table} GROUP BY {group_by})"
            )
            after = cursor.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            removed += before - after

        for table, _group_by, cols in self._UNIQUE_KEYS:
            index_name = "ux_" + table + "_" + "_".join(cols)
            cursor.execute(
                f"CREATE UNIQUE INDEX IF NOT EXISTS {index_name} "
                f"ON {table}({', '.join(cols)})"
            )

        conn.commit()
        if removed:
            print(f"🧹 已清理历史重复记录 {removed} 条（同一天只保留最新一条）")

    def _ensure_decision_columns(self, cursor, conn):
        """给存量库补上「人工拍板」需要的列。

        price_suggestions 原来只有 status 一个默认 'pending'，没有任何地方能把它
        改成 approved/rejected——所谓「建议模式」其实是个死状态。中台要支持人工
        确认，至少得记录谁在什么时候按了什么结论、附带什么备注。
        """
        existing = {
            row[1] for row in cursor.execute("PRAGMA table_info(price_suggestions)")
        }
        for name, ddl in (
            ("decided_at", "TIMESTAMP"),
            ("decision_note", "TEXT"),
        ):
            if name not in existing:
                cursor.execute(
                    f"ALTER TABLE price_suggestions ADD COLUMN {name} {ddl}"
                )

        # 调价记录表补上「关联的建议id」与「执行人」，便于追溯
        adj_cols = {
            row[1] for row in cursor.execute("PRAGMA table_info(price_adjustments)")
        }
        for name, ddl in (
            ("suggestion_id", "INTEGER"),
            ("decided_by", "TEXT"),
        ):
            if name not in adj_cols:
                cursor.execute(
                    f"ALTER TABLE price_adjustments ADD COLUMN {name} {ddl}"
                )
        conn.commit()
    
    def save_my_price(self, date: str, price: float, occupancy_rate: float, 
                      rooms_available: int = None, rooms_total: int = None):
        """保存自己酒店的价格和入住率"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO my_hotel_prices (date, price, occupancy_rate, rooms_available, rooms_total)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(date) DO UPDATE SET
                price = excluded.price,
                occupancy_rate = excluded.occupancy_rate,
                rooms_available = excluded.rooms_available,
                rooms_total = excluded.rooms_total,
                created_at = CURRENT_TIMESTAMP
        """, (date, price, occupancy_rate, rooms_available, rooms_total))
        conn.commit()
        conn.close()
    
    def save_competitor_prices(self, date: str, competitors: List[Dict]):
        """保存竞品价格"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        for comp in competitors:
            cursor.execute("""
                INSERT INTO competitor_prices 
                (date, hotel_name, hotel_id, price, distance_km, rating, reviews_count)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(date, hotel_name) DO UPDATE SET
                    hotel_id = excluded.hotel_id,
                    price = excluded.price,
                    distance_km = excluded.distance_km,
                    rating = excluded.rating,
                    reviews_count = excluded.reviews_count,
                    created_at = CURRENT_TIMESTAMP
            """, (
                date,
                comp.get('name'),
                comp.get('id'),
                comp.get('price'),
                comp.get('distance'),
                comp.get('rating'),
                comp.get('reviews')
            ))
        
        conn.commit()
        conn.close()
    
    def save_price_suggestion(self, date: str, current_price: float, 
                            suggested_price: float, reason: str,
                            occupancy_rate: float, competitor_stats: Dict):
        """保存调价建议"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO price_suggestions 
            (date, current_price, suggested_price, reason, occupancy_rate, 
             competitor_min, competitor_avg, competitor_max)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(date) DO UPDATE SET
                current_price = excluded.current_price,
                suggested_price = excluded.suggested_price,
                reason = excluded.reason,
                occupancy_rate = excluded.occupancy_rate,
                competitor_min = excluded.competitor_min,
                competitor_avg = excluded.competitor_avg,
                competitor_max = excluded.competitor_max,
                status = 'pending',
                decided_at = NULL,
                decision_note = NULL,
                created_at = CURRENT_TIMESTAMP
        """, (
            date,
            current_price,
            suggested_price,
            reason,
            occupancy_rate,
            competitor_stats.get('min'),
            competitor_stats.get('avg'),
            competitor_stats.get('max')
        ))
        conn.commit()
        conn.close()
    
    def get_recent_competitors(self, days: int = 7) -> List[Dict]:
        """获取最近N天的竞品数据"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute("""
            SELECT date, hotel_name, AVG(price) as avg_price, COUNT(*) as count
            FROM competitor_prices
            WHERE date >= date('now', '-' || ? || ' days')
            GROUP BY date, hotel_name
            ORDER BY date DESC
        """, (days,))
        results = cursor.fetchall()
        conn.close()
        
        return [
            {"date": r[0], "hotel": r[1], "price": r[2], "count": r[3]}
            for r in results
        ]

    # ------------------------------------------------------------------
    # 以下方法供 Web 中台读取，只做查询与「人工拍板」，不改动定价逻辑
    # ------------------------------------------------------------------

    def _rows(self, sql: str, params=()) -> List[sqlite3.Row]:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            return conn.execute(sql, params).fetchall()
        finally:
            conn.close()

    def get_dates(self) -> List[str]:
        """有竞品数据的日期，新的在前。"""
        return [
            r["date"]
            for r in self._rows(
                "SELECT DISTINCT date FROM competitor_prices ORDER BY date DESC"
            )
        ]

    def mark_daily_collect(self, collect_type, date_str, status, *, job_id=None,
                           detail=None, count_attempt=False):
        """记录/更新某类数据在某天的采集结果（幂等 upsert）。

        count_attempt=True 只在每日自动采集的执行链里用：attempts 只统计
        自动尝试，手动「立即采集」不占用自动重试的次数额度。
        updated_at 写本地时间，与任务列表的 started/finished 同口径
        （不用 SQLite 的 CURRENT_TIMESTAMP，那是 UTC，冷却间隔会算错 8 小时）。
        """
        conn = sqlite3.connect(self.db_path)
        try:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO daily_collects
                    (collect_type, date, status, attempts, job_id, detail, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(collect_type, date) DO UPDATE SET
                    status = excluded.status,
                    attempts = attempts + excluded.attempts,
                    job_id = excluded.job_id,
                    detail = excluded.detail,
                    updated_at = excluded.updated_at
                """,
                (
                    collect_type, date_str, status,
                    1 if count_attempt else 0, job_id, detail,
                    datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                ),
            )
            conn.commit()
            return self.get_daily_collects(date_str).get(collect_type)
        finally:
            conn.close()

    def get_daily_collects(self, date_str) -> Dict[str, Dict]:
        """某天的每日采集记录，形如 {"prices": {...}, "reviews": {...}}。

        没有记录的类型不出现在返回里，调用方用 .get() 取。
        """
        rows = self._rows(
            "SELECT collect_type, status, attempts, job_id, detail, updated_at "
            "FROM daily_collects WHERE date = ?",
            (date_str,),
        )
        out = {}
        for r in rows:
            out[r["collect_type"]] = {
                "type": r["collect_type"],
                "date": date_str,
                "status": r["status"],
                "attempts": r["attempts"] or 0,
                "jobId": r["job_id"],
                "detail": r["detail"],
                "updatedAt": r["updated_at"],
            }
        return out

    def get_competitors(self, date: Optional[str] = None) -> List[Dict]:
        """某日的竞品明细；不传日期则取最新一天。"""
        if date is None:
            dates = self.get_dates()
            if not dates:
                return []
            date = dates[0]
        rows = self._rows(
            """
            SELECT hotel_name, hotel_id, price, distance_km, rating, reviews_count
            FROM competitor_prices WHERE date = ?
            ORDER BY price ASC
            """,
            (date,),
        )
        return [
            {
                "name": r["hotel_name"],
                "id": r["hotel_id"],
                "price": r["price"],
                "distance_km": r["distance_km"],
                "rating": r["rating"],
                "reviews": r["reviews_count"],
            }
            for r in rows
        ]

    def get_latest_my_price(self) -> Optional[Dict]:
        rows = self._rows(
            "SELECT date, price, occupancy_rate, rooms_available, rooms_total "
            "FROM my_hotel_prices ORDER BY date DESC LIMIT 1"
        )
        if not rows:
            return None
        r = rows[0]
        return {
            "date": r["date"],
            "price": r["price"],
            "occupancy_rate": r["occupancy_rate"],
            "rooms_available": r["rooms_available"],
            "rooms_total": r["rooms_total"],
        }

    def get_suggestions(self, limit: int = 30) -> List[Dict]:
        rows = self._rows(
            """
            SELECT id, date, current_price, suggested_price, reason, occupancy_rate,
                   competitor_min, competitor_avg, competitor_max, status,
                   created_at, decided_at, decision_note
            FROM price_suggestions ORDER BY date DESC, id DESC LIMIT ?
            """,
            (limit,),
        )
        return [dict(r) for r in rows]

    def get_latest_suggestion(self) -> Optional[Dict]:
        items = self.get_suggestions(limit=1)
        return items[0] if items else None

    def decide_suggestion(
        self, suggestion_id: int, decision: str, note: str = "", decided_by: str = "店长"
    ) -> Dict:
        """人工拍板一条调价建议。

        decision: approved = 确认执行 / rejected = 不执行
        确认后写入 price_adjustments —— 这张表建库起一直没人写过，因为
        「建议模式」缺一个确认入口。这里补上入口，但只记录「人拍板了」，
        不代替人去渠道后台改价（真实调价需要商家后台权限，MVP 不碰）。
        """
        decision = (decision or "").strip().lower()
        if decision not in ("approved", "rejected"):
            raise ValueError("decision 只能是 approved 或 rejected")

        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute(
                "SELECT * FROM price_suggestions WHERE id = ?", (suggestion_id,)
            ).fetchone()
            if row is None:
                raise LookupError(f"调价建议不存在: id={suggestion_id}")
            if row["status"] != "pending":
                raise ValueError(
                    f"该建议已经是 {row['status']} 状态，不能重复拍板"
                )

            conn.execute(
                "UPDATE price_suggestions SET status = ?, decided_at = CURRENT_TIMESTAMP, "
                "decision_note = ? WHERE id = ?",
                (decision, note, suggestion_id),
            )

            if decision == "approved":
                conn.execute(
                    """
                    INSERT INTO price_adjustments
                    (date, old_price, new_price, reason, occupancy_rate,
                     competitor_avg, auto_adjusted, suggestion_id, decided_by)
                    VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?)
                    """,
                    (
                        row["date"],
                        row["current_price"],
                        row["suggested_price"],
                        note or row["reason"],
                        row["occupancy_rate"],
                        row["competitor_avg"],
                        suggestion_id,
                        decided_by,
                    ),
                )

            conn.commit()
            updated = conn.execute(
                "SELECT * FROM price_suggestions WHERE id = ?", (suggestion_id,)
            ).fetchone()
            return dict(updated)
        finally:
            conn.close()

    def get_adjustments(self, limit: int = 30) -> List[Dict]:
        rows = self._rows(
            """
            SELECT id, date, old_price, new_price, reason, occupancy_rate,
                   auto_adjusted, created_at, suggestion_id, decided_by
            FROM price_adjustments ORDER BY id DESC LIMIT ?
            """,
            (limit,),
        )
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    # 差评回复草稿
    # ------------------------------------------------------------------
    def save_reply_draft(
        self,
        review_key: str,
        draft: str,
        source: str = "template",
        review_date: str = "",
        rating: float = None,
        room: str = "",
        themes="",
        note: str = "",
        edited: bool = False,
    ) -> Dict:
        """写入或更新一条草稿（按 review_key + source 唯一）。

        重新生成同一来源的草稿会覆盖正文，但**保留采用状态**——
        已经执行过的回复不该因为点了一次「重新生成」就退回草稿态。
        """
        if isinstance(themes, (list, tuple)):
            themes = "、".join(themes)
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            cur = conn.cursor()
            cur.execute(
                """
                INSERT INTO reply_drafts
                    (review_key, review_date, rating, room, themes, draft,
                     source, status, edited, note, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, 'draft', ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(review_key, source) DO UPDATE SET
                    draft = excluded.draft,
                    review_date = excluded.review_date,
                    rating = excluded.rating,
                    room = excluded.room,
                    themes = excluded.themes,
                    edited = excluded.edited,
                    note = excluded.note,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (
                    review_key, review_date, rating, room, themes, draft,
                    source, 1 if edited else 0, note,
                ),
            )
            conn.commit()
            row = cur.execute(
                "SELECT * FROM reply_drafts WHERE review_key = ? AND source = ?",
                (review_key, source),
            ).fetchone()
            return dict(row)
        finally:
            conn.close()

    def get_reply_drafts(self) -> List[Dict]:
        rows = self._rows(
            """
            SELECT id, review_key, review_date, rating, room, themes, draft,
                   source, status, edited, note, created_at, updated_at
            FROM reply_drafts ORDER BY updated_at DESC, id DESC
            """
        )
        return [dict(r) for r in rows]

    def get_reply_draft_map(self) -> Dict[str, Dict]:
        """review_key -> 该条点评最近更新的那份草稿（模型稿优先于模板稿）。

        页面渲染 30 条差评时用这个，避免逐条查库。
        """
        out = {}
        for row in self.get_reply_drafts():
            key = row["review_key"]
            current = out.get(key)
            # 已有采用状态的优先保留；否则模型稿覆盖模板稿
            if current is None:
                out[key] = row
            elif current["status"] != "draft":
                continue
            elif row["status"] != "draft" or row["source"] == "llm":
                out[key] = row
        return out

    def set_reply_draft_status(
        self, review_key: str, status: str, source: str = None, note: str = None
    ) -> Dict:
        """标记草稿状态：draft / used / discarded。"""
        if status not in ("draft", "used", "discarded"):
            raise ValueError(f"未知状态: {status}")
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            cur = conn.cursor()
            params = [status, note]
            sql = "UPDATE reply_drafts SET status = ?, note = ?, updated_at = CURRENT_TIMESTAMP"
            if note is None:
                sql = "UPDATE reply_drafts SET status = ?, updated_at = CURRENT_TIMESTAMP"
                params = [status]
            where = " WHERE review_key = ?"
            params.append(review_key)
            if source:
                where += " AND source = ?"
                params.append(source)
            cur.execute(sql + where, params)
            if cur.rowcount == 0:
                raise ValueError("找不到对应的草稿，请先生成")
            conn.commit()
            row = cur.execute(
                "SELECT * FROM reply_drafts WHERE review_key = ? "
                "ORDER BY (status != 'draft') DESC, (source = 'llm') DESC LIMIT 1",
                (review_key,),
            ).fetchone()
            return dict(row)
        finally:
            conn.close()

    def get_reply_stats(self) -> Dict:
        rows = self._rows(
            "SELECT status, COUNT(*) AS n FROM reply_drafts GROUP BY status"
        )
        stats = {r["status"]: r["n"] for r in rows}
        return {
            "total": sum(stats.values()),
            "draft": stats.get("draft", 0),
            "used": stats.get("used", 0),
            "discarded": stats.get("discarded", 0),
        }

    def get_price_trend(self, days: int = 14) -> List[Dict]:
        """按日聚合竞品价格，用于趋势图。"""
        rows = self._rows(
            """
            SELECT date, COUNT(*) AS n, MIN(price) AS lo, MAX(price) AS hi,
                   AVG(price) AS avg
            FROM competitor_prices
            GROUP BY date ORDER BY date DESC LIMIT ?
            """,
            (days,),
        )
        out = []
        for r in rows:
            prices = [
                x["price"]
                for x in self._rows(
                    "SELECT price FROM competitor_prices WHERE date = ?", (r["date"],)
                )
            ]
            prices.sort()
            mid = (
                prices[len(prices) // 2]
                if len(prices) % 2
                else (prices[len(prices) // 2 - 1] + prices[len(prices) // 2]) / 2
            )
            out.append(
                {
                    "date": r["date"],
                    "count": r["n"],
                    "min": r["lo"],
                    "max": r["hi"],
                    "avg": round(r["avg"], 2),
                    "median": mid,
                }
            )
        return list(reversed(out))
