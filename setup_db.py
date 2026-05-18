import sqlite3
from pathlib import Path


DB_PATH = Path(__file__).with_name("recruitment_review.db")


def setup_database():
    connection = sqlite3.connect(DB_PATH)
    connection.execute("PRAGMA foreign_keys = ON")

    try:
        cursor = connection.cursor()

        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS job_descriptions (
                job_id TEXT PRIMARY KEY,
                title TEXT,
                min_years_exp INTEGER,
                required_degree TEXT,
                mandatory_skills TEXT
            )
            """
        )

        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS applicants (
                applicant_id TEXT PRIMARY KEY,
                job_id TEXT,
                name TEXT,
                email TEXT,
                years_experience INTEGER,
                highest_degree TEXT,
                skills_list TEXT,
                FOREIGN KEY (job_id) REFERENCES job_descriptions(job_id)
            )
            """
        )

        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS skills_taxonomy (
                skill_name TEXT PRIMARY KEY,
                category TEXT,
                equivalent_terms TEXT
            )
            """
        )

        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS background_checks (
                check_id INTEGER PRIMARY KEY AUTOINCREMENT,
                applicant_id TEXT,
                search_query TEXT,
                retrieved_summary TEXT,
                FOREIGN KEY (applicant_id) REFERENCES applicants(applicant_id)
            )
            """
        )

        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS recruiting_verdicts (
                verdict_id INTEGER PRIMARY KEY AUTOINCREMENT,
                applicant_id TEXT UNIQUE,
                final_status TEXT,
                match_score INTEGER,
                ai_reasoning TEXT,
                processed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (applicant_id) REFERENCES applicants(applicant_id)
            )
            """
        )

        cursor.executemany(
            """
            INSERT OR REPLACE INTO job_descriptions (
                job_id,
                title,
                min_years_exp,
                required_degree,
                mandatory_skills
            )
            VALUES (?, ?, ?, ?, ?)
            """,
            [
                (
                    "JOB-001",
                    "Junior Data Analyst",
                    2,
                    "Bachelors",
                    "Python, SQL",
                ),
                (
                    "JOB-002",
                    "Project Manager",
                    4,
                    "Bachelors",
                    (
                        "Project Management, Stakeholder Management, "
                        "Business Analysis, Agile, Budgeting"
                    ),
                ),
            ],
        )

        cursor.executemany(
            """
            INSERT OR REPLACE INTO skills_taxonomy (
                skill_name,
                category,
                equivalent_terms
            )
            VALUES (?, ?, ?)
            """,
            [
                (
                    "Python",
                    "Data Science",
                    "pandas, numpy, script, coding",
                ),
                (
                    "SQL",
                    "Data Analytics",
                    "queries, database, sqlite, postgres",
                ),
                (
                    "Project Management",
                    "Business Operations",
                    "project plan, roadmap, milestones, delivery, timeline",
                ),
                (
                    "Stakeholder Management",
                    "Business Operations",
                    "clients, executives, cross-functional, communication",
                ),
                (
                    "Business Analysis",
                    "Business Strategy",
                    "requirements, process improvement, business case, KPI",
                ),
                (
                    "Agile",
                    "Delivery Management",
                    "scrum, sprint, backlog, standup, product owner",
                ),
                (
                    "Budgeting",
                    "Business Finance",
                    "budget, forecast, cost, resource planning, financials",
                ),
            ],
        )

        connection.commit()
        print(f"Database setup complete: {DB_PATH}")
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


if __name__ == "__main__":
    setup_database()
