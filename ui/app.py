
import streamlit as st
import requests
import time
import pandas as pd
import json
import os
from dotenv import load_dotenv

# Load local .env if present
load_dotenv()

# --- Configuration ---
# Default to localhost for local testing
API_BASE = os.getenv("API_URL", "http://localhost:8000")
API_V1 = f"{API_BASE}/ai-assessments/v1"

st.set_page_config(page_title="Assessment Generator (Interactive)", layout="wide", page_icon="🧩")

# --- Sidebar: Auth & Config ---
st.sidebar.title("⚙️ Configuration")
auth_token = st.sidebar.text_input("Auth Token (JWT)", type="password", help="Enter your x-authenticated-user-token from iGot")

if not auth_token:
    st.sidebar.warning("⚠️ Auth Token is required for API")
    
force_new = st.sidebar.checkbox("Bypass Cache (Force New)", value=False)

# Headers helper
DISPLAY_LABELS = {
    "Multiple Choice Question": "Single selection MCQs",
    "Multi-Choice Question": "Multiple selection MCQs",
}

def get_headers():
    return {
        "x-authenticated-user-token": auth_token,
        "bg-bypass-cache": "true" if force_new else "false"
    }

st.title("🧩 Assessment Generator")
st.markdown("Test the full **Generate -> Clone -> Edit -> Event** lifecycle.")

# --- Tab Layout ---
tab_gen, tab_comp, tab_view, tab_history = st.tabs(["🚀 Generate / Clone", "📚 Comprehensive", "📝 View & Edit Result", "🗂️ History"])

# ==========================================
# TAB 1: GENERATE (Standard)
# ==========================================
with tab_gen:
    col_input, col_mode = st.columns([3, 1])
    with col_mode:
        use_custom = st.checkbox("Upload Only Mode", help="Generate from uploaded files without a Course ID")

    with col_input:
        if use_custom:
            st.info("Upload-only mode active. Please upload files below.")
            course_id = ""
            course_ids_input = ""
            course_names_input = ""
        else:
            course_ids_input = st.text_input("Course IDs (comma-separated)", placeholder="do_114297785654214656137, do_123...", help="Optional for competency type — leave blank to generate purely from KCM descriptions")
            course_names_input = st.text_input("Course Names (comma-separated)", placeholder="Foundations of Public Policy, Ethics in Governance", help="Optional — used to show course names in history immediately")

    # Config Form
    with st.expander("Detailed Configuration", expanded=True):
        col1, col2, col3 = st.columns(3)
        with col1:
            assessment_type = st.selectbox("Assessment Type", ["practice", "final", "standalone", "competency"])
        with col2:
            difficulty = st.selectbox("Difficulty", ["beginner", "intermediate", "advanced"], index=1)
        with col3:
            language = st.selectbox(
                "Language", 
                ["english", "hindi", "bengali", "gujarati", "kannada", "malayalam", "marathi", "tamil", "telugu", "odia", "punjabi", "assamese"]
            )
            
        st.markdown("#### Question Counts")
        c1, c2, c3, c4, c5 = st.columns(5)
        mcq = c1.number_input("MCQ", 0, 20, 5)
        ftb = c2.number_input("FTB", 0, 20, 5)
        mtf = c3.number_input("MTF", 0, 20, 5)
        multi = c4.number_input("Multi-Choice", 0, 20, 0)
        tf  = c5.number_input("True/False", 0, 20, 0)
        
        st.markdown("#### Advanced Settings")
        adv1, adv2 = st.columns(2)
        with adv1:
            time_limit = st.number_input("Time Limit (Minutes)", min_value=0, value=0, help="0 means no limit. Influences cognitive depth of questions.")
            enable_blooms = st.checkbox("Enable Bloom's Taxonomy", value=True, help="If disabled, relies purely on Difficulty level")
            
            if enable_blooms:
                st.markdown("**Bloom's Distribution (%)**")
                b1, b2, b3 = st.columns(3)
                b_rem = b1.number_input("Remember", 0, 100, 20)
                b_und = b2.number_input("Understand", 0, 100, 30)
                b_app = b3.number_input("Apply", 0, 100, 30)
                
                b4, b5, b6 = st.columns(3)
                b_ana = b4.number_input("Analyze", 0, 100, 10)
                b_eva = b5.number_input("Evaluate", 0, 100, 10)
                b_cre = b6.number_input("Create", 0, 100, 0)
                
                blooms_total = b_rem + b_und + b_app + b_ana + b_eva + b_cre
                if blooms_total != 100:
                    st.warning(f"Total Bloom's percentage is {blooms_total}%. It should equal 100%.")
                
                blooms_config = {
                    "Remember": b_rem,
                    "Understand": b_und,
                    "Apply": b_app,
                    "Analyze": b_ana,
                    "Evaluate": b_eva,
                    "Create": b_cre
                }
            else:
                blooms_config = None
                
        with adv2:
            st.write("")

        if assessment_type == "competency":
            st.markdown("#### Competency Focus (required for competency type)")
            st.caption("Leave Course IDs blank to generate purely from KCM descriptions (no course content needed).")
            comp_area = st.text_input("Competency Area", placeholder="e.g. Behavioural", key="comp_area")
            comp_themes = st.text_input("Competency Themes (comma-separated)", placeholder="e.g. Service Orientation,Decision Making", key="comp_themes")
            comp_sub_themes = st.text_input("Competency Sub-Themes (comma-separated)", placeholder="e.g. Citizen Centricity,Empathy", key="comp_sub_themes")
        else:
            comp_area = comp_themes = comp_sub_themes = None

        uploaded_files = st.file_uploader("Upload Context (PDF/VTT)", accept_multiple_files=True)

    if st.button("Start Generation", type="primary"):
        if not auth_token:
            st.error("Please enter an Auth Token in the sidebar first.")
            st.stop()
            
        # Construct Payload
        course_weightage = None
        q_counts = {"mcq": mcq, "ftb": ftb, "mtf": mtf, "multichoice": multi, "truefalse": tf}
        
        payload = {
            'force': 'true' if force_new else 'false',
            'assessment_type': assessment_type,
            'difficulty': difficulty,
            'total_questions': sum(q_counts.values()),
            'question_type_counts': json.dumps(q_counts),
            'language': language,
            'enable_blooms': 'true' if enable_blooms else 'false'
        }
        if course_ids_input and course_ids_input.strip():
            payload['course_ids'] = course_ids_input
        
        if enable_blooms and blooms_config:
            payload['blooms_config'] = json.dumps(blooms_config)
        
        if course_weightage and course_weightage.strip():
            payload['course_weightage'] = course_weightage.strip()

        if course_names_input and course_names_input.strip():
            payload['course_names'] = [n.strip() for n in course_names_input.split(",") if n.strip()]
        
        if time_limit > 0:
            payload['time_limit'] = time_limit

        if comp_area:
            payload['competency_area'] = comp_area
        if comp_themes:
            payload['competency_themes'] = [t.strip() for t in comp_themes.split(",") if t.strip()]
        if comp_sub_themes:
            payload['competency_sub_themes'] = [s.strip() for s in comp_sub_themes.split(",") if s.strip()]

        files = []
        if uploaded_files:
            for f in uploaded_files:
                mime = "application/pdf" if f.name.endswith(".pdf") else "text/vtt"
                files.append(('files', (f.name, f.getvalue(), mime)))

        with st.spinner("Calling API..."):
            try:
                # Generate Call
                r = requests.post(f"{API_V1}/generate", data=payload, files=files, headers=get_headers())
                
                if r.status_code in [200, 202]:
                    data = r.json()
                    st.session_state['current_job_id'] = data.get("job_id")
                    st.session_state['job_status'] = data.get("status")
                    
                    if r.status_code == 200:
                        st.success(f"⚡ Instant Result! (Cache Hit/Cloned). Job ID: {data.get('job_id')}")
                        st.balloons()
                    else: # 202
                        st.info(f"⏳ Job Started (Async). Job ID: {data.get('job_id')}")
                        st.info("Go to 'View & Edit Result' tab to poll status.")
                else:
                    st.error(f"API Error ({r.status_code}): {r.text}")
                    
            except Exception as e:
                st.error(f"Connection Failed: {e}")

