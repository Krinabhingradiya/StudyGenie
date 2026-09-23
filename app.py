from flask import (
    Flask,
    render_template,
    request,
    redirect,
    url_for,
    session,
    send_from_directory,
    flash,
    jsonify
)
import sqlite3
import os
from datetime import datetime
from dotenv import load_dotenv
import fitz
import markdown
import pytesseract
from PIL import Image
import io
from groq import Groq
from flask import make_response, jsonify
from xhtml2pdf import pisa
from io import BytesIO
import json
import re
import requests
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity
from datetime import timedelta
import zipfile
from xml.etree import ElementTree as ET

def get_page_count(filepath):
    """Returns page count for PDF (exact) and DOCX (best estimate)."""
    ext = filepath.lower()

    try:
        if ext.endswith(".pdf"):
            doc = fitz.open(filepath)
            count = doc.page_count
            doc.close()
            return count

        elif ext.endswith(".docx"):
            try:
                with zipfile.ZipFile(filepath) as z:
                    with z.open("docProps/app.xml") as f:
                        tree = ET.parse(f)
                        ns = {"ep": "http://schemas.openxmlformats.org/officeDocument/2006/extended-properties"}
                        pages_el = tree.find("ep:Pages", ns)
                        if pages_el is not None and pages_el.text and int(pages_el.text) > 0:
                            return int(pages_el.text)
            except Exception:
                pass

            from docx import Document
            d = Document(filepath)
            word_count = sum(len(p.text.split()) for p in d.paragraphs)
            return max(1, round(word_count / 500))

    except Exception as e:
        print("PAGE COUNT ERROR:", e)

    return "--"


app = Flask(__name__)
app.secret_key = "studygenie_secret_key"
pytesseract.pytesseract.tesseract_cmd = r"C:\Program Files\Tesseract-OCR\tesseract.exe"
load_dotenv()

UPLOAD_FOLDER = "uploads"
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

GROQ_API_KEY = os.getenv("GROQ_API_KEY")
YOUTUBE_API_KEY = os.getenv("YOUTUBE_API_KEY")

client = Groq(api_key=GROQ_API_KEY)
ACTIVITY_META = {
    "upload":      {"icon": "bi-file-earmark-check", "color": "text-primary",   "label": "{filename} uploaded"},
    "summary":     {"icon": "bi-stars",               "color": "text-success",  "label": "Summary generated for {filename}"},
    "quiz":        {"icon": "bi-bullseye",            "color": "text-danger",   "label": "Quiz attempted — {filename}"},
    "study_plan":  {"icon": "bi-calendar-check",      "color": "text-warning",  "label": "Study plan created for {filename}"},
    "interview":   {"icon": "bi-person-workspace",    "color": "text-primary",  "label": "Interview practice — {filename}"},
}

