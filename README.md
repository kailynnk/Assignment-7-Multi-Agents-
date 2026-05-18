# Assignment 7: Recruitment Review Multi-Agent Tool App

This project is a local recruiting review app. A user chooses a job, uploads or
pastes a resume, and the app uses AWS Bedrock plus a local SQLite database to
evaluate whether the applicant should move forward.

The current version is not just a hardcoded Python-only sequence. It uses
Bedrock tool calling: each agent is given a role, a prompt, and a set of tools
it is allowed to request. Python executes the tool the model asks for, sends the
tool result back to the model, and then the agent continues reasoning.

## What The App Does

- Runs a browser UI at a local URL.
- Lets the user drag and drop a PDF, DOCX, or TXT resume.
- Extracts resume text into the text box before review.
- Lets the user select which job to review against.
- Stores job rules, applicants, public-footprint evidence, and verdicts in
  SQLite.
- Uses six specialized Bedrock agents.
- Lets Agent 2 and Agent 3 decide whether the pipeline should continue.
- Shows a separate process walkthrough page after each run.
- Blocks duplicate applications for the same name, email, and job.

## Files

- `setup_db.py`: creates and seeds `recruitment_review.db`.
- `run_pipeline.py`: runs the UI, Bedrock agents, tool calls, and process page.
- `requirements.txt`: Python dependencies.
- `.env.example`: optional AWS Bedrock configuration.
- `.gitignore`: excludes generated local files.
- `recruitment_review.db`: generated local SQLite database.
- `traces/`: generated internal JSON used to render the process page.

The database and trace files are generated locally and are ignored by Git.

## Setup

Install dependencies:

```bash
python3 -m pip install -r requirements.txt
```

Create or refresh the database:

```bash
python3 setup_db.py
```

If your AWS SSO session is expired, log in:

```bash
aws sso login --profile default
```

Start the app:

```bash
python3 run_pipeline.py
```

If the default port is busy:

```bash
python3 run_pipeline.py --port 8774
```

Then open the printed local URL in your browser.

## AWS Bedrock Configuration

The app uses AWS Bedrock through `boto3`. It does not use an OpenAI API key.

Default values:

```python
AWS_PROFILE = ""
AWS_REGION = "us-west-2"
BEDROCK_MODEL_ID = "deepseek.v3.2"
```

You can override those values in a local `.env` file:

```env
AWS_PROFILE=
AWS_REGION=us-west-2
BEDROCK_MODEL_ID=deepseek.v3.2
```

DeepSeek is used because it works with the class AWS Bedrock account. Claude
Sonnet required a different Bedrock inference-profile setup and was blocked by
the account policy in the earlier tests.

## Database

The app creates five tables.

### `job_descriptions`

Stores the jobs the UI dropdown can review against.

```text
job_id TEXT PRIMARY KEY
title TEXT
min_years_exp INTEGER
required_degree TEXT
mandatory_skills TEXT
```

Seeded jobs:

```text
JOB-001 | Junior Data Analyst | 2 years | Bachelors | Python, SQL
JOB-002 | Project Manager     | 4 years | Bachelors | Project Management, Stakeholder Management, Business Analysis, Agile, Budgeting
```

### `applicants`

Stores applicant profile data extracted by Agent 1.

```text
applicant_id TEXT PRIMARY KEY
job_id TEXT FOREIGN KEY
name TEXT
email TEXT
years_experience INTEGER
highest_degree TEXT
skills_list TEXT
```

### `skills_taxonomy`

Stores required skills and equivalent terms. Agent 3 uses this table when
deciding if the resume matches the selected job's required skills.

```text
skill_name TEXT PRIMARY KEY
category TEXT
equivalent_terms TEXT
```

Examples:

```text
Python -> pandas, numpy, script, coding
SQL -> queries, database, sqlite, postgres
Budgeting -> budget, forecast, cost, resource planning, financials
```

### `background_checks`

Stores Agent 4's mock public-footprint evidence.

```text
check_id INTEGER PRIMARY KEY AUTOINCREMENT
applicant_id TEXT FOREIGN KEY
search_query TEXT
retrieved_summary TEXT
```