# ==========================================
# TAB 2: COMPREHENSIVE GENERATION
# ==========================================
with tab_comp:
    st.markdown("### 📚 Comprehensive Assessment Builder")
    st.info("Combine multiple courses with specific percentage weightages to generate a comprehensive cross-course assessment.")

    # Dynamic Course Inputs
    if "comp_courses" not in st.session_state:
        st.session_state.comp_courses = [{"id": "", "name": "", "weight": 50}, {"id": "", "name": "", "weight": 50}]

    # Adding and removing courses runs through `on_click` callbacks rather than
    # mutate-then-`st.rerun()`. A rerun raised from up here aborts the script
    # before the question-count inputs further down are rendered, and Streamlit
    # discards the state of any widget a run did not render — which silently
    # reset the user's question counts to their defaults. A callback runs before
    # the rerun, so the script then executes in full and every widget survives.
    def _add_course():
        st.session_state.comp_courses.append({"id": "", "name": "", "weight": 0})

    def _remove_course(index):
        # The typed values live in widget state (`cid_i` / `cname_i` / `cw_i`),
        # not in `comp_courses`, so removing a row means compacting that state by
        # hand — otherwise the rows below shift up and show the wrong course.
        rows = len(st.session_state.comp_courses)
        ids = [st.session_state.get(f"cid_{i}", "") for i in range(rows)]
        names = [st.session_state.get(f"cname_{i}", "") for i in range(rows)]
        weights = [st.session_state.get(f"cw_{i}", 0) for i in range(rows)]
        ids.pop(index)
        names.pop(index)
        weights.pop(index)
        st.session_state.comp_courses.pop(index)
        for i, (cid, cname, weight) in enumerate(zip(ids, names, weights)):
            st.session_state[f"cid_{i}"] = cid
            st.session_state[f"cname_{i}"] = cname
            st.session_state[f"cw_{i}"] = weight
        st.session_state.pop(f"cid_{len(ids)}", None)
        st.session_state.pop(f"cname_{len(ids)}", None)
        st.session_state.pop(f"cw_{len(ids)}", None)

    st.markdown("#### Input Courses & Weights")

    course_data = []
    total_weight = 0
    for i, course in enumerate(st.session_state.comp_courses):
        col1, col2, col3, col4 = st.columns([4, 4, 2, 1])
        with col1:
            c_id = st.text_input(f"Course ID {i+1}", value=course["id"], key=f"cid_{i}")
        with col2:
            c_name = st.text_input(f"Course Name {i+1}", value=course.get("name", ""), placeholder="e.g. Ethics in Governance", key=f"cname_{i}")
        with col3:
            # Clamped to the widget's own minimum: a freshly added row carries no
            # weight yet, and Streamlit refuses a starting value below min_value.
            c_w = st.number_input(f"Weight (%)", min_value=1, max_value=100,
                                  value=max(1, int(course.get("weight") or 0)), key=f"cw_{i}")
        with col4:
            st.write("")
            st.write("")
            st.button("🗑️", key=f"del_{i}", on_click=_remove_course, args=(i,),
                      disabled=len(st.session_state.comp_courses) <= 1)

        course_data.append({"id": c_id, "name": c_name, "weight": c_w})
        total_weight += c_w

    st.button("➕ Add Another Course", on_click=_add_course)

    if total_weight != 100:
        st.warning(f"⚠️ Total weight is currently {total_weight}%. It should ideally sum to 100%.")
    else:
        st.success("✅ Total weight is exactly 100%!")

    # Standard Configs
    st.markdown("#### Configuration")
    ccol1, ccol2, ccol3 = st.columns(3)
    with ccol1:
        comp_diff = st.selectbox("Difficulty Level", ["beginner", "intermediate", "advanced"], index=1)
    with ccol2:
        comp_lang = st.selectbox("Output Language", ["english", "hindi", "bengali", "gujarati", "kannada", "malayalam", "marathi", "tamil", "telugu", "odia", "punjabi", "assamese"])
    with ccol3:
        comp_enable_blooms = st.checkbox("Enable Bloom's", value=True, key="comp_blooms")
        
    comp_blooms_config = None
    if comp_enable_blooms:
        st.markdown("**Bloom's Distribution (%)**")
        b1, b2, b3 = st.columns(3)
        cb_rem = b1.number_input("Remember", 0, 100, 20, key="cb_rem")
        cb_und = b2.number_input("Understand", 0, 100, 30, key="cb_und")
        cb_app = b3.number_input("Apply", 0, 100, 30, key="cb_app")
        
        b4, b5, b6 = st.columns(3)
        cb_ana = b4.number_input("Analyze", 0, 100, 10, key="cb_ana")
        cb_eva = b5.number_input("Evaluate", 0, 100, 10, key="cb_eva")
        cb_cre = b6.number_input("Create", 0, 100, 0, key="cb_cre")
        
        cb_total = cb_rem + cb_und + cb_app + cb_ana + cb_eva + cb_cre
        if cb_total != 100:
            st.warning(f"Total Bloom's percentage is {cb_total}%. It should equal 100%.")
            
        comp_blooms_config = {
            "Remember": cb_rem,
            "Understand": cb_und,
            "Apply": cb_app,
            "Analyze": cb_ana,
            "Evaluate": cb_eva,
            "Create": cb_cre
        }
        
    st.markdown("#### Question Counts")
    c1, c2, c3, c4, c5 = st.columns(5)
    cmcq = c1.number_input("MCQ", 0, 50, 10, key="cmcq")
    cftb = c2.number_input("FTB", 0, 50, 0, key="cftb")
    cmtf = c3.number_input("MTF", 0, 50, 0, key="cmtf")
    cmulti = c4.number_input("Multi-Choice", 0, 50, 0, key="cmulti")
    ctf  = c5.number_input("True/False", 0, 50, 0, key="ctf")
    
    total_q = cmcq + cftb + cmtf + cmulti + ctf

    if st.button("Generate Comprehensive", type="primary"):
        if not auth_token:
            st.error("Please enter an Auth Token in the sidebar first.")
            st.stop()

        valid_courses = [c for c in course_data if c["id"].strip()]
        if len(valid_courses) < 2:
            st.error("A comprehensive assessment requires at least 2 valid courses.")
            st.stop()

        c_ids = [c["id"].strip() for c in valid_courses]
        c_names = [c.get("name", "").strip() for c in valid_courses]
        c_weights = {c["id"].strip(): c["weight"] for c in valid_courses}

        comp_q_counts = {"mcq": cmcq, "ftb": cftb, "mtf": cmtf, "multichoice": cmulti, "truefalse": ctf}
        comp_payload = {
            'course_ids': ",".join(c_ids),
            'force': 'true' if force_new else 'false',
            'assessment_type': 'comprehensive',
            'difficulty': comp_diff,
            'total_questions': total_q,
            'question_type_counts': json.dumps(comp_q_counts),
            'language': comp_lang,
            'enable_blooms': 'true' if comp_enable_blooms else 'false',
            'course_weightage': json.dumps(c_weights)
        }
        if any(c_names):
            comp_payload['course_names'] = [n for n in c_names if n]
        
        if comp_enable_blooms and comp_blooms_config:
            comp_payload['blooms_config'] = json.dumps(comp_blooms_config)
        
        with st.spinner("Calling API (Comprehensive)..."):
            try:
                r = requests.post(f"{API_V1}/generate", data=comp_payload, headers=get_headers())
                if r.status_code in [200, 202]:
                    data = r.json()
                    st.session_state['current_job_id'] = data.get("job_id")
                    st.session_state['job_status'] = data.get("status")
                    
                    if r.status_code == 200:
                        st.success(f"⚡ Instant Result! (Cache Hit/Cloned). Job ID: {data.get('job_id')}")
                        st.balloons()
                    else:
                        st.info(f"⏳ Job Started (Async). Job ID: {data.get('job_id')}")
                        st.info("Go to 'View & Edit Result' tab to poll status.")
                else:
                    st.error(f"API Error ({r.status_code}): {r.text}")
            except Exception as e:
                st.error(f"Connection Failed: {e}")

