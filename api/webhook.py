from http.server import BaseHTTPRequestHandler
import json
import os
import anthropic
import requests

ALLOWED_ASSISTANT_IDS = {
    "b6a189bb-258b-40db-9182-8a292adb72b8",  # tester assistant
    "64d60896-3502-45a7-9c5e-da3db60ffb02",  # 1.0.4
}
SLACK_CHANNEL = "astra-call-snippets"
MIN_DURATION_SECONDS = 240


def call_duration_seconds(call):
    s, e = call.get("startedAt"), call.get("endedAt")
    if not s or not e:
        return 0
    try:
        from datetime import datetime
        p = lambda t: datetime.fromisoformat(t.replace("Z", "+00:00"))
        return (p(e) - p(s)).total_seconds()
    except Exception:
        return 0


def get_named_output(call, name):
    outputs = call.get("artifact", {}).get("structuredOutputs") or {}
    for _id, output in outputs.items():
        if output.get("name") == name:
            return output.get("result") or {}
    return {}


def get_transcript(call):
    messages = call.get("artifact", {}).get("messages") or []
    lines = [
        f"{m.get('role')}: {m.get('message','').strip()}"
        for m in messages
        if m.get("role") in ("bot", "user") and m.get("message", "").strip()
    ]
    return "\n".join(lines) or "(no transcript available)"


def generate_reflection(call):
    persona = get_named_output(call, "astra_user_persona_v3")
    feedback = get_named_output(call, "astra_feedback_v2")
    transcript = get_transcript(call)

    from datetime import datetime
    started = call.get("startedAt", "")
    date_str = ""
    if started:
        try:
            date_str = datetime.fromisoformat(started.replace("Z", "+00:00")).strftime("%Y-%m-%d %H:%M UTC")
        except Exception:
            date_str = started

    duration_s = int(call_duration_seconds(call))
    assistant_id = call.get("assistantId", "")
    version = "tester" if assistant_id == "b6a189bb-258b-40db-9182-8a292adb72b8" else "1.0.4"

    metadata = feedback.get("metadata") or {}
    questions_answered = int(metadata.get("questions_answered") or 0)
    feedback_note = (
        "\n⚠️ Post-call survey not completed — feedback metrics are unavailable and must not be interpreted as real scores."
        if questions_answered == 0 else ""
    )

    context = f"""Call date: {date_str}
Duration: {duration_s // 60}m {duration_s % 60}s
Assistant version: {version}{feedback_note}

=== STRUCTURED OUTPUTS ===
Persona: {json.dumps(persona, indent=2)}
Feedback: {json.dumps(feedback, indent=2)}

=== TRANSCRIPT ===
{transcript}"""

    prompt = f"""You are analyzing a transcript of a coaching call. Your job is to extract critical feedback insights from the user's (coachee's) perspective.

Focus only on moments where the user:
- Expressed friction, confusion, or discomfort
- Deflected, went quiet, or gave evasive answers
- Shifted the conversation themselves to something that worked better
- Gave explicit or implicit signals that something wasn't landing

For each friction insight, output:

*Friction point:* [What happened, in one sentence]
*Signal:* [The specific user behavior or language that reveals it]
*Implication:* [What this suggests about the product/experience]

---

Also identify moments where the user:
- Became more open, engaged, or forthcoming
- Responded positively to a specific question or approach
- Shifted from guarded to collaborative
- Gave explicit or implicit signals that something was working

For each positive insight, output:

*What worked:* [What happened, in one sentence]
*Signal:* [The specific user behavior or language that reveals it]
*Implication:* [What this suggests about the product/experience]

---

Rules:
- Do not infer intent beyond what the transcript supports
- Ignore technical issues unless they caused behavioral changes in the user
- Maximum 5 insights per section. If there are fewer real ones, report fewer. Do not pad.
- Be blunt. These insights are for a product team, not the coach.
- Do not include any names — refer to the user as "the user" and Astra as "Astra"
- Use Slack mrkdwn formatting: *bold* for labels, plain text for content
- If the context says the post-call survey was not completed, do not reference or interpret any feedback metric scores — note clearly that post-call data is unavailable
- Individual feedback fields that are null, empty, or contain placeholder-like responses should be treated as unanswered and ignored entirely

*Overall signal:* [One sentence on the friction/engagement balance of this call]

{context}"""

    claude = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    response = claude.messages.create(
        model="claude-opus-4-7",
        max_tokens=1500,
        thinking={"type": "adaptive"},
        messages=[{"role": "user", "content": prompt}],
    )
    return next((b.text.strip() for b in response.content if b.type == "text"), "")


def post_to_slack(call, reflection):
    from datetime import datetime
    token = os.environ["SLACK_BOT_TOKEN"]
    headers = {"Authorization": f"Bearer {token}"}

    channels = requests.get(
        "https://slack.com/api/conversations.list",
        headers=headers, params={"limit": 200}, timeout=15
    ).json().get("channels", [])
    channel_id = next((c["id"] for c in channels if c["name"] == SLACK_CHANNEL), None)
    if not channel_id:
        raise RuntimeError(f"Channel #{SLACK_CHANNEL} not found")

    started = call.get("startedAt", "")
    date_str = ""
    if started:
        try:
            date_str = datetime.fromisoformat(started.replace("Z", "+00:00")).strftime("%Y-%m-%d %H:%M UTC")
        except Exception:
            date_str = started

    duration_s = int(call_duration_seconds(call))
    assistant_id = call.get("assistantId", "")
    version = "tester" if assistant_id == "b6a189bb-258b-40db-9182-8a292adb72b8" else "1.0.4"
    call_id = call.get("id", "")
    recording_url = call.get("recordingUrl") or call.get("artifact", {}).get("recordingUrl", "")
    recording_line = f" | <{recording_url}|Recording>" if recording_url else ""

    message = (
        f"*Call Reflection — {date_str}*\n"
        f"Version: `{version}` | Duration: {duration_s // 60}m {duration_s % 60}s{recording_line}\n"
        f"Call ID: `{call_id}`\n\n"
        + reflection
    )

    result = requests.post(
        "https://slack.com/api/chat.postMessage",
        headers={**headers, "Content-Type": "application/json"},
        json={"channel": channel_id, "text": message, "mrkdwn": True},
        timeout=15,
    ).json()
    if not result.get("ok"):
        raise RuntimeError(f"Slack error: {result.get('error')}")


class handler(BaseHTTPRequestHandler):
    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length))

            call = body if "id" in body else body.get("call", body)
            assistant_id = call.get("assistantId", "")
            status = call.get("status", "")

            if assistant_id not in ALLOWED_ASSISTANT_IDS or status != "ended":
                self._respond(200, {"status": "ignored"})
                return

            if call_duration_seconds(call) < MIN_DURATION_SECONDS:
                self._respond(200, {"status": "too_short"})
                return

            reflection = generate_reflection(call)
            post_to_slack(call, reflection)
            self._respond(200, {"status": "ok"})

        except Exception as e:
            self._respond(500, {"error": str(e)})

    def do_GET(self):
        self._respond(200, {"status": "astra-reflections webhook is running"})

    def _respond(self, code, body):
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(body).encode())

    def log_message(self, format, *args):
        pass
