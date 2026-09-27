import os
import re
import json
from fastapi import FastAPI, File, UploadFile, HTTPException
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware
import fitz  # PyMuPDF

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/")
async def serve_home():
    if os.path.exists("index.html"):
        return FileResponse("index.html")
    return {"error": "index.html not found"}

def format_detected_tables(text: str) -> str:
    lines = [l.strip() for l in text.split('\n') if l.strip()]
    formatted = []
    i = 0

    while i < len(lines):
        # Column headers detect karein (Year 1, Year 2, Company A, Company B etc.)
        col_series = []
        temp_i = i
        while temp_i < len(lines) and re.match(r'^(?:Year\s*\d+|Company\s*[A-Z]|Prior\s*Year|Current\s*Year|Previous\s*year|Industry(?:\s*Average)?)$', lines[temp_i], re.I):
            col_series.append(lines[temp_i])
            temp_i += 1

        num_cols = len(col_series)
        if num_cols >= 2:
            table_html = "<div class='table-responsive'><table class='mcq-table'><thead><tr><th>Particulars</th>"
            for col in col_series:
                table_html += f"<th>{col}</th>"
            table_html += "</tr></thead><tbody>"

            curr_idx = temp_i
            while curr_idx < len(lines):
                # Check karein agar data values khatam ho gayi hain aur sentence/paragraph shuru ho gaya hai
                line_to_check = lines[curr_idx]
                if len(line_to_check.split()) > 5 and not re.search(r'[\$£¥%?\d]', line_to_check):
                    break
                if re.match(r'^(?:[•\-\*]?\s*[A-D][\.\)]|Gordon has|Bisbee estimates|Using|On the basis|Based on|\d{1,4}[\.\)])', line_to_check, re.I):
                    break

                row_label = lines[curr_idx]
                curr_idx += 1
                row_vals = []
                
                # Rows ke cells sirf valid table values honge
                is_valid_row = True
                for _ in range(num_cols):
                    if curr_idx < len(lines):
                        val_str = lines[curr_idx]
                        if re.match(r'^(?:[•\-\*]?\s*[A-D][\.\)]|Gordon has|Bisbee estimates|Using|\d{1,4}[\.\)])', val_str, re.I) or (len(val_str.split()) > 4 and not re.search(r'[\$£¥%?\d]', val_str)):
                            is_valid_row = False
                            break
                        row_vals.append(val_str)
                        curr_idx += 1
                    else:
                        row_vals.append("-")

                if not is_valid_row:
                    curr_idx -= 1
                    break

                table_html += f"<tr><td><strong>{row_label}</strong></td>"
                for val in row_vals:
                    table_html += f"<td>{val}</td>"
                table_html += "</tr>"

            table_html += "</tbody></table></div>"
            formatted.append(table_html)
            i = curr_idx
            continue

        formatted.append(lines[i])
        i += 1

    return "\n\n".join(formatted)

def extract_full_clean_text(stream_bytes: bytes) -> str:
    doc = fitz.open(stream=stream_bytes, filetype="pdf")
    pages = []
    for page in doc:
        txt = page.get_text("text")
        if txt.strip():
            cleaned = re.sub(r'https?://\S+', '', txt)
            cleaned = re.sub(r'قنوات\s+التلجيرام.*', '', cleaned)
            cleaned = re.sub(r'Hock\s+P2\s+2020\s+Section\s+A.*', '', cleaned, flags=re.I)
            cleaned = re.sub(r'Section\s+A\s*-\s*Financial\s+Statement\s+Analysis.*', '', cleaned, flags=re.I)
            cleaned = re.sub(r'Answers\b', '', cleaned, flags=re.I)
            cleaned = re.sub(r'Copyright\s+\d{4}.*', '', cleaned, flags=re.I)
            pages.append(cleaned)
    return "\n".join(pages)

