from fastapi import FastAPI, Depends, HTTPException, Header
from fastapi.middleware.cors import CORSMiddleware
# Firebase (Auth only)
from firebase_admin import auth 
from database import get_db_cursor  
import psycopg2  
from pydantic import BaseModel
from typing import Optional
from datetime import datetime, timedelta, time, timezone
import firebase_admin
from firebase_admin import credentials
import logging
import json
from google.oauth2 import service_account
import os

# Set up logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

if not firebase_admin._apps:
    service_account_json = os.environ.get('GOOGLE_SERVICE_ACCOUNT_JSON')
    if service_account_json:
        service_account_info = json.loads(service_account_json)
        cred = credentials.Certificate(service_account_info)
    else:
        # Fallback to local file (for development)
        cred = credentials.Certificate("serviceAccountKey.json")
    
    firebase_admin.initialize_app(cred)

app = FastAPI()

origins = [
    "http://localhost:3000",         
    "https://repeetcode.com",        
    "https://www.repeetcode.com"     
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/")
def root():
    return {"message": "RepeetCode backend is live!"}


def calculate_current_streak(dates: list[str]) -> int:
    if not dates:
        return 0
    logged_days = set(datetime.fromisoformat(d).date() for d in dates)
    streak = 0
    today = datetime.utcnow().date()
    while today in logged_days:
        streak += 1
        today -= timedelta(days=1)
    return streak


def get_user_problem_logs(user_id: str):
    try:
        with get_db_cursor() as cur:
            cur.execute("""
                SELECT * FROM user_problem 
                WHERE user_id = %s
                ORDER BY date_solved DESC
            """, (user_id,))
            return cur.fetchall()
    except Exception as e:
        logger.error(f"Error fetching user logs: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Error fetching user logs: {str(e)}")

def verify_token(authorization: Optional[str] = Header(None)) -> str:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or invalid Authorization header")
    token = authorization.split("Bearer ")[1]
    try:
        decoded_token = auth.verify_id_token(token)
        return decoded_token['uid']
    except Exception as e:
        logger.error(f"Token verification failed: {str(e)}")
        raise HTTPException(status_code=401, detail="Invalid or expired token")


class ProblemLog(BaseModel):
    slug: str
    title: str
    difficulty: int  # user-rated 1–5


@app.get("/dashboard_stats")
def dashboard_stats(user_id: str = Depends(verify_token)):
    try:
        problem_logs = get_user_problem_logs(user_id)
        timestamps = [log['date_solved'].isoformat() for log in problem_logs]
        streak = calculate_current_streak(timestamps)
        return {"current_streak": streak}
    except Exception as e:
        logger.error(f"Error in dashboard_stats: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Error fetching dashboard stats: {str(e)}")


def calculate_next_review(difficulty: int, last_review_date: Optional[datetime]) -> datetime:
    now = datetime.utcnow()

    # If never reviewed before, review sooner for harder problems
    initial_days = {1: 8, 2: 6, 3: 4, 4: 2, 5: 1}
    if not last_review_date:
        return now + timedelta(days=initial_days[difficulty])

    days_since_last = (now - last_review_date).days

    # Harder problems reviewed more often → smaller multiplier
    # So inverse of difficulty scale: easier = larger gap
    multiplier = {1: 0.5, 2: 0.7, 3: 0.9, 4: 1.2, 5: 1.5}
    next_gap = max(1, int(days_since_last * multiplier[difficulty]))

    return now + timedelta(days=min(next_gap, 90))

    return now + timedelta(days=min(next_gap, 90))
@app.post("/log")
def log_problem(data: ProblemLog, user_id: str = Depends(verify_token)):
    try:
        logger.info(f"Logging problem for user {user_id}: {data}")
        with get_db_cursor() as cur:
            # 1. Check if problem exists
            logger.info(f"Checking if problem exists: {data.slug}")
            cur.execute("SELECT 1 FROM leetcode_problem WHERE slug = %s", (data.slug,))
            if not cur.fetchone():
                raise HTTPException(400, "Problem does not exist in database")

            # 2. Get tags
            cur.execute("SELECT tags FROM leetcode_problem WHERE slug = %s", (data.slug,))
            result = cur.fetchone()
            tags = result['tags'] if result and result['tags'] is not None else []

            # --- FIX HERE ---
            if isinstance(tags, dict):
                tags = list(tags.keys())
            elif tags is None:
                tags = []
            # --- END FIX ---

            logger.info(f"Tags: {tags}")

            # 3. Last review
            cur.execute("""
                SELECT date_solved FROM user_problem 
                WHERE user_id = %s AND slug = %s
                ORDER BY date_solved DESC LIMIT 1
            """, (user_id, data.slug))
            last_review = cur.fetchone()

            # 4. Next review
            next_review = calculate_next_review(
                data.difficulty,
                last_review['date_solved'] if last_review else None
            )

            # 5. Insert/upsert
            cur.execute("""
                INSERT INTO user_problem (
                    user_id, slug, title, difficulty, 
                    date_solved, next_review_date, tags
                ) VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (user_id, slug) 
                DO UPDATE SET
                    difficulty = EXCLUDED.difficulty,
                    date_solved = EXCLUDED.date_solved,
                    next_review_date = EXCLUDED.next_review_date,
                    tags = EXCLUDED.tags
            """, (
                user_id, data.slug, data.title, data.difficulty,
                datetime.now(timezone.utc), next_review, json.dumps(tags)
            ))

            logger.info("Problem logged successfully")

            return {
                "message": f"{data.title} logged!",
                "next_review": next_review.date().isoformat()
            }

    except Exception as e:
        logger.exception("Error logging problem")
        raise HTTPException(status_code=500, detail=f"Internal log error: {str(e)}")

@app.get("/reviews")
def get_todays_reviews(user_id: str = Depends(verify_token)):
    try:
        logger.info(f"Fetching reviews for user: {user_id}")
        today_end = datetime.combine(datetime.utcnow().date(), time(23, 59, 59))
        
        with get_db_cursor() as cur:
            # Due reviews
            logger.info("Executing due reviews query")
            cur.execute("""
                SELECT up.*, lp.tags 
                FROM user_problem up
                JOIN leetcode_problem lp ON up.slug = lp.slug
                WHERE up.user_id = %s AND up.next_review_date <= %s
            """, (user_id, today_end))
            due_reviews = cur.fetchall()
            
            logger.info(f"Found {len(due_reviews)} due reviews")
            
            if due_reviews:
                # Convert datetime objects to ISO format strings for JSON serialization
                serialized_reviews = []
                for review in due_reviews:
                    review_dict = dict(review)
                    # Convert datetime fields to strings
                    if 'date_solved' in review_dict and review_dict['date_solved']:
                        review_dict['date_solved'] = review_dict['date_solved'].isoformat()
                    if 'next_review_date' in review_dict and review_dict['next_review_date']:
                        review_dict['next_review_date'] = review_dict['next_review_date'].isoformat()
                    serialized_reviews.append(review_dict)
                
                return {"reviews_due": serialized_reviews}
            
            # Next upcoming review
            logger.info("No due reviews, fetching next upcoming")
            cur.execute("""
                SELECT up.*, lp.tags 
                FROM user_problem up
                JOIN leetcode_problem lp ON up.slug = lp.slug
                WHERE up.user_id = %s
                ORDER BY up.next_review_date ASC
                LIMIT 1
            """, (user_id,))
            next_up = cur.fetchone()
            
            if next_up:
                next_up_dict = dict(next_up)
                # Convert datetime fields to strings
                if 'date_solved' in next_up_dict and next_up_dict['date_solved']:
                    next_up_dict['date_solved'] = next_up_dict['date_solved'].isoformat()
                if 'next_review_date' in next_up_dict and next_up_dict['next_review_date']:
                    next_up_dict['next_review_date'] = next_up_dict['next_review_date'].isoformat()
                
                return {"reviews_due": [], "next_up": next_up_dict}
            
            return {"reviews_due": [], "next_up": None}
            
    except Exception as e:
        logger.error(f"Error in get_todays_reviews: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Error fetching reviews: {str(e)}")


@app.get("/all_problems")
def get_all_problems(user_id: str = Depends(verify_token)):
    try:
        with get_db_cursor() as cur:
            cur.execute("""
                SELECT 
                    up.slug,
                    up.title,
                    up.difficulty AS user_difficulty,
                    up.date_solved,
                    up.next_review_date,
                    up.tags AS user_tags,
                    lp.official_difficulty,
                    lp.tags AS official_tags
                FROM user_problem up
                JOIN leetcode_problem lp ON up.slug = lp.slug
                WHERE up.user_id = %s
                ORDER BY up.date_solved DESC
            """, (user_id,))
            
            problems = []
            for row in cur.fetchall():
                # Parse tags JSON strings to Python objects if needed
                user_tags = row['user_tags']
                official_tags = row['official_tags']

                # The tags might be stored as JSON strings (text) or native JSONB
                # Ensure both are Python objects (lists or dicts)
                if isinstance(user_tags, str):
                    user_tags = json.loads(user_tags)
                if isinstance(official_tags, str):
                    official_tags = json.loads(official_tags)

                # Fix tags to always be lists
                def fix_tags(t):
                    if isinstance(t, dict):
                        return list(t.keys())
                    elif isinstance(t, list):
                        return t
                    else:
                        return []
                
                tags = fix_tags(user_tags) or fix_tags(official_tags) or []

                problem_dict = {
                    "slug": row['slug'],
                    "title": row['title'],
                    "difficulty": row['user_difficulty'],
                    "date_solved": row['date_solved'].isoformat() if row['date_solved'] else None,
                    "next_review_date": row['next_review_date'].isoformat() if row['next_review_date'] else None,
                    "tags": tags,
                    "official_difficulty": row['official_difficulty']
                }
                problems.append(problem_dict)
            
            return {"all_problems": problems}
            
    except Exception as e:
        logger.error(f"Error in get_all_problems: {str(e)}")
        raise HTTPException(
            status_code=500,
            detail=f"Failed to fetch user problems: {str(e)}"
        )


@app.get("/problem_bank")
def get_problem_bank(user_id: str = Depends(verify_token)):
    try:
        with get_db_cursor() as cur:
            cur.execute("""
                SELECT slug, title, official_difficulty, tags
                FROM leetcode_problem
            """)
            problems = cur.fetchall()
            
            # Convert to list of dicts for JSON serialization
            problems_list = []
            for problem in problems:
                problems_list.append(dict(problem))
            
            return {"problems": problems_list}
    except Exception as e:
        logger.error(f"Error in get_problem_bank: {str(e)}")
        raise HTTPException(500, detail=f"Failed to fetch problem bank: {str(e)}")