# ==========================================
# TAB 3: VIEW & EDIT
# ==========================================
with tab_view:
    job_id = st.text_input("Job ID", value=st.session_state.get('current_job_id', ''))
    
    col_act1, col_act2 = st.columns([1, 4])
    with col_act1:
        if st.button("Check Status / Fetch"):
            if not job_id: st.warning("Enter Job ID"); st.stop()
            if not auth_token: st.error("Auth Token Required"); st.stop()
            
            try:
                # GET /ai-assessments/v1/status/{job_id} — returns status and result when COMPLETED
                r = requests.get(f"{API_V1}/status/{job_id}", headers=get_headers())
                if r.status_code == 200:
                    st.session_state['fetch_data'] = r.json()
                    st.success("Fetched!")
                else:
                    st.error(f"Error ({r.status_code}): {r.text}")
            except Exception as e:
                st.error(f"Conn Error: {e}")

    # Display Data
    data = st.session_state.get('fetch_data', {})
    if data:
        status = data.get("status")
        st.metric("Status", status)
        
        if status == "COMPLETED":
            # Downloads — token sent in header, not URL query param
            st.markdown("### 📥 Downloads")
            d1, d2, d3, d4, d5 = st.columns(5)
            for fmt, col, label, mime in [
                ("csv",       d1, "Download CSV",       "text/csv"),
                ("csv_basic", d2, "Download CSV Basic", "text/csv"),
                ("json",      d3, "Download JSON",      "application/json"),
                ("pdf",       d4, "Download PDF",       "application/pdf"),
                ("docx",      d5, "Download DOCX",      "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
            ]:
                try:
                    dl_resp = requests.get(
                        f"{API_V1}/download/{job_id}",
                        params={"format": fmt},
                        headers=get_headers(),
                    )
                    if dl_resp.status_code == 200:
                        ext = "csv" if fmt == "csv_basic" else fmt
                        col.download_button(label, dl_resp.content, file_name=f"assessment_{job_id}_{fmt}.{ext}", mime=mime)
                    else:
                        col.error(f"{fmt.upper()} failed ({dl_resp.status_code})")
                except Exception as e:
                    col.error(f"{fmt.upper()} error: {e}")

            # ==================================================
            # EDITOR — exercises the granular editing endpoints
            # ==================================================
            st.markdown("### ✏️ Interactive Editor")
            st.caption(
                "Each action below calls one editing endpoint. Every save is validated, "
                "versioned and audited by the backend."
            )

            BLOOMS = ["Remember", "Understand", "Apply", "Analyze", "Evaluate", "Create"]
            # The difficulty tags the generator prompt asks for. `difficulty_level`
            # is free text as far as the API is concerned, so a stored value from
            # outside this list is offered back rather than overwritten.
            DIFFICULTIES = ["Easy", "Medium", "Hard"]
            UNSET = "— not set —"
            # The editing endpoints are shaped for a prefix-matching Kong API
            # entity: a static verb prefix, then job_id as the only trailing
            # segment. Identifiers travel in the body, wrapped in the Sunbird
            # `{"request": {...}}` envelope.
            Q = f"{API_V1}/questions"

            def pick(label, options, current, key, container=st, help=None):
                """
                Selectbox for an optional classification field.

                The stored value is always among the choices — matched
                case-insensitively, or inserted verbatim when the backend holds
                something off-list — so merely opening an editor can never
                silently retag a question. Returns "" when nothing is selected.
                """
                cur = str(current or "").strip()
                choices = list(options)
                if cur and not any(c.lower() == cur.lower() for c in choices):
                    choices.insert(0, cur)
                choices = [UNSET] + choices
                idx = next((i for i, c in enumerate(choices)
                            if c.lower() == cur.lower()), 0) if cur else 0
                chosen = container.selectbox(label, choices, index=idx, key=key, help=help)
                return "" if chosen == UNSET else chosen

            def get_path(obj, path):
                cur = obj
                for part in path.split("."):
                    if not isinstance(cur, dict):
                        return None
                    cur = cur.get(part)
                return cur

            def prune_unchanged(updates, question):
                """
                Drop the fields the reviewer did not actually change.

                A form renders a field the question never had as "", so an
                untouched question with no `difficulty_level` would otherwise be
                submitted as changing from None to "" — a spurious audit row and
                a spurious ai_generated → ai_assisted provenance flip on every
                save. Absent and empty are treated as the same value here.
                """
                pruned = {}
                for path, value in updates.items():
                    current = get_path(question, path)
                    if current == value:
                        continue
                    if current is None and value == "":
                        continue
                    pruned[path] = value
                return pruned

            def show_api_error(resp, prefix="Failed"):
                """Render a validation (400) / conflict (409) response readably."""
                try:
                    body = resp.json()
                except Exception:
                    st.error(f"{prefix} ({resp.status_code}): {resp.text}")
                    return
                if resp.status_code == 409:
                    st.error(
                        f"⚠️ Concurrent update detected. {body.get('detail')} "
                        f"(current version: {body.get('current_version')})"
                    )
                    return
                st.error(f"{prefix} ({resp.status_code}): {body.get('detail')}")
                for err in body.get("errors", []):
                    st.warning(f"• `{err.get('field')}` — {err.get('message')}")

            def report_event(code, **fields):
                """
                Report an editor lifecycle event (Assessment Edit Opened,
                Question Edit Started, Question Edit Cancelled, Assessment
                Reopened). These happen entirely in the client, so the backend
                cannot see them.
                """
                # Callers pass snake_case kwargs; the envelope is camelCase.
                camel = {"question_id": "questionId", "question_type": "questionType",
                         "question_position": "questionPosition",
                         "entry_point": "entryPoint", "source": "source"}
                try:
                    requests.post(
                        f"{API_V1}/telemetry/{job_id}", headers=get_headers(),
                        json={"request": {
                            "eventCode": code,
                            **{camel.get(k, k): v for k, v in fields.items()
                               if v is not None}}})
                except Exception:
                    pass  # telemetry must never break the editor

            def show_alerts(alerts):
                """Render the pre-update / post-save alerts."""
                icons = {"high": "🔴", "medium": "🟠", "low": "🟡", "info": "ℹ️"}
                for alert in alerts or []:
                    st.write(f"{icons.get(alert.get('severity'), '•')} {alert.get('message')}")

            @st.dialog("Delete this question?")
            def confirm_delete_dialog(qid, version, preview_text, job_key):
                """
                Explicit confirmation, as a modal the user must act on
                rather than a checkbox ticked ahead of time.
                """
                if preview_text:
                    st.write(f"**{preview_text}**")
                st.warning("This will permanently remove the question from the "
                           "assessment. This cannot be undone.")
                try:
                    r_prev = requests.post(
                        f"{Q}/delete/{job_id}", params={"dry_run": "true"},
                        json={"request": {"questionId": qid}},
                        headers=get_headers())
                    if r_prev.status_code == 200:
                        show_alerts(r_prev.json().get("alerts"))
                except Exception:
                    pass  # preview is a nicety; deletion is still blocked without confirm

                c1, c2 = st.columns(2)
                if c1.button("Cancel", key=f"del_cancel_{job_key}_{qid}", use_container_width=True):
                    st.rerun()
                if c2.button("Delete question", key=f"del_confirm_{job_key}_{qid}",
                             type="primary", use_container_width=True):
                    r_del = requests.post(
                        f"{Q}/delete/{job_id}",
                        json={"request": {"questionId": qid, "confirm": True,
                                          "version": version}},
                        headers=get_headers())
                    if r_del.status_code == 200:
                        st.success(r_del.json().get("announcement", "Deleted"))
                        st.rerun()
                    else:
                        show_api_error(r_del, "Delete failed")

            # Load the authoritative ordered question list
            try:
                r_q = requests.get(f"{Q}/list/{job_id}", headers=get_headers())
            except Exception as e:
                st.error(f"Could not load questions: {e}")
                r_q = None

            if r_q is not None and r_q.status_code != 200:
                show_api_error(r_q, "Could not load questions")
            elif r_q is not None:
                q_data = r_q.json()
                version = q_data["version"]
                q_list = q_data["questions"]
                q_order = q_data["question_order"]

                # Reports Assessment Edit Opened, and Assessment Reopened when this
                # assessment has been saved before (i.e. reopened rather than freshly
                # generated).
                if st.session_state.get("editor_opened_for") != job_id:
                    st.session_state["editor_opened_for"] = job_id
                    report_event("TEL-01", entry_point="streamlit_test_ui")
                    if version > 1:
                        report_event("TEL-16", source="Past Assessment")

                m1, m2, m3 = st.columns(3)
                m1.metric("Questions", q_data["total_questions"])
                m2.metric("Assessment version", version)
                if m3.button("🔄 Reload from backend"):
                    st.rerun()

                # ---------- add a question ----------
                # Course choices for this assessment — the same names the LLM was
                # given at generation time (aggregated metadata's `courses`, or the
                # flat `course_names` list when no course content was fetched).
                # Taken as-is, exactly like the LLM's own course_name output — no
                # separate validation, just the pick surfaced as a dropdown/autofill
                # instead of free text.
                _add_meta = data.get("metadata") or {}
                _course_opts = [
                    c.get("name") for c in (_add_meta.get("courses") or [])
                    if isinstance(c, dict) and c.get("name")
                ] or [n for n in (_add_meta.get("course_names") or []) if n]
                course_options = list(dict.fromkeys(_course_opts))  # de-dup, keep order

                with st.expander("➕ Add a question (POST /questions/create/{job_id})"):
                    with st.form("add_question_form"):
                        a1, a2 = st.columns(2)
                        QUESTION_TYPE_CHOICES = [
                            ("Single selection MCQs", "mcq"),
                            ("Fill in the blanks", "ftb"),
                            ("Match the following", "mtf"),
                            ("True/False", "truefalse"),
                            ("Multiple selection MCQs", "multichoice"),
                        ]
                        new_type_label = a1.selectbox(
                            "Question type",
                            [label for label, _ in QUESTION_TYPE_CHOICES],
                            key="add_type",
                        )
                        new_type = dict(QUESTION_TYPE_CHOICES)[new_type_label]
                        new_pos = a2.number_input(
                            "Position (1-based, blank = append)", min_value=0,
                            max_value=q_data["total_questions"] + 1, value=0, key="add_pos",
                        )
                        new_text = st.text_area("Question text / matching context", key="add_text")
                        st.caption(
                            "MCQ and Multi-Choice require 2 to 5 options. "
                            "MTF requires at least 2 pairs (one per line as `left | right`)."
                        )
                        new_opts = st.text_area(
                            "Options (one per line, 2 to 5) — or MTF pairs as `left | right`",
                            key="add_opts",
                        )
                        new_correct = st.text_input(
                            "Correct answer — MCQ: 0-based index · Multi-Choice: comma-separated "
                            "indexes · FTB: the answer text · True/False: True or False",
                            key="add_correct",
                        )
                        st.markdown("**Answer rationale**")
                        new_rationale = st.text_area(
                            "Correct answer explanation (required)", key="add_rat")
                        ar1, ar2 = st.columns(2)
                        new_why = ar1.text_input("Why factor", key="add_why")
                        new_logic = ar2.text_input("Logic justification", key="add_logic")

                        st.markdown("**Classification**")
                        ac1, ac2, ac3 = st.columns(3)
                        new_blooms = ac1.selectbox("Bloom's level", BLOOMS, key="add_blooms")
                        new_difficulty = pick(
                            "Difficulty level", DIFFICULTIES, "", "add_diff", container=ac2,
                            help="Drives the QuestionTagging column of the CSV export. "
                                 "Left unset, that export falls back to Medium.")
                        new_relevance = ac3.slider("Relevance %", 0, 100, 80, key="add_rel")

                        st.markdown("**Mapping**")
                        if len(course_options) == 1:
                            new_course = course_options[0]
                            st.text_input("Course name", value=new_course,
                                         key="add_course", disabled=True,
                                         help="Auto-populated — this assessment has only one course.")
                        elif len(course_options) > 1:
                            new_course = st.selectbox("Course name", course_options, key="add_course")
                        else:
                            new_course = st.text_input("Course name", key="add_course")
                        new_lo = st.text_area("Learning outcome", key="add_lo")
                        st.caption(
                            "Competency area, theme and sub-theme are validated as a set — "
                            "supply all three, or none.")
                        mc1, mc2, mc3 = st.columns(3)
                        new_area = mc1.text_input("Competency area", key="add_ka")
                        new_theme = mc2.text_input("Competency theme", key="add_kt")
                        new_sub = mc3.text_input("Competency sub-theme", key="add_ks")
                        new_domain = st.text_input("Competency domain", key="add_kd")

                        if st.form_submit_button("Add question"):
                            payload_q = {
                                "blooms_level": new_blooms,
                                "relevance_percentage": int(new_relevance),
                                "answer_rationale": {
                                    "correct_answer_explanation": new_rationale,
                                    "why_factor": new_why,
                                    "logic_justification": new_logic,
                                },
                            }
                            if new_difficulty:
                                payload_q["difficulty_level"] = new_difficulty
                            if new_course:
                                payload_q["course_name"] = new_course

                            # The add endpoint takes `reasoning` as one object, so
                            # it is assembled here from whatever was filled in. A
                            # partially filled competency triple is sent as-is
                            # rather than dropped, so the backend reports the
                            # incomplete mapping instead of the UI hiding it.
                            kcm = {k: v for k, v in (
                                ("competency_area", new_area.strip()),
                                ("competency_theme", new_theme.strip()),
                                ("competency_sub_theme", new_sub.strip()),
                            ) if v}
                            alignment = {}
                            if kcm:
                                alignment["kcm"] = kcm
                            if new_domain.strip():
                                alignment["domain"] = new_domain.strip()
                            reasoning = {}
                            if new_lo.strip():
                                reasoning["learning_objective_alignment"] = new_lo.strip()
                            if alignment:
                                reasoning["competency_alignment"] = alignment
                            if reasoning:
                                payload_q["reasoning"] = reasoning

                            lines = [l.strip() for l in new_opts.splitlines() if l.strip()]
                            if new_type == "mtf":
                                payload_q["matching_context"] = new_text
                                payload_q["pairs"] = [
                                    {"left": l.split("|")[0].strip(),
                                     "right": l.split("|", 1)[1].strip() if "|" in l else ""}
                                    for l in lines
                                ]
                            else:
                                payload_q["question_text"] = new_text
                                if new_type in ("mcq", "multichoice"):
                                    payload_q["options"] = [
                                        {"text": t, "index": i} for i, t in enumerate(lines)
                                    ]
                                    if new_type == "mcq":
                                        try:
                                            payload_q["correct_option_index"] = int(new_correct)
                                        except ValueError:
                                            payload_q["correct_option_index"] = None
                                    else:
                                        payload_q["correct_option_index"] = [
                                            int(x) for x in new_correct.split(",") if x.strip().isdigit()
                                        ]
                                else:
                                    payload_q["correct_answer"] = new_correct

                            body = {"questionType": new_type, "question": payload_q,
                                    "version": version}
                            if new_pos:
                                body["position"] = int(new_pos)
                            try:
                                r_add = requests.post(f"{Q}/create/{job_id}",
                                                      json={"request": body},
                                                      headers=get_headers())
                                if r_add.status_code == 201:
                                    res = r_add.json()
                                    st.success(f"Added as {res['question_id']} "
                                               f"(version {res['version']})")
                                    show_alerts(res.get("alerts"))
                                    st.rerun()
                                else:
                                    show_api_error(r_add, "Add failed")
                            except Exception as e:
                                st.error(f"Add error: {e}")

                # ---------- Questions in assessment order ----------
                st.markdown("#### Questions (in assessment order)")
                prov_badge = {"ai_generated": "🤖 AI", "ai_assisted": "🤖✏️ AI-assisted",
                              "human_authored": "👤 Human"}

                for q in q_list:
                    qid = q["question_id"]
                    pos = q["position"]
                    bucket = q["question_bucket"]
                    tkey = q["question_type_key"]
                    label = q.get("question_text") or q.get("matching_context") or "(no text)"
                    header = (f"Q{pos} · {DISPLAY_LABELS.get(bucket, bucket)} · "
                              f"{prov_badge.get(q.get('provenance'), q.get('provenance'))} — "
                              f"{label[:60]}")

                    with st.expander(header):
                        # Question Edit Started — reported once per question
                        # per session, when its editor is first rendered.
                        started_key = f"edit_started_{job_id}_{qid}"
                        if not st.session_state.get(started_key):
                            st.session_state[started_key] = True
                            report_event("TEL-02", question_id=qid, question_type=tkey,
                                         question_position=pos)

                        # ---------- reorder ----------
                        rc1, rc2, rc3, rc4 = st.columns([1, 1, 2, 3])
                        def move_to(target):
                            try:
                                r_mv = requests.post(
                                    f"{Q}/order/{job_id}",
                                    json={"request": {
                                        "questionId": qid, "position": target,
                                        "version": version}},
                                    headers=get_headers(),
                                )
                                if r_mv.status_code == 200:
                                    # Screen readers would consume this via aria-live.
                                    st.success(r_mv.json().get("announcement", "Reordered"))
                                    st.rerun()
                                else:
                                    show_api_error(r_mv, "Reorder failed")
                            except Exception as e:
                                st.error(f"Reorder error: {e}")

                        if rc1.button("⬆️ Up", key=f"up_{job_id}_{qid}", disabled=(pos == 1)):
                            move_to(pos - 1)
                        if rc2.button("⬇️ Down", key=f"dn_{job_id}_{qid}",
                                      disabled=(pos == len(q_list))):
                            move_to(pos + 1)
                        jump = rc3.number_input("Move to position", min_value=1,
                                                max_value=len(q_list), value=pos,
                                                key=f"jump_{job_id}_{qid}")
                        if rc4.button("Move", key=f"jumpbtn_{job_id}_{qid}", disabled=(jump == pos)):
                            move_to(int(jump))

                        # ---------- delete ----------
                        if not q.get("can_delete", True):
                            st.caption("Last remaining question — cannot be deleted (AC-13)")
                        if st.button("🗑️ Delete question", key=f"del_{job_id}_{qid}",
                                     disabled=not q.get("can_delete", True)):
                            confirm_delete_dialog(
                                qid, version,
                                (q.get("question_text") or q.get("matching_context") or "").strip(),
                                job_id,
                            )

                        st.divider()

                        # ---------- edit fields ----------
                        # Every widget below is keyed off `qkey` rather than the bare
                        # `qid`. `qkey` carries a per-question "generation" counter that
                        # Cancel increments (see below): that gives every widget a brand
                        # new key after a cancel, so the browser mounts fresh widgets
                        # seeded from `q` instead of trying to redisplay a stale one —
                        # deleting the old session_state entry alone was not reliably
                        # forcing the on-screen value to redraw.
                        #
                        # `qkey` is also scoped by `job_id`, because `question_id` is only
                        # unique *within* an assessment — the generator emits MCQ_001,
                        # FTB_001, ... for every assessment it produces. Without the job
                        # prefix, opening assessment B after assessment A reuses A's widget
                        # keys, and Streamlit ignores the `value=` argument whenever a key
                        # already exists in session_state. The form then redisplays A's
                        # content under B's heading, and saving writes A's question text,
                        # options and answer key into B.
                        edit_gen = st.session_state.get(f"edit_gen_{job_id}_{qid}", 0)
                        qkey = f"{job_id}_{qid}_g{edit_gen}"

                        with st.form(f"edit_{job_id}_{qid}"):
                            updates = {}

                            if bucket == "MTF Question":
                                updates["matching_context"] = st.text_area(
                                    "Matching context", q.get("matching_context", ""),
                                    key=f"mc_{qkey}")
                                pairs = q.get("pairs", [])
                                st.caption(f"Pairs — at least 2 are required "
                                           f"(currently {len(pairs)}).")
                                new_pairs = []
                                for pi, pair in enumerate(pairs):
                                    pcol1, pcol2, pcol3 = st.columns([5, 5, 1])
                                    left = pcol1.text_input(f"Left {pi+1}", pair.get("left", ""),
                                                            key=f"pl_{qkey}_{pi}")
                                    right = pcol2.text_input(f"Right {pi+1}", pair.get("right", ""),
                                                             key=f"pr_{qkey}_{pi}")
                                    # Removing below 2 pairs would fail validation.
                                    drop = pcol3.checkbox("Remove", key=f"pdel_{qkey}_{pi}",
                                                          disabled=len(pairs) <= 2)
                                    if not drop:
                                        new_pairs.append({"left": left, "right": right})
                                pa1, pa2 = st.columns(2)
                                extra_left = pa1.text_input(
                                    "New pair — left (leave blank to skip)",
                                    key=f"padd_l_{qkey}")
                                extra_right = pa2.text_input("New pair — right",
                                                             key=f"padd_r_{qkey}")
                                if extra_left.strip() or extra_right.strip():
                                    new_pairs.append({"left": extra_left, "right": extra_right})
                                updates["pairs"] = new_pairs
                            else:
                                updates["question_text"] = st.text_area(
                                    "Question text", q.get("question_text", ""),
                                    key=f"qt_{qkey}")

                            # Options / answer key
                            if bucket in ("Multiple Choice Question", "Multi-Choice Question"):
                                options = q.get("options", [])
                                st.caption(
                                    f"Options — at least 2 are required "
                                    f"(currently {len(options)}). "
                                    f"Add is {'enabled' if q.get('can_add_option') else 'disabled'}; "
                                    f"remove is {'enabled' if q.get('can_remove_option') else 'disabled'}."
                                )
                                st.caption(
                                    "**New index** re-sequences the options within this "
                                    "question. It is zero-based — the same scale as the "
                                    "option indexes themselves, so there is only ever one "
                                    "set of numbers on this screen. Like every other field "
                                    "here it is applied on save, so the options stay where "
                                    "they are until then. Two options given the same index "
                                    "keep the sequence shown below. The correct option "
                                    "travels with its option — leave the picker below alone."
                                )
                                # Each entry carries the text as edited, the index the
                                # option is currently offered under in the answer-key
                                # picker below (`pick_index`), and the position the
                                # reviewer asked it to take. `pick_index` is what the
                                # picker shows and therefore what the reviewer chose
                                # against; it is translated to the post-reorder index
                                # only at the point the payload is built.
                                kept = []
                                for oi, opt in enumerate(options):
                                    ocol1, ocol2, ocol3 = st.columns([6, 2, 1])
                                    text = ocol1.text_input(
                                        f"Option {oi} (index {opt.get('index', oi)})",
                                        opt.get("text", ""), key=f"opt_{qkey}_{oi}")
                                    order = ocol2.number_input(
                                        "New index", min_value=0,
                                        max_value=max(len(options) - 1, 0),
                                        value=oi, step=1, key=f"optord_{qkey}_{oi}",
                                        help="Zero-based index this option takes once "
                                             "saved — the same scale as the indexes above.")
                                    # Remove is only permitted above the minimum.
                                    drop = ocol3.checkbox(
                                        "Remove", key=f"optdel_{qkey}_{oi}",
                                        disabled=not q.get("can_remove_option", False))
                                    if not drop:
                                        kept.append({"text": text,
                                                     "pick_index": opt.get("index", oi),
                                                     # `oi` breaks ties, so two options
                                                     # given the same index resolve the
                                                     # same way every time instead of
                                                     # being rejected.
                                                     "sort_key": (int(order), oi)})
                                # Add is only permitted below the bucket's ceiling. A
                                # new option goes last; it can be moved on a later edit.
                                if q.get("can_add_option"):
                                    extra = st.text_input(
                                        "New option text (leave blank to skip)",
                                        key=f"optadd_{qkey}")
                                    if extra.strip():
                                        used = {o["pick_index"] for o in kept}
                                        nxt = next(i for i in range(100) if i not in used)
                                        # `len(options)` is one past the highest index a
                                        # reviewer can enter, so the new option lands last.
                                        kept.append({"text": extra, "pick_index": nxt,
                                                     "sort_key": (len(options), len(options))})

                                sequenced = sorted(kept, key=lambda o: o["sort_key"])
                                reordered = sequenced != kept
                                if reordered:
                                    # Renumbering only happens when the sequence really
                                    # moved, so an ordinary save still round-trips the
                                    # stored index values untouched.
                                    new_options = [{"text": o["text"], "index": ni}
                                                   for ni, o in enumerate(sequenced)]
                                else:
                                    new_options = [{"text": o["text"], "index": o["pick_index"]}
                                                   for o in sequenced]
                                # Previous index -> index after saving, so the answer key
                                # keeps pointing at the option the reviewer chose.
                                remap = {o["pick_index"]: new_options[ni]["index"]
                                         for ni, o in enumerate(sequenced)}
                                updates["options"] = new_options

                                idxs = [o["pick_index"] for o in kept]

                                # The picker identifies an option, it does not set a
                                # number. Offering bare indexes made that ambiguous the
                                # moment reordering existed: a reviewer compensating for
                                # a reorder reads "3" as "the correct option ends up at
                                # index 3", while the widget reads it as "the option
                                # currently at index 3 is the correct one" — and those
                                # select different options. Labelling each entry with
                                # its own text removes the number from the decision.
                                text_by_index = {o["pick_index"]: (o["text"] or "").strip()
                                                 for o in kept}

                                def option_label(i, _texts=text_by_index):
                                    return f"[{i}] {_texts.get(i) or '(empty)'}"[:60]

                                st.caption(
                                    "Pick the option that is correct **by its content**. "
                                    "Its index is recalculated from the order above when "
                                    "you save, so there is nothing to adjust here after "
                                    "reordering — changing this picker changes *which* "
                                    "option is correct."
                                )
                                if bucket == "Multiple Choice Question":
                                    current = q.get("correct_option_index")
                                    chosen = st.selectbox(
                                        "Correct option", idxs,
                                        index=idxs.index(current) if current in idxs else 0,
                                        format_func=option_label, key=f"ci_{qkey}")
                                    updates["correct_option_index"] = remap.get(chosen, chosen)
                                else:
                                    current = q.get("correct_option_index") or []
                                    chosen = st.multiselect(
                                        "Correct options", idxs,
                                        default=[c for c in current if c in idxs],
                                        format_func=option_label, key=f"cm_{qkey}")
                                    # Sorted, so re-picking the same options in a
                                    # different order is not saved as an answer change.
                                    updates["correct_option_index"] = sorted(
                                        remap.get(c, c) for c in chosen)
                                if reordered:
                                    saved_text = {o["index"]: (o["text"] or "").strip()
                                                  for o in new_options}
                                    correct = updates["correct_option_index"]
                                    st.info(
                                        "**Pending reorder** — on save the options become: "
                                        + " · ".join(f"[{o['index']}] {saved_text[o['index']][:40]}"
                                                     for o in new_options)
                                        + ". Correct: "
                                        + " · ".join(
                                            f"[{i}] {saved_text.get(i, '')[:40]}"
                                            for i in (correct if isinstance(correct, list)
                                                      else [correct]))
                                        + "."
                                    )
                            elif bucket == "True/False Question":
                                updates["correct_answer"] = st.radio(
                                    "Correct answer", ["True", "False"],
                                    index=0 if str(q.get("correct_answer")) == "True" else 1,
                                    key=f"tf_{qkey}", horizontal=True)
                            elif bucket == "FTB Question":
                                updates["correct_answer"] = st.text_input(
                                    "Correct answer", q.get("correct_answer", "") or "",
                                    key=f"ftb_{qkey}")

                            # Rationale
                            ar = q.get("answer_rationale") or {}
                            updates["answer_rationale.correct_answer_explanation"] = st.text_area(
                                "Rationale — correct answer explanation",
                                ar.get("correct_answer_explanation", "") or "", key=f"ar1_{qkey}")
                            updates["answer_rationale.why_factor"] = st.text_input(
                                "Rationale — why factor", ar.get("why_factor", "") or "",
                                key=f"ar2_{qkey}")
                            updates["answer_rationale.logic_justification"] = st.text_input(
                                "Rationale — logic justification",
                                ar.get("logic_justification", "") or "", key=f"ar3_{qkey}")

                            # Bloom's level, difficulty, relevance
                            bc1, bc2, bc3 = st.columns(3)
                            updates["blooms_level"] = pick(
                                "Bloom's level", BLOOMS, q.get("blooms_level"),
                                f"bl_{qkey}", container=bc1)
                            updates["difficulty_level"] = pick(
                                "Difficulty level", DIFFICULTIES, q.get("difficulty_level"),
                                f"df_{qkey}", container=bc2,
                                help="Drives the QuestionTagging column of the CSV export. "
                                     "Left unset, that export falls back to Medium.")
                            updates["relevance_percentage"] = bc3.slider(
                                "Relevance %", 0, 100,
                                int(q.get("relevance_percentage") or 0), key=f"rl_{qkey}")

                            # Learning outcome, competency, course mapping
                            rs = q.get("reasoning") or {}
                            kcm = (rs.get("competency_alignment") or {}).get("kcm") or {}
                            updates["reasoning.learning_objective_alignment"] = st.text_area(
                                "Learning outcome", rs.get("learning_objective_alignment", "") or "",
                                key=f"lo_{qkey}")
                            st.caption(
                                "Competency mapping — area, theme and sub-theme are "
                                "validated together against the KCM dataset, so change "
                                "them as a set.")
                            k1, k2, k3 = st.columns(3)
                            updates["reasoning.competency_alignment.kcm.competency_area"] = \
                                k1.text_input("Competency area",
                                              kcm.get("competency_area", "") or "", key=f"ka_{qkey}")
                            updates["reasoning.competency_alignment.kcm.competency_theme"] = \
                                k2.text_input("Competency theme",
                                              kcm.get("competency_theme", "") or "", key=f"kt_{qkey}")
                            updates["reasoning.competency_alignment.kcm.competency_sub_theme"] = \
                                k3.text_input("Competency sub-theme",
                                              kcm.get("competency_sub_theme", "") or "",
                                              key=f"ks_{qkey}")
                            updates["reasoning.competency_alignment.domain"] = st.text_input(
                                "Competency domain",
                                (rs.get("competency_alignment") or {}).get("domain", "") or "",
                                key=f"kd_{qkey}")
                            updates["course_name"] = st.text_input(
                                "Course mapping", q.get("course_name", "") or "", key=f"cn_{qkey}")

                            # Everything else the API returns for this question.
                            # The identifiers are server-owned, and the generator's
                            # justification narrative is readable but not writable
                            # through the editing endpoints — shown here so the
                            # editor is a complete view of the question rather than
                            # a partial one.
                            st.markdown("**Read-only fields** — returned by the API, "
                                        "not editable through it")
                            ro1, ro2 = st.columns(2)
                            ro1.write(f"- `question_id`: {qid}")
                            ro1.write(f"- `question_type`: {q.get('question_type')} "
                                      f"(`{tkey}` · {bucket})")
                            ro1.write(f"- `provenance`: {q.get('provenance')}")
                            ro2.write(f"- `position`: {pos} of {len(q_list)}")
                            if q.get("option_count") is not None:
                                ro2.write(
                                    f"- `option_count`: {q['option_count']} — add "
                                    f"{'enabled' if q.get('can_add_option') else 'disabled'}, "
                                    f"remove "
                                    f"{'enabled' if q.get('can_remove_option') else 'disabled'}")
                            ro2.write(f"- `can_delete`: {q.get('can_delete')}")
                            for rkey, rlabel in (
                                ("blooms_level_justification", "Bloom's level justification"),
                                ("difficulty_justification", "Difficulty justification"),
                                ("question_type_rationale", "Question type rationale"),
                                ("assessment_type_relevance", "Assessment type relevance"),
                            ):
                                st.caption(f"**{rlabel}** — {rs.get(rkey) or '—'}")

                            b1, b2, b3 = st.columns(3)
                            preview_clicked = b1.form_submit_button("👁️ Preview changes")
                            save_clicked = b2.form_submit_button("💾 Save question")
                            cancel_clicked = b3.form_submit_button("✖️ Cancel")

                            if cancel_clicked:
                                # Nothing is applied and nothing is
                                # persisted; the API never sees this. Bump the
                                # generation counter so every widget above is keyed
                                # under a key it has never used before: the browser
                                # mounts brand-new widgets seeded from `q` (the
                                # unmodified server data) instead of trying to
                                # redisplay the ones that just held the edit.
                                st.session_state[f"edit_gen_{job_id}_{qid}"] = edit_gen + 1
                                report_event("TEL-04", question_id=qid)
                                st.info("Changes discarded. Nothing was saved.")
                                st.rerun()

                            if preview_clicked or save_clicked:
                                body = {"request": {
                                    "questionId": qid,
                                    "updates": prune_unchanged(updates, q),
                                    "version": version}}
                                try:
                                    if preview_clicked:
                                        # Pre-update alert, nothing is written
                                        r_dry = requests.post(
                                            f"{Q}/update/{job_id}",
                                            params={"dry_run": "true"}, json=body,
                                            headers=get_headers())
                                        if r_dry.status_code == 200:
                                            res = r_dry.json()
                                            if not res.get("valid"):
                                                st.error("This change would be rejected:")
                                                for err in res.get("errors", []):
                                                    st.warning(f"• `{err.get('field')}` — "
                                                               f"{err.get('message')}")
                                            elif not res.get("changed_fields"):
                                                st.info("No changes to save.")
                                            else:
                                                st.write("**Fields that will change:**")
                                                for c in res["changed_fields"]:
                                                    st.write(f"• `{c['field']}`: "
                                                             f"{c['previous_value']!r} → "
                                                             f"{c['new_value']!r}")
                                                show_alerts(res.get("alerts"))
                                        else:
                                            show_api_error(r_dry, "Preview failed")
                                    else:
                                        r_sv = requests.post(f"{Q}/update/{job_id}",
                                                             json=body, headers=get_headers())
                                        if r_sv.status_code == 200:
                                            res = r_sv.json()
                                            st.success(f"{res.get('message')} "
                                                       f"(version {res.get('version')})")
                                            show_alerts(res.get("alerts"))
                                            # Re-key every widget, exactly as Cancel
                                            # does. A save can change what a widget
                                            # should now display without changing the
                                            # value the widget itself holds — reordering
                                            # the options renumbers them, so the Order
                                            # inputs and the correct-index picker must
                                            # re-seed from the saved question rather
                                            # than redisplay the pre-save numbering.
                                            st.session_state[f"edit_gen_{job_id}_{qid}"] = edit_gen + 1
                                            st.rerun()
                                        else:
                                            show_api_error(r_sv, "Save failed")
                                except Exception as e:
                                    st.error(f"Request error: {e}")

                # ---------- audit trail ----------
                with st.expander("🧾 Audit trail (GET /audit/{job_id})"):
                    try:
                        r_audit = requests.get(f"{API_V1}/audit/{job_id}",
                                               headers=get_headers())
                        if r_audit.status_code == 200:
                            audit = r_audit.json()
                            st.caption(f"Version {audit['version']} · "
                                       f"{audit['count']} recorded change(s) · "
                                       f"first edited: {audit.get('edited_at') or 'never'}")
                            EVENT_LABELS = {
                                "TEL-03": "Edit saved", "TEL-05": "Added",
                                "TEL-06": "Deleted", "TEL-07": "Reordered",
                                "TEL-10": "Answer key changed",
                                "TEL-11": "Mapping updated",
                            }
                            for entry in audit["audit_trail"]:
                                bits = [
                                    f"**v{entry['assessment_version']}**",
                                    EVENT_LABELS.get(entry["event_code"], entry["event_code"]),
                                    f"`{entry.get('question_type') or '-'}`",
                                    f"by `{entry['editor_id']}`",
                                    str(entry.get("created_at", ""))[:19].replace("T", " "),
                                ]
                                st.write(" · ".join(bits))
                                if entry.get("previous_position") is not None or \
                                   entry.get("new_position") is not None:
                                    st.caption(f"    position "
                                               f"{entry.get('previous_position')} → "
                                               f"{entry.get('new_position')}")
                                if entry["event_code"] == "TEL-10":
                                    d = entry.get("details") or {}
                                    st.caption(f"    answer {d.get('previous_answer')!r} → "
                                               f"{d.get('updated_answer')!r}")
                                for c in entry.get("changed_fields") or []:
                                    st.caption(f"    `{c['field']}`: "
                                               f"{str(c['previous_value'])[:80]!r} → "
                                               f"{str(c['new_value'])[:80]!r}")
                            if audit.get("ai_original"):
                                with st.expander("Original AI-generated assessment (retained "
                                                 "for audit)"):
                                    st.json(audit["ai_original"], expanded=False)
                        else:
                            show_api_error(r_audit, "Could not load audit trail")
                    except Exception as e:
                        st.error(f"Audit error: {e}")

# ==========================================
# TAB 3: HISTORY
# ==========================================
with tab_history:
    st.markdown("### 🗂️ Your Assessment History")
    st.info("View previously generated tests. Ensure you have provided your Auth Token in the sidebar.")
    
    if st.button("🔄 Refresh History"):
        if not auth_token:
            st.warning("Enter your Auth Token to retrieve history.")
        else:
            with st.spinner("Fetching history..."):
                try:
                    r_hist = requests.get(f"{API_V1}/history", headers=get_headers())
                    if r_hist.status_code == 200:
                        st.session_state['history_data'] = r_hist.json()
                    else:
                        st.error(f"Failed to fetch history: {r_hist.text}")
                except Exception as e:
                    st.error(f"Error fetching history: {e}")

    history_items = st.session_state.get('history_data', [])
    
    if history_items:
        for idx, item in enumerate(history_items):
            job_id = item.get("job_id", "Unknown")
            status = item.get("status", "Unknown")
            updated = item.get("updated_at", "Unknown")
            config = item.get("config", {})
            course_names = item.get("course_names", [])
            course_ids_list = item.get("course_ids", [])
            course_label = ", ".join(course_names) if course_names else "Unknown Course"

            # Status badge logic
            status_emoji = "⏳"
            if status == "COMPLETED": status_emoji = "✅"
            elif status == "FAILED": status_emoji = "❌"
            elif status == "PENDING": status_emoji = "🕒"

            with st.expander(f"{status_emoji} {course_label} | Updated: {updated[:10]}", expanded=(idx == 0)):
                cols = st.columns([2, 1])

                with cols[0]:
                    st.write(f"- **Job ID:** `{job_id}`")
                    if course_names:
                        st.write(f"- **Course(s):** {', '.join(course_names)}")
                    if course_ids_list:
                        st.write(f"- **Course ID(s):** {', '.join(course_ids_list)}")
                    st.write(f"- **Created:** {item.get('created_at', 'N/A')[:19].replace('T', ' ')}")
                    st.write(f"- **Updated:** {updated[:19].replace('T', ' ')}")
                    st.markdown("**Configuration:**")
                    if config:
                        st.write(f"- **Type:** {config.get('assessment_type', 'N/A')}")
                        st.write(f"- **Difficulty:** {config.get('difficulty', 'N/A')}")
                        st.write(f"- **Language:** {config.get('language', 'N/A')}")
                        st.write(f"- **Total Questions:** {config.get('total_questions', 'N/A')}")
                        q_counts = config.get('question_type_counts', {})
                        if q_counts:
                            active = ", ".join(f"{k.upper()}:{v}" for k, v in q_counts.items() if v > 0)
                            st.write(f"- **Question Types:** {active}")
                        st.write(f"- **Time Limit:** {config.get('time_limit', 0) or 'No limit'}")
                        if config.get('course_weightage'):
                            st.write(f"- **Course Weightage:** `{config.get('course_weightage')}`")
                    else:
                        st.write("No configuration metadata available (Legacy Job).")
                
                with cols[1]:
                    st.markdown("**Actions:**")
                    if status == "COMPLETED":
                        if st.button("Load into Editor", key=f"load_{job_id}"):
                            st.session_state['current_job_id'] = job_id
                            st.success(f"Job {job_id} loaded! Switch to 'View & Edit Result' tab.")
                        
                        # Downloads — token sent in header, not URL query param
                        dl_col1, dl_col2, dl_col3, dl_col4 = st.columns(4)
                        for fmt, col, label, mime in [
                            ("csv",       dl_col1, "CSV",       "text/csv"),
                            ("csv_basic", dl_col2, "CSV Basic", "text/csv"),
                            ("pdf",       dl_col3, "PDF",       "application/pdf"),
                            ("docx",      dl_col4, "DOCX",      "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
                        ]:
                            try:
                                dl_r = requests.get(
                                    f"{API_V1}/download/{job_id}",
                                    params={"format": fmt},
                                    headers=get_headers(),
                                )
                                if dl_r.status_code == 200:
                                    ext = "csv" if fmt == "csv_basic" else fmt
                                    col.download_button(f"Download {label}", dl_r.content, file_name=f"assessment_{job_id}_{fmt}.{ext}", mime=mime, key=f"dl_{fmt}_{job_id}")
                                else:
                                    col.error(f"{label} failed ({dl_r.status_code})")
                            except Exception as e:
                                col.error(f"{label} error: {e}")
                    else:
                        st.write(f"*Job is {status}* ‒ wait for completion.")
    elif history_items == []:
         st.write("No history found for your account.")
