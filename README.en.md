<div align="center">

# 🇬🇧 AI English Learning Telegram Bot

**Intelligent Telegram Bot for adaptive English language acquisition powered by Spaced Repetition (SRS), Google Gemini AI integration, and a native Telegram Stars monetization engine.**

[![Python](https://img.shields.io/badge/Python-3.10%2B-blue.svg?logo=python&logoColor=white)](https://www.python.org/)
[![Aiogram](https://img.shields.io/badge/Aiogram-3.x-2CA5E0.svg?logo=telegram&logoColor=white)](https://docs.aiogram.dev/)
[![Google Gemini](https://img.shields.io/badge/Google_Gemini-API-orange.svg?logo=google&logoColor=white)](https://ai.google.dev/)
[![License](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)

[🇷🇺 Читать на русском](README.md) • [🇬🇧 Read in English](README.en.md)

---

</div>

## 📑 Table of Contents

1. [Project Overview & Business Logic](#1-project-overview--business-logic)
   * [1.1. Problem Statement](#11-problem-statement)
   * [1.2. The Solution](#12-the-solution)
2. [Core Features](#2-core-features)
   * [2.1. Spaced Repetition System (SRS)](#21-spaced-repetition-system-srs)
   * [2.2. AI-Powered Explanations Cache (Google Gemini)](#22-ai-powered-explanations-cache-google-gemini)
   * [2.3. Access Control & Monetization (Telegram Stars)](#23-access-control--monetization-telegram-stars)
   * [2.4. Performance Analytics & Dynamic Charts (Matplotlib)](#24-performance-analytics--dynamic-charts-matplotlib)
   * [2.5. In-Chat Translation & Knowledge Base](#25-in-chat-translation--knowledge-base)
3. [Architecture & Workflow](#3-architecture--workflow)
   * [3.1. Data Flow Diagram](#31-data-flow-diagram)
   * [3.2. Project Structure](#32-project-structure)
4. [Technology Stack](#4-technology-stack)
5. [Installation & Quickstart](#5-installation--quickstart)
   * [5.1. Clone & Dependencies](#51-clone--dependencies)
   * [5.2. Environment Setup](#52-environment-setup)
   * [5.3. Running the Bot](#53-running-the-bot)
6. [Data Schema](#6-data-schema)
7. [Author & Contacts](#7-author--contacts)

---

## 1. Project Overview & Business Logic

### 1.1. Problem Statement
Traditional quiz bots present static questionnaires that ignore human memory decay curves (Ebbinghaus forgetting curve). Students repeatedly make the same grammatical mistakes without personalized remediation, while product owners face substantial latency and high LLM token costs when requesting explanations dynamically on every response.

### 1.2. The Solution
**AI English Learning Bot** provides a scalable EdTech solution combining:
* **Spaced Repetition System (SRS)**: Algorithmic scheduling of review sessions (1 → 3 → 7 days) targeting specific grammar gaps.
* **Hybrid LLM Caching Pipeline**: High-quality pedagogical explanations are pre-generated using Google Gemini AI and stored locally in a high-speed cache, guaranteeing **zero runtime latency** and **zero recurring token costs**.
* **Complete Monetization Flow**: Flexible subscription tiers (Free, Monthly Premium, Lifetime Access) with native **Telegram Stars** digital payments.

---

## 2. Core Features

### 2.1. Spaced Repetition System (SRS)
* **Error Tracking:** Any incorrect answer automatically flags the question into the user's active mistake ledger at `Level 0`.
* **Mastery Thresholds:** To graduate a card to `Mastered`, the user must provide consecutive correct answers across spaced intervals:
  * `Review 1`: 1 day after error;
  * `Review 2`: 3 days after Review 1;
  * `Review 3`: 7 days after Review 2.
* **Penalty Loop:** Any mistake at any stage resets progress back to `Level 0`.

### 2.2. AI-Powered Explanations Cache (Google Gemini)
* The `batch_explanations.py` CLI module ingests question batches through Google Gemini API.
* Generates granular pedagogical breakdowns:
  1. *Sentence translation & semantics*;
  2. *Underlying grammatical principle*;
  3. *Why each incorrect distractor is invalid*.
* Run-time delivery pulls directly from `explanations_cache.json`, ensuring instantaneous response delivery.

### 2.3. Access Control & Monetization (Telegram Stars)
* The `access_control.py` module manages rate limits and payment gateways:
  * **Free Tier:** 15 questions per 24-hour cycle (daily reset).
  * **Monthly Premium:** Unlimited questions, all topics unlocked, advanced analytics.
  * **Lifetime Access:** One-time purchase for permanent access.
* Full integration with **Telegram Stars** digital goods invoices (`PreCheckoutQuery`, `SuccessfulPayment`).

### 2.4. Performance Analytics & Dynamic Charts (Matplotlib)
* Generates on-the-fly image dashboards delivered directly into the chat:
  * Accuracy progression over time;
  * Category proficiency breakdown (strengths vs. weaknesses);
  * Count of active, pending, and mastered SRS cards.

### 2.5. In-Chat Translation & Knowledge Base
* Contextual translation via `deep-translator` for unknown words on demand.
* Integrated reference grammar guide (`cheat_sheet.md`) navigable via bot menu commands.

---

## 3. Architecture & Workflow

### 3.1. Data Flow Diagram

```mermaid
flowchart TD
    User([👤 Telegram User]) -->|Command / Answer| Bot[🤖 Aiogram 3 Bot Core]
    
    subgraph Access["Access Control Layer (access_control.py)"]
        Bot -->|Check Allowance| ACL{Free Quota Available?}
        ACL -->|No| Paywall[💳 Paywall / Telegram Stars]
        ACL -->|Yes| Quiz[📝 Quiz Engine]
    end
    
    subgraph QuizEngine["Testing & SRS Engine"]
        Quiz -->|Fetch Task| QDB[(questions_db.json<br/>1000+ Items)]
        Quiz -->|Evaluate Answer| Logic{Correct?}
        Logic -->|No| AddSRS[Queue for SRS<br/>Level 0]
        Logic -->|Yes| UpSRS[Advance SRS Stage<br/>Level +1]
    end
    
    subgraph Explanation["Knowledge Base & AI"]
        AddSRS --> Cache[(explanations_cache.json)]
        Cache -->|Retrieve Breakdown| ShowExp[💡 Granular Explanation]
    end
    
    ShowExp --> User
    QuizEngine -->|On Request| Analytics[📊 Matplotlib Chart Gen]
    Analytics --> User
```

### 3.2. Project Structure

```text
01-english-learning-bot/
├── bot.py                  # Primary Telegram bot daemon (Aiogram 3)
├── access_control.py       # Permissions, daily quotas & Telegram Stars billing
├── batch_explanations.py   # Offline AI batch generation via Google Gemini
├── questions_db.json       # Categorized question bank (1,000+ entries)
├── explanations_cache.json # High-performance explanations and hints cache
├── cheat_sheet.md          # Comprehensive English grammar reference
├── requirements.txt        # Python dependency manifest
├── .env.example            # Environment variable template
├── .gitignore              # Version control ignore patterns
├── README.md               # Russian documentation
└── README.en.md            # English documentation
```

---

## 4. Technology Stack

* **Runtime:** `Python 3.10+`
* **Bot Framework:** `Aiogram 3.x` (FSM, Routers, AsyncIO, Filters)
* **LLM Engine:** `google-genai` (Google Gemini 1.5 Flash)
* **Data Visualization:** `Matplotlib`, `Pillow`
* **Localization & Translation:** `deep-translator`
* **Config:** `python-dotenv`

---

## 5. Installation & Quickstart

### 5.1. Clone & Dependencies
```bash
git clone https://github.com/Andrew123485/english-learning-bot.git
cd english-learning-bot

# Initialize virtual environment
python -m venv venv
# Windows:
.\venv\Scripts\activate
# Linux/macOS:
source venv/bin/activate

# Install dependencies
pip install -r requirements.txt
```

### 5.2. Environment Setup
Copy `.env.example` to `.env`:
```bash
cp .env.example .env
```
Populate environment variables:
```env
BOT_TOKEN=123456789:ABCdefGHIjklMNOpqrsTUVwxyz
ADMIN_ID=576401462
GEMINI_API_KEY=AIzaSyYourGeminiApiKeyHere
PROXY=  # Optional: socks5://user:pass@host:port
```

### 5.3. Running the Bot
```bash
python bot.py
```

---

## 6. Data Schema

Sample record from `questions_db.json`:
```json
{
  "id": 142,
  "block_name": "Conditionals (Types 0-3)",
  "question": "If I ___ more free time, I would learn Spanish.",
  "options": ["have", "had", "would have", "will have"],
  "correct_idx": 1,
  "level": "B1"
}
```

---

## 7. Author & Contacts

* **Author:** Andrei Vladimirov
* **Specialization:** Business Analyst / Product Manager / Python Developer
* **Telegram:** [@amorfatimementomoria](https://t.me/amorfatimementomoria)
* **GitHub:** [github.com/Andrew123485](https://github.com/Andrew123485)
