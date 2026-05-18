import json
import os
import sqlite3
import sys
import tempfile
from html import escape
from datetime import datetime
from email.parser import BytesParser
from email.policy import default as email_policy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import boto3
from docx import Document
from dotenv import load_dotenv
from pypdf import PdfReader

from setup_db import setup_database


APP_DIR = Path(__file__).resolve().parent
DB_PATH = APP_DIR / "recruitment_review.db"
TRACE_DIR = APP_DIR / "traces"

load_dotenv(APP_DIR / ".env")

AWS_PROFILE = os.getenv("AWS_PROFILE", "GSB570-BedrockOnly-KK")
AWS_REGION = os.getenv("AWS_REGION", "us-west-2")
BEDROCK_MODEL_ID = os.getenv("BEDROCK_MODEL_ID", "deepseek.v3.2")
PROCESS_RUNS = {}

SAMPLE_RESUME = """
Alex Rivera - alex.rivera@example.com
Applying to JOB-001 / Junior Data Analyst.

Messy notes from resume:
Graduated from Cal Poly with a Bachelors in Business Analytics. About 2.5 years
total analytics experience across internships and part-time analyst work. Built
Python scripts for cleaning sales and operations data, used pandas and numpy,
created SQL queries against sqlite and postgres tables, and made dashboards for
weekly reporting. Also mentions Excel, stakeholder interviews, documentation,
and presenting findings to non-technical managers.
"""


def get_connection():
    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def find_existing_application(data_dict):
    name_key = str(data_dict["name"]).strip().lower()
    email_key = str(data_dict["email"]).strip().lower()

    with get_connection() as connection:
        row = connection.execute(
            """
            SELECT
                applicant_id,
                job_id,
                name,
                email,
                years_experience,
                highest_degree,
                skills_list
            FROM applicants
            WHERE job_id = ?
              AND lower(trim(name)) = ?
              AND lower(trim(email)) = ?
            LIMIT 1
            """,
            (data_dict["job_id"], name_key, email_key),
        ).fetchone()

    return dict(row) if row else None


