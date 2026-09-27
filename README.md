# The Smoothie Diet Daily — Auto Publisher

Every trigger (cron-job.org calls this repo's `workflow_dispatch` twice a
day — 6:30 AM and 6:30 PM PKT, same job both times), this script:

- Picks an article type: ~60% free recipe (soft CTA) / ~40% review-style
  article about smoothie weight-management plans in general (stronger CTA)
- Writes the article via Gemini (6-model fallback chain, same as DecorVibe)
- Publishes it to Blogger, with the affiliate disclosure + a ClickBank CTA
- Builds a short AI-narrated vertical video from the article's images and
  posts it as a Pinterest **video** Pin — every run, no image/video split
- Posts the hero image + caption to Tumblr — every run

This reuses the exact same tested infrastructure as the DecorVibe repo
(Cloudflare R2 hosting, Blogger/Pinterest OAuth refresh-token flows, the
ffmpeg Ken Burns + edge-tts video builder, retry logic) — just with new
content rules (no numeric weight-loss claims, mandatory disclosure) and a
different publishing pattern.

## One-time setup

### 1. Get a Gemini API key
Use the **new, separate** Google AI Studio key already created for this
project (free tier) — don't reuse DecorVibe's, to avoid quota clashes.

### 2. Enable Blogger API + create a Desktop OAuth client
Same as DecorVibe:
1. Google Cloud Console → select/create a project (can be the same one the
   Gemini key lives in, or a separate one — doesn't matter for Blogger).
2. APIs & Services → Library → enable **Blogger API v3**.
3. APIs & Services → OAuth consent screen → set it up (External, add your
   own email as a test user if it stays in "Testing" mode).
4. APIs & Services → Credentials → Create Credentials → OAuth client ID →
   **Desktop app**. Copy the Client ID and Client Secret.
5. Get a refresh token (one-time, on your own computer, browser-based
   consent flow) — ask Claude for the `get_blogger_token.py` script if you
   don't have one yet.

### 3. Get your Blogger Blog ID
Blogger dashboard → Settings → Blog ID (numeric).

### 4. Pinterest OAuth (App ID / Secret / Refresh Token)
Once **Standard access** is approved for the "Smoothie Diet Daily Publisher"
app:
1. Do the OAuth consent flow once (same shape as the Tumblr script) to get
   a `refresh_token` — Pinterest's access tokens expire in 30 days, but the
   refresh token is long-lived and the script re-derives an access token
   from it on every run, so this never needs redoing manually.
2. `PINTEREST_APP_ID` / `PINTEREST_APP_SECRET` come from the app's page.
3. `PINTEREST_BOARD_ID` — open your board on pinterest.com, or call
   `GET /v5/boards` with a valid access token.

### 5. Cloudflare R2 (image/video hosting)
Free tier (10 GB). Create a bucket, an API token with read/write access,
and make the bucket's contents public (or use a custom domain) so
Blogger/Pinterest/Tumblr can all fetch the URLs directly.

### 6. Repo secrets
Settings → Secrets and variables → Actions → New repository secret. Add:

| Secret | Notes |
|---|---|
| `GEMINI_API_KEY` | new key, step 1 |
| `PEXELS_API_KEY` | can reuse DecorVibe's |
| `BLOGGER_BLOG_ID` | step 3 |
| `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` / `GOOGLE_REFRESH_TOKEN` | step 2 |
| `PINTEREST_APP_ID` / `PINTEREST_APP_SECRET` / `PINTEREST_REFRESH_TOKEN` | step 4 |
| `PINTEREST_BOARD_ID` | step 4 |
| `TUMBLR_CONSUMER_KEY` / `TUMBLR_CONSUMER_SECRET` / `TUMBLR_OAUTH_TOKEN` / `TUMBLR_OAUTH_TOKEN_SECRET` | already generated ✅ |
| `TUMBLR_BLOG_IDENTIFIER` | `thesmoothiedietdaily.tumblr.com` |
| `AFFILIATE_LINK` | your ClickBank HopLink |
| `R2_ACCOUNT_ID` / `R2_ACCESS_KEY_ID` / `R2_SECRET_ACCESS_KEY` / `R2_BUCKET_NAME` / `R2_PUBLIC_URL` | step 5 |
| `GH_SECRETS_PAT` | optional — fine-grained PAT, "Secrets: read and write", scoped to this repo only, so a rotated Pinterest refresh token auto-updates itself |
| `GMAIL_ADDRESS` / `GMAIL_APP_PASSWORD` / `NOTIFY_EMAIL` | optional — phone notification after each run |

### 7. Add a royalty-free music track (optional)
Drop an MP3 into `/music/` — used as quiet background music under the
voiceover. Silent video if the folder is empty.

### 8. cron-job.org
Same pattern as DecorVibe:
```
POST https://api.github.com/repos/<you>/<repo>/actions/workflows/auto-blog.yml/dispatches
```
Header: `Authorization: token <a GitHub PAT with repo scope>`
Body: `{"ref": "main"}` — no `run_type` needed, every run does everything.
Set up two cron jobs (6:30 AM and 6:30 PM PKT) pointing at the same URL.

### 9. First test run
Trigger manually from the **Actions** tab first (Run workflow button)
before relying on cron-job.org, so you can check the Blogger post, the
Pinterest video pin, and the Tumblr post all look right end to end.

## Compliance notes (read before scaling up)
- Every post carries the affiliate disclosure automatically — don't remove
  it from `AFFILIATE_DISCLOSURE` in `auto_blog.py`.
- The Gemini prompt hard-forbids numeric weight-loss claims, guarantee
  language, and before/after framing — these rules live in `generate_draft()`.
- Keep publish frequency at this repo's default (2x/day) for the first few
  weeks — Google treats health/weight-loss content ("YMYL") with more
  scrutiny than a decor blog.
- `history.json` grows automatically so Gemini avoids repeating topics —
  don't delete it.
