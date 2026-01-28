"""
数据库模块 - 存储历史价格和分析数据
"""
import sqlite3
from datetime import datetime
from typing import List, Dict, Optional

class PricingDatabase:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self.init_database()
    
    def init_database(self):
        """初始化数据库表"""
        conn = sqlite3.connect(self.db_path)
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
        
        conn.commit()
        conn.close()
        print("✅ 数据库初始化完成")
    
    def save_my_price(self, date: str, price: float, occupancy_rate: float, 
                      rooms_available: int = None, rooms_total: int = None):
        """保存自己酒店的价格和入住率"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO my_hotel_prices (date, price, occupancy_rate, rooms_available, rooms_total)
            VALUES (?, ?, ?, ?, ?)
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
