import sqlite3
from typing import List, Optional, Tuple
from config import DATABASE_FILE, DATABASE_URL


def _translate_postgres_sql(sql: str) -> str:
    """Translate the small SQLite SQL subset used here into Postgres SQL."""
    sql = sql.replace("INTEGER PRIMARY KEY AUTOINCREMENT", "BIGSERIAL PRIMARY KEY")
    sql = sql.replace(
        "datetime('now', ? || ' hours')",
        "(CURRENT_TIMESTAMP + (%s || ' hours')::interval)"
    )
    return sql.replace("?", "%s")


class _PostgresCursor:
    """Cursor wrapper that lets existing SQLite-style queries run on psycopg."""

    def __init__(self, cursor):
        self._cursor = cursor

    def execute(self, sql: str, params=None):
        return self._cursor.execute(_translate_postgres_sql(sql), params)

    def fetchone(self):
        return self._cursor.fetchone()

    def fetchall(self):
        return self._cursor.fetchall()

    @property
    def rowcount(self):
        return self._cursor.rowcount


class _PostgresConnection:
    """Connection wrapper returning Postgres cursors with SQL translation."""

    def __init__(self, connection):
        self._connection = connection

    def cursor(self):
        return _PostgresCursor(self._connection.cursor())

    def commit(self):
        self._connection.commit()

    def close(self):
        self._connection.close()


