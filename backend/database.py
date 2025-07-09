import psycopg2
from psycopg2.extras import RealDictCursor
from fastapi import HTTPException
from contextlib import contextmanager
import os
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

@contextmanager
def get_db_cursor():
    conn = None
    try:
        conn = psycopg2.connect(
            host="db.igbrtovzwzggsshovtak.supabase.co",
            database="postgres",
            user="postgres",
            password="repeetcodedb9926*",  
            port=5432,
            cursor_factory=RealDictCursor
        )
        cursor = conn.cursor()
        yield cursor
        conn.commit()
    except Exception as e:
        if conn:
            conn.rollback()
        raise HTTPException(500, f"Database error: {str(e)}")
    finally:
        if conn:
            conn.close()
