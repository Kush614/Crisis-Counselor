# Reaching the Crisis Counselor — 3 channels

All three share the same brain. **Voice** is the Pipecat bot (`crisis-bot.py`).
**SMS** and **iMessage** are the backend's `/sms` and `/imessage` webhooks, both
running on the 988 crisis LLM with per-sender memory.

Common prerequisite for SMS + iMessage: a **public URL** so the provider can
reach your local backend. Start the backend, then tunnel it:
```bash
cd CrisisLineAI/backend
uvicorn main:app --host 0.0.0.0 --port 1000
# in another terminal:
ngrok http 1000          # -> https://XXduck.ngrok-free.app  (your PUBLIC base)
```

---

## 1. SMS (text it) — green bubble, works on any phone
Status: **built** (`POST /sms`). Needs a Twilio number.

1. Twilio Console → buy a phone number with **SMS** capability.
2. That number → **Messaging** → "A message comes in" → **Webhook**,
   `https://<your-ngrok>/sms`, method **POST**.
3. Text the number. The backend replies via TwiML. Conversation memory is keyed
   to your phone number, so it remembers the thread.

No outbound API key needed — the reply rides back on Twilio's webhook response.

---

## 2. Voice (call it and TALK) — the real product, on-theme
Status: **bot built + Cekura agent configured**. Needs deploy + a Twilio voice number.

1. Deploy the agent to Pipecat Cloud:
   ```bash
   cd yc-voice-agents-hackathon-main/server
   pc cloud auth login
   pc cloud secrets set crisis-bot-secrets --file .env
   pc cloud deploy                      # deploys "crisis-bot" (see pcc-deploy.toml)
   pc cloud organizations list          # note <org> for the host below
   ```
2. Twilio Console → buy a number with **Voice** capability.
3. Create a **TwiML Bin**:
   ```xml
   <?xml version="1.0" encoding="UTF-8"?>
   <Response><Connect>
     <Stream url="wss://api.pipecat.daily.co/ws/twilio">
       <Parameter name="_pipecatCloudServiceHost" value="crisis-bot.<org>"/>
     </Stream>
   </Connect></Response>
   ```
4. Attach the TwiML Bin to the number → **Voice Configuration** → "A call comes in".
5. Call the number. You're talking to the counselor.

> The Cekura agent (`18038`) already points at `pipecat_agent_name=crisis-bot`,
> so once deployed, `cekura run start --mode pipecat` also works.

---

## 3. iMessage (blue bubble) — via Sendblue relay
Status: **built** (`POST /imessage`). Needs a paid Sendblue account (no Mac required).

> Apple has no native API; Sendblue (sendblue.co) runs the Macs and gives you a
> REST API + inbound webhook. ~$20/mo. This is the only way to get blue bubbles
> from a Windows box.

1. Sign up at **sendblue.co**, get **API Key ID** + **API Secret**, and register
   a sending number.
2. Put the keys in `CrisisLineAI/backend/.env`:
   ```
   SENDBLUE_API_KEY_ID=...
   SENDBLUE_API_SECRET=...
   ```
3. Sendblue dashboard → set the inbound **webhook / receive URL** to
   `https://<your-ngrok>/imessage`.
4. Restart the backend. iMessage the Sendblue number → the counselor replies in
   blue, with per-sender memory.

---

## Quick test (no phone) — confirm the brain works over HTTP
```bash
# simulate an inbound SMS
curl -X POST https://<your-ngrok>/sms \
  --data-urlencode "Body=I've been feeling really low lately" \
  --data-urlencode "From=+15555550123"
# -> <Response><Message>...empathetic reply...</Message></Response>
```