def format_relative_time(iso_str):
    try:
        dt = datetime.fromisoformat(iso_str)
    except Exception:
        return iso_str

    diff = datetime.now() - dt
    seconds = diff.total_seconds()

    if seconds < 60:
        return "Just now"

    minutes = int(seconds // 60)
    if minutes < 60:
        return f"{minutes} minute{'s' if minutes != 1 else ''} ago"

    hours = int(minutes // 60)
    if hours < 24:
        return f"{hours} hour{'s' if hours != 1 else ''} ago"

    if diff.days == 1:
        return "Yesterday"

    if diff.days < 7:
        return f"{diff.days} days ago"

    return dt.strftime("%d %b %Y")
def init_activity_table():
    conn = sqlite3.connect("users.db")
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS activity_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            activity_type TEXT,
            filename TEXT,
            created_at TEXT
        )
    """)
    conn.commit()
    conn.close()
    
init_activity_table()   # app.py load hote hi ek baar chal jayega
def generate_summary(text):
    last_error = None

    for attempt in range(3):
        try:
            response = client.chat.completions.create(
                model="openai/gpt-oss-20b",
                max_tokens=4096,
                response_format={"type": "json_object"},
                messages=[
                    {
                        "role": "user",
                        "content":f"""You are StudyGenie AI, generating professional academic study notes for university students.

Break the content into topics. For each topic give:
- "topic": a concise academic topic title
- "importance": one of "Important", "Moderate", or "Easy"
- "points": 3 to 4 professional study points

Rules:
- Extract topics ONLY from the study material.
- Cover the main topics only (maximum 10 topics).
- Use professional educational language suitable for college students.
- Do not use overly simplified explanations.
- Avoid examples like Gmail, YouTube, WhatsApp, etc. unless essential.
- Each point should be a complete informative sentence.
- Focus on definitions, concepts, features, advantages, applications, and key facts.
- Do not copy textbook sentences directly.
- Keep points concise but academically meaningful.
- Generate professional topic names.

Return ONLY valid JSON, nothing else, in exactly this format:
{{"topics":[{{"topic":"Topic Name","importance":"Important","points":["point one","point two","point three"]}}]}}

Study Material:
{text[:8000]}
"""
                    }
                ]
            )

            raw = response.choices[0].message.content.strip()
            raw = re.sub(r"^```(?:json)?\s*", "", raw)
            raw = re.sub(r"\s*```$", "", raw)

            start = raw.find('{')
            end = raw.rfind('}')
            if start != -1 and end != -1:
                raw = raw[start:end+1]

            data = json.loads(raw)

            if data.get("topics"):   # got a usable result
                return data

            last_error = "Empty topics list"
            continue

        except Exception as e:
            last_error = e
            print(f"SUMMARY ERROR (attempt {attempt + 1}/3):", e)
            continue

    print("SUMMARY ERROR: all retries failed:", last_error)
    return {"topics": [{"topic": "Summary", "importance": "Important",
                         "points": ["Could not generate summary. Please try again."]}]}

def get_youtube_videos(topic, max_results=4):
    if not YOUTUBE_API_KEY:
        return []
    search_query = f"{topic} tutorial explained simply"
    params = {
        "part": "snippet",
        "q": search_query,
        "type": "video",
        "videoDuration": "short",
        "relevanceLanguage": "en",
        "safeSearch": "strict",
        "maxResults": max_results,
        "key": YOUTUBE_API_KEY
    }
    try:
        resp = requests.get("https://www.googleapis.com/youtube/v3/search", params=params, timeout=8)
        resp.raise_for_status()
        items = resp.json().get("items", [])
    except requests.RequestException:
        return []
    return [
        {
            "title": item["snippet"]["title"],
            "video_id": item["id"]["videoId"],
            "channel": item["snippet"]["channelTitle"],
            "thumbnail": item["snippet"]["thumbnails"]["medium"]["url"]
        }
        for item in items
    ]
def generate_flashcards(text):
    last_error = None
    last_result = None

    for attempt in range(5):   # increased from 3 to 5 attempts

        try:
            response = client.chat.completions.create(
                model="openai/gpt-oss-20b",
                max_tokens=2048,
                response_format={"type": "json_object"},
                messages=[
                    {
                        "role": "user",
                        "content": f"""You are StudyGenie AI. Create flashcards from the study material below.

Rules:
- Create Exactly 10 flashcards, ideally 12, covering the most important concepts.
  If the material genuinely doesn't support 12 distinct concepts, create as
  many good ones as possible — but never fewer than 8.
- Extract content ONLY from the study material below. Do not add outside knowledge.
- Each flashcard has a short, clear "question" (one line, single concept).
- The "answer" must fully explain the concept in 1-2 complete sentences —
  it should make sense on its own, even to someone who hasn't read the
  document. Do NOT answer with a document fragment, a partial phrase, or
  a line copied out of context.
- If the source material doesn't clearly explain a concept, don't
  make a flashcard for it — pick a different concept instead.
- Do not copy textbook sentences directly — rephrase in simple everyday
  language, explaining any jargon briefly.
- Assign "difficulty": "Easy" for basic facts, "Medium" for concepts,
  "Hard" for comparisons/application. Cover a mix of difficulties —
  do not make all flashcards the same difficulty.

Return ONLY valid JSON, nothing else, no markdown code fences, in exactly this format:
{{"flashcards": [{{"question": "Question text", "answer": "Answer text", "difficulty": "Easy"}}]}}

Study Material:
{text[:4000]}"""
                    }
                ]
            )

            raw = response.choices[0].message.content.strip()
            raw = re.sub(r"^```(?:json)?\s*", "", raw)
            raw = re.sub(r"\s*```$", "", raw)

            start = raw.find("{")
            end = raw.rfind("}")
            if start != -1 and end != -1:
                raw = raw[start:end + 1]

            data = json.loads(raw)
            flashcards = data.get("flashcards", [])

            last_result = data   # keep the best result seen so far, even if short

            # Success criteria: got a reasonable number of cards
            if len(flashcards) >= 8:
                return data

            print(f"FLASHCARD WARNING (attempt {attempt + 1}/5): only {len(flashcards)} cards, retrying...")
            continue   # too few — try again

        except Exception as e:
            last_error = e
            print(f"FLASHCARD ERROR (attempt {attempt + 1}/5):", e)
            continue

    # All 5 attempts either errored or returned too few cards.
    # If we got at least SOME valid cards on any attempt, use the best one
    # rather than showing a total failure message.
    if last_result and last_result.get("flashcards"):
        print("FLASHCARD: using best available result after retries:",
              len(last_result["flashcards"]), "cards")
        return last_result

    print("FLASHCARD ERROR: all retries failed:", last_error)
    return {
        "flashcards": [
            {
                "question": "Flashcards could not be generated.",
                "answer": "Please try again — this is usually a temporary issue.",
                "difficulty": "Easy"
            }
        ]
    }
@app.route("/flashcard")
def flashcard():
    return render_template("flashcard.html")

@app.route("/quiz")
def quiz():

    files = os.listdir("uploads")

    return render_template(
        "quiz.html",
        files=files
    )

@app.route("/generate-flashcards")
def generate_flashcards_api():

    filename = request.args.get("file")

    if not filename:
        return jsonify({
            "success": False,
            "message": "No study file selected."
        })

    pdf_path = os.path.join("uploads", filename)

    if not os.path.exists(pdf_path):
        return jsonify({
            "success": False,
            "message": "Selected study file was not found."
        })

    try:
        print("FLASHCARD PDF:", filename)

        text = extract_text_from_pdf(pdf_path)

        if not text.strip():
            return jsonify({
                "success": False,
                "message": "Could not extract text from the PDF."
            })

        print("TEXT LENGTH:", len(text))

        result = generate_flashcards(text[:3000])

        flashcards = result.get("flashcards", [])

        print("FLASHCARDS GENERATED:", len(flashcards))

        return jsonify({
            "success": True,
            "flashcards": flashcards
        })

    except Exception as e:

        print("FLASHCARD ERROR:", e)

        return jsonify({
            "success": False,
            "message": str(e)
        })
@app.route("/mcq")
def mcq():
    return render_template("mcq.html")       
@app.route("/generate-mcq")
def generate_mcq():

    file_name = request.args.get("file")

    if not file_name:
        return jsonify({"error": "No file selected"}), 400

    file_path = os.path.join(UPLOAD_FOLDER, file_name)

    if not os.path.exists(file_path):
        return jsonify({"error": "File not found"}), 404

    try:
        # Extract study material
        text = extract_text_from_pdf(file_path)

        if not text.strip():
            return jsonify({"error": "No text found in file"}), 400

        prompt = f"""
You are an expert educational quiz generator for StudyGenie.

Create exactly 10 multiple-choice questions from the study material below.

Return ONLY valid JSON in exactly this format:

{{
  "questions": [
    {{
      "question": "Question text",
      "options": [
        "Option A",
        "Option B",
        "Option C",
        "Option D"
      ],
      "answer": 0,
      "difficulty": "Medium"
    }}
  ]
}}

Rules:

1. Create exactly 10 questions.
2. Every question must have exactly 4 options.
3. answer must be a number:
   0 = Option A
   1 = Option B
   2 = Option C
   3 = Option D
4. difficulty must be exactly one of:
   Easy
   Medium
   Hard
5. Make a balanced difficulty:
   - some Easy
   - some Medium
   - some Hard
6. Do NOT make every question Easy.
7. Questions must be based ONLY on the study material.
8. Do not include explanations.
9. Do not include markdown.
10. Do not include text outside the JSON.
11. Make sure the correct answer actually matches one of the four options.

STUDY MATERIAL:

{text[:12000]}
"""

        response = client.chat.completions.create(
            model="openai/gpt-oss-20b",
            messages=[
                {
                    "role": "user",
                    "content": prompt
                }
            ],
            max_tokens=2048,
            temperature=0.3,
            response_format={"type": "json_object"}
        )

        result = response.choices[0].message.content.strip()

        # Remove markdown fences if model adds them
        result = result.replace("```json", "")
        result = result.replace("```", "")
        result = result.strip()

        data = json.loads(result)

        # Basic validation
        if "questions" not in data:
            return jsonify({
                "error": "AI response does not contain questions"
            }), 500

        if len(data["questions"]) == 0:
            return jsonify({
                "error": "No questions generated"
            }), 500
        conn = sqlite3.connect("users.db")
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO activity_log (user_id, activity_type, filename, created_at) VALUES (?,?,?,?)",
            (session["user_id"], "quiz", file_name, datetime.now().isoformat())
        )
        conn.commit()
        conn.close()

        return jsonify(data)
        return jsonify(data)

    except Exception as e:

        print("MCQ ERROR:", repr(e))

        return jsonify({
            "error": str(e)
        }), 500
        
_document_chunks_cache = {}
# ---------------- CHUNKING ---------------- #

def chunk_text(text, chunk_size=180):
    """
    Splits text into chunks of roughly `chunk_size` words, trying to
    keep paragraph boundaries intact rather than cutting mid-sentence.
    """
    paragraphs = [p.strip() for p in text.split("\n") if p.strip()]

    chunks = []
    current_chunk = []
    current_len = 0

    for para in paragraphs:
        words = para.split()

        if current_len + len(words) > chunk_size and current_chunk:
            chunks.append(" ".join(current_chunk))
            current_chunk = []
            current_len = 0

        current_chunk.extend(words)
        current_len += len(words)

    if current_chunk:
        chunks.append(" ".join(current_chunk))

    return chunks


def get_document_chunks(filename):
    """Returns cached chunks for a file, extracting + chunking only once."""
    if filename in _document_chunks_cache:
        return _document_chunks_cache[filename]

    pdf_path = os.path.join(UPLOAD_FOLDER, filename)
    text = extract_text_from_pdf(pdf_path)
    chunks = chunk_text(text)

    _document_chunks_cache[filename] = chunks
    return chunks


# ---------------- RETRIEVAL (TF-IDF + cosine similarity) ---------------- #

def retrieve_relevant_chunks(question, chunks, top_k=4, min_score=0.0):
    """
    Returns the top_k chunks most relevant to the question.

    Note: TF-IDF matches on keyword overlap, not meaning. Vague/generic
    questions (e.g. "give me important topics") often share almost no
    vocabulary with the document text, so their similarity score is low
    even when the document is perfectly capable of answering them.
    Rather than filtering those out (min_score=0.0 by default), we return
    the best-available chunks and let the model use its judgment — this
    matters more for short study documents where any returned chunk is
    still genuinely part of the material.
    """
    if not chunks:
        return []

    if len(chunks) == 1:
        return chunks

    vectorizer = TfidfVectorizer(stop_words="english")

    try:
        chunk_vectors = vectorizer.fit_transform(chunks)
        question_vector = vectorizer.transform([question])
    except ValueError:
        return chunks[:top_k]

    similarities = cosine_similarity(question_vector, chunk_vectors)[0]
    ranked_indices = similarities.argsort()[::-1][:top_k]

    relevant = [chunks[i] for i in ranked_indices if similarities[i] >= min_score]

    return relevant


# ---------------- CHAT PAGE ---------------- #

@app.route("/chat")
def chat():
    if "user_email" not in session:
        return redirect(url_for("login"))

    files = os.listdir(UPLOAD_FOLDER)
    return render_template("chat.html", files=files)


# ---------------- SEND MESSAGE (RAG when a file is attached) ---------------- #

@app.route("/send-message", methods=["POST"])
def send_message():
    if "user_email" not in session:
        return jsonify({"success": False, "message": "Please log in again."})

    data = request.get_json(silent=True) or {}
    user_message = (data.get("message") or "").strip()
    filename = data.get("filename")  # None/empty if no file attached

    if not user_message:
        return jsonify({"success": False, "message": "Message is empty."})

    chat_history = session.get("chat_history", [])

    if filename:
        pdf_path = os.path.join(UPLOAD_FOLDER, filename)
        if not os.path.exists(pdf_path):
            return jsonify({"success": False, "message": "That file wasn't found."})

        chunks = get_document_chunks(filename)
        print(f"DEBUG: {filename} -> {len(chunks)} chunks extracted")
        if chunks:
            print(f"DEBUG: first chunk preview: {chunks[0][:150]}")
        relevant_chunks = retrieve_relevant_chunks(user_message, chunks, top_k=4)
        print(f"DEBUG: {len(relevant_chunks)} relevant chunks found for question: {user_message}")

        if relevant_chunks:
            context = "\n\n---\n\n".join(relevant_chunks)
            system_prompt = f"""You are StudyGenie AI, a helpful study assistant.
Answer the student's question using ONLY the context below, taken from their document "{filename}".
Explain clearly and simply. If the context doesn't fully answer the question,
say so honestly instead of guessing.

CRITICAL: A document is attached, so ALWAYS generate your answer FROM the
document content below — never from general/outside knowledge, even if the
student's phrasing is ambiguous or informal. For example:
- "imp questions" / "important questions" / "viva questions" / "exam questions"
  → generate practice questions based ONLY on the concepts in the document below,
  NOT generic interview or general-knowledge questions.
- "imp topics" / "summary" → summarize ONLY what's in the document below.
Do not reinterpret a vague request as a request for unrelated general content —
always assume it's about the attached document.

FORMATTING: Do NOT use markdown tables (no | pipe | characters or --- rows).
When giving multiple questions/points, use this simple format — bold the
question so it visually stands out from the answer:

1. **Question text here?**
Answer text here, in plain text.

2. **Next question here?**
Answer text here.

Just a number, the BOLDED question, then the answer on the next line.
Leave a blank line between each numbered item so they're easy to scan.
Nothing more complex than that — no tables, no extra columns.

VIVA/ORAL EXAM ANSWERS: When the student asks for "viva questions", "imp
questions", "important questions", "exam questions", or anything similar
(practice questions to prepare/revise from) — treat these all the SAME way:

1. The QUESTIONS themselves must cover a MIX of difficulty — start with
   the most basic, foundational definitions (e.g. "What is X?") before
   moving to more advanced or specialized concepts. Don't jump straight
   into niche/advanced topics — a student revising for a viva needs the
   fundamentals covered first, then depth.
2. The ANSWERS must be SHORT — 1 to 2 short sentences MAX, like something
   a student would actually say out loud in an exam. Do NOT write
   exhaustive, detailed paragraphs listing every possible point.
3. NEVER use bullet points (*, -) inside an answer, even for "difference
   between X and Y" style questions — write it as one short plain sentence.

Match this EXACT style (this is the target quality bar):

1. **What is IaaS?**
IaaS stands for Infrastructure as a Service. It provides infrastructure
resources such as compute, storage and networking over the cloud.

2. **What is the difference between IaaS, PaaS and SaaS?**
IaaS provides infrastructure, PaaS provides a development platform, and
SaaS provides ready-to-use software — each removes a different layer of
management from the user.

3. **What are the deployment models of cloud?**
Public, Private, Hybrid, Community and Multi-Cloud.

This applies no matter which exact word the student uses ("viva", "imp
questions", "important questions", etc.) — always prioritize important
concepts, keep answers short, and never use bullets inside an answer.

IMPORTANT: Look at the student's MOST RECENT message only to decide the reply
language — English, Hindi, or Hinglish. Ignore the language used earlier in the
conversation if the latest message is in a different language. If their latest
message is in English, reply in English. If in Hindi/Hinglish, reply in that.

Context from the document:
{context}"""
        else:
            system_prompt = f"""You are StudyGenie AI. The student attached "{filename}" but nothing
in it seems relevant to their question. Tell them clearly (in the SAME language/style
they used) that you couldn't find this in the document, and ask if they'd like a
general answer instead."""
    else:
        system_prompt = """You are StudyGenie AI, a helpful and friendly study assistant.
Have a normal, clear, helpful conversation. No document is attached right now.

Match your reply's length and tone to the message: for short casual messages
(like "thanks", "ok", "hello"), reply briefly and naturally — a sentence or
less. Save longer, detailed answers for when the student actually asks a
real question.

FORMATTING: Do NOT use markdown tables (no | pipe | characters or --- rows).
Use simple numbered lists or bullet points instead.

IMPORTANT: Look at the student's MOST RECENT message only to decide the reply
language — English, Hindi, or Hinglish. Ignore the language used earlier in the
conversation if the latest message is in a different language. If their latest
message is in English, reply in English. If in Hindi/Hinglish, reply in that."""

    messages = [{"role": "system", "content": system_prompt}]
    messages.extend(chat_history[-6:])  # last few turns for conversational context
    messages.append({"role": "user", "content": user_message})

    try:
        response = client.chat.completions.create(
            model="openai/gpt-oss-20b",
            max_tokens=800,
            messages=messages
        )
        answer = response.choices[0].message.content.strip()

    except Exception as e:
        print("CHAT ERROR:", e)
        return jsonify({"success": False, "message": "Could not generate a response. Please try again."})

    chat_history.append({"role": "user", "content": user_message})
    chat_history.append({"role": "assistant", "content": answer})
    session["chat_history"] = chat_history[-20:]  # cap history size

    return jsonify({"success": True, "answer": answer})


# ---------------- CLEAR CHAT ---------------- #

@app.route("/clear-chat", methods=["POST"])
def clear_chat():
    session.pop("chat_history", None)
    return jsonify({"success": True})

@app.route("/upload-chat-file", methods=["POST"])
def upload_chat_file():
    if "user_email" not in session:
        return jsonify({"success": False, "message": "Please log in again."})

    file = request.files.get("file")

    if not file or file.filename == "":
        return jsonify({"success": False, "message": "No file selected."})

    filename = file.filename
    file.save(os.path.join(UPLOAD_FOLDER, filename))

    return jsonify({"success": True, "filename": filename})
# =====================================================================
# ADD to app.py
# =====================================================================
def generate_study_plan(text, days_available):
    try:
        response = client.chat.completions.create(
            model="openai/gpt-oss-20b",
            max_tokens=2000,
            temperature=0.4,
            response_format={"type": "json_object"},
            messages=[
                {
                    "role": "user",
                    "content": f"""You are StudyGenie AI, creating a day-by-day study plan for a student
who has exactly {days_available} day(s) before their exam/deadline.

Rules:
- Extract topics ONLY from the study material below. Do not invent topics
  that aren't in the material.
- Break the material into {days_available} day(s), grouping related topics
  together per day. Spread the material evenly — don't cram everything into
  day 1 and leave day 2+ nearly empty.
- For each day, give:
  - "focus": a short 3-6 word title for what that day covers
  - "tasks": exactly 3 to 4 tasks. Each task is an OBJECT (not a plain
    string) with exactly two fields:
      - "text": ONE short actionable sentence (e.g. "Read the IaaS section
        and note 3 key benefits", not just "Study IaaS")
      - "minutes": a realistic integer estimate of how many minutes an
        average student needs to complete that single task (typically
        between 10 and 30)
    Keep every task at a SIMILAR level of detail — don't mix one-word tasks
    with overly long, hyper-specific ones.
- On the LAST day only, make the final task a full revision of all days'
  topics combined, with a slightly longer "minutes" estimate.
- Every task must be something the student can actually check off as done —
  avoid vague tasks like "understand the concept" or "review everything".

Return ONLY valid JSON, nothing else, no markdown code fences, in exactly
this format:
{{"plan": [{{"day": 1, "focus": "Short focus title", "tasks": [{{"text": "task one", "minutes": 15}}, {{"text": "task two", "minutes": 20}}]}}]}}

Study Material:
{text[:8000]}"""
                }
            ]
        )

        raw = response.choices[0].message.content.strip()
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)

        start = raw.find("{")
        end = raw.rfind("}")
        if start != -1 and end != -1:
            raw = raw[start:end + 1]

        return json.loads(raw)

    except Exception as e:
        print("STUDY PLAN ERROR:", e)
        return {"plan": [{"day": 1, "focus": "Could not generate plan",
                           "tasks": [{"text": "Please try again.", "minutes": 5}]}]}

def generate_interview_questions(interview_type, branch, experience, document_text=None):
    context_block = ""
    if document_text:
        context_block = f"""
Also base some questions on this study material where relevant:
{document_text[:6000]}
"""

    prompt = f"""You are an AI interview coach preparing a {branch} student for a
{interview_type} at the {experience} level.

CRITICAL RULE: All questions must be strictly relevant to the student's actual
field of study, which is "{branch}". Do NOT default to generic Computer
Science / programming questions unless "{branch}" is itself a computer
science, IT, or software-related field.

For example:
- If branch is "Arts / Humanities", technical questions should be about
  literature, history, design, writing, research methods, etc. — NOT code.
- If branch is "Biology / Life Sciences" or "Biotechnology", technical
  questions should be about biology concepts, lab techniques, genetics,
  microbiology, etc. — NOT code.
- If branch is "Commerce / B.Com" or "Business Administration / BBA / MBA",
  technical questions should be about accounting, finance, marketing,
  economics, business case scenarios, etc. — NOT code.
- Only ask programming/data-structures/software questions if the branch is
  genuinely Computer Engineering, IT, AI & ML, Data Science, or similar.

Interpret "{interview_type}" appropriately for this field:
- "Technical Interview" = core subject-knowledge questions specific to
  "{branch}".
- "HR Interview" = behavioral, motivational, and general fit questions
  (these can stay field-agnostic).
- "Project Viva" = questions about a college project/thesis a student in
  this field would typically have done.

QUESTION 1 RULE (always applies, regardless of interview type):
The very FIRST question must always be a warm-up introduction question —
something like "Tell me about yourself" or "Walk me through your background
and what you're currently studying." It should be general, not
subject-specific, exactly like how real interviews open. Questions 2 through
10 should then get progressively more specific to "{branch}" and
"{interview_type}".

Generate exactly 10 interview questions appropriate for this combination of
interview type, branch, and experience level. Questions should be realistic,
commonly asked in real interviews, and appropriately difficult for a
{experience} candidate.
{context_block}
Return ONLY valid JSON, no markdown, no extra text, in exactly this format:
{{"questions": ["question one", "question two", "..."]}}
"""

    response = client.chat.completions.create(
        model="openai/gpt-oss-20b",
        max_tokens=1200,
        temperature=0.5,
        response_format={"type": "json_object"},
        messages=[{"role": "user", "content": prompt}]
    )

    raw = response.choices[0].message.content.strip()
    raw = re.sub(r"^```(?:json)?\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)
    start = raw.find("{")
    end = raw.rfind("}")
    if start != -1 and end != -1:
        raw = raw[start:end + 1]
    return json.loads(raw)

def evaluate_interview_answer(question, answer, interview_type):
    try:
        response = client.chat.completions.create(
            model="openai/gpt-oss-20b",
            max_tokens=700,
            temperature=0.3,
            response_format={"type": "json_object"},
            messages=[
                {
                    "role": "user",
                    "content": f"""You are an AI interview coach evaluating a candidate's spoken/written
answer during a {interview_type}.

Question: {question}
Candidate's Answer: {answer}

Score the answer out of 10 based on clarity, correctness, structure and confidence.

Return ONLY valid JSON, no markdown, no extra text, in exactly this format:
{{"score": 7, "confidence": "Good", "strengths": ["point one", "point two"], "improvements": ["point one", "point two"]}}

Rules:
- "score" is an integer from 0 to 10.
- "confidence" is exactly one of: "Needs Work", "Average", "Good", "Excellent".
- "strengths": 1 to 3 short specific things the candidate did well.
- "improvements": 1 to 3 short specific things to improve.
- Keep every point under 15 words.
- If the answer is empty, off-topic, or nonsense, score it low and say so honestly.
"""
                }
            ]
        )

        raw = response.choices[0].message.content.strip()
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
        start = raw.find("{")
        end = raw.rfind("}")
        if start != -1 and end != -1:
            raw = raw[start:end + 1]

        data = json.loads(raw)

        data.setdefault("score", 5)
        data.setdefault("confidence", "Average")
        data.setdefault("strengths", [])
        data.setdefault("improvements", [])

        return data

    except Exception as e:
        print("INTERVIEW EVALUATION ERROR:", e)
        return {
            "score": 5,
            "confidence": "Average",
            "strengths": ["Answer was received."],
            "improvements": ["Could not fully evaluate this answer — please try again."]
        }
# ---------------- STUDY PLANNER PAGE ---------------- 
@app.route("/study-planner")
def study_planner():
    if "user_email" not in session:
        return redirect(url_for("login"))

    files = os.listdir(UPLOAD_FOLDER)
    return render_template("study_planner.html", files=files)


# ---------------- GENERATE PLAN (fetched by JS) ---------------- #

_document_text_cache = {}

def get_document_text(filename):
    if filename in _document_text_cache:
        return _document_text_cache[filename]

    pdf_path = os.path.join(UPLOAD_FOLDER, filename)
    text = extract_text_from_pdf(pdf_path)
    _document_text_cache[filename] = text
    return text


@app.route("/generate-study-plan")
def generate_study_plan_api():
    filename = request.args.get("file")
    days = request.args.get("days", "3")

    try:
        days_available = max(1, min(int(days), 14))  # cap between 1-14 days
    except ValueError:
        days_available = 3

    if not filename:
        return jsonify({"success": False, "message": "No study file selected."})

    pdf_path = os.path.join(UPLOAD_FOLDER, filename)

    if not os.path.exists(pdf_path):
        return jsonify({"success": False, "message": "Selected study file was not found."})

    try:
        text = get_document_text(filename)   # cached — no repeat OCR

        if not text.strip():
            return jsonify({"success": False, "message": "Could not extract text from the file."})

        result = generate_study_plan(text, days_available)
        plan = result.get("plan", [])

        conn = sqlite3.connect("users.db")
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO activity_log (user_id, activity_type, filename, created_at) VALUES (?,?,?,?)",
            (session["user_id"], "study_plan", filename, datetime.now().isoformat())
        )
        conn.commit()
        conn.close()

        return jsonify({"success": True, "plan": plan})

    except Exception as e:
        print("STUDY PLAN ERROR:", e)
        return jsonify({"success": False, "message": str(e)})
# ---------------- PDF TEXT EXTRACTION ---------------- #
def extract_text_from_pdf(pdf_path, max_pages=20):
    doc = None
    try:
        doc = fitz.open(pdf_path)
        text = ""
        page_count = len(doc)
        pages_to_read = min(page_count, max_pages)

        for i, page in enumerate(doc):
            if i >= pages_to_read:
                break

            page_text = page.get_text("text")

            if not page_text.strip():
                pix = page.get_pixmap(dpi=200)
                img = Image.open(io.BytesIO(pix.tobytes("png")))
                page_text = pytesseract.image_to_string(img, lang="eng")

            text += page_text + "\n"

        print("PDF TEXT LENGTH:", len(text))
        print("PDF PAGES READ:", pages_to_read, "of", page_count, "total")

        return text.strip()

    except Exception as e:
        print("PDF EXTRACTION ERROR:", e)
        return ""

    finally:
        if doc is not None:
            doc.close()   
        
# ---------------- DATABASE ---------------- #

def create_database():
    conn = sqlite3.connect("users.db")
    cursor = conn.cursor()

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS users(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT,
        email TEXT UNIQUE,
        password TEXT,
        role TEXT
    )
    """)

    conn.commit()
    conn.close()
    
def add_role_column():
    conn = sqlite3.connect("users.db")
    cursor = conn.cursor()

    try:
        cursor.execute("ALTER TABLE users ADD COLUMN role TEXT")
        print("Role column added successfully.")
    except sqlite3.OperationalError:
        print("Role column already exists.")

    conn.commit()
    conn.close()

# ---------------- HOME ---------------- #

@app.route("/")
def home():
    return redirect(url_for("login"))

# ---------------- LOGIN ---------------- #

@app.route("/login", methods=["GET", "POST"])
def login():

    if request.method == "POST":

        email = request.form["email"]
        password = request.form["password"]

        # Remember Me check
        remember_me = request.form.get("remember_me")

        conn = sqlite3.connect("users.db")
        cursor = conn.cursor()

        cursor.execute(
            "SELECT * FROM users WHERE email=? AND password=?",
            (email, password)
        )
        
        user = cursor.fetchone()
        print("LOGIN USER =", user)
        conn.close()

        if user:

            session["user_id"] = user[0]
            session["user_name"] = user[1]
            session["user_email"] = user[2]
            session["user_role"] = user[4]

            # Remember Me
            if remember_me:
                session.permanent = True
                app.permanent_session_lifetime = timedelta(days=30)
            else:
                session.permanent = False

            return redirect(url_for("dashboard"))

        return "Invalid Email or Password"

    return render_template("login.html")
# ---------------- SIGNUP ---------------- #

@app.route("/signup", methods=["GET", "POST"])
def signup():

    if request.method == "POST":

        name = request.form["name"]
        email = request.form["email"]
        password = request.form["password"]
    
        conn = sqlite3.connect("users.db")
        cursor = conn.cursor()

        try:
            cursor.execute(
            "INSERT INTO users(name,email,password) VALUES(?,?,?)",
            (name, email, password)
        )
            conn.commit()
            print("SIGNUP SAVED:", name, email)

            session["user_id"] = cursor.lastrowid
            session["user_name"] = name
            session["user_email"] = email
            return redirect(url_for("dashboard"))

        except sqlite3.IntegrityError:
            return "Email already exists."

        finally:
            conn.close()

    return render_template("signup.html")

# ---------------- DASHBOARD ---------------- #
@app.route("/dashboard")
def dashboard():

    if "user_email" not in session:
        return redirect(url_for("login"))

    files = os.listdir(UPLOAD_FOLDER)

    conn = sqlite3.connect("users.db")
    cursor = conn.cursor()

    cursor.execute(
        "SELECT name,email,role FROM users WHERE id=?",
        (session["user_id"],)
    )
    user = cursor.fetchone()

    # counts for the top stat cards (kept as before)
    cursor.execute(
        "SELECT COUNT(DISTINCT filename) FROM activity_log WHERE user_id=? AND activity_type='summary'",
        (session["user_id"],)
    )
    summaries_count = cursor.fetchone()[0]

    cursor.execute(
        "SELECT COUNT(*) FROM activity_log WHERE user_id=? AND activity_type='quiz'",
        (session["user_id"],)
    )
    quizzes_count = cursor.fetchone()[0]

    cursor.execute(
        "SELECT COUNT(*) FROM activity_log WHERE user_id=? AND activity_type='study_plan'",
        (session["user_id"],)
    )
    study_plans_count = cursor.fetchone()[0]

    # 👇 NEW: per-document progress calculation
    cursor.execute(
        "SELECT DISTINCT filename FROM activity_log WHERE user_id=? AND activity_type='summary'",
        (session["user_id"],)
    )
    summarized_files = {row[0] for row in cursor.fetchall()}

    cursor.execute(
        "SELECT DISTINCT filename FROM activity_log WHERE user_id=? AND activity_type='quiz'",
        (session["user_id"],)
    )
    quizzed_files = {row[0] for row in cursor.fetchall()}
    
    cursor.execute(
        "SELECT activity_type, filename, created_at FROM activity_log WHERE user_id=? ORDER BY created_at DESC LIMIT 2",
        (session["user_id"],)
    )
    raw_activities = cursor.fetchall()

    conn.close()
    
    recent_activities = []
    for activity_type, filename, created_at in raw_activities:
        meta = ACTIVITY_META.get(
            activity_type,
            {"icon": "bi-check-circle", "color": "text-secondary", "label": "{filename}"}
        )
        recent_activities.append({
            "icon": meta["icon"],
            "color": meta["color"],
            "text": meta["label"].format(filename=filename),
            "time": format_relative_time(created_at)
        })

    session["user_name"] = user[0]
    session["user_email"] = user[1]
    session["user_role"] = user[2]

    documents_count = len(files)

    if documents_count == 0:
        progress_percent = 0
    else:
        total_pct = 0
        for f in files:
            checkpoints = 0
            if f in summarized_files:
                checkpoints += 1
            if f in quizzed_files:
                checkpoints += 1
            total_pct += (checkpoints / 2) * 100
        progress_percent = int(total_pct / documents_count)

    return render_template(
        "dashboard.html",
        user_name=user[0],
        user_email=user[1],
        user_role=user[2],
        recent_activities=recent_activities,
        documents_count=documents_count,
        summaries_count=summaries_count,
        quizzes_count=quizzes_count,
        study_plans_count=study_plans_count,
        progress_percent=progress_percent,
        files=files,
        today=datetime.now().strftime("%d-%m-%Y")
    )
@app.route("/profile")
def profile():

    if "user_email" not in session:
        return redirect(url_for("login"))

    return render_template(
        "profile.html",
        user_name=session["user_name"],
        user_email=session["user_email"],
        user_role=session["user_role"]
    )
# ---------------- EDIT PROFILE ---------------- #

@app.route("/edit_profile", methods=["GET", "POST"])
def edit_profile():

    if "user_email" not in session:
        return redirect(url_for("login"))

    if request.method == "POST":

        name = request.form["name"]
        email = request.form["email"]
        role = request.form["role"]

        conn = sqlite3.connect("users.db")
        cursor = conn.cursor()

        cursor.execute("""
        UPDATE users
        SET name=?, email=?, role=?
        WHERE id=?
        """,
        (
        name,
        email,
        role,
        session["user_id"]
        ))
        print("Rows Updated:", cursor.rowcount)
        print("Session Email:", session["user_email"])
        print("New Name:", name)
        conn.commit()
        conn.close()

        session["user_name"] = name
        session["user_email"] = email
        session["user_role"] = role
        
        flash("Profile updated successfully!", "success")

        return redirect(url_for("dashboard"))

    return render_template(
        "edit_profile.html",
        user_name=session["user_name"],
        user_email=session["user_email"],
        user_role=session["user_role"]
    )
# ---------------- CHANGE PASSWORD ---------------- #

@app.route("/change_password", methods=["GET", "POST"])
def change_password():

    if "user_email" not in session:
        return redirect(url_for("login"))

    if request.method == "POST":

        current_password = request.form["current_password"]
        new_password = request.form["new_password"]
        confirm_password = request.form["confirm_password"]

        conn = sqlite3.connect("users.db")
        cursor = conn.cursor()

        cursor.execute(
            "SELECT password FROM users WHERE id=?",
            (session["user_id"],)
        )

        user = cursor.fetchone()
        if user[0] != current_password:
            flash("Current password is incorrect!", "danger")
            conn.close()
            return redirect(url_for("change_password"))

        if new_password != confirm_password:
            flash("New password and Confirm password do not match!", "danger")
            conn.close()
            return redirect(url_for("change_password"))
        
        # Check new password is different
        if current_password == new_password:
            flash("New password cannot be the same as current password!", "danger")
            conn.close()
            return redirect(url_for("change_password"))
        # Check confirm password
        if new_password != confirm_password:
            flash("New password and Confirm password do not match!", "danger")
            conn.close()
            return redirect(url_for("change_password"))

        cursor.execute(
            "UPDATE users SET password=? WHERE id=?",
            (new_password, session["user_id"])
        )

        conn.commit()
        conn.close()

        flash("Password changed successfully!", "success")

        return redirect(url_for("dashboard"))

    return render_template("change_password.html")

@app.route("/interview")
def interview_coach():
    if "user_email" not in session:
        return redirect(url_for("login"))
    files = os.listdir(UPLOAD_FOLDER)
    return render_template("interview.html", files=files)


@app.route("/generate-interview-questions")
def generate_interview_questions_api():
    if "user_email" not in session:
        return jsonify({"success": False, "message": "Please log in again."})

    interview_type = request.args.get("type", "Technical Interview")
    branch = request.args.get("branch", "Computer Engineering")
    experience = request.args.get("experience", "Fresher")
    source = request.args.get("source", "Placement Interview")
    filename = request.args.get("file")

    document_text = None
    if source == "From Uploaded Notes" and filename:
        try:
            document_text = get_document_text(filename)
        except Exception:
            return jsonify({"success": False, "message": "Could not read the selected document."})

    try:
        result = generate_interview_questions(interview_type, branch, experience, document_text)
        questions = result.get("questions", [])

        if not questions:
            return jsonify({"success": False, "message": "Could not generate questions. Please try again."})

        conn = sqlite3.connect("users.db")
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO activity_log (user_id, activity_type, filename, created_at) VALUES (?,?,?,?)",
            (session["user_id"], "interview", filename or interview_type, datetime.now().isoformat())
        )
        conn.commit()
        conn.close()

        return jsonify({"success": True, "questions": questions})

    except Exception as e:
        print("INTERVIEW QUESTIONS ERROR:", e)
        return jsonify({"success": False, "message": "Something went wrong generating questions."})


@app.route("/evaluate-interview-answer", methods=["POST"])
def evaluate_interview_answer_api():
    if "user_email" not in session:
        return jsonify({"success": False, "message": "Please log in again."})

    data = request.get_json()
    question = data.get("question", "")
    answer = data.get("answer", "")
    interview_type = data.get("type", "Technical Interview")

    if not answer.strip():
        return jsonify({"success": False, "message": "Please write an answer first."})

    try:
        result = evaluate_interview_answer(question, answer, interview_type)
        return jsonify({"success": True, "feedback": result})
    except Exception as e:
        print("INTERVIEW FEEDBACK ERROR:", e)
        return jsonify({"success": False, "message": "Could not evaluate your answer. Please try again."})
# ---------------- LOGOUT ---------------- #

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))