### `recruiting_verdicts`

Stores the saved final decision.

```text
verdict_id INTEGER PRIMARY KEY AUTOINCREMENT
applicant_id TEXT UNIQUE FOREIGN KEY
final_status TEXT
match_score INTEGER
ai_reasoning TEXT
processed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
```

## Resume Upload Flow

```text
1. User uploads a PDF, DOCX, TXT, or pastes resume text.
2. Python receives the file through the /extract endpoint.
3. extract_resume_text() extracts readable text.
4. PDF files use pypdf.
5. DOCX files use python-docx.
6. Plain text files are decoded as text.
7. Extracted text appears in the resume text box.
8. User chooses a job from the dropdown.
9. User clicks Run Review.
10. The selected job and resume text are passed into the agent pipeline.
```

Uploading a file only extracts text. The database is updated only after the user
clicks `Run Review`.

## Duplicate Application Rule

Agent 1 requests the `insert_applicant` tool after extracting the applicant
profile. Inside that tool, Python checks whether the same person already applied
to the same job.

Duplicate check:

```text
same applicant name
same applicant email
same selected job_id
```

If all three already exist, the app stops with:

```text
DUPLICATE_APPLICATION
```

No new applicant, background check, or verdict row is added. The same person can
still apply to a different job.

## Tool Calling

The agents use Bedrock's `client.converse()` API with `toolConfig`.

The important function is:

```python
call_agent_with_tools()
```

That function:

```text
1. Sends the agent prompt and tool definitions to Bedrock.
2. Waits for the model response.
3. Checks whether the model requested a tool.
4. If a tool was requested, Python executes that exact tool.
5. Python sends the tool result back to the same agent.
6. The loop repeats until the agent returns final text instead of another tool call.
```

So Python controls the app infrastructure and database access, but the model
chooses when to request the tools it was given.

Available tools:

```text
insert_applicant(data)
query_db(sql_string)
mock_tavily_search(query)
store_evidence(applicant_id, query, summary)
store_verdict(applicant_id, status, score, reasoning)
```

## Multi-Agent Workflow

The app uses a sequential multi-agent workflow. The overall order is fixed, but
Agent 2 and Agent 3 decide whether the next agent should run.

```text
Input: selected job + resume file/text
        |
        v
Extract resume text
        |
        v
Agent 1: Profile Extractor
        |
        v
Agent 1 requests insert_applicant()
        |
        | if duplicate
        v
Stop with DUPLICATE_APPLICATION
        |
        | otherwise
        v
Agent 2: Prerequisite Filter
        |
        v
Agent 2 requests query_db()
        |
        | if Agent 2 decides proceed_to_next_agent=false
        v
Agent 2 requests store_verdict() + stop
        |
        | otherwise
        v
Agent 3: Skills Verifier
        |
        v
Agent 3 requests query_db()
        |
        | if Agent 3 decides proceed_to_next_agent=false
        v
Agent 3 requests store_verdict() + stop
        |
        | otherwise
        v
Agent 4: Public Digital Footprint
        |
        v
Agent 4 requests mock_tavily_search()
        |
        v
Agent 4 requests store_evidence()
        |
        v
Agent 5: Fit Scorer
        |
        v
Agent 6: Final Recruiter Verdict
        |
        v
Agent 6 requests store_verdict()
        |
        v
Output: final status, match score, reasoning, process walkthrough
```

## Agents

### Agent 1: Profile Extractor

Agent 1 reads the raw resume text and selected job. It extracts:

```text
applicant_id
job_id
name
email
years_experience
highest_degree
skills_list
```

Agent 1 must request:

```python
insert_applicant()
```

Python then normalizes the profile, enforces the selected job from the dropdown,
checks for duplicates, and writes the applicant row if allowed.

### Agent 2: Prerequisite Filter

Agent 2 decides whether the applicant meets the selected job's minimum
experience and degree requirements.

Agent 2 must request:

```python
query_db()
```

It queries the `job_descriptions` table for the selected job. Then it returns
JSON with:

```text
meets_minimum_requirements
experience_requirement_met
degree_requirement_met
proceed_to_next_agent
recommended_status
match_score
reasoning
```

If Agent 2 decides:

```json
"proceed_to_next_agent": false
```

then Agent 2 must request:

```python
store_verdict()
```

The pipeline stops before Agent 3.

### Agent 3: Skills Verifier

Agent 3 decides whether the applicant has the mandatory skills for the selected
job.

Agent 3 must request:

```python
query_db()
```

It queries `skills_taxonomy` and compares the resume skills against the job's
mandatory skills and equivalent terms. Then it returns JSON with:

```text
mandatory_skills_met
matched_required_skills
missing_required_skills
proceed_to_next_agent
recommended_status
match_score
reasoning
```

If Agent 3 decides:

```json
"proceed_to_next_agent": false
```

then Agent 3 must request:

```python
store_verdict()
```

The pipeline stops before Agent 4.

### Agent 4: Public Digital Footprint

Agent 4 creates a search query for the applicant's public footprint.

Agent 4 must request:

```python
mock_tavily_search()
```

This is not a real web search. It returns a hardcoded clean GitHub/LinkedIn-style
summary so the demo can run without a Tavily API key.

After receiving the mock search result, Agent 4 must request:

```python
store_evidence()
```

That saves the query and summary in `background_checks`.

### Agent 5: Fit Scorer

Agent 5 is an LLM-only reasoning step. It does not call a tool.

It reads the prior agent outputs and evaluates:

```text
communication
collaboration
coachability
business context
role alignment
```

It produces a fit score and explanation based on the context passed to it.

### Agent 6: Final Recruiter Verdict

Agent 6 reviews Agents 1 through 5 and makes the final decision.

Valid final statuses:

```text
INVITE_TO_INTERVIEW
HOLD
REJECT
```

Agent 6 must request:

```python
store_verdict()
```

That writes the final status, score, and reasoning to `recruiting_verdicts`.

## Tool Use By Agent

```text
Agent 1 can request -> insert_applicant()
Agent 2 can request -> query_db(), store_verdict()
Agent 3 can request -> query_db(), store_verdict()
Agent 4 can request -> mock_tavily_search(), store_evidence()
Agent 5 has no direct tool
Agent 6 can request -> store_verdict()
```

## AI Decisions And Tool Calls

This section is the clearest way to explain what is automated by Python versus
what is decided by the AI agents.

### Agent 1: Profile Extractor

Tool call:

```text
insert_applicant()
```

AI decisions:

```text
1. Decide the applicant's name from the resume.
2. Decide the applicant's email from the resume.
3. Decide the years of experience stated or implied by the resume.
4. Decide the highest degree listed.
5. Decide which skills should go into skills_list.
6. Request insert_applicant() with the extracted profile.
```

Python responsibilities:

```text
1. Normalize the profile format.
2. Force the selected job_id from the dropdown.
3. Check whether the same name + email + job already exists.
4. Insert the applicant only if it is not a duplicate.
```

### Agent 2: Prerequisite Filter

Tool calls:

```text
query_db()
store_verdict() only if Agent 2 decides the application should stop
```

AI decisions:

```text
1. Request query_db() to read the selected job requirements.
2. Decide whether the applicant meets the minimum years of experience.
3. Decide whether the applicant meets the required degree.
4. Decide whether proceed_to_next_agent should be true or false.
5. If false, decide whether the saved status should be REJECT or HOLD.
6. If false, decide the match score and reasoning.
7. If false, request store_verdict().
```

Important behavior:

```text
If Agent 2 says proceed_to_next_agent=false, the pipeline stops before Agent 3.
That means no skills check happens when Agent 2 decides the applicant does not
meet the minimum requirements.
```

Python responsibilities:

```text
1. Execute the query_db() tool requested by Agent 2.
2. Execute store_verdict() if Agent 2 requests it.
3. Read Agent 2's proceed_to_next_agent value.
4. Stop or continue based on Agent 2's decision.
```

### Agent 3: Skills Verifier

Tool calls:

```text
query_db()
store_verdict() only if Agent 3 decides the application should stop
```

AI decisions:

```text
1. Request query_db() to read the skills_taxonomy table.
2. Decide which resume skills match the selected job's mandatory skills.
3. Decide whether equivalent terms count as matches.
4. Decide which required skills are missing.
5. Decide whether proceed_to_next_agent should be true or false.
6. If false, decide whether the saved status should be REJECT or HOLD.
7. If false, decide the match score and reasoning.
8. If false, request store_verdict().
```

Important behavior:

```text
If Agent 3 says proceed_to_next_agent=false, the pipeline stops before Agent 4.
That means no public-footprint review, fit scoring, or final recruiter review
happens when Agent 3 decides the skills do not match.
```

Python responsibilities:

```text
1. Execute the query_db() tool requested by Agent 3.
2. Execute store_verdict() if Agent 3 requests it.
3. Read Agent 3's proceed_to_next_agent value.
4. Stop or continue based on Agent 3's decision.
```

### Agent 4: Public Digital Footprint

Tool calls:

```text
mock_tavily_search()
store_evidence()
```

AI decisions:

```text
1. Decide what public search query should be used for the applicant.
2. Request mock_tavily_search() with that query.
3. Read the mock search result.
4. Decide what evidence summary should be saved.
5. Request store_evidence().
```

Python responsibilities:

```text
1. Return the hardcoded mock search result.
2. Save the evidence in background_checks.
```

### Agent 5: Fit Scorer

Tool calls:

```text
No direct tool calls.
```

AI decisions:

```text
1. Read the prior agent outputs.
2. Decide how well the applicant fits the selected role.
3. Decide a soft-skill/team-fit score.
4. Explain the score using the context passed from earlier agents.
```

Python responsibilities:

```text
1. Pass the previous agent outputs into Agent 5.
2. Save Agent 5's output into the process walkthrough.
```

### Agent 6: Final Recruiter Verdict

Tool call:

```text
store_verdict()
```

AI decisions:

```text
1. Review outputs from Agents 1 through 5.
2. Decide the final status: INVITE_TO_INTERVIEW, HOLD, or REJECT.
3. Decide the final match score.
4. Decide the final reasoning.
5. Request store_verdict() with that final decision.
```

Python responsibilities:

```text
1. Execute store_verdict().
2. Save the final result in recruiting_verdicts.
3. Return the final status, score, and reasoning to the UI.
```

## Process Walkthrough Page

After a run, the UI shows a link:

```text
Open agent process walkthrough
```

That page shows:

- The selected job.
- The final status and match score.
- Each agent's system instructions.
- The exact input passed to each agent.
- The tools requested by each agent.
- The tool inputs and tool results.
- Any database/tool data saved for the step.
- The output produced by each agent.

This page is meant to make the workflow explainable during a demo.

## Console Test

Run the built-in sample resume:

```bash
python3 run_pipeline.py --once
```

Run the sample against a specific job:

```bash
python3 run_pipeline.py --once --job JOB-002
```

The pipeline may stop early if Agent 2 or Agent 3 decides not to continue.

## Why This Counts As Multi-Agent

This is a multi-agent workflow because each stage is a separate Bedrock model
call with a different role, prompt, responsibility, and allowed tools.

The context is passed forward:

```text
Agent 1 output -> Agent 2 prompt
Agent 2 output -> Agent 3 prompt
Agent 3 output -> Agent 4 prompt
Agent 4 output -> Agent 5 prompt
Agent 5 output -> Agent 6 prompt
```

The best description is:

```text
a sequential multi-agent recruitment review app with Bedrock tool-calling agents
```

## What The AI Decides

The app still has a fixed review order so the demo is predictable. The AI makes
the important review decisions inside that order:

- Agent 1 decides what profile data to extract from the resume.
- Agent 2 decides whether minimum requirements are met and whether to continue.
- Agent 3 decides whether required skills are met and whether to continue.
- Agent 4 decides what public-footprint query to run.
- Agent 5 decides the fit score from the prior context.
- Agent 6 decides the final recruiting status and match score.

Python provides the UI, database, file parsing, and guardrails. The agents do
the resume evaluation and request their assigned tools.
