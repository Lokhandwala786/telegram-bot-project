# Amazon UK Jobs & Shifts — Telegram Alert Bot  
## Marketing team brief (product overview + messaging prompt)

Use this document to understand what the product is, who it’s for, and how to talk about it in ads, landing pages, Telegram posts, and sales scripts.

---

## 1. Elevator pitch (30 seconds)

**A Telegram bot that watches public Amazon UK warehouse job pages and your personal “My jobs” shift screen, then pings you within seconds when new roles appear or when shift slots open — so you can apply before they fill.**

It is **not** an official Amazon product. It is an **independent alert service** that helps UK warehouse job seekers move faster than manual checking.

---

## 2. What problem does it solve?

| Pain | How we help |
|------|-------------|
| Amazon UK warehouse jobs and shifts disappear in **minutes** | Polls every ~20 seconds (faster in “drop windows”) |
| Job pages hide details until the site loads (React/SPA) | Reads the same public data the browser uses (JSON + smart parsing) |
| “No shifts available” suddenly becomes “pick your shift” | **Shift watcher** detects the change and sends a screenshot + link |
| Too many locations / wrong area | Per-user **location filter** on Telegram |
| Users miss drops while at work | **Telegram push** on phone — tap link in 2–3 seconds |

---

## 3. Product = two engines (explain separately in marketing)

### A) Job listing alerts (`app.main`)

**What it watches:** Public Amazon UK job search pages (e.g. `amazon.jobs`, optional `jobsatamazon.co.uk` job detail URLs).

**What users get:** Telegram messages when a **new or updated** warehouse-style job matches their filters (location, title keywords like warehouse / fulfilment / operative).

**Typical alert includes:** Job title, location, pay (if visible), link, UK timestamp, optional header branding.

**Who cares:** People hunting for **new warehouse job postings** in Midlands / UK centres (Coventry, Northampton, Derby, Nottingham, etc. — configurable).

---

### B) Shift slot alerts (`shift_alert`) — premium differentiator

**What it watches:** The user’s own **Amazon “My jobs”** dashboard (after they log in once with their Amazon jobs account).

**What users get:** Telegram **photo + urgent message** when the page moves from **“no shifts available”** to **actual shift slots** (schedule picker, “save and continue”, week patterns, etc.).

**Why it matters:** Many competitors only alert **new jobs**. This alerts when **shifts open for an application you already have** — the critical “select your shift” moment.

**Optional “Heads up”:** Text alert when the page changes *before* slots fully open (early warning).

**High-confidence mode:** Up to **3 consecutive photo notifications** when shifts open on the deep schedule screen (stronger phone vibration).

---

## 4. User journey (for funnel / onboarding copy)

```
Discover bot → /start
    → Welcome + list of tracked UK locations
    → "1 day free trial" + Subscribe button
    → User receives job + shift alerts during trial

1 hour before trial ends → reminder + value message (stay ahead, work smart)

20 minutes before end → paid Subscribe button + ROI message (£145–£178 induction day earnings angle)

1 minute before end → "trial ending, alerts will stop unless you subscribe"

Payment (Telegram Pay) → 30 days access (configurable)

Ongoing: /setlocation, /mute, /status, /stop
```

**Admin-only (not for mass marketing):** VIP owner account, admin panel (trial users list, paid users list, kick user, **reference grant** — free access for a specific chat ID as a special favor).

---

## 5. Business model (how to position pricing)

| Tier | Marketing angle |
|------|-----------------|
| **1-day free trial** | “Try real alerts risk-free — see jobs and shifts in your area for 24 hours.” |
| **Paid subscription** (~£4.99 / 30 days — configurable) | “Less than a coffee per week to not miss a shift that pays £145+ on your first induction day.” |
| **Reference / special access** (admin-granted) | “Invite-only extended trial for referrals” — use sparingly, creates exclusivity |

**Upsell line (approved tone):**  
*“Slots fill in minutes. A small subscription buys speed — tap the alert within seconds, stay logged into Amazon, and apply before everyone else.”*

---

## 6. Target audience

**Primary**
- UK residents applying for **Amazon warehouse / fulfilment centre** hourly roles
- People already in the Amazon jobs funnel with an active application on **jobsatamazon.co.uk**
- Shift-chasers who watch **Thursday afternoon** (or configured) drop windows

**Secondary**
- Telegram communities sharing UK logistics / warehouse hiring tips
- South Asian / UK Midlands diaspora job groups (if that’s your channel — align language: English + optional Hinglish in community posts)

**Not for**
- Amazon corporate employees (internal systems)
- Non-UK markets (product tuned to UK URLs and timezone)
- People expecting guaranteed employment (we only **alert**, we don’t place jobs)

---

## 7. Key messages (copy bank)

**Headlines**
- “UK Amazon warehouse jobs & shifts — on your phone, in seconds.”
- “Don’t refresh Amazon jobs all day. Let Telegram tell you.”
- “When ‘No shifts available’ disappears — you’ll know first.”