def parse_mcq_document(full_text: str):
    clean = re.sub(r'\r\n', '\n', full_text)
    
    ans_split = re.split(r'\n(?=Answer(?:s)?\s+for\s+Section|Answers\s+Section)', clean, flags=re.I)
    is_separate_answer_key = len(ans_split) > 1

    questions = []

    if is_separate_answer_key:
        q_section = ans_split[0]
        ans_section = ans_split[1]
        
        q_blocks = re.split(r'\n(?=(?:Q(?:uestion)?\.?\s*|MCQ\s*)?\d{1,4}[\.\:\-\)])', q_section, flags=re.I)
        q_map = {}
        for b in q_blocks:
            m_id = re.search(r'^(?:Q(?:uestion)?\.?\s*|MCQ\s*)?(\d{1,4})[\.\:\-\)]', b.strip(), re.I)
            if not m_id:
                continue
            num = int(m_id.group(1))
            
            opt_pat = r'(?:^|\n)\s*[\(\[]?([A-Da-d])[\)\]\.\:\-]\s+([\s\S]*?)(?=(?:\n\s*[\(\[]?[A-Da-d][\)\]\.\:\-])|\n\s*(?:CSO:|LOS:|\d{1,4}[\.\:\-\)])|\Z)'
            opts_raw = re.findall(opt_pat, b)
            if len(opts_raw) >= 2:
                first_opt_idx = re.search(r'(?:^|\n)\s*[\(\[]?[A-Da-d][\)\]\.\:\-]', b)
                q_text = b[:first_opt_idx.start()] if first_opt_idx else b
                q_text = re.sub(r'^(?:Q(?:uestion)?\.?\s*|MCQ\s*)?\d{1,4}[\.\:\-\)]\s*', '', q_text.strip(), flags=re.I)
                q_text = re.sub(r'CSO:.*|LOS:.*', '', q_text, flags=re.I).strip()
                
                table_ready_q = format_detected_tables(q_text)

                clean_opts = {}
                for k, v in opts_raw:
                    clean_opts[k.upper()] = re.sub(r'\s+', ' ', v).strip()
                
                q_map[num] = {
                    "id": num,
                    "questionText": table_ready_q,
                    "options": clean_opts,
                    "correct": "A",
                    "explanation": "",
                    "optionExplanations": {}
                }

        ans_blocks = re.split(r'\n(?=\d{1,4}[\.\:\-\)])', ans_section)
        for ab in ans_blocks:
            m_ans = re.search(r'^(\d{1,4})[\.\:\-\)]\s*(?:Correct\s*answer\s*)?[\(\[]?([A-Da-d])[\)\]\.\:\-]?(.*)', ab.strip(), re.I | re.DOTALL)
            if m_ans:
                num = int(m_ans.group(1))
                corr = m_ans.group(2).upper()
                exp = m_ans.group(3).strip()
                if num in q_map:
                    q_map[num]["correct"] = corr
                    q_map[num]["explanation"] = exp if exp else f"Option ({corr}) is the correct answer."
                    q_map[num]["optionExplanations"] = {
                        corr: exp if exp else "Correct option according to official answer key."
                    }

        questions = [q_map[k] for k in sorted(q_map.keys())]

    else:
        blocks = re.split(r'\n(?=\d{1,4}[\.\:\-\)]\s+(?:Question\s+ID:|\bCSO\b|\bLOS\b|[A-Z]))', clean, flags=re.I)
        
        for blk in blocks:
            m_id = re.search(r'^(\d{1,4})[\.\:\-\)]', blk.strip())
            if not m_id:
                continue
            q_num = int(m_id.group(1))
            
            first_opt_match = re.search(r'(?:^|\n)\s*[•\-\*]?\s*[\(\[]?([A-Da-d])[\)\]\.\:\-]\s+', blk)
            if not first_opt_match:
                continue

            raw_q_part = blk[:first_opt_match.start()].strip()
            clean_q = re.sub(r'^\d{1,4}[\.\:\-\)]\s*', '', raw_q_part)
            clean_q = re.sub(r'Question\s+ID:.*?\)', '', clean_q, flags=re.I)
            clean_q = re.sub(r'CSO:.*|LOS:.*', '', clean_q, flags=re.I).strip()

            table_ready_q = format_detected_tables(clean_q)

            rest_part = blk[first_opt_match.start():]

            correct_key = 'A'
            corr_flag = re.search(r'[•\-\*]?\s*[\(\[]?([A-Da-d])[\)\]\.\:\-][^\n]*?correct\b', rest_part, re.I)
            if corr_flag:
                correct_key = corr_flag.group(1).upper()
            else:
                corr_exp_tag = re.search(r'Correct\s*Answer\s*[:\-\=]?\s*([A-Da-d])', rest_part, re.I)
                if corr_exp_tag:
                    correct_key = corr_exp_tag.group(1).upper()

            exp_split = re.split(r'\n\s*(?:Correct\s*Answer\s*Explanation:|Question\s+was\s+not\s+answered)', rest_part, flags=re.I)
            opts_text = exp_split[0]
            explanations_text = exp_split[1] if len(exp_split) > 1 else ""

            opt_matches = re.findall(r'(?:^|\n)\s*[•\-\*]?\s*[\(\[]?([A-Da-d])[\)\]\.\:\-]\s+([\s\S]*?)(?=(?:\n\s*[•\-\*]?\s*[\(\[]?[A-Da-d][\)\]\.\:\-])|\Z)', opts_text)
            clean_options = {}
            for k, val in opt_matches:
                v_clean = re.sub(r'\bcorrect\b|\bQuestion was not answered\b', '', val, flags=re.I)
                clean_options[k.upper()] = re.sub(r'\s+', ' ', v_clean).strip()

            if len(clean_options) < 2:
                continue

            general_exp = ""
            gen_match = re.search(r'^(.*?)(?=\n\s*Explanation\s+for\s+Choice|\Z)', explanations_text, re.I | re.DOTALL)
            if gen_match:
                general_exp = gen_match.group(1).strip()
            if not general_exp:
                general_exp = f"Option ({correct_key}) is the correct answer."

            # Har choice ka explanation extract karein (Explanation for Choice A, B, C, D)
            choice_explanations = {}
            choice_exp_matches = re.findall(r'Explanation\s+for\s+Choice\s+([A-Da-d])\s*[:\-\=]([\s\S]*?)(?=\n\s*Explanation\s+for\s+Choice|\n\s*\d{1,4}[\.\:\-\)]|\Z)', explanations_text, re.I)
            for ck, cval in choice_exp_matches:
                choice_explanations[ck.upper()] = re.sub(r'\s+', ' ', cval).strip()

            choice_explanations[correct_key] = general_exp

            questions.append({
                "id": q_num,
                "questionText": table_ready_q,
                "options": {
                    "A": clean_options.get("A", "Option A"),
                    "B": clean_options.get("B", "Option B"),
                    "C": clean_options.get("C", "Option C"),
                    "D": clean_options.get("D", "Option D")
                },
                "correct": correct_key,
                "explanation": general_exp,
                "optionExplanations": choice_explanations
            })

    return questions

@app.post("/analyze-mcq-pdf")
async def analyze_mcq_pdf(file: UploadFile = File(...)):
    if not file.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Only PDF files are supported.")

    file_bytes = await file.read()
    raw_text = extract_full_clean_text(file_bytes)

    if not raw_text.strip():
        raise HTTPException(status_code=400, detail="No readable text found in PDF.")

    parsed_questions = parse_mcq_document(raw_text)

    if not parsed_questions:
        raise HTTPException(status_code=400, detail="Could not detect MCQs. Ensure PDF contains standard questions and options.")

    return parsed_questions

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)