def insert_applicant(data_dict):
    existing_application = find_existing_application(data_dict)
    if existing_application:
        return {
            "inserted": False,
            "reason": (
                "Duplicate application blocked: this applicant name and email "
                "already exist for the selected job."
            ),
            "existing_application": existing_application,
            "attempted_application": data_dict,
        }

    with get_connection() as connection:
        connection.execute(
            """
            INSERT OR REPLACE INTO applicants (
                applicant_id,
                job_id,
                name,
                email,
                years_experience,
                highest_degree,
                skills_list
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                data_dict["applicant_id"],
                data_dict["job_id"],
                data_dict["name"],
                data_dict["email"],
                data_dict["years_experience"],
                data_dict["highest_degree"],
                data_dict["skills_list"],
            ),
        )

    return {
        "inserted": True,
        "inserted_applicant": data_dict,
    }


def query_db(sql_string):
    sql = sql_string.strip()
    if not sql.lower().startswith("select"):
        raise ValueError("query_db only allows SELECT statements.")

    with get_connection() as connection:
        rows = connection.execute(sql).fetchall()
        return [dict(row) for row in rows]


def list_jobs():
    return query_db(
        """
        SELECT job_id, title, min_years_exp, required_degree, mandatory_skills
        FROM job_descriptions
        ORDER BY job_id
        """
    )


def store_evidence(applicant_id, query, summary):
    with get_connection() as connection:
        connection.execute(
            """
            INSERT INTO background_checks (
                applicant_id,
                search_query,
                retrieved_summary
            )
            VALUES (?, ?, ?)
            """,
            (applicant_id, query, summary),
        )

    return {
        "stored": True,
        "applicant_id": applicant_id,
        "search_query": query,
        "retrieved_summary": summary,
    }


def store_verdict(applicant_id, status, score, reasoning):
    with get_connection() as connection:
        connection.execute(
            """
            INSERT INTO recruiting_verdicts (
                applicant_id,
                final_status,
                match_score,
                ai_reasoning
            )
            VALUES (?, ?, ?, ?)
            ON CONFLICT(applicant_id) DO UPDATE SET
                final_status = excluded.final_status,
                match_score = excluded.match_score,
                ai_reasoning = excluded.ai_reasoning,
                processed_at = CURRENT_TIMESTAMP
            """,
            (applicant_id, status, score, reasoning),
        )

    return {
        "stored": True,
        "applicant_id": applicant_id,
        "final_status": status,
        "match_score": score,
        "ai_reasoning": reasoning,
    }


def mock_tavily_search(query):
    return (
        f"Mock Tavily results for '{query}': Public GitHub profile shows small "
        "Python data-cleaning projects using pandas, numpy, and sqlite. Public "
        "LinkedIn profile lists a Business Analytics degree, internships in "
        "operations analytics, SQL reporting work, and collaborative dashboard "
        "projects. No negative public signals were found in this mock search."
    )


def extract_resume_text(file_name, file_bytes):
    suffix = Path(file_name).suffix.lower()

    if suffix == ".pdf":
        with tempfile.NamedTemporaryFile(suffix=suffix) as temp_file:
            temp_file.write(file_bytes)
            temp_file.flush()
            reader = PdfReader(temp_file.name)
            page_text = [
                page.extract_text() or ""
                for page in reader.pages
            ]
        return "\n\n".join(text.strip() for text in page_text if text.strip())

    if suffix == ".docx":
        with tempfile.NamedTemporaryFile(suffix=suffix) as temp_file:
            temp_file.write(file_bytes)
            temp_file.flush()
            document = Document(temp_file.name)
            paragraphs = [
                paragraph.text.strip()
                for paragraph in document.paragraphs
                if paragraph.text.strip()
            ]
        return "\n".join(paragraphs)

    return file_bytes.decode("utf-8", errors="replace")


def build_client():
    session = boto3.Session(profile_name=AWS_PROFILE)
    return session.client("bedrock-runtime", region_name=AWS_REGION)


INSERT_APPLICANT_TOOL = {
    "toolSpec": {
        "name": "insert_applicant",
        "description": (
            "Insert a parsed applicant profile into the local applicants table. "
            "Use this after extracting profile fields from the resume."
        ),
        "inputSchema": {
            "json": {
                "type": "object",
                "properties": {
                    "applicant_id": {"type": "string"},
                    "job_id": {"type": "string"},
                    "name": {"type": "string"},
                    "email": {"type": "string"},
                    "years_experience": {"type": "number"},
                    "highest_degree": {"type": "string"},
                    "skills_list": {
                        "type": "string",
                        "description": "Comma-separated skills from the resume.",
                    },
                },
                "required": [
                    "name",
                    "email",
                    "years_experience",
                    "highest_degree",
                    "skills_list",
                ],
            }
        },
    }
}

QUERY_DB_TOOL = {
    "toolSpec": {
        "name": "query_db",
        "description": (
            "Run a read-only SELECT query against the local SQLite database."
        ),
        "inputSchema": {
            "json": {
                "type": "object",
                "properties": {
                    "sql_string": {
                        "type": "string",
                        "description": "A SELECT statement to run.",
                    }
                },
                "required": ["sql_string"],
            }
        },
    }
}

MOCK_TAVILY_SEARCH_TOOL = {
    "toolSpec": {
        "name": "mock_tavily_search",
        "description": (
            "Simulate a public web search for a candidate's GitHub/LinkedIn "
            "footprint."
        ),
        "inputSchema": {
            "json": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "The public search query to run.",
                    }
                },
                "required": ["query"],
            }
        },
    }
}

STORE_EVIDENCE_TOOL = {
    "toolSpec": {
        "name": "store_evidence",
        "description": (
            "Store a public-footprint search summary in background_checks."
        ),
        "inputSchema": {
            "json": {
                "type": "object",
                "properties": {
                    "applicant_id": {"type": "string"},
                    "query": {"type": "string"},
                    "summary": {"type": "string"},
                },
                "required": ["applicant_id", "query", "summary"],
            }
        },
    }
}

STORE_VERDICT_TOOL = {
    "toolSpec": {
        "name": "store_verdict",
        "description": (
            "Store the final recruiting status, score, and reasoning."
        ),
        "inputSchema": {
            "json": {
                "type": "object",
                "properties": {
                    "applicant_id": {"type": "string"},
                    "status": {
                        "type": "string",
                        "description": "INVITE_TO_INTERVIEW, HOLD, or REJECT.",
                    },
                    "score": {"type": "integer"},
                    "reasoning": {"type": "string"},
                },
                "required": ["applicant_id", "status", "score", "reasoning"],
            }
        },
    }
}


def run_agent_tool(name, inputs, context):
    if name == "insert_applicant":
        tool_profile = dict(inputs)
        tool_profile["job_id"] = context["selected_job_id"]
        profile = normalize_profile(tool_profile, context["selected_job_id"])
        return insert_applicant(profile)

    if name == "query_db":
        return {"rows": query_db(inputs["sql_string"])}

    if name == "mock_tavily_search":
        query = inputs["query"]
        return {
            "query": query,
            "summary": mock_tavily_search(query),
        }

    if name == "store_evidence":
        return store_evidence(
            inputs["applicant_id"],
            inputs.get("query") or inputs.get("search_query"),
            inputs.get("summary") or inputs.get("retrieved_summary"),
        )

    if name == "store_verdict":
        return store_verdict(
            inputs["applicant_id"],
            inputs.get("status") or inputs.get("final_status"),
            int(inputs.get("score") or inputs.get("match_score") or 0),
            inputs.get("reasoning") or inputs.get("ai_reasoning"),
        )

    raise ValueError(f"Unknown tool requested by model: {name}")


def call_agent_with_tools(
    client,
    agent_name,
    system_prompt,
    user_prompt,
    tools=None,
    context=None,
    temperature=0.2,
):
    messages = [
        {
            "role": "user",
            "content": [{"text": user_prompt}],
        }
    ]
    tool_calls = []

    while True:
        request = {
            "modelId": BEDROCK_MODEL_ID,
            "system": [{"text": system_prompt}],
            "messages": messages,
            "inferenceConfig": {
                "maxTokens": 900,
                "temperature": temperature,
            },
        }
        if tools:
            request["toolConfig"] = {
                "tools": tools,
                "toolChoice": {"auto": {}},
            }

        response = client.converse(**request)
        output_msg = response["output"]["message"]
        messages.append(output_msg)

        requested_tools = [
            block["toolUse"]
            for block in output_msg["content"]
            if "toolUse" in block
        ]
        if not requested_tools:
            final_text = "\n".join(
                block.get("text", "")
                for block in output_msg["content"]
                if "text" in block
            ).strip()
            return final_text, tool_calls

        tool_results = []
        for tool_use in requested_tools:
            result_data = run_agent_tool(
                tool_use["name"],
                tool_use["input"],
                context or {},
            )
            tool_calls.append({
                "tool": tool_use["name"],
                "input": tool_use["input"],
                "result": result_data,
            })
            tool_results.append({
                "toolResult": {
                    "toolUseId": tool_use["toolUseId"],
                    "content": [{"json": result_data}],
                    "status": "success",
                }
            })

        messages.append({"role": "user", "content": tool_results})


def call_agent(client, agent_name, system_prompt, user_prompt, temperature=0.2):
    final_text, _ = call_agent_with_tools(
        client,
        agent_name,
        system_prompt,
        user_prompt,
        temperature=temperature,
    )
    return final_text


def parse_json_object(text):
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`")
        cleaned = cleaned.replace("json\n", "", 1).replace("JSON\n", "", 1)

    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start == -1 or end == -1:
        raise ValueError(f"Expected a JSON object but received: {text}")

    return json.loads(cleaned[start : end + 1])


def numeric_applicant_id(value):
    digits = "".join(character for character in str(value or "") if character.isdigit())
    if digits:
        return digits
    return datetime.now().strftime("%Y%m%d%H%M%S%f")


def normalize_profile(profile, selected_job_id="JOB-001"):
    return {
        "applicant_id": numeric_applicant_id(profile.get("applicant_id")),
        "job_id": selected_job_id,
        "name": str(profile.get("name") or "Alex Rivera"),
        "email": str(profile.get("email") or "alex.rivera@example.com"),
        "years_experience": int(float(profile.get("years_experience") or 0)),
        "highest_degree": str(profile.get("highest_degree") or ""),
        "skills_list": str(profile.get("skills_list") or ""),
    }


def history_text(steps):
    return "\n\n".join(
        f"{step['agent']}:\n{step['output']}" for step in steps
    )


def make_step(
    agent_name,
    system_prompt,
    user_prompt,
    output,
    tool_result=None,
    tool_calls=None,
):
    step = {
        "agent": agent_name,
        "input": {
            "system_prompt": system_prompt,
            "user_prompt": user_prompt,
        },
        "output": output,
    }
    if tool_calls is not None:
        step["tool_calls"] = tool_calls
    if tool_result is not None:
        step["tool_result"] = tool_result
    return step


def save_trace(trace):
    TRACE_DIR.mkdir(exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    trace_path = TRACE_DIR / f"recruitment_trace_{timestamp}.json"
    trace_path.write_text(json.dumps(trace, indent=2), encoding="utf-8")
    return trace_path


def decision_allows_continue(agent_decision):
    value = agent_decision.get("proceed_to_next_agent")
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"true", "yes", "continue", "1"}
    return False


def decision_match_score(agent_decision):
    raw_score = (
        agent_decision.get("match_score")
        if agent_decision.get("match_score") is not None
        else agent_decision.get("skill_match_score")
    )
    try:
        return int(float(raw_score))
    except (TypeError, ValueError):
        return 0


def stop_from_agent_decision(
    profile,
    selected_job,
    raw_resume,
    steps,
    agent_name,
    agent_decision,
):
    ai_reasoning = str(
        agent_decision.get("reasoning")
        or f"{agent_name} decided the applicant should not proceed."
    )
    final_status = str(agent_decision.get("recommended_status") or "REJECT").upper()
    if final_status not in {"HOLD", "REJECT"}:
        final_status = "REJECT"
    match_score = decision_match_score(agent_decision)

    stored_by_agent = any(
        tool_call["tool"] == "store_verdict"
        for tool_call in steps[-1].get("tool_calls", [])
    )
    if not stored_by_agent:
        store_verdict(profile["applicant_id"], final_status, match_score, ai_reasoning)
    steps[-1]["tool_result"]["early_stop"] = (
        f"{agent_name} set proceed_to_next_agent to false, so the "
        "pipeline stopped here and did not run later agents."
    )
    steps[-1]["tool_result"]["stored_verdict"] = {
        "applicant_id": profile["applicant_id"],
        "final_status": final_status,
        "match_score": match_score,
        "ai_reasoning": ai_reasoning,
    }

    execution_log = history_text(steps)
    trace = {
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "provider": "AWS Bedrock",
        "aws_profile": AWS_PROFILE,
        "aws_region": AWS_REGION,
        "model": BEDROCK_MODEL_ID,
        "database": str(DB_PATH),
        "selected_job": selected_job,
        "raw_resume": raw_resume,
        "steps": steps,
        "execution_log": execution_log,
        "final": {
            "applicant_id": profile["applicant_id"],
            "name": profile["name"],
            "job_id": selected_job["job_id"],
            "job_title": selected_job["title"],
            "final_status": final_status,
            "match_score": match_score,
            "ai_reasoning": ai_reasoning,
        },
    }
    trace_path = save_trace(trace)
    run_id = trace_path.stem
    PROCESS_RUNS[run_id] = trace

    print("\nRecruitment Review Stopped")
    print("=" * 28)
    print(f"Stopped by: {agent_name}")
    print(f"Applicant: {profile['name']} ({profile['applicant_id']})")
    print(f"Job: {selected_job['job_id']} - {selected_job['title']}")
    print(f"Final status: {final_status}")
    print(f"Match score: {match_score}")
    print(f"Reason: {ai_reasoning}")
    print(f"Process page: /process?run_id={run_id}")

    return {
        **trace["final"],
        "run_id": run_id,
        "process_url": f"/process?run_id={run_id}",
        "execution_log": execution_log,
    }


def run_pipeline(resume_text, selected_job_id="JOB-001"):
    setup_database()
    TRACE_DIR.mkdir(exist_ok=True)

    client = build_client()
    raw_resume = (resume_text or SAMPLE_RESUME).strip()
    selected_job_id = selected_job_id or "JOB-001"
    selected_job_rows = [
        job for job in list_jobs() if job["job_id"] == selected_job_id
    ]
    if not selected_job_rows:
        raise ValueError(f"Unknown job_id: {selected_job_id}")
    selected_job = selected_job_rows[0]

    steps = []

    agent_1_system = (
        "You are Agent 1, a recruiting profile extractor. Extract only "
        "facts supported by the resume. You must call the insert_applicant "
        "tool with the extracted profile data. Use the selected job_id "
        "provided by the recruiter even if the resume mentions a different "
        "job. After the tool result is returned, summarize the profile as "
        "JSON with keys: applicant_id, job_id, name, email, years_experience, "
        "highest_degree, skills_list."
    )
    agent_1_prompt = (
        f"Selected job for this review:\n{json.dumps(selected_job, indent=2)}"
        f"\n\nRaw resume text:\n{raw_resume}"
    )
    agent_1_output, agent_1_tool_calls = call_agent_with_tools(
        client,
        "Agent 1 - Profile Extractor",
        agent_1_system,
        agent_1_prompt,
        tools=[INSERT_APPLICANT_TOOL],
        context={"selected_job_id": selected_job_id},
    )
    applicant_insert_result = next(
        (
            tool_call["result"]
            for tool_call in reversed(agent_1_tool_calls)
            if tool_call["tool"] == "insert_applicant"
        ),
        None,
    )
    if applicant_insert_result is None:
        raise RuntimeError("Agent 1 did not request the insert_applicant tool.")
    profile = (
        applicant_insert_result.get("inserted_applicant")
        or applicant_insert_result.get("attempted_application")
        or applicant_insert_result.get("existing_application")
    )
    steps.append(make_step(
        "Agent 1 - Profile Extractor",
        agent_1_system,
        agent_1_prompt,
        agent_1_output,
        {
            "selected_job": selected_job,
            "applicant_insert_result": applicant_insert_result,
        },
        agent_1_tool_calls,
    ))

    if not applicant_insert_result["inserted"]:
        duplicate_reason = applicant_insert_result["reason"]
        trace = {
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "provider": "AWS Bedrock",
            "aws_profile": AWS_PROFILE,
            "aws_region": AWS_REGION,
            "model": BEDROCK_MODEL_ID,
            "database": str(DB_PATH),
            "selected_job": selected_job,
            "raw_resume": raw_resume,
            "steps": steps,
            "execution_log": history_text(steps),
            "final": {
                "applicant_id": (
                    applicant_insert_result["existing_application"]["applicant_id"]
                ),
                "name": profile["name"],
                "job_id": selected_job["job_id"],
                "job_title": selected_job["title"],
                "final_status": "DUPLICATE_APPLICATION",
                "match_score": 0,
                "ai_reasoning": duplicate_reason,
            },
        }
        trace_path = save_trace(trace)
        run_id = trace_path.stem
        PROCESS_RUNS[run_id] = trace

        print("\nRecruitment Review Blocked")
        print("=" * 28)
        print(f"Applicant: {profile['name']}")
        print(f"Job: {selected_job['job_id']} - {selected_job['title']}")
        print(f"Reason: {duplicate_reason}")
        print(f"Process page: /process?run_id={run_id}")

        return {
            **trace["final"],
            "run_id": run_id,
            "process_url": f"/process?run_id={run_id}",
            "execution_log": trace["execution_log"],
        }

    agent_2_system = (
        "You are Agent 2, a prerequisite filter. Decide whether the "
        "candidate meets the job's minimum years of experience and degree "
        "requirements. Be strict when the selected job requires business, "
        "operations, stakeholder, budget, or delivery experience. Be "
        "concise and cite the database rule values. You must call the query_db "
        "tool to fetch the selected job requirements before making your "
        "decision. You are responsible for deciding whether this application "
        "should continue to the skills verification step. Return JSON only with "
        "keys: meets_minimum_requirements, experience_requirement_met, "
        "degree_requirement_met, proceed_to_next_agent, recommended_status, "
        "match_score, reasoning. proceed_to_next_agent must be true only if "
        "you decide Agent 3 should review skills. If you decide not to "
        "continue, recommended_status must be REJECT or HOLD and you must "
        "call store_verdict before your final response."
    )
    agent_2_prompt = (
        f"Selected job_id: {selected_job_id}\n\n"
        f"Applicant ID for any stored verdict: {profile['applicant_id']}\n\n"
        f"Prior agent outputs:\n{history_text(steps)}\n\n"
        "Call query_db with a SELECT statement against job_descriptions for "
        "the selected job_id, then make your proceed_to_next_agent decision."
    )
    agent_2_output, agent_2_tool_calls = call_agent_with_tools(
        client,
        "Agent 2 - Prerequisite Filter",
        agent_2_system,
        agent_2_prompt,
        tools=[QUERY_DB_TOOL, STORE_VERDICT_TOOL],
        context={"selected_job_id": selected_job_id},
    )
    steps.append(make_step(
        "Agent 2 - Prerequisite Filter",
        agent_2_system,
        agent_2_prompt,
        agent_2_output,
        {},
        agent_2_tool_calls,
    ))
    prerequisite_decision = parse_json_object(agent_2_output)
    steps[-1]["tool_result"]["agent_decision"] = prerequisite_decision

    if not decision_allows_continue(prerequisite_decision):
        return stop_from_agent_decision(
            profile,
            selected_job,
            raw_resume,
            steps,
            "Agent 2 - Prerequisite Filter",
            prerequisite_decision,
        )

    agent_3_system = (
        "You are Agent 3, a technical skills verifier. Use the taxonomy "
        "to decide whether the candidate demonstrates the mandatory "
        "skills for the selected job, including equivalent terms. Do not "
        "give credit for unrelated technical depth when the job requires "
        "business/project-management experience. You must call query_db to "
        "fetch the skills_taxonomy table before making your decision. You are "
        "responsible for deciding whether this application should continue to "
        "public footprint review. Return JSON only with keys: "
        "mandatory_skills_met, matched_required_skills, missing_required_skills, "
        "proceed_to_next_agent, recommended_status, match_score, reasoning. "
        "proceed_to_next_agent must be true only if you decide Agent 4 "
        "should continue the review. If you decide not to continue, "
        "recommended_status must be REJECT or HOLD and you must call "
        "store_verdict before your final response."
    )
    agent_3_prompt = (
        f"Applicant ID for any stored verdict: {profile['applicant_id']}\n\n"
        f"Prior agent outputs:\n{history_text(steps)}\n\n"
        f"Selected job:\n{json.dumps(selected_job, indent=2)}\n\n"
        "Call query_db with a SELECT statement against skills_taxonomy, then "
        "make your proceed_to_next_agent decision."
    )
    agent_3_output, agent_3_tool_calls = call_agent_with_tools(
        client,
        "Agent 3 - Skills Verifier",
        agent_3_system,
        agent_3_prompt,
        tools=[QUERY_DB_TOOL, STORE_VERDICT_TOOL],
        context={"selected_job_id": selected_job_id},
    )
    steps.append(make_step(
        "Agent 3 - Skills Verifier",
        agent_3_system,
        agent_3_prompt,
        agent_3_output,
        {},
        agent_3_tool_calls,
    ))
    skills_decision = parse_json_object(agent_3_output)
    steps[-1]["tool_result"]["agent_decision"] = skills_decision

    if not decision_allows_continue(skills_decision):
        return stop_from_agent_decision(
            profile,
            selected_job,
            raw_resume,
            steps,
            "Agent 3 - Skills Verifier",
            skills_decision,
        )

    agent_4_system = (
        "You are Agent 4, a public digital footprint reviewer. Formulate "
        "one targeted public search query for the applicant. You must call "
        "mock_tavily_search with your query. After receiving the mock search "
        "summary, you must call store_evidence to save it. Then return a "
        "concise summary of the query, review focus, and evidence."
    )
    agent_4_prompt = f"Prior agent outputs:\n{history_text(steps)}"
    agent_4_output, agent_4_tool_calls = call_agent_with_tools(
        client,
        "Agent 4 - Public Digital Footprint",
        agent_4_system,
        agent_4_prompt,
        tools=[MOCK_TAVILY_SEARCH_TOOL, STORE_EVIDENCE_TOOL],
        context={"selected_job_id": selected_job_id},
    )
    if not any(tool_call["tool"] == "store_evidence" for tool_call in agent_4_tool_calls):
        raise RuntimeError("Agent 4 did not request the store_evidence tool.")
    steps.append(make_step(
        "Agent 4 - Public Digital Footprint",
        agent_4_system,
        agent_4_prompt,
        agent_4_output,
        {},
        agent_4_tool_calls,
    )
    )

    agent_5_system = (
        "You are Agent 5, a soft-skill and team-fit scorer. Evaluate "
        "communication, collaboration, coachability, business context, "
        "and role alignment for the selected job. Provide a numerical "
        "percentage alignment score and concise reason."
    )
    agent_5_prompt = (
        f"Selected job:\n{json.dumps(selected_job, indent=2)}\n\n"
        f"Prior agent outputs:\n{history_text(steps)}"
    )
    agent_5_output = call_agent(
        client,
        "Agent 5 - Fit Scorer",
        agent_5_system,
        agent_5_prompt,
        temperature=0.3,
    )
    steps.append(make_step(
        "Agent 5 - Fit Scorer",
        agent_5_system,
        agent_5_prompt,
        agent_5_output,
    ))

    agent_6_system = (
        "You are Agent 6, the final recruiter. Review Agents 1-5 and make "
        "a final decision against the selected job only. A strong data "
        "science resume should not receive a high score for a Project "
        "Manager role unless it clearly shows business leadership, "
        "stakeholder management, budgeting, delivery, and project "
        "ownership. You must call store_verdict with your final decision. "
        "Then return JSON only with keys: final_status, match_score, "
        "ai_reasoning. final_status must be one of INVITE_TO_INTERVIEW, HOLD, "
        "or REJECT. match_score must be an integer from 0 to 100."
    )
    agent_6_prompt = (
        f"Selected job:\n{json.dumps(selected_job, indent=2)}\n\n"
        f"Prior agent outputs:\n{history_text(steps)}"
    )
    agent_6_output, agent_6_tool_calls = call_agent_with_tools(
        client,
        "Agent 6 - Final Recruiter Verdict",
        agent_6_system,
        agent_6_prompt,
        tools=[STORE_VERDICT_TOOL],
        context={"selected_job_id": selected_job_id},
    )
    if not any(tool_call["tool"] == "store_verdict" for tool_call in agent_6_tool_calls):
        raise RuntimeError("Agent 6 did not request the store_verdict tool.")
    verdict = parse_json_object(agent_6_output)
    final_status = str(verdict.get("final_status") or "HOLD")
    match_score = int(verdict.get("match_score") or 0)
    ai_reasoning = str(verdict.get("ai_reasoning") or agent_6_output)
    steps.append(make_step(
        "Agent 6 - Final Recruiter Verdict",
        agent_6_system,
        agent_6_prompt,
        agent_6_output,
        {
            "stored_verdict": {
                "applicant_id": profile["applicant_id"],
                "final_status": final_status,
                "match_score": match_score,
                "ai_reasoning": ai_reasoning,
            }
        },
        agent_6_tool_calls,
    ))

    execution_log = history_text(steps)
    trace = {
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "provider": "AWS Bedrock",
        "aws_profile": AWS_PROFILE,
        "aws_region": AWS_REGION,
        "model": BEDROCK_MODEL_ID,
        "database": str(DB_PATH),
        "selected_job": selected_job,
        "raw_resume": raw_resume,
        "steps": steps,
        "execution_log": execution_log,
        "final": {
            "applicant_id": profile["applicant_id"],
            "name": profile["name"],
            "job_id": selected_job["job_id"],
            "job_title": selected_job["title"],
            "final_status": final_status,
            "match_score": match_score,
            "ai_reasoning": ai_reasoning,
        },
    }
    trace_path = save_trace(trace)
    run_id = trace_path.stem
    PROCESS_RUNS[run_id] = trace

    print("\nRecruitment Review Complete")
    print("=" * 28)
    print(f"Applicant: {profile['name']} ({profile['applicant_id']})")
    print(f"Job: {selected_job['job_id']} - {selected_job['title']}")
    print(f"Final status: {final_status}")
    print(f"Match score: {match_score}")
    print(f"Reason: {ai_reasoning}")
    print(f"Process page: /process?run_id={run_id}")

    return {
        **trace["final"],
        "run_id": run_id,
        "process_url": f"/process?run_id={run_id}",
        "execution_log": execution_log,
    }


HTML_PAGE = """
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Recruitment Review</title>
  <style>
    :root {
      color-scheme: light;
      --ink: #202124;
      --muted: #5f6368;
      --line: #d8dee8;
      --panel: #ffffff;
      --bg: #f6f8fb;
      --accent: #1a73e8;
      --accent-dark: #1558b0;
      --ok: #137333;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      color: var(--ink);
      background: var(--bg);
    }
    header {
      padding: 20px 28px;
      border-bottom: 1px solid var(--line);
      background: #fff;
    }
    h1 {
      margin: 0;
      font-size: 22px;
      font-weight: 700;
      letter-spacing: 0;
    }
    main {
      width: min(1180px, calc(100% - 32px));
      margin: 22px auto;
      display: grid;
      grid-template-columns: minmax(0, 1fr) minmax(320px, 420px);
      gap: 18px;
    }
    section {
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      overflow: hidden;
    }
    .section-head {
      padding: 14px 16px;
      border-bottom: 1px solid var(--line);
      font-weight: 650;
    }
    .dropzone {
      margin: 16px;
      min-height: 138px;
      border: 2px dashed #9eb5d6;
      border-radius: 8px;
      display: grid;
      place-items: center;
      text-align: center;
      padding: 18px;
      color: var(--muted);
      background: #f9fbff;
      transition: border-color 0.15s ease, background 0.15s ease;
    }
    .dropzone.dragover {
      border-color: var(--accent);
      background: #eef5ff;
    }
    textarea {
      display: block;
      width: calc(100% - 32px);
      min-height: 300px;
      margin: 0 16px 16px;
      padding: 14px;
      resize: vertical;
      border: 1px solid var(--line);
      border-radius: 8px;
      font: 14px/1.45 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
      color: var(--ink);
    }
    .field-row {
      display: grid;
      gap: 6px;
      padding: 16px 16px 0;
    }
    .field-row label {
      color: var(--muted);
      font-size: 12px;
      font-weight: 650;
      text-transform: uppercase;
      letter-spacing: 0.04em;
    }
    select {
      width: 100%;
      min-height: 40px;
      border: 1px solid var(--line);
      border-radius: 6px;
      background: #fff;
      color: var(--ink);
      padding: 8px 10px;
      font: inherit;
    }
    .actions {
      display: flex;
      gap: 10px;
      align-items: center;
      padding: 0 16px 16px;
    }
    button {
      border: 0;
      border-radius: 6px;
      background: var(--accent);
      color: #fff;
      padding: 10px 14px;
      font-weight: 650;
      cursor: pointer;
    }
    button:hover { background: var(--accent-dark); }
    button.secondary {
      background: #edf2fa;
      color: #174ea6;
    }
    button.secondary:hover { background: #dbe8fb; }
    button:disabled {
      background: #b8c3d2;
      cursor: wait;
    }
    .status {
      min-height: 20px;
      color: var(--muted);
      font-size: 14px;
    }
    .result {
      padding: 16px;
      display: grid;
      gap: 12px;
    }
    .metric {
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 12px;
      background: #fff;
    }
    .label {
      color: var(--muted);
      font-size: 12px;
      text-transform: uppercase;
      letter-spacing: 0.04em;
      margin-bottom: 5px;
    }
    .value {
      font-size: 20px;
      font-weight: 750;
      overflow-wrap: anywhere;
    }
    .reason {
      line-height: 1.45;
      color: #303134;
    }
    pre {
      white-space: pre-wrap;
      word-break: break-word;
      max-height: 360px;
      overflow: auto;
      margin: 0;
      padding: 12px;
      background: #f8fafc;
      border: 1px solid var(--line);
      border-radius: 8px;
      font-size: 12px;
      line-height: 1.4;
    }
    @media (max-width: 860px) {
      main { grid-template-columns: 1fr; }
    }
  </style>
</head>
<body>
  <header><h1>Recruitment Review</h1></header>
  <main>
    <section>
      <div class="section-head">Resume Input</div>
      <div class="field-row">
        <label for="job-select">Review Against</label>
        <select id="job-select"></select>
      </div>
      <div id="dropzone" class="dropzone">
        <div>
          <strong>Drop resume file</strong><br>
          <span>PDF, DOCX, TXT, or paste text below</span>
        </div>
      </div>
      <textarea id="resume"></textarea>
      <div class="actions">
        <button id="run">Run Review</button>
        <button id="sample" class="secondary">Use Sample</button>
        <span id="status" class="status"></span>
      </div>
    </section>
    <section>
      <div class="section-head">Execution Summary</div>
      <div class="result">
        <div class="metric">
          <div class="label">Final Status</div>
          <div id="final-status" class="value">Ready</div>
        </div>
        <div class="metric">
          <div class="label">Match Score</div>
          <div id="match-score" class="value">-</div>
        </div>
        <div class="metric">
          <div class="label">Reason</div>
          <div id="reason" class="reason">Run the review to generate a verdict.</div>
        </div>
        <div class="metric">
          <div class="label">Agent Process</div>
          <div id="process-link" class="reason">Run the review to see the step-by-step process.</div>
        </div>
      </div>
    </section>
  </main>
  <script>
    const sampleResume = __SAMPLE_RESUME__;
    const dropzone = document.getElementById("dropzone");
    const resume = document.getElementById("resume");
    const statusEl = document.getElementById("status");
    const runButton = document.getElementById("run");
    const jobSelect = document.getElementById("job-select");

    resume.value = sampleResume;

    function setStatus(text) {
      statusEl.textContent = text;
    }

    async function loadJobs() {
      const response = await fetch("/jobs");
      const jobs = await response.json();
      jobSelect.innerHTML = "";
      jobs.forEach((job) => {
        const option = document.createElement("option");
        option.value = job.job_id;
        option.textContent = `${job.job_id} - ${job.title}`;
        option.title = `${job.min_years_exp}+ years, ${job.required_degree}, ${job.mandatory_skills}`;
        jobSelect.appendChild(option);
      });
    }

    ["dragenter", "dragover"].forEach((eventName) => {
      dropzone.addEventListener(eventName, (event) => {
        event.preventDefault();
        dropzone.classList.add("dragover");
      });
    });

    ["dragleave", "drop"].forEach((eventName) => {
      dropzone.addEventListener(eventName, (event) => {
        event.preventDefault();
        dropzone.classList.remove("dragover");
      });
    });

    dropzone.addEventListener("drop", (event) => {
      const file = event.dataTransfer.files[0];
      if (!file) return;
      const formData = new FormData();
      formData.append("resume_file", file);
      setStatus(`Extracting ${file.name}...`);

      fetch("/extract", {
        method: "POST",
        body: formData
      })
        .then(async (response) => {
          const data = await response.json();
          if (!response.ok) throw new Error(data.error || "Could not extract resume");
          resume.value = data.text;
          setStatus(`Loaded ${file.name}`);
        })
        .catch((error) => {
          setStatus(error.message);
        });
    });

    document.getElementById("sample").addEventListener("click", () => {
      resume.value = sampleResume;
      setStatus("Sample loaded");
    });

    runButton.addEventListener("click", async () => {
      runButton.disabled = true;
      setStatus("Running agents...");
      document.getElementById("final-status").textContent = "Running";
      document.getElementById("match-score").textContent = "-";
      document.getElementById("reason").textContent = "";
      document.getElementById("process-link").textContent = "-";

      try {
        const response = await fetch("/run", {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify({
            resume_text: resume.value,
            selected_job_id: jobSelect.value
          })
        });
        const data = await response.json();
        if (!response.ok) throw new Error(data.error || "Pipeline failed");

        document.getElementById("final-status").textContent = data.final_status;
        document.getElementById("match-score").textContent = `${data.match_score}%`;
        document.getElementById("reason").textContent = data.ai_reasoning;
        document.getElementById("process-link").innerHTML =
          `<a href="${data.process_url}" target="_blank" rel="noopener">Open agent process walkthrough</a>`;
        setStatus("Complete");
      } catch (error) {
        document.getElementById("final-status").textContent = "Error";
        document.getElementById("reason").textContent = error.message;
        setStatus("Needs attention");
      } finally {
        runButton.disabled = false;
      }
    });

    loadJobs().catch((error) => setStatus(error.message));
  </script>
</body>
</html>
"""


def safe_run_id(run_id):
    return bool(run_id) and all(
        character.isalnum() or character in "_-"
        for character in run_id
    )


def load_process_trace(run_id):
    if run_id in PROCESS_RUNS:
        return PROCESS_RUNS[run_id]

    if not safe_run_id(run_id):
        return None

    trace_path = TRACE_DIR / f"{run_id}.json"
    if not trace_path.exists():
        return None

    return json.loads(trace_path.read_text(encoding="utf-8"))


def render_process_page(trace):
    final = trace.get("final", {})
    selected_job = trace.get("selected_job", {})
    step_cards = []

    for index, step in enumerate(trace.get("steps", []), start=1):
        step_input = step.get("input", {})
        tool_calls = step.get("tool_calls") or []
        tool_calls_html = ""
        if tool_calls:
            tool_calls_html = (
                "<h3>Tools Requested By This Agent</h3>"
                f"<pre>{escape(json.dumps(tool_calls, indent=2))}</pre>"
            )
        tool_result = step.get("tool_result")
        tool_html = ""
        if tool_result is not None:
            tool_html = (
                "<h3>Tool / Database Data</h3>"
                f"<pre>{escape(json.dumps(tool_result, indent=2))}</pre>"
            )

        step_cards.append(
            f"""
            <article class="step">
              <div class="step-title">Step {index}: {escape(step.get("agent", ""))}</div>
              <h3>System Instructions</h3>
              <pre>{escape(step_input.get("system_prompt", ""))}</pre>
              <h3>Input Passed To This Agent</h3>
              <pre>{escape(step_input.get("user_prompt", ""))}</pre>
              {tool_calls_html}
              {tool_html}
              <h3>Agent Output</h3>
              <pre>{escape(step.get("output", ""))}</pre>
            </article>
            """
        )

    return f"""
    <!doctype html>
    <html lang="en">
    <head>
      <meta charset="utf-8">
      <meta name="viewport" content="width=device-width, initial-scale=1">
      <title>Agent Process Walkthrough</title>
      <style>
        :root {{
          --ink: #202124;
          --muted: #5f6368;
          --line: #d8dee8;
          --bg: #f6f8fb;
          --panel: #ffffff;
          --accent: #1a73e8;
        }}
        * {{ box-sizing: border-box; }}
        body {{
          margin: 0;
          font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
          color: var(--ink);
          background: var(--bg);
        }}
        header {{
          background: #fff;
          border-bottom: 1px solid var(--line);
          padding: 18px 28px;
        }}
        main {{
          width: min(1100px, calc(100% - 32px));
          margin: 22px auto 40px;
        }}
        h1 {{
          margin: 0 0 6px;
          font-size: 24px;
          letter-spacing: 0;
        }}
        .subtle {{
          color: var(--muted);
          line-height: 1.4;
        }}
        .summary, .step {{
          background: var(--panel);
          border: 1px solid var(--line);
          border-radius: 8px;
          padding: 16px;
          margin-bottom: 16px;
        }}
        .summary-grid {{
          display: grid;
          grid-template-columns: repeat(4, minmax(0, 1fr));
          gap: 12px;
        }}
        .label {{
          color: var(--muted);
          font-size: 12px;
          font-weight: 700;
          text-transform: uppercase;
          letter-spacing: 0.04em;
          margin-bottom: 4px;
        }}
        .value {{
          font-weight: 700;
          overflow-wrap: anywhere;
        }}
        .step-title {{
          font-size: 18px;
          font-weight: 750;
          margin-bottom: 10px;
        }}
        h3 {{
          margin: 14px 0 8px;
          font-size: 13px;
          text-transform: uppercase;
          letter-spacing: 0.04em;
          color: var(--muted);
        }}
        pre {{
          margin: 0;
          padding: 12px;
          white-space: pre-wrap;
          word-break: break-word;
          background: #f8fafc;
          border: 1px solid var(--line);
          border-radius: 8px;
          font: 12px/1.45 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
        }}
        a {{ color: var(--accent); }}
        @media (max-width: 800px) {{
          .summary-grid {{ grid-template-columns: 1fr; }}
        }}
      </style>
    </head>
    <body>
      <header>
        <h1>Agent Process Walkthrough</h1>
        <div class="subtle">This page shows what context was passed to each agent, what local tool/database data was used, and what each agent produced.</div>
      </header>
      <main>
        <section class="summary">
          <div class="summary-grid">
            <div>
              <div class="label">Applicant</div>
              <div class="value">{escape(final.get("name", ""))}</div>
            </div>
            <div>
              <div class="label">Job</div>
              <div class="value">{escape(selected_job.get("job_id", ""))} - {escape(selected_job.get("title", ""))}</div>
            </div>
            <div>
              <div class="label">Final Status</div>
              <div class="value">{escape(final.get("final_status", ""))}</div>
            </div>
            <div>
              <div class="label">Match Score</div>
              <div class="value">{escape(str(final.get("match_score", "")))}%</div>
            </div>
          </div>
        </section>
        {"".join(step_cards)}
      </main>
    </body>
    </html>
    """


class RecruitmentRequestHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/jobs":
            setup_database()
            self.send_json(200, list_jobs())
            return

        if parsed.path == "/process":
            run_id = parse_qs(parsed.query).get("run_id", [""])[0]
            trace = load_process_trace(run_id)
            if trace is None:
                self.send_error(404, "Process run not found")
                return

            page = render_process_page(trace)
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(page.encode("utf-8"))
            return

        if parsed.path != "/":
            self.send_error(404)
            return

        page = HTML_PAGE.replace("__SAMPLE_RESUME__", json.dumps(SAMPLE_RESUME.strip()))
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(page.encode("utf-8"))

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path == "/extract":
            self.handle_extract()
            return

        if parsed.path != "/run":
            self.send_error(404)
            return

        try:
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length).decode("utf-8")
            payload = json.loads(body or "{}")
            result = run_pipeline(
                payload.get("resume_text") or SAMPLE_RESUME,
                payload.get("selected_job_id") or "JOB-001",
            )
            self.send_json(200, result)
        except Exception as exc:
            self.send_json(500, {"error": str(exc)})

    def handle_extract(self):
        try:
            content_type = self.headers.get("Content-Type", "")
            if "multipart/form-data" not in content_type:
                raise ValueError("Upload must use multipart/form-data.")

            boundary = content_type.split("boundary=", 1)[1].encode("utf-8")
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length)
            file_name, file_bytes = parse_multipart_upload(body, boundary)
            text = extract_resume_text(file_name, file_bytes)
            if not text.strip():
                raise ValueError(f"No readable text found in {file_name}.")
            self.send_json(200, {"file_name": file_name, "text": text})
        except Exception as exc:
            self.send_json(500, {"error": str(exc)})

    def send_json(self, status_code, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        return


def parse_multipart_upload(body, boundary):
    raw_message = (
        b"Content-Type: multipart/form-data; boundary="
        + boundary
        + b"\r\n\r\n"
        + body
    )
    message = BytesParser(policy=email_policy).parsebytes(raw_message)
    for part in message.iter_parts():
        if part.get_param("name", header="content-disposition") != "resume_file":
            continue

        file_name = part.get_filename() or "resume.txt"
        return file_name, part.get_payload(decode=True)

    raise ValueError("No resume_file upload found.")


def run_server(host="127.0.0.1", port=8765):
    setup_database()
    TRACE_DIR.mkdir(exist_ok=True)
    server = ThreadingHTTPServer((host, port), RecruitmentRequestHandler)
    print(f"Recruitment Review UI running at http://{host}:{port}")
    print("Press Ctrl+C to stop.")
    server.serve_forever()


def cli_port(default=8765):
    if "--port" in sys.argv:
        port_index = sys.argv.index("--port") + 1
        if port_index < len(sys.argv):
            return int(sys.argv[port_index])
    return int(os.getenv("PORT", default))


def cli_job(default="JOB-001"):
    if "--job" in sys.argv:
        job_index = sys.argv.index("--job") + 1
        if job_index < len(sys.argv):
            return sys.argv[job_index]
    return default


if __name__ == "__main__":
    if "--once" in sys.argv:
        run_pipeline(SAMPLE_RESUME, cli_job())
    else:
        run_server(port=cli_port())