**Bullets**
- ⚡ Fast polling (around 20s; faster in peak windows)
- 📍 Location filters (your area only)
- 🏭 Warehouse-focused keywords (operative, fulfilment, picker, etc.)
- 📸 Shift-open screenshots when slots appear
- 🔕 Mute anytime; unsubscribe with /stop

**Trust / compliance (must include somewhere)**
- Uses **public** job pages only
- Not affiliated with Amazon
- User logs into **their own** Amazon jobs account for shift watching
- No payment to Amazon for jobs — subscription is for **our alert service** only
- Amazon fraud warning awareness: legitimate hiring never asks applicants to pay Amazon for a job

---

## 8. What NOT to claim (legal / reputation)

❌ “Official Amazon bot” or Amazon partnership  
❌ “Guaranteed job” or “guaranteed shift”  
❌ “We apply for you” or bypass CAPTCHA / security  
❌ Employee-only or internal Amazon systems  
❌ Specific hourly pay unless sourced from the live listing (pay may show “not listed”)  

✅ “Independent alert service” / “unofficial productivity tool for job seekers”

---

## 9. Competitor differentiation (talking points)

| Others often do | We do |
|-----------------|--------|
| Scrape static HTML (miss SPA job details) | Public JSON + optional browser render for rich fields |
| Only new job posts | Jobs **+** shift-open detection on My jobs |
| Generic UK job boards | Focused on **Amazon warehouse** funnel |
| Email/SMS slow | **Telegram** instant push + inline buttons |
| One-size-fits-all | Per-user location + trial → paid funnel |

---

## 10. Channels & assets marketing can produce

- Telegram channel intro post + pinned “how to start”
- 30s screen recording: notification → tap link → Amazon apply page
- Before/after: “No shifts available” → “Select your shift” (shift alert screenshot)
- Comparison carousel: manual refresh vs instant alert
- FAQ: trial length, price, mute, privacy, “not Amazon”
- Referral campaign using admin “reference grant” for influencers (chat ID whitelist)

---

## 11. Technical credibility (one paragraph for “how it works” page)

The service runs on a server that periodically checks public Amazon UK job URLs and, for subscribers who complete a one-time browser login, monitors their **My jobs** dashboard. When content changes — new listings, updated pay/location, or shift availability — matching users receive a Telegram notification with a direct link. Duplicate alerts are suppressed; subscribers can filter by location and pause alerts anytime.

---

## 12. MASTER PROMPT — paste into ChatGPT / Claude for marketing assets

Copy everything below the line into your AI tool when you need copy, ads, or landing page text.

---

```
You are a UK-focused performance copywriter for an independent Telegram alert service called "Amazon UK Jobs & Shifts Alert Bot" (working name — replace with final brand).

PRODUCT FACTS (do not invent beyond these):
- Telegram bot for UK Amazon warehouse job seekers (NOT official Amazon).
- Two features: (1) New/updated job listing alerts from public amazon.jobs and jobsatamazon pages with location/title filters. (2) Shift slot alerts: watches user's own logged-in "My jobs" page and sends urgent Telegram photo+text when "no shifts available" changes to actual shift selection UI.
- Free 1-day trial via /start and Subscribe button. Paid subscription ~£4.99 for 30 days via Telegram payment (configurable). Reminders at 1h, 20m, 1m before trial ends.
- Users must tap links within 2-3 seconds and stay logged into Amazon to apply fast.
- ROI angle approved for paid conversion: first induction day can earn roughly £145-£178 — subscription is tiny vs missing one shift.
- Commands: /start, /stop, /mute, /unmute, /setlocation, /status, /help.
- Compliance: public pages only; not affiliated with Amazon; no guaranteed employment; independent alert tool.

AUDIENCE: UK warehouse job applicants, especially Midlands, anxious about missing shift drops, heavy Telegram users.

TONE: Urgent but honest, mobile-first, short sentences. British English. Avoid scammy hype. Always include "not affiliated with Amazon" on landing pages and checkout.

TASK: [INSERT ONE]
- Write 5 Telegram channel posts introducing the bot
- Write a landing page (hero, benefits, how it works, pricing, FAQ, disclaimer)
- Write Meta ad variants (3 headlines, 3 primary texts, 3 CTAs)
- Write a 60-second explainer script for Reels/TikTok
- Write FAQ for skeptical users
- Write referral influencer outreach DM (offer free reference access via admin)

For each asset include a one-line disclaimer: Independent service, not affiliated with Amazon UK.
```

---

## 13. One-page handout (print / WhatsApp summary)

**Product:** Telegram bot — Amazon UK warehouse **job** + **shift** alerts  
**Trial:** 1 day free  
**Paid:** ~£4.99 / 30 days (Telegram Pay)  
**USP:** Alerts when **shift slots open**, not only new jobs  
**CTA:** Search bot on Telegram → /start → Subscribe trial  
**Disclaimer:** Independent tool; not Amazon; no job guarantee  

---

*Document version: aligned with codebase features as of project handoff. Update pricing/hours in `config.yaml` if marketing numbers change.*