# ---------------- UPLOAD PDF ---------------- #

@app.route("/upload", methods=["POST"])
def upload():

    file = request.files.get("pdf_file")

    if file and file.filename != "":
        file.save(os.path.join(UPLOAD_FOLDER, file.filename))

        conn = sqlite3.connect("users.db")
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO activity_log (user_id, activity_type, filename, created_at) VALUES (?,?,?,?)",
            (session.get("user_id"), "upload", file.filename, datetime.now().isoformat())
        )
        conn.commit()
        conn.close()

    return redirect(url_for("dashboard"))

# ---------------- DOCUMENTS ---------------- #
@app.route("/documents")
def documents():

    if "user_email" not in session:
        return redirect(url_for("login"))

    documents_data = []

    for filename in os.listdir(UPLOAD_FOLDER):

        filepath = os.path.join(UPLOAD_FOLDER, filename)

        size_mb = round(os.path.getsize(filepath) / (1024 * 1024), 2)

        upload_date = datetime.fromtimestamp(
            os.path.getmtime(filepath)
        ).strftime("%d %b %Y")

        documents_data.append({
            "name": filename,
            "date": upload_date,
            "size": f"{size_mb} MB",
            "pages": get_page_count(filepath)
        })

    return render_template(
        "upload.html",
        files=documents_data
    )
