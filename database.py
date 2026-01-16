import sqlite3
from typing import List, Optional, Tuple
from config import DATABASE_FILE


class Database:
    """SQLite database for storing tracked collections and alerts"""
    
    def __init__(self):
        self.db_file = DATABASE_FILE
        self._init_db()
    
    def _get_connection(self) -> sqlite3.Connection:
        """Get a database connection"""
        return sqlite3.connect(self.db_file)
    
    def _init_db(self):
        """Initialize database tables"""
        conn = self._get_connection()
        cursor = conn.cursor()
        
        # Table for tracked collections
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS tracked_collections (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                collection_slug TEXT NOT NULL,
                added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(user_id, collection_slug)
            )
        """)
        
        # Table for price alerts
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS price_alerts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                collection_slug TEXT NOT NULL,
                target_price REAL NOT NULL,
                alert_type TEXT DEFAULT 'below',
                is_active INTEGER DEFAULT 1,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                triggered_at TIMESTAMP,
                UNIQUE(user_id, collection_slug, target_price)
            )
        """)
        
        conn.commit()
        conn.close()
    
    # Tracked Collections Methods
    def add_tracked_collection(self, user_id: int, collection_slug: str) -> bool:
        """Add a collection to user's tracked list"""
        conn = self._get_connection()
        cursor = conn.cursor()
        
        try:
            cursor.execute(
                "INSERT INTO tracked_collections (user_id, collection_slug) VALUES (?, ?)",
                (user_id, collection_slug.lower())
            )
            conn.commit()
            return True
        except sqlite3.IntegrityError:
            return False  # Already tracking
        finally:
            conn.close()
    
    def remove_tracked_collection(self, user_id: int, collection_slug: str) -> bool:
        """Remove a collection from user's tracked list"""
        conn = self._get_connection()
        cursor = conn.cursor()
        
        cursor.execute(
            "DELETE FROM tracked_collections WHERE user_id = ? AND collection_slug = ?",
            (user_id, collection_slug.lower())
        )
        affected = cursor.rowcount
        conn.commit()
        conn.close()
        
        return affected > 0
    
    def get_tracked_collections(self, user_id: int) -> List[str]:
        """Get all collections tracked by a user"""
        conn = self._get_connection()
        cursor = conn.cursor()
        
        cursor.execute(
            "SELECT collection_slug FROM tracked_collections WHERE user_id = ?",
            (user_id,)
        )
        collections = [row[0] for row in cursor.fetchall()]
        conn.close()
        
        return collections
    
    def get_all_tracked_collections(self) -> List[Tuple[int, str]]:
        """Get all tracked collections with user IDs"""
        conn = self._get_connection()
        cursor = conn.cursor()
        
        cursor.execute("SELECT user_id, collection_slug FROM tracked_collections")
        results = cursor.fetchall()
        conn.close()
        
        return results
    
    # Price Alerts Methods
    def add_price_alert(self, user_id: int, collection_slug: str, target_price: float, 
                        alert_type: str = "below") -> bool:
        """Add a price alert for a collection"""
        conn = self._get_connection()
        cursor = conn.cursor()
        
        try:
            cursor.execute(
                """INSERT INTO price_alerts (user_id, collection_slug, target_price, alert_type) 
                   VALUES (?, ?, ?, ?)""",
                (user_id, collection_slug.lower(), target_price, alert_type)
            )
            conn.commit()
            return True
        except sqlite3.IntegrityError:
            return False  # Alert already exists
        finally:
            conn.close()
    
    def remove_price_alert(self, user_id: int, collection_slug: str) -> bool:
        """Remove price alert for a collection"""
        conn = self._get_connection()
        cursor = conn.cursor()
        
        cursor.execute(
            "DELETE FROM price_alerts WHERE user_id = ? AND collection_slug = ?",
            (user_id, collection_slug.lower())
        )
        affected = cursor.rowcount
        conn.commit()
        conn.close()
        
        return affected > 0
    
    def get_user_alerts(self, user_id: int) -> List[Tuple[str, float, str]]:
        """Get all active alerts for a user"""
        conn = self._get_connection()
        cursor = conn.cursor()
        
        cursor.execute(
            """SELECT collection_slug, target_price, alert_type 
               FROM price_alerts WHERE user_id = ? AND is_active = 1""",
            (user_id,)
        )
        alerts = cursor.fetchall()
        conn.close()
        
        return alerts
    
    def get_all_active_alerts(self) -> List[Tuple[int, str, float, str]]:
        """Get all active alerts for checking"""
        conn = self._get_connection()
        cursor = conn.cursor()
        
        cursor.execute(
            """SELECT user_id, collection_slug, target_price, alert_type 
               FROM price_alerts WHERE is_active = 1"""
        )
        alerts = cursor.fetchall()
        conn.close()
        
        return alerts
    
    def deactivate_alert(self, user_id: int, collection_slug: str, target_price: float):
        """Mark an alert as triggered/inactive"""
        conn = self._get_connection()
        cursor = conn.cursor()
        
        cursor.execute(
            """UPDATE price_alerts SET is_active = 0, triggered_at = CURRENT_TIMESTAMP 
               WHERE user_id = ? AND collection_slug = ? AND target_price = ?""",
            (user_id, collection_slug.lower(), target_price)
        )
        conn.commit()
        conn.close()


# Singleton instance
db = Database()
