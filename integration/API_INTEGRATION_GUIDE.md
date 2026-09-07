# AI Assessment Service — API Integration Guide

This document covers all endpoints, request parameters, response formats, and end-to-end integration flows for the AI Assessment Generation Service.

## Postman Collections

Import the appropriate collection based on your access method:

| Collection | Access Method | Link |
|---|---|---|
| Kong (API / server-to-server) | Requires `x-authenticated-user-token` + Kong JWT | [postman_collection_kong.json](https://github.com/aswinpradeep/ai-assessment-service/raw/refs/heads/dev/integration/postman_collection_kong.json) |
| UI Proxy (browser / frontend) | Requires session cookie only | [postman_collection_proxy.json](https://github.com/aswinpradeep/ai-assessment-service/raw/refs/heads/dev/integration/postman_collection_proxy.json) |

---

## Base URLs

| Environment | Access Method | Base URL |
|---|---|---|
| UAT | Kong API Gateway | `https://portal.uat.karmayogibharat.net/api/ai/assessments/v1` |
| UAT | UI Proxy (cookie) | `https://portal.uat.karmayogibharat.net/apis/proxies/v8/ai/assessments/v1` |
| Local | Direct | `http://localhost:8000/ai-assessments/v1` |

---

## Authentication

### Kong (API / server-to-server)
Two headers are required:

| Header | Value |
|---|---|
| `x-authenticated-user-token` | Keycloak JWT obtained after user login |
| `Authorization` | `bearer <kong_jwt_credential>` |

### UI Proxy (browser / frontend)
Only a session cookie is required — no tokens:

| Header | Value |
|---|---|
| `cookie` | `connect.sid=<session_cookie>` (set automatically by the browser) |

The proxy reads the session and injects user identity on behalf of the caller. No token handling needed in frontend code.

---

## Endpoints

### 1. Generate Assessment

**`POST /generate`**

Submits an assessment generation request. Returns immediately with a `job_id`. Generation happens asynchronously in the background.

**Cache behaviour:**
- If this user has already generated the exact same assessment (same course + same parameters), returns `COMPLETED` instantly from cache.
- If another user generated the same assessment before, it is cloned instantly to this user and returns `COMPLETED`.
- Otherwise, queues a new generation job and returns `PENDING`.

#### Kong

```bash
curl --location 'https://portal.uat.karmayogibharat.net/api/ai/assessments/v1/generate' \
  --header 'x-authenticated-user-token: <keycloak_jwt>' \
  --header 'Authorization: bearer <kong_jwt_credential>' \
  --form 'course_ids="do_1144540583527301121908"' \
  --form 'course_names="Foundations of Public Policy"' \
  --form 'assessment_type="practice"' \
  --form 'difficulty="intermediate"' \
  --form 'language="english"' \
  --form 'total_questions="10"' \
  --form 'question_type_counts="{\"mcq\":5,\"ftb\":5,\"mtf\":0,\"multichoice\":0,\"truefalse\":0}"' \
  --form 'enable_blooms="true"' \
  --form 'blooms_config="{\"Remember\":20,\"Understand\":30,\"Apply\":30,\"Analyze\":10,\"Evaluate\":10,\"Create\":0}"' \
  --form 'force="false"' \
  --form 'time_limit="0"'
```

#### UI Proxy

```bash
curl --location 'https://portal.uat.karmayogibharat.net/apis/proxies/v8/ai/assessments/v1/generate' \
  --header 'cookie: connect.sid=<session_cookie>' \
  --form 'course_ids="do_1144540583527301121908"' \
  --form 'course_names="Foundations of Public Policy"' \
  --form 'assessment_type="practice"' \
  --form 'difficulty="intermediate"' \
  --form 'language="english"' \
  --form 'total_questions="10"' \
  --form 'question_type_counts="{\"mcq\":5,\"ftb\":5,\"mtf\":0,\"multichoice\":0,\"truefalse\":0}"' \
  --form 'enable_blooms="true"' \
  --form 'blooms_config="{\"Remember\":20,\"Understand\":30,\"Apply\":30,\"Analyze\":10,\"Evaluate\":10,\"Create\":0}"' \
  --form 'force="false"' \
  --form 'time_limit="0"'
```

#### Comprehensive Assessment (multi-course) — Kong

```bash
curl --location 'https://portal.uat.karmayogibharat.net/api/ai/assessments/v1/generate' \
  --header 'x-authenticated-user-token: <keycloak_jwt>' \
  --header 'Authorization: bearer <kong_jwt_credential>' \
  --form 'course_ids="do_1144540583527301121908"' \
  --form 'course_ids="do_113948972799877120197"' \
  --form 'course_names="Foundations of Public Policy"' \
  --form 'course_names="Ethics in Governance"' \
  --form 'assessment_type="comprehensive"' \
  --form 'difficulty="intermediate"' \
  --form 'language="english"' \
  --form 'total_questions="20"' \
  --form 'question_type_counts="{\"mcq\":10,\"ftb\":5,\"mtf\":5,\"multichoice\":0,\"truefalse\":0}"' \
  --form 'course_weightage="{\"do_1144540583527301121908\":60,\"do_113948972799877120197\":40}"' \
  --form 'force="false"'
```

#### Competency Assessment — Kong

```bash
curl --location 'https://portal.uat.karmayogibharat.net/api/ai/assessments/v1/generate' \
  --header 'x-authenticated-user-token: <keycloak_jwt>' \
  --header 'Authorization: bearer <kong_jwt_credential>' \
  --form 'course_ids="do_1144540583527301121908"' \
  --form 'course_names="Foundations of Public Policy"' \
  --form 'assessment_type="competency"' \
  --form 'difficulty="intermediate"' \
  --form 'language="english"' \
  --form 'total_questions="10"' \
  --form 'question_type_counts="{\"mcq\":10,\"ftb\":0,\"mtf\":0,\"multichoice\":0,\"truefalse\":0}"' \
  --form 'competency_area="Behavioural"' \
  --form 'competency_themes="Service Orientation"' \
  --form 'competency_sub_themes="Citizen Centricity"' \
  --form 'competency_sub_themes="Empathy"' \
  --form 'force="false"'
```

#### Request Parameters

| Parameter | Type | Required | Accepted Values | Description |
|---|---|---|---|---|
| `course_ids` | string (repeated) | Yes* | Any valid iGOT course ID | Pass as repeated form fields — one per course. `*`Required unless uploading files or using `competency` type. Example: `--form 'course_ids="do_1"' --form 'course_ids="do_2"'` |
| `assessment_type` | string | Yes | `practice`, `final`, `comprehensive`, `standalone`, `competency` | Type of assessment to generate. `comprehensive` combines multiple courses. `competency` generates KCM-focused questions — course_ids are optional, questions can be generated purely from KCM descriptions. |
| `difficulty` | string | Yes | `beginner`, `intermediate`, `advanced` | Target difficulty level of questions. |
| `language` | string | Yes | `english`, `hindi`, `tamil`, `telugu`, `kannada`, `malayalam`, `marathi`, `bengali`, `gujarati`, `punjabi`, `odia`, `assamese` | Language for generated questions. |
| `total_questions` | integer | No | Any positive integer | Total number of questions to generate. Default: `5`. |
| `question_type_counts` | JSON string | No | Keys: `mcq`, `ftb`, `mtf`, `multichoice`, `truefalse` | Count per question type. Values must add up to (or be consistent with) `total_questions`. Example: `{"mcq":5,"ftb":5,"mtf":0,"multichoice":0,"truefalse":0}` |
| `enable_blooms` | boolean | No | `true`, `false` | Whether to apply Bloom's taxonomy distribution. Default: `true`. |
| `blooms_config` | JSON string | No | Keys: `Remember`, `Understand`, `Apply`, `Analyze`, `Evaluate`, `Create` | Percentage distribution across Bloom's levels. Values must sum to 100. Example: `{"Remember":20,"Understand":30,"Apply":30,"Analyze":10,"Evaluate":10,"Create":0}` |
| `time_limit` | integer | No | `0` or any positive integer | Time limit in minutes. `0` means no limit. |
| `topic_names` | string | No | Comma-separated topic names | Restrict question generation to specific topics within the course. Leave blank to use all topics. |
| `course_weightage` | JSON string | No | `{"<course_id>": <percent>, ...}` | Weightage per course when `assessment_type` is `comprehensive`. Values must sum to 100. Example: `{"do_1":60,"do_2":40}` |
| `course_names` | string (repeated) | No | One value per field | Names matching the order of `course_ids`. Pass as repeated form fields. Used to populate history immediately without waiting for job completion. Example: `--form 'course_names="Foundations of Public Policy"' --form 'course_names="Ethics in Governance"'` |
| `competency_area` | string | Yes* | Any valid KCM competency area | `*`Required when `assessment_type=competency`. e.g. `"Behavioural"` |
| `competency_themes` | string (repeated) | Yes* | One value per field | `*`Required when `assessment_type=competency`. Pass as repeated form fields. e.g. `--form 'competency_themes="Service Orientation"' --form 'competency_themes="Decision Making"'` |
| `competency_sub_themes` | string (repeated) | Yes* | One value per field | `*`Required when `assessment_type=competency`. Pass as repeated form fields. e.g. `--form 'competency_sub_themes="Citizen Centricity"' --form 'competency_sub_themes="Empathy"'` |
| `additional_instructions` | string | No | Free text | Any extra instructions passed to the AI model (e.g. "focus on case studies", "avoid numerical questions"). |
| `files` | file (repeated, multipart) | Yes* | One or more uploaded files | `*`Required for `assessment_type=standalone` unless `course_ids` is provided instead — `standalone` needs at least one of the two. Pass as repeated multipart file fields. |
| `force` | boolean | No | `true`, `false` | `true` bypasses cache and forces a fresh generation. Default: `false`. |

#### Question Types

| Type | Key | Description |
|---|---|---|
| Multiple Choice (Single) | `mcq` | One correct answer from 4 options |
| Fill in the Blank | `ftb` | Complete a missing word or phrase |
| Match the Following | `mtf` | Match items in two columns |
| Multiple Choice (Multi) | `multichoice` | More than one correct answer from options |
| True / False | `truefalse` | Binary true or false question |

#### Response — New Job (202 Accepted equivalent, returns 200)

```json
{
  "message": "Generation started (Queued)",
  "status": "PENDING",
  "job_id": "do_1144540583527301121908_7fa321bd_1e8b6826-3326-4175-b202-f5f5971f457a"
}
```

#### Response — Cache Hit or Clone

```json
{
  "message": "Assessment retrieved from cache",
  "status": "COMPLETED",
  "job_id": "do_1144540583527301121908_7fa321bd_1e8b6826-3326-4175-b202-f5f5971f457a",
  "result": { ... }
}
```

#### Response Fields

| Field | Description |
|---|---|
| `message` | Human-readable description of what happened |
| `status` | `PENDING` (queued), `IN_PROGRESS` (generating), `COMPLETED` (done), `FAILED` (error) |
| `job_id` | Unique identifier for this assessment. Format: `{course_id}_{param_hash}_{user_id}`. Store this — it is used in all subsequent calls. |
| `result` | Present only on cache hits. Contains the full assessment data (same as Status response when COMPLETED). |

---

### 2. Get Status

**`GET /status/{job_id}`**

Polls the status of a generation job. When `status` becomes `COMPLETED`, the `assessment_data` field contains the full generated assessment.

#### Kong

```bash
curl --location 'https://portal.uat.karmayogibharat.net/api/ai/assessments/v1/status/<job_id>' \
  --header 'x-authenticated-user-token: <keycloak_jwt>' \
  --header 'Authorization: bearer <kong_jwt_credential>'
```

#### UI Proxy

```bash
curl --location 'https://portal.uat.karmayogibharat.net/apis/proxies/v8/ai/assessments/v1/status/<job_id>' \
  --header 'cookie: connect.sid=<session_cookie>'
```

#### Response — In Progress

```json
{
  "job_id": "do_1144540583527301121908_7fa321bd_1e8b6826-3326-4175-b202-f5f5971f457a",
  "status": "IN_PROGRESS",
  "assessment_data": null,
  "error_message": null
}
```

#### Response — Completed

```json
{
  "job_id": "do_1144540583527301121908_7fa321bd_1e8b6826-3326-4175-b202-f5f5971f457a",
  "status": "COMPLETED",
  "metadata": {
    "config": {
      "assessment_type": "practice",
      "difficulty": "intermediate",
      "language": "english",
      "total_questions": 10,
      "question_type_counts": { "mcq": 5, "ftb": 5, "mtf": 0, "multichoice": 0, "truefalse": 0 },
      "enable_blooms": true,
      "blooms_config": { "remember": 20, "understand": 30, "apply": 30, "analyze": 10, "evaluate": 10, "create": 0 },
      "topic_names": null,
      "additional_instructions": null,
      "time_limit": null,
      "course_weightage": null,
      "competency_area": null,
      "competency_themes": [],
      "competency_sub_themes": []
    },
    "content_availability": {
      "do_1144540583527301121908": {
        "has_vtt": true,
        "has_pdf": false
      }
    }
  },
  "version": 1,
  "assessment_data": {
    "blueprint": { "...": "..." },
    "question_order": ["mcq_001", "mcq_002", "ftb_001"],
    "questions": {
      "Multiple Choice Question": [
        {
          "question_id": "mcq_001",
          "provenance": "ai_generated",
          "course_name": "Foundations of Public Policy",
          "question_text": "What is the primary purpose of ...?",
          "options": [
            { "text": "Option A", "index": 0 },
            { "text": "Option B", "index": 1 },
            { "text": "Option C", "index": 2 },
            { "text": "Option D", "index": 3 }
          ],
          "correct_option_index": 0,
          "blooms_level": "Understand",
          "answer_rationale": {
            "correct_answer_explanation": "Option A is correct because ...",
            "why_factor": "...",
            "logic_justification": "..."
          },
          "reasoning": {
            "learning_objective_alignment": "...",
            "competency_alignment": { "kcm": { "competency_area": "...", "competency_theme": "...", "competency_sub_theme": "..." } },
            "blooms_level_justification": "...",
            "difficulty_justification": "...",
            "question_type_rationale": "...",
            "assessment_type_relevance": "..."
          },
          "relevance_percentage": 92
        }
      ],
      "FTB Question": [],
      "MTF Question": [],
      "Multi-Choice Question": [],
      "True/False Question": []
    }
  },
  "error_message": null
}
```

#### Response Fields

| Field | Description |
|---|---|
| `job_id` | The assessment job ID |
| `status` | `PENDING`, `IN_PROGRESS`, `COMPLETED`, or `FAILED` |
| `version` | Assessment version. Starts at `1` on generation and increments on every saved edit. Pass it back on any editing call to detect concurrent updates. |
| `metadata.config` | All generation parameters used for this job — `assessment_type`, `difficulty`, `language`, `total_questions`, `question_type_counts`, `enable_blooms`, `blooms_config`, `topic_names`, `additional_instructions`, `time_limit`, `course_weightage`, `competency_area`, `competency_themes`, `competency_sub_themes` |
| `metadata.content_availability` | Per-course VTT and PDF availability. For course-based jobs: `{ "<course_id>": { "has_vtt": true, "has_pdf": false } }`. For standalone uploads: `{ "uploaded_files": { "has_vtt": true, "has_pdf": true } }`. `has_vtt: false` when the VTT file exists but contains no subtitle content. |
| `assessment_data` | `null` until complete. On completion contains `blueprint`, `question_order` and `questions` keyed by type. |
| `assessment_data.question_order` | **The authoritative question sequence** — an array of `question_id` in presentation order. The type buckets under `questions` store the content; this array decides the order. Every download honours it. |
| `error_message` | `null` on success. Contains error details if `status` is `FAILED`. |

#### Question Object Fields

| Field | Description |
|---|---|
| `question_id` | Unique identifier within the assessment. Required for every editing call. Server-assigned — never set or change it from a client. |
| `provenance` | `ai_generated` (untouched AI output), `ai_assisted` (AI output a reviewer has edited), `human_authored` (added manually). Server-controlled; ignored if sent by a client. |
| `course_name` | The course this question is derived from. `"User Uploaded Content"` for standalone assessments. |
| `question_text` | The question text (MTF uses `matching_context` instead) |
| `options` | List of `{ "text": "...", "index": 0 }` answer choices. MCQ and Multi-Choice must have **at least 2**, and **at most 5 when the question is being added**. `index` is **zero-based** — the first option is `0`. |
| `correct_option_index` | For MCQ, the single `index` of the correct option. For Multi-Choice, an array of correct `index` values. **Zero-based**, and matched against each option's own `index` field, not its array position. |
| `correct_answer` | FTB: the answer text. True/False: `"True"` or `"False"`. |
| `pairs` | MTF: list of `{ "left": "...", "right": "..." }`, at least 2. |
| `blooms_level` | `Remember`, `Understand`, `Apply`, `Analyze`, `Evaluate`, or `Create` |
| `answer_rationale` | `correct_answer_explanation`, `why_factor`, `logic_justification` |
| `reasoning` | `learning_objective_alignment`, `competency_alignment` (KCM area/theme/sub-theme), `blooms_level_justification`, `difficulty_justification`, `question_type_rationale`, `assessment_type_relevance` |
| `relevance_percentage` | 0–100 confidence score based on LO alignment (35%), competency fit (30%), Bloom's match (20%), assessment fit (15%) |

---

## Editing Workspace

Endpoints 3–8 let a reviewer compose the final question set inside the platform. They share one set of rules.

> **These endpoints persist and audit; they do not present.** They return no
> user-facing sentences, no impact warnings and no screen-reader copy. Read
> [What the client owns](#what-the-client-owns) before building against them —
> several things a previous version of this API returned are now the client's
> to produce.

### Path shape and request envelope

These endpoints are routed by a Kong `API` entity (Kong 0.10–0.14), which can only
prefix-match: `strip_uri: true` strips the matched prefix and appends the remaining path
verbatim to the upstream URL. It cannot reorder path segments and cannot express a path
parameter followed by further segments. Every path is therefore a **static verb prefix
followed by `job_id` as the single trailing segment**:

| Method | Path | Was |
|---|---|---|
| `POST` | `/questions/create/{job_id}` | `POST /assessments/{job_id}/questions` |
| `POST` | `/questions/update/{job_id}` | `PATCH /assessments/{job_id}/questions/{question_id}` |
| `POST` | `/questions/delete/{job_id}` | `DELETE /assessments/{job_id}/questions/{question_id}` |
| `POST` | `/questions/order/{job_id}` | `PUT /assessments/{job_id}/questions/order` |
| `GET` | `/audit/{job_id}` | `GET /assessments/{job_id}/audit` |

Three things follow, and they matter for anyone writing a client:

1. **`PATCH`, `PUT` and `DELETE` are all `POST` now.** The identifiers they used to carry
   in the path travel in the body instead, and a `GET` or `DELETE` must never carry a
   body — the semantics are undefined and intermediate proxies may drop it.
2. **Every identifier except `job_id` is a body field**: `questionId` and
   `questionOrder`.
3. **There is no bare `/questions` route.** On a longest-prefix router it would shadow
   all four verb routes above.

Request bodies use the Sunbird envelope:

```json
{ "request": { "questionId": "mcq_001", "version": 4, "updates": { "...": "..." } } }
```

A body that is not wrapped in `request` returns 400 `request_required`. Field names are
camelCase (`questionId`, `questionOrder`, `questionType`, `eventCode`); the snake_case
equivalents are accepted as aliases. **Responses are not enveloped** — they are unchanged
from before the reshape.

`limit` and `offset` remain query parameters; a prefix-matching gateway passes
the query string through untouched.

### Versioning and concurrent updates

Every assessment carries an integer `version`, returned by `GET /status` and by every editing call. To make a change safely:

1. Read the assessment (`GET /status/{job_id}`) and keep its `version`.
2. Send that `version` back — in the request body, or as an `If-Match: <version>` header.
3. If another update landed in between, the call returns **409** and **nothing is written**. Reload and re-apply.

```json
{
  "detail": "version_conflict",
  "errors": [
    { "code": "version_conflict", "field": null, "question_id": null,
      "params": { "current_version": 7 } }
  ],
  "job_id": "...",
  "current_version": 7
}
```

`current_version` stays a top-level field as well as an error param, so a client can
resync without reading the `errors` array.

Omitting the version still protects against lost updates within a single request, but cannot detect that the user's screen was stale. **Always send it.**

Sending the version is also what makes a **double submission** safe. Two requests carrying the same version cannot both apply: the first commits and moves the version on, the second matches nothing and returns 409. Even with no version supplied, a repeated identical request diffs to no changes and returns `"code": "no_changes"` without bumping the version. A double-clicked Save button can never apply the same edit twice.

### Validation

A save is rejected outright if the resulting question would be invalid — nothing partial is ever stored. Validation failures return **400** with a machine-readable `errors` array:

```json
{
  "detail": "option_count_invalid",
  "errors": [
    {
      "code": "option_count_invalid",
      "field": "options",
      "question_id": "mcq_001",
      "params": { "minimum": 2, "maximum": 5, "found": 1 }
    }
  ]
}
```

**Errors carry no message string.** Each is `code` + `field` + `question_id`, plus a
`params` bag holding every value a message needs to interpolate. The client maps `code`
to its own copy and fills in `params` — which is what lets one error render in any of the
twelve languages this service generates assessments in. `detail` is the primary error's
`code` (suffixed `(+N more)` when there are several), not a sentence: use it for logs and
generic toasts, and `errors` for anything a user reads.

So `{"code": "option_count_invalid", "params": {"minimum": 2, "maximum": 5, "found": 1}}`
becomes "Needs between 2 and 5 options — this one has 1" in your string table.
`maximum` is `null` when no ceiling applies, which is every edit of an existing question.

`params` keys by code:

| `code` | `params` |
|---|---|
| `option_count_invalid` | `minimum`, `maximum` (nullable), `found` |
| `option_text_required`, `option_malformed`, `option_index_invalid` | `option_position` (1-based) |
| `correct_option_index_out_of_range` | `found`, `valid_indexes` |
| `correct_option_index_invalid`, `correct_option_index_required` | `expects` (`single_index` \| `index_array`) |
| `correct_answer_invalid` | `allowed` |
| `blooms_level_invalid` | `allowed`, `found` |
| `relevance_invalid` | `minimum`, `maximum`, `found` |
| `provenance_invalid` | `allowed`, `found` |
| `pair_count_invalid` | `minimum`, `found` |
| `pair_left_required`, `pair_right_required`, `pair_malformed` | `pair_position` (1-based) |
| `competency_mapping_incomplete` | `missing` |
| `field_not_editable` | `question_bucket`, `editable_fields` |
| `question_type_invalid` | `found`, `expected` |
| `question_order_invalid` | `missing`, `unknown`, `duplicated` |
| `version_conflict` | `current_version` |
| `assessment_not_editable` | `status` |

Validation covers the five limbs the specification names — question, answer, option, mapping and assessment-level:

| Limb | Rules |
|---|---|
| Question | Question text (or MTF matching context) cannot be empty. Answer rationale cannot be empty. `blooms_level` must be one of the six levels while Bloom's is enabled. `relevance_percentage` must be an integer 0–100. |
| Option | MCQ and Multi-Choice must have **at least 2 options**, each with non-empty text and a unique integer `index`. A question being **added** must also have **at most 5** — editing an existing question has no ceiling, so a generated question carrying more options stays editable. MTF requires at least 2 complete pairs. |
| Answer | The correct answer must reference an existing option `index`. MCQ takes one index, Multi-Choice at least one. True/False must be `"True"` or `"False"`. FTB requires answer text. A question can never be left unscorable. |
| Mapping | A mapping field cannot be blanked once set. The competency triple is all-or-nothing — area, theme and sub-theme together. The triple's values are free text and are not checked against the KCM dataset. |
| Assessment | At least one question must remain. Question identifiers must be unique. |

A scoping rule keeps validation from blocking unrelated work:

- Per-question rules apply to the questions a save **adds or changes**, not to untouched ones, so a gap in an older question cannot block an edit elsewhere.

> **Edit the competency triple together.** Changing only `competency_theme` leaves the stored sub-theme belonging to the old theme, which fails validation. Send `competency_area`, `competency_theme` and `competency_sub_theme` in the same request.

### What the client owns

This API persists, validates and audits. It does not describe, warn or narrate. The
following are **not** server concerns, and no endpoint returns them — build them in the
client, where the before-state was rendered and the user's language is known.

| Concern | Why it is yours | What you have to work with |
|---|---|---|
| **Pre-save impact warnings** ("the correct answer will change") | A pure function of before/after. You rendered `before` and the user typed `after` — no round trip can tell you anything you don't already hold. | Your local before/after |
| **Confirmation dialogs** | A dialog is UI. There is no `dry_run` and no `confirm` flag: a server-side confirmation gate stopped nothing, since any caller that wanted the write simply set it. | Your own modal |
| **Telling an option *reorder* from an option *rewrite*** | Same-texts-different-order means the answer key moves without the answer changing. Warning "the correct answer will change" there is wrong, and it is the one thing a reviewer must be able to trust. Compare option text arrays before/after. | Your local before/after |
| **Screen-reader announcements** | Presentation copy, and it must be in the user's language. Compose from the response. | `question_order`, `total_questions`, `version` |
| **Error and alert wording** | See [Validation](#validation) — `code` + `params`, mapped to your string table. | `errors[]` |
| **The flat, position-annotated question list** | Derivable from `GET /status` in a few lines. See [Building the editor list](#building-the-editor-list). | `assessment_data` |
| **Per-question affordance state** (`can_delete`, `can_add_option`, …) | One-line predicates over the same data. See below. | `assessment_data` |

Cancelling an edit is entirely local: discard your draft and re-render from the copy you
already have. There is nothing to tell the server.

### Building the editor list

`GET /status/{job_id}` returns the whole assessment, so the editor's flat list is a
client-side projection of it — walk `assessment_data.question_order`, look each id up in
`assessment_data.questions`, and annotate:

```js
const BUCKET_KEY = {
  "Multiple Choice Question": "mcq", "FTB Question": "ftb", "MTF Question": "mtf",
  "Multi-Choice Question": "multichoice", "True/False Question": "truefalse",
};
const OPTION_BUCKETS = ["Multiple Choice Question", "Multi-Choice Question"];
const MIN_OPTIONS = 2;

function editorList(assessmentData) {
  const { question_order: order, questions } = assessmentData;
  const byId = new Map();
  for (const [bucket, list] of Object.entries(questions ?? {})) {
    for (const q of list ?? []) byId.set(q.question_id, { bucket, q });
  }
  const total = [...byId.keys()].length;

  return (order ?? []).map((id, i) => {
    const { bucket, q } = byId.get(id);
    const optionCount = Array.isArray(q.options) ? q.options.length : null;
    const hasOptions = OPTION_BUCKETS.includes(bucket) && optionCount !== null;
    return {
      ...q,
      position: i + 1,
      question_bucket: bucket,
      question_type_key: BUCKET_KEY[bucket] ?? bucket,
      option_count: optionCount,
      // The edit path has NO option ceiling, so add is always available on an
      // option-based question. The 5-option limit applies only to authoring a
      // new one — enforce it in the add form, never in the editor.
      can_add_option: hasOptions,
      can_remove_option: hasOptions && optionCount > MIN_OPTIONS,
      can_delete: total > 1,
    };
  });
}
```

Two rules this encodes, both of which matter:

- **`question_order` is the sequence — never infer order from the buckets.** They are
  storage, and `assessment_data` is a jsonb column, so bucket key order is whatever
  Postgres returns, not the question order.
- **`correct_option_index` matches an option's own `index` value, not its array
  position.** Always resolve it by looking for the option whose `index` equals it.
  Assessments generated before prompt v4.3 may carry one-based indexes, and that is
  harmless precisely because every reader matches on the value.

### Provenance

Provenance is set by the server and cannot be supplied by a client:

- Generated questions start as `ai_generated`.
- The first reviewer edit moves them to `ai_assisted`, and they stay there however many further edits they receive.
- Manually added questions are `human_authored`, and stay that way when edited.

---

### 3. Edit a Question

**`POST /questions/update/{job_id}`**

`questionId` names the question and travels in the body. Updates are keyed by **dotted path**, so you send only what changed rather than the whole question.

```bash
curl --location \
  'https://portal.uat.karmayogibharat.net/api/ai/assessments/v1/questions/update/<job_id>' \
  --header 'x-authenticated-user-token: <keycloak_jwt>' \
  --header 'Authorization: bearer <kong_jwt_credential>' \
  --header 'Content-Type: application/json' \
  --data '{
    "request": {
      "questionId": "mcq_001",
      "version": 4,
      "updates": {
        "question_text": "Which of the following best describes ...?",
        "correct_option_index": 2,
        "blooms_level": "Apply",
        "relevance_percentage": 88,
        "answer_rationale.correct_answer_explanation": "Option C is correct because ...",
        "reasoning.learning_objective_alignment": "LO-3: Evaluate policy instruments",
        "reasoning.competency_alignment.kcm.competency_area": "Behavioural",
        "reasoning.competency_alignment.kcm.competency_theme": "Outcome Orientation",
        "reasoning.competency_alignment.kcm.competency_sub_theme": "Accountability",
        "course_name": "Foundations of Public Policy"
      }
    }
  }'
```

| Field | Required | Description |
|---|---|---|
| `questionId` | Yes | Identifier of the question to edit. Missing, non-string or blank returns 400 `question_id_required` / `question_id_invalid`. An id naming no question in this assessment returns **404** `question_not_found`. |
| `updates` | Yes | Field updates keyed by dotted path (see below). |
| `version` | No (recommended) | Version this edit is based on. |

#### Editable paths

| Path | Applies to |
|---|---|
| `question_text` | All except MTF |
| `matching_context` | MTF only |
| `options` | MCQ, Multi-Choice — full replacement list, at least 2 items of `{text, index}` (no ceiling on edit) |
| `correct_option_index` | MCQ (integer), Multi-Choice (array of integers) |
| `correct_answer` | FTB (text), True/False (`"True"` / `"False"`) |
| `pairs` | MTF — full replacement list of `{left, right}` |
| `answer_rationale.correct_answer_explanation` · `.why_factor` · `.logic_justification` | All |
| `blooms_level` | All |
| `relevance_percentage` | All |
| `difficulty_level` | All |
| `reasoning.learning_objective_alignment` | All |
| `reasoning.competency_alignment.kcm.competency_area` · `.competency_theme` · `.competency_sub_theme` | All |
| `reasoning.competency_alignment.domain` | All |
| `course_name` | All |

Any other path returns 400 `field_not_editable`. `question_id`, `question_type` and `provenance` are server-owned: sending them is harmless (clients naturally echo the whole object back) but they are ignored.

> **Changing a question's type is not supported in this release.** Sending `question_type` does not retype the question.

#### Response

```json
{
  "code": "saved",
  "status": "COMPLETED",
  "job_id": "...",
  "version": 5,
  "question_order": ["mcq_001", "q_a1b2c3d4e5f6", "ftb_001"],
  "total_questions": 3,
  "question": { "...": "the saved question, including its updated provenance" }
}
```

The write is confirmed by the database before this returns, so a successful response
means the change is durable. If every submitted value already matched what was stored,
`code` is `"no_changes"` and the version does not move.

Take `version` from every response and use it on your next call. There is no `alerts`
array and no `announcement` — the impact of the change and anything read aloud are
[yours to produce](#what-the-client-owns).

---

### 4. Add a Question

**`POST /questions/create/{job_id}`** → **201 Created**

The reviewer authors the question in full. No AI generation is involved. The server assigns the `question_id` and marks it `human_authored`.

```bash
curl --location \
  'https://portal.uat.karmayogibharat.net/api/ai/assessments/v1/questions/create/<job_id>' \
  --header 'x-authenticated-user-token: <keycloak_jwt>' \
  --header 'Authorization: bearer <kong_jwt_credential>' \
  --header 'Content-Type: application/json' \
  --data '{
    "request": {
      "version": 5,
      "questionType": "mcq",
      "position": 2,
      "question": {
        "question_text": "Which body approves the Union Budget?",
        "options": [
          { "text": "Parliament", "index": 0 },
          { "text": "NITI Aayog", "index": 1 },
          { "text": "RBI",        "index": 2 },
          { "text": "Cabinet Secretariat", "index": 3 }
        ],
        "correct_option_index": 0,
        "blooms_level": "Remember",
        "relevance_percentage": 85,
        "difficulty_level": "intermediate",
        "course_name": "Foundations of Public Policy",
        "answer_rationale": {
          "correct_answer_explanation": "Parliament approves the Union Budget.",
          "why_factor": "Constitutional mandate",
          "logic_justification": "Article 112"
        },
        "reasoning": {
          "learning_objective_alignment": "LO-1: Identify budget authorities",
          "competency_alignment": { "kcm": {
            "competency_area": "Functional",
            "competency_theme": "Public Finance",
            "competency_sub_theme": "Budgeting"
          }}
        }
      }
    }
  }'
```

| Field | Required | Description |
|---|---|---|
| `questionType` | Yes | `mcq`, `ftb`, `mtf`, `multichoice`, `truefalse` |
| `question` | Yes | The authored question. Accepts the same fields as the editable-path table above (nested, not dotted). |
| `position` | No | 1-based position in the sequence. Omit to append at the end. |
| `version` | No (recommended) | Version this add is based on. |

The response mirrors the edit response and adds `question_id` for the new question.

---

### 5. Delete a Question

**`POST /questions/delete/{job_id}`**

The last remaining question cannot be deleted. This is a `POST`, not a `DELETE`, because `questionId` is a body field and a `DELETE` must not carry a body.

**Confirm in your own UI, then call this once.** There is no `confirm` flag and no
`dry_run`: both were server-side stand-ins for a dialog, and a flag checked here stopped
nothing because any caller that wanted the deletion simply set it. This call deletes.

```bash
curl --location '.../questions/delete/<job_id>' \
  --header 'x-authenticated-user-token: <keycloak_jwt>' \
  --header 'Content-Type: application/json' \
  --data '{ "request": { "questionId": "mcq_001", "version": 6 } }'
```

| Field | In | Description |
|---|---|---|
| `questionId` | body | Required. Missing, non-string or blank returns 400 `question_id_required` / `question_id_invalid`. |
| `version` | body | Version this delete is based on. |

Deleting the only remaining question returns 400 `last_question_cannot_be_deleted` — check `can_delete` (see [Building the editor list](#building-the-editor-list)) to disable the control instead of discovering this from a rejected call. A `questionId` naming no question in this assessment returns **404** `question_not_found`.

---

### 6. Reorder Questions

**`POST /questions/order/{job_id}`**

Send the complete new sequence. There is one input form: a single-question move
("this one, one step up") is expressed by sending the sequence it produces, because
you hold the whole array and the move is a splice.

```bash
curl --location '.../questions/order/<job_id>' \
  --header 'Content-Type: application/json' \
  --data '{ "request": { "version": 6,
            "questionOrder": ["ftb_001", "mcq_001", "q_a1b2c3d4e5f6"] } }'
```

`questionOrder` must list every question in the assessment exactly once. A partial, padded or duplicated list returns 400 `question_order_invalid` (with `missing`, `unknown` and `duplicated` in `params`) rather than being partially applied — a stale client cannot drop questions by sending an out-of-date array. Omitting it entirely returns 400 `question_order_required`.

**Reorder locally, save once.** Let the user shuffle freely and re-render optimistically,
then send the final sequence when they settle. One call per drag session, not one per
move — each call is a version bump and a round trip.

Unlike the rest of the editing workspace, this one genuinely cannot move client-side:
`question_order` is what every export reads, each moved question gets an audit row, and
the permutation check above is precisely a guard against the client being stale.

#### Response

```json
{
  "code": "saved",
  "status": "COMPLETED",
  "job_id": "...",
  "version": 7,
  "question_order": ["ftb_001", "mcq_001", "q_a1b2c3d4e5f6"],
  "total_questions": 3
}
```

If the requested order matches the current one, `code` is `"order_unchanged"` and the version does not move. Compose any screen-reader announcement from the returned `question_order` — see [What the client owns](#what-the-client-owns).

---

### 7. Get Audit Trail

**`GET /audit/{job_id}`**

Every human change to the assessment, oldest first. Owner-only, the same access rule as the rest of the editing endpoints.

```bash
curl --location '.../audit/<job_id>?limit=200&offset=0' \
  --header 'x-authenticated-user-token: <keycloak_jwt>'
```

#### Response

```json
{
  "job_id": "...",
  "version": 7,
  "edited_at": "2026-08-27T10:14:02.113000",
  "count": 3,
  "audit_trail": [
    {
      "id": 41,
      "assessment_version": 5,
      "event_code": "TEL-03",
      "editor_id": "1e8b6826-3326-4175-b202-f5f5971f457a",
      "question_id": "mcq_001",
      "question_type": "mcq",
      "previous_position": 1,
      "new_position": 1,
      "changed_fields": [
        { "field": "correct_option_index", "previous_value": 0, "new_value": 2 }
      ],
      "original_question": { "...": "the question exactly as the AI generated it" },
      "question_snapshot": { "...": "the question after this change" },
      "details": { "provenance_before": "ai_generated", "provenance_after": "ai_assisted",
                   "answer_key_changed": true },
      "created_at": "2026-08-27T10:14:02.113000"
    }
  ],
  "ai_original": { "...": "the complete pristine AI-generated assessment" }
}
```

| Field | Description |
|---|---|
| `event_code` | `TEL-03` Question Edit Saved · `TEL-05` Question Added · `TEL-06` Question Deleted · `TEL-07` Question Reordered · `TEL-10` Correct Answer Changed · `TEL-11` Mapping Updated. The code is the whole fact — there is no display-name field, because the wording is [yours](#what-the-client-owns). |
| `editor_id` | The user who made the change |
| `changed_fields` | Each changed field with its previous and new value |
| `original_question` | The AI-generated question, captured on the first human edit. `null` for human-authored questions. |
| `assessment_version` | The version this change produced |
| `ai_original` | The complete pristine AI-generated assessment, retained regardless of later edits |

A reorder that shifts several questions produces one row per question whose position changed, all sharing the same `assessment_version`.

A single edit can produce several rows. Changing an answer key writes Question Edit Saved **and** Correct Answer Changed; changing a mapping writes Question Edit Saved **and** Mapping Updated. All rows from one save share an `assessment_version`, so they can be grouped back into a single reviewer action.

These six audit feeds are the only record kept of a reviewer's activity. Nothing else about what a user does in the editor is tracked.

---

### 8. Update Whole Assessment (legacy)

**`PUT /update/{job_id}`**

Replaces the entire `assessment_data` payload. Retained for backward compatibility and now subject to the same rules as the granular endpoints: the payload is validated, the change is versioned, and the difference against the stored copy is written to the audit trail.

```bash
curl --location --request PUT '.../update/<job_id>' \
  --header 'Content-Type: application/json' \
  --data '{ "version": 7, "assessment_data": { "blueprint": {}, "question_order": [], "questions": {} } }'
```

**Prefer endpoints 4–7.** This path can only report the observable difference between two payloads, so it records less than the granular endpoints: an edit that restores a previous value is invisible to it, and it cannot distinguish a deliberate reorder from positions shifting because of an add. It also forces provenance back to the stored value, so a client cannot relabel a question by rewriting the blob.

#### Response

```json
{
  "message": "Assessment updated successfully",
  "status": "COMPLETED",
  "job_id": "...",
  "version": 8,
  "question_order": ["..."],
  "total_questions": 3,
  "changes_recorded": 2
}
```

---

### 9. Get History

**`GET /history`**

Returns all assessments previously generated or cloned by the authenticated user, sorted by most recent first.

#### Kong

```bash
curl --location 'https://portal.uat.karmayogibharat.net/api/ai/assessments/v1/history' \
  --header 'x-authenticated-user-token: <keycloak_jwt>' \
  --header 'Authorization: bearer <kong_jwt_credential>'
```

#### UI Proxy

```bash
curl --location 'https://portal.uat.karmayogibharat.net/apis/proxies/v8/ai/assessments/v1/history' \
  --header 'cookie: connect.sid=<session_cookie>'
```

#### Sample Response

```json
[
  {
    "job_id": "do_113948972799877120197_7fa321bd_1e8b6826-3326-4175-b202-f5f5971f457a",
    "status": "COMPLETED",
    "created_at": "2026-05-08T17:04:31.793723",
    "updated_at": "2026-05-08T20:38:12.732428",
    "course_ids": ["do_113948972799877120197"],
    "course_names": ["Foundations of Public Policy"],
    "config": {
      "language": "english",
      "difficulty": "beginner",
      "time_limit": 0,
      "assessment_type": "practice",
      "total_questions": 5,
      "course_weightage": null,
      "question_type_counts": {
        "ftb": 5,
        "mcq": 5,
        "mtf": 0,
        "truefalse": 0,
        "multichoice": 0
      },
      "topic_names": ["Public Policy Fundamentals"],
      "blooms_config": {"Remember": 20, "Understand": 30, "Apply": 30, "Analyze": 10, "Evaluate": 10, "Create": 0},
      "enable_blooms": true,
      "additional_instructions": null
    },
    "error_message": null,
    "version": 8,
    "edited": true,
    "edited_at": "2026-08-27T10:14:02.113000"
  }
]
```

#### Response Fields

| Field | Description |
|---|---|
| `job_id` | Unique assessment ID. Use this to call Status or Download. |
| `status` | `PENDING`, `IN_PROGRESS`, `COMPLETED`, or `FAILED` |
| `created_at` | ISO 8601 timestamp when the job was first created |
| `updated_at` | ISO 8601 timestamp of the last status change |
| `course_ids` | List of iGOT course IDs the assessment was generated from. |
| `course_names` | List of course names corresponding to `course_ids`. Multiple entries for comprehensive assessments. |
| `config` | The parameters used when this assessment was generated. Includes: `language`, `difficulty`, `assessment_type`, `total_questions`, `question_type_counts`, `time_limit`, `course_weightage`, `competency_area`, `competency_themes`, `competency_sub_themes`, `topic_names`, `blooms_config`, `enable_blooms`, `additional_instructions`. |
| `error_message` | `null` on success. Error detail if `status` is `FAILED`. |
| `version` | Assessment version. `1` means generated but never edited. |
| `edited` | `true` if a reviewer has saved at least one change. Use it to badge reviewed assessments in a listing without fetching each audit trail. |
| `edited_at` | Timestamp of the first human edit, or `null`. |

---

### 10. Download Assessment

**`GET /download/{job_id}?format={format}`**

Downloads a completed assessment in the requested file format. Only available when `status` is `COMPLETED`.

**Every format is built from the persisted final assessment** — edited questions, manually added questions, deletions and the saved `question_order` are all reflected, and never reconstructed from the original AI-generation payload. All formats present questions in the same sequence.

PDF and DOCX list questions as one ordered sequence (rather than grouped under per-type headings, as before) so their numbering matches CSV and JSON. Each question shows its type as a label and its provenance alongside the reasoning.

#### Kong

```bash
# CSV (full — with QuestionTagging, all question types)
curl --location 'https://portal.uat.karmayogibharat.net/api/ai/assessments/v1/download/<job_id>?format=csv' \
  --header 'x-authenticated-user-token: <keycloak_jwt>' \
  --header 'Authorization: bearer <kong_jwt_credential>' \
  --output assessment.csv

# CSV Basic (MCQ only, SR/Question/Option columns, TRUE/FALSE correctness, max 6 options)
curl --location 'https://portal.uat.karmayogibharat.net/api/ai/assessments/v1/download/<job_id>?format=csv_basic' \
  --header 'x-authenticated-user-token: <keycloak_jwt>' \
  --header 'Authorization: bearer <kong_jwt_credential>' \
  --output assessment_basic.csv

# JSON
curl ... ?format=json --output assessment.json

# PDF
curl ... ?format=pdf --output assessment.pdf

# DOCX
curl ... ?format=docx --output assessment.docx
```

#### UI Proxy

```bash
curl --location 'https://portal.uat.karmayogibharat.net/apis/proxies/v8/ai/assessments/v1/download/<job_id>?format=csv' \
  --header 'cookie: connect.sid=<session_cookie>' \
  --output assessment.csv
```

#### Query Parameters

| Parameter | Required | Accepted Values | Description |
|---|---|---|---|
| `format` | Yes | `csv`, `csv_basic`, `json`, `pdf`, `docx` | Output file format. `csv_basic` includes MCQ questions only (single and multi-answer), with columns `SR`, `Question`, `Option1`–`Option6`, `IsOption1Correct`–`IsOption6Correct`. Correctness values are `TRUE`/`FALSE`. No `QuestionType` or `QuestionTagging` columns. |

#### Response

Returns the file as a binary stream with the appropriate `Content-Type` header. The filename in `Content-Disposition` will be `<job_id>_assessment.<format>`.

---

## End-to-End Integration Flow

### Flow 1: Generate a New Assessment

This is the primary flow. The generation is asynchronous — you submit a job and poll until done.

```
1. POST /generate          → receive job_id + status (PENDING or COMPLETED)
                                        │
                    ┌───────────────────┴───────────────────┐
                    │ status == COMPLETED                    │ status == PENDING / IN_PROGRESS
                    │ (cache hit)                            │
                    ▼                                        ▼
           Use result directly               2. GET /status/{job_id}   ◄──┐
                                                      │                    │
                                         ┌────────────┴──────────┐        │
                                         │ COMPLETED              │ still  │
                                         ▼                        │ waiting├──┘
                                   Show assessment            wait 3–5s,
                                                              poll again
```

**Recommended polling interval:** 3–5 seconds. Most generations complete within 30–90 seconds depending on course size and question count.

Requests asking for more than 25 questions (the default batch-size threshold) are split into several parallel generation calls and merged before saving. This batched path can have different timing characteristics than the figures above, so keep polling until `status` is `COMPLETED` rather than assuming a fixed duration.

**Implementation example (pseudocode):**

```js
// Step 1: Submit
const { job_id, status, result } = await POST('/generate', formData);

if (status === 'COMPLETED') {
  showAssessment(result);
  return;
}

// Step 2: Poll
let assessment = null;
while (!assessment) {
  await sleep(4000);
  const resp = await GET(`/status/${job_id}`);
  if (resp.status === 'COMPLETED') {
    assessment = resp.assessment_data;
  } else if (resp.status === 'FAILED') {
    showError(resp.error_message);
    return;
  }
}

showAssessment(assessment);
```

---

### Flow 2: Show Assessment History

Use this to show a user's previously generated assessments. Each history item has a `job_id` which can be used to re-fetch the full assessment data or download it.

```
1. GET /history                        → list of past jobs with job_id + config + status
2. User selects a COMPLETED job
3. GET /status/{job_id}                → full assessment_data for that job
   OR
   GET /download/{job_id}?format=json  → download as file
```

**Implementation example (pseudocode):**

```js
// Show history list
const history = await GET('/history');
renderHistoryList(history);  // show job_id, created_at, config, status

// On user click — load full assessment
const selected = history[i];
if (selected.status === 'COMPLETED') {
  const detail = await GET(`/status/${selected.job_id}`);
  showAssessment(detail.assessment_data);
}
```

---

### Flow 3: Review and Edit an Assessment

The reviewer composes the final question set in the platform. Downloads and publication then use exactly what was saved.

```
1. GET  /status/{job_id}                     → assessment_data + version
        └─ editorList(assessment_data)       → ordered, annotated questions  [client-side]
                                                      │
2. User edits question N                              │
   ├─ diff local before/after                         │  [client-side]
   │     → impact warnings, confirmation dialog        │   no API call
   │                                                  │
   └─ POST /questions/update/{job_id}                 │
      { request: { questionId, version, updates } }   │
                    ┌─────────────────────────────────┴────────────────┐
                    │ 200                     │ 400              │ 409 │
                    ▼                         ▼                  ▼
          durable — keep version       render errors[]     reload, re-apply
                                       via code+params      nothing saved
                    │
3. Add / delete / reorder as needed — each returns the new version
                    │
4. GET /download/{job_id}?format=csv        → reflects every saved change
5. GET /audit/{job_id}                      → who changed what, and when
```

**Implementation example (pseudocode):**

```js
// 1. Load once; the editor list is a local projection (see "Building the editor list")
let { assessment_data, version } = await GET(`/status/${jobId}`);
let questions = editorList(assessment_data);

// 2. Confirm locally — you already hold the before-state, so no preview call
const before = questions.find((q) => q.question_id === qid);
const updates = { question_text: newText, correct_option_index: newIndex };
if (!await confirmWithUser(impactOf(before, updates))) return;   // cancel = do nothing

// 3. Save
const res = await POST(`/questions/update/${jobId}`,
                       { request: { questionId: qid, updates, version } });

if (res.status === 409) {
  // someone else saved first — nothing was written
  ({ assessment_data, version } = await GET(`/status/${jobId}`));
  questions = editorList(assessment_data);
  return showConflict();
}
if (res.status === 400) {
  // code + params -> your own localized copy
  return showErrors(res.errors.map((e) => t(e.code, e.params)));
}

version = res.version;                  // carry the new version into the next edit
toast(t("saved"));                      // your copy, your language
announce(t("saved_position", { total: res.total_questions }));   // ARIA live region
```

`impactOf(before, updates)` is the client-side replacement for the old `dry_run` call.
The one case worth implementing carefully: if the option texts before and after are the
same multiset in a different order, this is a **reorder**, so say "options reordered" —
not "the correct answer will change", even though `correct_option_index` differs.

Cancelling an edit needs no API call to undo anything — discard local state and re-render from the copy you hold. The editor makes no calls the user did not ask for.

---

## Status Reference

| Status | Meaning |
|---|---|
| `PENDING` | Job is queued, worker has not picked it up yet |
| `IN_PROGRESS` | Worker is actively generating questions |
| `COMPLETED` | Generation finished, `assessment_data` is available |
| `FAILED` | Generation failed, see `error_message` for details |

---

## Error Responses

| HTTP Code | Meaning |
|---|---|
| `400` | Bad request — invalid parameter (e.g. unknown question type, invalid JSON) |
| `401` | Missing or invalid authentication token / session |
| `403` | Authenticated but not authorized — either missing required role or trying to access another user's assessment |
| `404` | Job not found, assessment not yet completed, or unknown `question_id` |
| `409` | **Concurrent update detected** — the assessment changed since you read it, or it is not `COMPLETED` yet and cannot be edited. Nothing was written. Response includes `current_version`. |
| `500` | Internal server error |

**Error response format:**

```json
{
  "detail": "Human-readable error message"
}
```

That prose shape applies to generation, status and download. **Every failure on the
editing endpoints** — `/questions/*`, `/audit`, `/update` — returns the machine-readable
shape instead, not just 400s. `detail` is the primary error's `code`, and `errors` carries
the detail:

| `code` | HTTP | Meaning |
|---|---|---|
| `assessment_not_found` | 404 | No assessment with that `job_id` |
| `assessment_access_denied` | 403 | Authenticated, but not the owner |
| `assessment_not_editable` | 409 | Generation has not finished — `params.status` holds the current status |
| `version_conflict` | 409 | Changed since you read it — `params.current_version` |
| `if_match_invalid` | 400 | `If-Match` was not an integer version |

```json
{
  "detail": "option_count_invalid",
  "errors": [
    { "code": "option_count_invalid", "field": "options", "question_id": "mcq_001",
      "params": { "minimum": 2, "maximum": 5, "found": 1 } }
  ]
}
```

`errors[]` entries carry **no `message`** — map `code` to your own copy and interpolate
`params`. See [Validation](#validation) for the full code/params table.

---

## Notes

- **Job ID format:** `{course_id}_{param_hash}_{user_id}`. The same user requesting the same course+config will always get the same job ID — cache is automatic.
- **Ownership:** All assessments are user-scoped. Status and Download will return `403` if called with a different user's credentials.
- **Force regeneration:** Pass `force=true` in the Generate request to bypass cache and generate fresh questions.
- **Multi-course assessments:** Pass multiple `course_ids` and set `assessment_type=comprehensive`. Use `course_weightage` to control the proportion of questions per course.
- **Question order is data, not presentation.** `assessment_data.question_order` is the authoritative sequence. Do not infer order from the type buckets — they only store content.
- **Always send `version` on editing calls.** Without it a stale screen can silently overwrite someone else's change; with it the call fails cleanly with 409.
- **A clone is never another reviewer's edits.** A `/generate` cache hit may match an assessment that has since been edited, but what it clones is the pristine AI-generated copy retained for audit — so another user's reviewer edits are never handed to you as fresh AI output.
- **Editing requires `COMPLETED` status.** Editing calls against a `PENDING`, `IN_PROGRESS` or `FAILED` job return 409.
- **The editing API returns no user-facing copy.** No sentence on errors, no `message` on successful writes (they carry an outcome `code` instead), no `alerts`, no `announcement`, and no display name on audit rows. Impact warnings, confirmation dialogs, screen-reader text and error wording are all client-side — see [What the client owns](#what-the-client-owns).
- **`correct_option_index` is an option's `index` value, not its array position.** Resolve it by matching on the value.