# ---------------- DOWNLOAD ---------------- #

@app.route("/download/<path:filename>")
def download(filename):

    print("Downloading:", filename)

    return send_from_directory(
        UPLOAD_FOLDER,
        filename,
        as_attachment=True
    )

# ---------------- SUMMARY ---------------- #
@app.route("/summary/<filename>")
def summary(filename):

    if "user_email" not in session:
        return redirect(url_for("login"))

    pdf_path = os.path.join(UPLOAD_FOLDER, filename)
    extracted_text = extract_text_from_pdf(pdf_path)
    summary_data = generate_summary(extracted_text)
    topics = summary_data.get("topics", [])

    total_words = sum(len(str(pt).split()) for t in topics for pt in t.get("points", []))
    reading_time = max(1, round(total_words / 200))

    # Save topics in session for PDF download
    session["last_summary_topics"] = topics
    session["last_summary_filename"] = filename

    # 👇 NEW: log this summary generation for progress tracking
    conn = sqlite3.connect("users.db")
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO activity_log (user_id, activity_type, filename, created_at) VALUES (?,?,?,?)",
        (session["user_id"], "summary", filename, datetime.now().isoformat())
    )
    conn.commit()
    conn.close()

    return render_template(
        "summary.html",
        topics=topics,
        reading_time=reading_time,
        filename=filename
    )