class Database:
    """SQLite database for storing tracked collections and alerts"""

    def __init__(self):
        self.database_url = DATABASE_URL
        self.is_postgres = bool(self.database_url)
        self.db_file = DATABASE_FILE
        self._init_db()

    def _get_connection(self):
        """Get a database connection"""
        if self.is_postgres:
            try:
                import psycopg
            except ImportError as exc:
                raise RuntimeError(
                    "DATABASE_URL is set, but psycopg is not installed. "
                    "Run pip install -r requirements.txt."
                ) from exc
            database_url = self.database_url
            if database_url.startswith("postgres://"):
                database_url = "postgresql://" + database_url[len("postgres://"):]
            return _PostgresConnection(psycopg.connect(database_url))
        return sqlite3.connect(self.db_file)

    def _column_exists(self, cursor, table: str, column_name: str) -> bool:
        if self.is_postgres:
            cursor.execute(
                """SELECT 1 FROM information_schema.columns
                   WHERE table_schema = 'public' AND table_name = ? AND column_name = ?""",
                (table, column_name)
            )
            return cursor.fetchone() is not None

        cursor.execute(f"PRAGMA table_info({table})")
        return any(row[1] == column_name for row in cursor.fetchall())

    def _add_column_if_missing(self, cursor, table: str, column_def: str):
        """Add a column during lightweight migrations when it does not exist yet."""
        column_name = column_def.split()[0]
        if not self._column_exists(cursor, table, column_name):
            cursor.execute(f"ALTER TABLE {table} ADD COLUMN {column_def}")

    def _is_integrity_error(self, exc: Exception) -> bool:
        exc_type = type(exc).__name__.lower()
        exc_module = type(exc).__module__.lower()
        return (
            isinstance(exc, sqlite3.IntegrityError)
            or "integrity" in exc_type
            or "uniqueviolation" in exc_type
            or "psycopg.errors" in exc_module
        )

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
                is_recurring INTEGER DEFAULT 0,
                current_price_at_set REAL DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                triggered_at TIMESTAMP,
                UNIQUE(user_id, collection_slug, target_price, alert_type)
            )
        """)

        # Migrate: add columns if missing
        self._add_column_if_missing(cursor, "price_alerts", "is_recurring INTEGER DEFAULT 0")
        self._add_column_if_missing(cursor, "price_alerts", "current_price_at_set REAL DEFAULT 0")

        # Table for price history (for percentage calculations)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS price_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                collection_slug TEXT NOT NULL,
                floor_price REAL NOT NULL,
                volume_24h REAL DEFAULT 0,
                sales_count INTEGER DEFAULT 0,
                avg_price REAL DEFAULT 0,
                recorded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Table for percentage-based alerts
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS percentage_alerts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                collection_slug TEXT NOT NULL,
                percentage_threshold REAL NOT NULL,
                direction TEXT DEFAULT 'both',
                is_active INTEGER DEFAULT 1,
                is_recurring INTEGER DEFAULT 0,
                reference_price REAL DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                triggered_at TIMESTAMP,
                UNIQUE(user_id, collection_slug, percentage_threshold, direction)
            )
        """)

        # Migrate: add columns if missing
        self._add_column_if_missing(cursor, "percentage_alerts", "is_recurring INTEGER DEFAULT 0")
        self._add_column_if_missing(cursor, "percentage_alerts", "reference_price REAL DEFAULT 0")

        # Table for volume spike alerts
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS volume_alerts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                collection_slug TEXT NOT NULL,
                spike_multiplier REAL DEFAULT 2.0,
                is_active INTEGER DEFAULT 1,
                last_triggered_at TIMESTAMP,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(user_id, collection_slug)
            )
        """)
        self._add_column_if_missing(cursor, "volume_alerts", "last_triggered_at TIMESTAMP")

        # Table for user portfolio
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS portfolio (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                collection_slug TEXT NOT NULL,
                quantity INTEGER NOT NULL,
                buy_price REAL NOT NULL,
                added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(user_id, collection_slug)
            )
        """)

        # Table for gas fee alerts
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS gas_alerts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                target_gwei REAL NOT NULL,
                alert_type TEXT DEFAULT 'below',
                is_active INTEGER DEFAULT 1,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                triggered_at TIMESTAMP,
                UNIQUE(user_id, target_gwei, alert_type)
            )
        """)

        # Table for mint reminders
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS mint_reminders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                nft_name TEXT NOT NULL,
                mint_price TEXT NOT NULL,
                mint_date TEXT NOT NULL,
                mint_link TEXT DEFAULT '',
                is_active INTEGER DEFAULT 1,
                reminded_30min INTEGER DEFAULT 0,
                reminded_5min INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_price_alerts_active ON price_alerts(is_active, collection_slug)"
        )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_percentage_alerts_active ON percentage_alerts(is_active, collection_slug)"
        )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_volume_alerts_active ON volume_alerts(is_active, collection_slug)"
        )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_price_history_collection_time ON price_history(collection_slug, recorded_at)"
        )

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
        except Exception as exc:
            if self._is_integrity_error(exc):
                return False  # Already tracking
            raise
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
                        alert_type: str = "below", is_recurring: bool = False,
                        current_price: float = 0) -> bool:
        """Add a price alert for a collection"""
        conn = self._get_connection()
        cursor = conn.cursor()

        try:
            cursor.execute(
                """INSERT INTO price_alerts
                   (user_id, collection_slug, target_price, alert_type, is_recurring, current_price_at_set)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (user_id, collection_slug.lower(), target_price, alert_type,
                 1 if is_recurring else 0, current_price)
            )
            conn.commit()
            return True
        except Exception as exc:
            if self._is_integrity_error(exc):
                return False  # Alert already exists
            raise
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

    def remove_alert_by_id(self, user_id: int, alert_id: int) -> bool:
        """Remove a specific alert by ID"""
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute(
            "DELETE FROM price_alerts WHERE id = ? AND user_id = ?",
            (alert_id, user_id)
        )
        affected = cursor.rowcount
        conn.commit()
        conn.close()
        return affected > 0

    def remove_percent_alert_by_id(self, user_id: int, alert_id: int) -> bool:
        """Remove a specific percentage alert by ID"""
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute(
            "DELETE FROM percentage_alerts WHERE id = ? AND user_id = ?",
            (alert_id, user_id)
        )
        affected = cursor.rowcount
        conn.commit()
        conn.close()
        return affected > 0

    def remove_volume_alert_by_id(self, user_id: int, alert_id: int) -> bool:
        """Remove a specific volume alert by ID"""
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute(
            "DELETE FROM volume_alerts WHERE id = ? AND user_id = ?",
            (alert_id, user_id)
        )
        affected = cursor.rowcount
        conn.commit()
        conn.close()
        return affected > 0

    def remove_gas_alert_by_id(self, user_id: int, alert_id: int) -> bool:
        """Remove a specific gas alert by ID"""
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute(
            "DELETE FROM gas_alerts WHERE id = ? AND user_id = ?",
            (alert_id, user_id)
        )
        affected = cursor.rowcount
        conn.commit()
        conn.close()
        return affected > 0

    def get_user_alerts(self, user_id: int) -> List[Tuple]:
        """Get all active alerts for a user with IDs"""
        conn = self._get_connection()
        cursor = conn.cursor()

        cursor.execute(
            """SELECT id, collection_slug, target_price, alert_type, is_recurring
               FROM price_alerts WHERE user_id = ? AND is_active = 1""",
            (user_id,)
        )
        alerts = cursor.fetchall()
        conn.close()

        return alerts

    def get_all_active_alerts(self) -> List[Tuple]:
        """Get all active alerts for checking"""
        conn = self._get_connection()
        cursor = conn.cursor()

        cursor.execute(
            """SELECT user_id, collection_slug, target_price, alert_type,
                      is_recurring, current_price_at_set, triggered_at
               FROM price_alerts WHERE is_active = 1"""
        )
        alerts = cursor.fetchall()
        conn.close()

        return alerts

    def deactivate_alert(self, user_id: int, collection_slug: str, target_price: float,
                         alert_type: str, current_price: Optional[float] = None):
        """Mark an alert as triggered/inactive. If recurring, reset reference price instead."""
        conn = self._get_connection()
        cursor = conn.cursor()

        # Check if recurring
        cursor.execute(
            """SELECT is_recurring FROM price_alerts
               WHERE user_id = ? AND collection_slug = ? AND target_price = ?
               AND alert_type = ? AND is_active = 1""",
            (user_id, collection_slug.lower(), target_price, alert_type)
        )
        row = cursor.fetchone()
        price_update = current_price if current_price is not None else 0
        if row and row[0] == 1:
            # Recurring: update triggered_at but keep active
            cursor.execute(
                """UPDATE price_alerts
                   SET triggered_at = CURRENT_TIMESTAMP, current_price_at_set = ?
                   WHERE user_id = ? AND collection_slug = ? AND target_price = ? AND alert_type = ?""",
                (price_update, user_id, collection_slug.lower(), target_price, alert_type)
            )
        else:
            cursor.execute(
                """UPDATE price_alerts
                   SET is_active = 0, triggered_at = CURRENT_TIMESTAMP, current_price_at_set = ?
                   WHERE user_id = ? AND collection_slug = ? AND target_price = ? AND alert_type = ?""",
                (price_update, user_id, collection_slug.lower(), target_price, alert_type)
            )
        conn.commit()
        conn.close()

    def update_price_alert_observed_price(self, user_id: int, collection_slug: str,
                                          target_price: float, alert_type: str,
                                          current_price: float):
        """Store the latest checked floor price so recurring alerts trigger on crossing."""
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute(
            """UPDATE price_alerts SET current_price_at_set = ?
               WHERE user_id = ? AND collection_slug = ? AND target_price = ?
               AND alert_type = ? AND is_active = 1""",
            (current_price, user_id, collection_slug.lower(), target_price, alert_type)
        )
        conn.commit()
        conn.close()

    # ============== Price History Methods ==============

    def save_price_history(self, collection_slug: str, floor_price: float,
                           volume_24h: float, sales_count: int, avg_price: float):
        """Save price snapshot for history tracking"""
        conn = self._get_connection()
        cursor = conn.cursor()

        cursor.execute(
            """INSERT INTO price_history (collection_slug, floor_price, volume_24h, sales_count, avg_price)
               VALUES (?, ?, ?, ?, ?)""",
            (collection_slug.lower(), floor_price, volume_24h, sales_count, avg_price)
        )
        conn.commit()
        conn.close()

    def get_price_history(self, collection_slug: str, hours: int = 24) -> List[Tuple]:
        """Get price history for a collection within the last N hours"""
        conn = self._get_connection()
        cursor = conn.cursor()

        cursor.execute(
            """SELECT floor_price, volume_24h, sales_count, avg_price, recorded_at
               FROM price_history
               WHERE collection_slug = ?
               AND recorded_at >= datetime('now', ? || ' hours')
               ORDER BY recorded_at DESC""",
            (collection_slug.lower(), f"-{hours}")
        )
        results = cursor.fetchall()
        conn.close()
        return results

    def get_oldest_price(self, collection_slug: str, hours: int = 24) -> Optional[float]:
        """Get the oldest recorded price within N hours for percentage calculation"""
        conn = self._get_connection()
        cursor = conn.cursor()

        cursor.execute(
            """SELECT floor_price FROM price_history
               WHERE collection_slug = ?
               AND recorded_at >= datetime('now', ? || ' hours')
               ORDER BY recorded_at ASC LIMIT 1""",
            (collection_slug.lower(), f"-{hours}")
        )
        result = cursor.fetchone()
        conn.close()
        return result[0] if result else None

    # ============== Percentage Alerts Methods ==============

    def add_percentage_alert(self, user_id: int, collection_slug: str,
                              percentage: float, direction: str = "both",
                              is_recurring: bool = False, reference_price: float = 0) -> bool:
        """Add a percentage-based alert with reference price"""
        conn = self._get_connection()
        cursor = conn.cursor()

        try:
            cursor.execute(
                """INSERT INTO percentage_alerts
                   (user_id, collection_slug, percentage_threshold, direction, is_recurring, reference_price)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (user_id, collection_slug.lower(), percentage, direction,
                 1 if is_recurring else 0, reference_price)
            )
            conn.commit()
            return True
        except Exception as exc:
            if self._is_integrity_error(exc):
                return False
            raise
        finally:
            conn.close()

    def get_percentage_alerts(self, user_id: int) -> List[Tuple]:
        """Get all active percentage alerts for a user with IDs"""
        conn = self._get_connection()
        cursor = conn.cursor()

        cursor.execute(
            """SELECT id, collection_slug, percentage_threshold, direction, is_recurring
               FROM percentage_alerts WHERE user_id = ? AND is_active = 1""",
            (user_id,)
        )
        results = cursor.fetchall()
        conn.close()
        return results

    def get_all_percentage_alerts(self) -> List[Tuple]:
        """Get all active percentage alerts for checking"""
        conn = self._get_connection()
        cursor = conn.cursor()

        cursor.execute(
            """SELECT user_id, collection_slug, percentage_threshold, direction, reference_price, is_recurring
               FROM percentage_alerts WHERE is_active = 1"""
        )
        results = cursor.fetchall()
        conn.close()
        return results

    def deactivate_percentage_alert(self, user_id: int, collection_slug: str, percentage: float,
                                    direction: str, new_ref_price: Optional[float] = None):
        """Mark a percentage alert as triggered. If recurring, update ref price."""
        conn = self._get_connection()
        cursor = conn.cursor()

        # Check if recurring
        cursor.execute(
            """SELECT is_recurring FROM percentage_alerts
               WHERE user_id = ? AND collection_slug = ? AND percentage_threshold = ?
               AND direction = ? AND is_active = 1""",
            (user_id, collection_slug.lower(), percentage, direction)
        )
        row = cursor.fetchone()
        if row and row[0] == 1:
            # Recurring: reset the reference price to the trigger price and keep active.
            cursor.execute(
                """UPDATE percentage_alerts
                   SET triggered_at = CURRENT_TIMESTAMP, reference_price = COALESCE(?, reference_price)
                   WHERE user_id = ? AND collection_slug = ? AND percentage_threshold = ? AND direction = ?""",
                (new_ref_price, user_id, collection_slug.lower(), percentage, direction)
            )
        else:
            cursor.execute(
                """UPDATE percentage_alerts SET is_active = 0, triggered_at = CURRENT_TIMESTAMP
                   WHERE user_id = ? AND collection_slug = ? AND percentage_threshold = ? AND direction = ?""",
                (user_id, collection_slug.lower(), percentage, direction)
            )
        conn.commit()
        conn.close()

    def update_percentage_alert_ref_price(self, user_id: int, collection_slug: str,
                                          percentage: float, direction: str, new_ref_price: float):
        """Update reference price for a recurring percentage alert after trigger."""
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute(
            """UPDATE percentage_alerts SET reference_price = ?
               WHERE user_id = ? AND collection_slug = ? AND percentage_threshold = ?
               AND direction = ? AND is_active = 1""",
            (new_ref_price, user_id, collection_slug.lower(), percentage, direction)
        )
        conn.commit()
        conn.close()

    # ============== Volume Alerts Methods ==============

    def add_volume_alert(self, user_id: int, collection_slug: str,
                         spike_multiplier: float = 2.0) -> bool:
        """Add a volume spike alert"""
        conn = self._get_connection()
        cursor = conn.cursor()

        try:
            cursor.execute(
                """INSERT INTO volume_alerts (user_id, collection_slug, spike_multiplier)
                   VALUES (?, ?, ?)""",
                (user_id, collection_slug.lower(), spike_multiplier)
            )
            conn.commit()
            return True
        except Exception as exc:
            if self._is_integrity_error(exc):
                return False
            raise
        finally:
            conn.close()

    def get_volume_alerts(self, user_id: int) -> List[Tuple]:
        """Get all active volume alerts for a user with IDs"""
        conn = self._get_connection()
        cursor = conn.cursor()

        cursor.execute(
            """SELECT id, collection_slug, spike_multiplier
               FROM volume_alerts WHERE user_id = ? AND is_active = 1""",
            (user_id,)
        )
        results = cursor.fetchall()
        conn.close()
        return results

    def get_all_volume_alerts(self) -> List[Tuple]:
        """Get all active volume alerts for checking"""
        conn = self._get_connection()
        cursor = conn.cursor()

        cursor.execute(
            """SELECT user_id, collection_slug, spike_multiplier, last_triggered_at
               FROM volume_alerts WHERE is_active = 1"""
        )
        results = cursor.fetchall()
        conn.close()
        return results

    def mark_volume_alert_triggered(self, user_id: int, collection_slug: str):
        """Update volume alert trigger timestamp for cooldown checks."""
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute(
            """UPDATE volume_alerts SET last_triggered_at = CURRENT_TIMESTAMP
               WHERE user_id = ? AND collection_slug = ? AND is_active = 1""",
            (user_id, collection_slug.lower())
        )
        conn.commit()
        conn.close()

    def get_average_volume(self, collection_slug: str, hours: int = 168) -> Optional[float]:
        """Get average volume over last N hours (default 7 days)"""
        conn = self._get_connection()
        cursor = conn.cursor()

        cursor.execute(
            """SELECT AVG(volume_24h) FROM price_history
               WHERE collection_slug = ?
               AND recorded_at >= datetime('now', ? || ' hours')""",
            (collection_slug.lower(), f"-{hours}")
        )
        result = cursor.fetchone()
        conn.close()
        return result[0] if result and result[0] else None

    # ============== Portfolio Methods ==============

    def add_portfolio_item(self, user_id: int, collection_slug: str,
                           quantity: int, buy_price: float) -> bool:
        """Add or update portfolio item"""
        conn = self._get_connection()
        cursor = conn.cursor()

        try:
            cursor.execute(
                """INSERT INTO portfolio (user_id, collection_slug, quantity, buy_price)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(user_id, collection_slug)
                   DO UPDATE SET quantity = ?, buy_price = ?, updated_at = CURRENT_TIMESTAMP""",
                (user_id, collection_slug.lower(), quantity, buy_price, quantity, buy_price)
            )
            conn.commit()
            return True
        except Exception:
            return False
        finally:
            conn.close()

    def remove_portfolio_item(self, user_id: int, collection_slug: str) -> bool:
        """Remove item from portfolio"""
        conn = self._get_connection()
        cursor = conn.cursor()

        cursor.execute(
            "DELETE FROM portfolio WHERE user_id = ? AND collection_slug = ?",
            (user_id, collection_slug.lower())
        )
        affected = cursor.rowcount
        conn.commit()
        conn.close()
        return affected > 0

    def get_portfolio(self, user_id: int) -> List[Tuple]:
        """Get user's portfolio"""
        conn = self._get_connection()
        cursor = conn.cursor()

        cursor.execute(
            """SELECT collection_slug, quantity, buy_price, added_at
               FROM portfolio WHERE user_id = ?""",
            (user_id,)
        )
        results = cursor.fetchall()
        conn.close()
        return results

    # ============== Gas Alerts Methods ==============

    def add_gas_alert(self, user_id: int, target_gwei: float,
                      alert_type: str = "below") -> bool:
        """Add a gas price alert"""
        conn = self._get_connection()
        cursor = conn.cursor()

        try:
            cursor.execute(
                """INSERT INTO gas_alerts (user_id, target_gwei, alert_type)
                   VALUES (?, ?, ?)""",
                (user_id, target_gwei, alert_type)
            )
            conn.commit()
            return True
        except Exception as exc:
            if self._is_integrity_error(exc):
                return False
            raise
        finally:
            conn.close()

    def get_gas_alerts(self, user_id: int) -> List[Tuple]:
        """Get all active gas alerts for a user with IDs"""
        conn = self._get_connection()
        cursor = conn.cursor()

        cursor.execute(
            """SELECT id, target_gwei, alert_type
               FROM gas_alerts WHERE user_id = ? AND is_active = 1""",
            (user_id,)
        )
        results = cursor.fetchall()
        conn.close()
        return results

    def get_all_gas_alerts(self) -> List[Tuple]:
        """Get all active gas alerts for checking"""
        conn = self._get_connection()
        cursor = conn.cursor()

        cursor.execute(
            """SELECT user_id, target_gwei, alert_type
               FROM gas_alerts WHERE is_active = 1"""
        )
        results = cursor.fetchall()
        conn.close()
        return results

    def deactivate_gas_alert(self, user_id: int, target_gwei: float, alert_type: str):
        """Mark a gas alert as triggered"""
        conn = self._get_connection()
        cursor = conn.cursor()

        cursor.execute(
            """UPDATE gas_alerts SET is_active = 0, triggered_at = CURRENT_TIMESTAMP
               WHERE user_id = ? AND target_gwei = ? AND alert_type = ?""",
            (user_id, target_gwei, alert_type)
        )
        conn.commit()
        conn.close()

    def get_all_monitored_collection_slugs(self) -> List[str]:
        """Get unique collection slugs that need history for watchlists, alerts, or portfolios."""
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute("""
            SELECT collection_slug FROM tracked_collections
            UNION
            SELECT collection_slug FROM price_alerts WHERE is_active = 1
            UNION
            SELECT collection_slug FROM percentage_alerts WHERE is_active = 1
            UNION
            SELECT collection_slug FROM volume_alerts WHERE is_active = 1
            UNION
            SELECT collection_slug FROM portfolio
        """)
        results = [row[0] for row in cursor.fetchall()]
        conn.close()
        return results

    # ============== Mint Reminder Methods ==============

    def add_mint_reminder(self, user_id: int, nft_name: str, mint_price: str,
                          mint_date: str, mint_link: str = "") -> bool:
        """Add a mint reminder"""
        conn = self._get_connection()
        cursor = conn.cursor()

        try:
            cursor.execute(
                """INSERT INTO mint_reminders (user_id, nft_name, mint_price, mint_date, mint_link)
                   VALUES (?, ?, ?, ?, ?)""",
                (user_id, nft_name, mint_price, mint_date, mint_link)
            )
            conn.commit()
            return True
        except Exception:
            return False
        finally:
            conn.close()

    def get_mint_reminders(self, user_id: int) -> List[Tuple]:
        """Get all active mint reminders for a user"""
        conn = self._get_connection()
        cursor = conn.cursor()

        cursor.execute(
            """SELECT id, nft_name, mint_price, mint_date, mint_link
               FROM mint_reminders WHERE user_id = ? AND is_active = 1
               ORDER BY mint_date ASC""",
            (user_id,)
        )
        results = cursor.fetchall()
        conn.close()
        return results

    def get_upcoming_reminders(self) -> List[Tuple]:
        """Get all active reminders for background checking"""
        conn = self._get_connection()
        cursor = conn.cursor()

        cursor.execute(
            """SELECT id, user_id, nft_name, mint_price, mint_date, mint_link,
                      reminded_30min, reminded_5min
               FROM mint_reminders WHERE is_active = 1"""
        )
        results = cursor.fetchall()
        conn.close()
        return results

    def mark_reminded(self, reminder_id: int, reminder_type: str):
        """Mark a reminder as sent (30min or 5min)"""
        conn = self._get_connection()
        cursor = conn.cursor()

        if reminder_type == "30min":
            cursor.execute(
                "UPDATE mint_reminders SET reminded_30min = 1 WHERE id = ?",
                (reminder_id,)
            )
        elif reminder_type == "5min":
            cursor.execute(
                "UPDATE mint_reminders SET reminded_5min = 1 WHERE id = ?",
                (reminder_id,)
            )

        conn.commit()
        conn.close()

    def deactivate_mint_reminder(self, reminder_id: int):
        """Deactivate a mint reminder after it has passed"""
        conn = self._get_connection()
        cursor = conn.cursor()

        cursor.execute(
            "UPDATE mint_reminders SET is_active = 0 WHERE id = ?",
            (reminder_id,)
        )
        conn.commit()
        conn.close()

    def remove_mint_reminder(self, user_id: int, reminder_id: int) -> bool:
        """Remove a mint reminder"""
        conn = self._get_connection()
        cursor = conn.cursor()

        cursor.execute(
            "DELETE FROM mint_reminders WHERE id = ? AND user_id = ?",
            (reminder_id, user_id)
        )
        affected = cursor.rowcount
        conn.commit()
        conn.close()
        return affected > 0


# Singleton instance
db = Database()