@app.route("/download-summary/<filename>")
def download_summary(filename):

    if "user_email" not in session:
        return redirect(url_for("login"))

    topics = session.get("last_summary_topics", [])

    summary_html = ""
    for t in topics:
        summary_html += f"<h2>{t['topic']}</h2><ul>"
        for point in t.get("points", []):
            summary_html += f"<li>{point}</li>"
        summary_html += "</ul>"

    html = f"""<html><head><meta charset="UTF-8"></head>
    <body>
    <h1>StudyGenie AI Summary</h1><hr>
    {summary_html}
    </body></html>"""

    pdf = BytesIO()
    pisa.CreatePDF(src=html, dest=pdf)
    response = make_response(pdf.getvalue())
    response.headers["Content-Type"] = "application/pdf"
    response.headers["Content-Disposition"] = f"attachment; filename={filename}_Summary.pdf"
    return response

# ---------------- YOUTUBE VIDEOS ---------------- #

@app.route("/videos/<topic>")
def get_videos(topic):
    videos = get_youtube_videos(topic)
    return jsonify(videos)

# ---------------- DELETE ---------------- #

@app.route("/delete/<filename>")
def delete(filename):

    path = os.path.join(UPLOAD_FOLDER, filename)

    if os.path.exists(path):
        os.remove(path)

    return redirect(url_for("documents"))

create_database()
init_activity_table()
if __name__ == "__main__": 
    app.run(debug=True)